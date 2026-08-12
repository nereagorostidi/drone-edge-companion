#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 Captura y envío resiliente del estado del sistema (Raspberry Pi)
 Sistema SAR basado en dron — Raspberry Pi 5 (nodo edge)
=====================================================================

Este script recoge métricas del propio nodo edge (CPU, temperatura, RAM,
disco, throttling y uptime) y las envía a un broker MQTT (Mosquitto) del
servidor EC2, que a su vez las almacena en InfluxDB.

Sigue la MISMA plantilla que el colector ambiental (sensor.py): captura,
guarda en un buffer local SQLite y reenvía por MQTT con la lógica
"store-and-forward". Lo único que cambia es el origen de los datos, el
esquema de la tabla y el dominio.

Este proceso cubre el DOMINIO SISTEMA. Publica en dronsar/{dron_id}/sistema,
con el identificador del dron leído del .env.

Origen de los datos:
    - psutil       -> CPU, RAM, disco, uptime (multiplataforma).
    - vcgencmd     -> código de throttling (SOLO Raspberry Pi). Fuera de
                      una Pi, el campo se guarda como "no_disp".

Uso:
    python3 sistema.py                # captura cada 10 s (por defecto)
    python3 sistema.py -i 5           # captura cada 5 s

Variables de entorno (.env):
    DRON_ID     identificador del dron (p. ej. dron01)   [obligatoria]
    EC2_HOST    IP o dominio del broker MQTT             [obligatoria]
    MQTT_PORT   puerto MQTT (por defecto 1883)
    BUFFER_DB   ruta del buffer SQLite (por defecto: sistema.db)
    LOTE        filas enviadas por ciclo (por defecto 50)
"""

import os
import time
import json
import sqlite3
import argparse
import subprocess
from datetime import datetime
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
import psutil


# =====================================================================
#  CARGA DE VARIABLES DE ENTORNO
# =====================================================================
load_dotenv()


# =====================================================================
#  ARGUMENTOS DE LÍNEA DE COMANDOS
# =====================================================================
parser = argparse.ArgumentParser(
    description="Lectura y envío MQTT del estado del sistema con buffer local")
parser.add_argument("-i", "--intervalo", type=float, default=10.0,
                    help="Segundos entre cada lectura (por defecto: 10)")
args = parser.parse_args()


# =====================================================================
#  CONFIGURACIÓN
# =====================================================================
DOMINIO = "sistema"                                          # dominio de este proceso
DRON_ID = os.getenv("DRON_ID")                               # identificador del dron
EC2_HOST = os.getenv("EC2_HOST")                             # IP o dominio del broker
PORT = int(os.getenv("MQTT_PORT", 1883))                     # Puerto MQTT
# Cada dominio usa su propia base de datos. Por defecto se nombra a partir
# del dominio (sistema.db), de modo que un .env compartido entre procesos
# no provoca colisiones.
DB = os.getenv("BUFFER_SISTEMA", f"./{DOMINIO}.db")
LOTE = int(os.getenv("LOTE", 50))                            # Filas por ciclo

# El topic y el client_id se construyen a partir del identificador del dron.
#   Topic jerárquico:   dronsar/{dron_id}/{dominio}
#   client_id ÚNICO:    {dron_id}-{dominio}
TOPIC = f"dronsar/{DRON_ID}/{DOMINIO}"
CLIENT_ID = f"{DRON_ID}-{DOMINIO}"

# Topic de configuración remota (panel de control -> este script), con el
# mismo esquema "dronsar/..." que usa receptor.py para comandos.
CONFIG_TOPIC = f"dronsar/{DRON_ID}/sistema/config"

# Validación temprana de las variables obligatorias.
required = {"DRON_ID": DRON_ID, "EC2_HOST": EC2_HOST}
missing = [k for k, v in required.items() if not v]
if missing:
    raise RuntimeError(f"Faltan variables de entorno: {', '.join(missing)}")


# =====================================================================
#  BUFFER LOCAL — LA FUENTE DE VERDAD
# =====================================================================
# Mismo principio que en el colector ambiental: SQLite en disco, cada fila
# con una marca "enviado" (0 pendiente, 1 confirmado por el broker). Lo que
# cambia son las columnas, adaptadas a las métricas del sistema.
db = sqlite3.connect(DB)
db.execute("""CREATE TABLE IF NOT EXISTS lecturas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,                 -- hora de captura en formato ISO 8601
    cpu_pct REAL,            -- uso de CPU (%)
    cpu_temp REAL,           -- temperatura de CPU (°C); puede ser NULL
    ram_pct REAL,            -- uso de RAM (%)
    disco_pct REAL,          -- uso de disco (%)
    throttled TEXT,          -- código de throttling de la Pi (o "no_disp")
    uptime_s INTEGER,        -- segundos desde el arranque
    enviado INTEGER DEFAULT 0)""")
db.commit()


# =====================================================================
#  LECTURA DEL SISTEMA
# =====================================================================
# "Ceba" el medidor de CPU: la primera llamada a cpu_percent siempre
# devuelve 0.0, porque mide el uso DESDE la llamada anterior. Al hacerla
# aquí, la primera lectura real del bucle ya es significativa.
psutil.cpu_percent(interval=None)


def _leer_throttled():
    """Devuelve el código de throttling del firmware de la Raspberry Pi.

    El comando vcgencmd solo existe en la Pi. Fuera de ella (p. ej. en el
    portátil de desarrollo) no está disponible, así que se devuelve
    "no_disp" en lugar de fallar. Así el mismo código sirve en ambos sitios.
    Salida típica del comando: "throttled=0x0".
    """
    try:
        salida = subprocess.check_output(
            ["vcgencmd", "get_throttled"], text=True, timeout=2)
        return salida.strip().split("=")[1]
    except (FileNotFoundError, subprocess.SubprocessError, IndexError):
        return "no_disp"


def _leer_cpu_temp():
    """Temperatura de la CPU en °C, o None si no se puede leer aquí.

    Se intenta primero con psutil (en la Pi el sensor suele llamarse
    'cpu_thermal') y, si no, leyendo el fichero térmico del kernel. En un
    equipo donde no haya ninguna de las dos vías (p. ej. Windows), devuelve
    None y el campo simplemente no se envía.
    """
    try:
        temps = psutil.sensors_temperatures()
        for nombre in ("cpu_thermal", "coretemp", "cpu-thermal", "k10temp"):
            if temps.get(nombre):
                return round(temps[nombre][0].current, 1)
        for lista in temps.values():       # cualquier sensor disponible
            if lista:
                return round(lista[0].current, 1)
    except (AttributeError, NotImplementedError, OSError):
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read()) / 1000.0, 1)
    except (FileNotFoundError, ValueError, OSError):
        return None


def leer_medida():
    """Devuelve un dict con las métricas del sistema.

    cpu_temp puede ser None si el equipo no expone la temperatura, en cuyo
    caso ese campo no se envía.
    """
    return {
        "cpu_pct": round(psutil.cpu_percent(interval=None), 1),
        "cpu_temp": _leer_cpu_temp(),
        "ram_pct": round(psutil.virtual_memory().percent, 1),
        "disco_pct": round(psutil.disk_usage("/").percent, 1),
        "throttled": _leer_throttled(),
        "uptime_s": int(time.time() - psutil.boot_time()),
    }


# =====================================================================
#  CONFIGURACIÓN REMOTA (panel de control -> este script)
# =====================================================================
def on_connect(client, userdata, flags, reason_code, properties):
    """Al conectar (o reconectar), nos suscribimos al topic de configuración."""
    client.subscribe(CONFIG_TOPIC, qos=1)
    print(f"Suscrito a '{CONFIG_TOPIC}'")


def on_message(client, userdata, msg):
    """Apaga la Raspberry Pi al recibir un comando de apagado por MQTT.

    Formato del payload (lo publica api.py, ver COMANDOS_CONFIG):
        {"command": "shutdown", "params": {}, ...}
    """
    try:
        orden = json.loads(msg.payload)
    except json.JSONDecodeError:
        print(f"Mensaje recibido en '{CONFIG_TOPIC}' que no es JSON válido; se ignora.")
        return

    command = orden.get("command")
    cmd_id = orden.get("command_id", "?")
    print(f"Comando de configuración recibido [{cmd_id}]: {command}")

    if command != "shutdown":
        print(f"  -> Comando desconocido '{command}' en este topic; se ignora.")
        return

    print("  -> Apagando la Raspberry Pi...")
    subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)


# =====================================================================
#  CLIENTE MQTT
# =====================================================================
client = mqtt.Client(client_id=CLIENT_ID,
                     callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message
client.reconnect_delay_set(min_delay=1, max_delay=30)

print(f"Conectando al broker en {EC2_HOST} como '{CLIENT_ID}'...")
client.connect_async(EC2_HOST, PORT, 60)
client.loop_start()

print(f"Dominio '{DOMINIO}' -> topic '{TOPIC}'")
print(f"Captura cada {args.intervalo}s con buffer local. (Ctrl+C para salir)")


# =====================================================================
#  FUNCIÓN: guardar() — CAPTURA (siempre activa)
# =====================================================================
def guardar():
    """Lee las métricas del sistema y las almacena en el buffer local.

    Ocurre SIEMPRE, haya red o no. La hora se toma en el instante de la
    captura, en ISO 8601 con zona horaria.
    """
    m = leer_medida()
    db.execute(
        "INSERT INTO lecturas "
        "(ts, cpu_pct, cpu_temp, ram_pct, disco_pct, throttled, uptime_s) "
        "VALUES (?,?,?,?,?,?,?)",
        (datetime.now().astimezone().isoformat(),
         m["cpu_pct"], m["cpu_temp"], m["ram_pct"],
         m["disco_pct"], m["throttled"], m["uptime_s"]))
    db.commit()


# =====================================================================
#  FUNCIÓN: reenviar() — ENVÍO (oportunista)
# =====================================================================
def reenviar():
    """Vacía el buffer hacia MQTT, solo con conexión confirmada (QoS 1).

    Una fila se marca como enviada TRAS el ACK del broker; si no llega,
    permanece pendiente y se reintenta en el siguiente ciclo.
    """
    if not client.is_connected():
        return

    filas = db.execute(
        "SELECT id, ts, cpu_pct, cpu_temp, ram_pct, disco_pct, throttled, "
        "uptime_s FROM lecturas WHERE enviado=0 ORDER BY id LIMIT ?",
        (LOTE,)).fetchall()

    for id_, ts, cpu_pct, cpu_temp, ram_pct, disco_pct, throttled, uptime_s in filas:
        # Se construye el JSON y se omiten los campos nulos (p. ej. cpu_temp
        # cuando el equipo no expone la temperatura), para no enviar "null".
        datos = {
            "cpu_pct": cpu_pct,
            "cpu_temp": cpu_temp,
            "ram_pct": ram_pct,
            "disco_pct": disco_pct,
            "throttled": throttled,
            "uptime_s": uptime_s,
            "timestamp": ts,
        }
        payload = json.dumps({k: v for k, v in datos.items() if v is not None})
        try:
            info = client.publish(TOPIC, payload, qos=1)
            info.wait_for_publish(timeout=5)
            if info.is_published():
                db.execute("UPDATE lecturas SET enviado=1 WHERE id=?", (id_,))
                db.commit()
                print(f"Enviado [{TOPIC}]: {payload}")
            else:
                break
        except (ValueError, RuntimeError):
            break


# =====================================================================
#  BUCLE PRINCIPAL
# =====================================================================
try:
    while True:
        guardar()
        reenviar()
        time.sleep(args.intervalo)

except KeyboardInterrupt:
    print("\nDetenido por el usuario.")
    client.loop_stop()
    client.disconnect()
    db.close()