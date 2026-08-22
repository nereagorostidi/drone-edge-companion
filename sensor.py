#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 Captura y envío resiliente de datos del sensor BME680
 Sistema SAR basado en dron — Raspberry Pi 5 (nodo edge)
=====================================================================

Este script lee un sensor ambiental BME680 por I2C y envía las medidas
a un broker MQTT (Mosquitto) alojado en un servidor EC2, que a su vez
las almacena en InfluxDB.

Arquitectura "store-and-forward" (almacenar y reenviar):
La captura y el envío están DESACOPLADOS. Cada lectura se guarda primero
en una base de datos local (SQLite) que actúa como "fuente de verdad".
Un reenviador vacía ese buffer hacia MQTT solo cuando hay conexión.
De este modo, si la WiFi se cae, los datos se siguen recogiendo en el
disco de la Pi y se envían más tarde, sin perder ningún intervalo.
Esto materializa el principio EDGE-FIRST del proyecto: el nodo opera de
forma autónoma y la comunicación con la estación base es oportunista.

Este proceso cubre el DOMINIO AMBIENTAL. Publica en un topic jerárquico
dronsar/{dron_id}/ambiental, donde el identificador del dron se lee del .env.
Cada dominio (vuelo, sistema, deteccion) sigue esta misma plantilla.

Uso:
    python3 sensor.py                 # captura cada 30 s (por defecto)
    python3 sensor.py -i 5            # captura cada 5 s
    python3 sensor.py --fake          # datos simulados, sin sensor físico
    python3 sensor.py --fake -i 2     # simulados y rápido, para pruebas

Variables de entorno (.env):
    DRON_ID     identificador del dron (p. ej. dron01)   [obligatoria]
    EC2_HOST    IP o dominio del broker MQTT             [obligatoria]
    MQTT_PORT   puerto MQTT (por defecto 1883)
    BUFFER_DB   ruta del buffer SQLite
    LOTE        filas enviadas por ciclo (por defecto 50)
"""

import os
import time
import json
import random
import sqlite3
import argparse
from datetime import datetime
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
# El módulo bme680 se importa MÁS ABAJO, solo cuando NO se usa --fake.
# Así el script puede ejecutarse en un equipo sin el sensor (ni la
# librería) instalados, algo útil para las pruebas en el portátil.


# =====================================================================
#  CARGA DE VARIABLES DE ENTORNO
# =====================================================================
# Se leen desde un archivo .env situado en el mismo directorio del
# script (o el que systemd defina como WorkingDirectory).
load_dotenv()


# =====================================================================
#  ARGUMENTOS DE LÍNEA DE COMANDOS
# =====================================================================
# Permite parametrizar el script sin editar el código. argparse genera
# además una ayuda automática con "python3 sensor.py -h".
parser = argparse.ArgumentParser(
    description="Lectura y envío MQTT del sensor BME680 con buffer local")
parser.add_argument("-i", "--intervalo", type=float, default=30.0,
                    help="Segundos entre cada lectura (por defecto: 30)")
parser.add_argument("--fake", action="store_true",
                    help="Genera datos simulados en vez de leer el BME680 "
                         "(para probar sin el sensor físico)")
args = parser.parse_args()


# =====================================================================
#  CONFIGURACIÓN
# =====================================================================
# Todas las variables sensibles y configurables se cargan desde el .env,
# nunca hardcodeadas en el código. Esto permite versionar el script en
# público sin exponer credenciales ni infraestructura.
DOMINIO = "ambiental"                                        # dominio de este proceso
DRON_ID = os.getenv("DRON_ID")                               # identificador del dron
EC2_HOST = os.getenv("EC2_HOST")                             # IP o dominio del broker
PORT = int(os.getenv("MQTT_PORT", 1883))                     # Puerto MQTT
DB = os.getenv("BUFFER_SENSOR", f"./{DOMINIO}.db")
LOTE = int(os.getenv("LOTE", 50))                            # Filas por ciclo

# El topic y el client_id se CONSTRUYEN a partir del identificador del dron,
# no se escriben a mano. Así el mismo código sirve para cualquier unidad:
# basta con cambiar DRON_ID en su .env.
#   Topic jerárquico:   dronsar/{dron_id}/{dominio}
#   client_id ÚNICO:    {dron_id}-{dominio}
# El client_id debe ser único por proceso: si dos clientes se conectan al
# broker con el mismo id, Mosquitto los va expulsando en un bucle sin fin.
TOPIC = f"dronsar/{DRON_ID}/{DOMINIO}"
CLIENT_ID = f"{DRON_ID}-{DOMINIO}"

# Topic de configuración remota (panel de control -> este script), con el
# mismo esquema "dronsar/..." que usa receptor.py para comandos.
CONFIG_TOPIC = f"dronsar/{DRON_ID}/sensor/config"

# Validación temprana: si falta alguna variable obligatoria, el script
# falla con un mensaje claro en lugar de con errores crípticos más tarde.
required = {"DRON_ID": DRON_ID, "EC2_HOST": EC2_HOST}
missing = [k for k, v in required.items() if not v]
if missing:
    raise RuntimeError(f"Faltan variables de entorno: {', '.join(missing)}")


# =====================================================================
#  BUFFER LOCAL — LA FUENTE DE VERDAD
# =====================================================================
# Se usa SQLite en disco (no en memoria) a propósito: sobrevive a un
# reinicio o a un corte de corriente, algo crítico en un nodo alimentado
# por batería LiPo. Cada lectura se guarda con una marca "enviado":
#   enviado = 0  -> pendiente de enviar
#   enviado = 1  -> confirmado por el broker
db = sqlite3.connect(DB)
db.execute("""CREATE TABLE IF NOT EXISTS lecturas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,                 -- hora de captura en formato ISO 8601
    temp REAL,               -- temperatura (°C)
    hum REAL,                -- humedad relativa (%)
    pres REAL,               -- presión (hPa)
    enviado INTEGER DEFAULT 0)""")
db.commit()


# =====================================================================
#  ORIGEN DE LOS DATOS — SENSOR REAL o MODO FAKE
# =====================================================================
# El origen del dato se decide UNA sola vez aquí. En modo normal se
# inicializa el BME680; en modo --fake ni se importa la librería, para
# poder ejecutar el script en un equipo que no tenga el sensor.
if args.fake:
    print("MODO FAKE: se generan datos simulados, no se lee el BME680.")
    # Estado inicial de la simulación. Se parte de valores plausibles y en
    # cada lectura se les suma una pequeña variación (ver datos_simulados).
    _sim = {"temp": 22.0, "hum": 48.0, "pres": 1013.0}
else:
    import bme680
    # Dirección I2C 0x76 (SDO conectado a GND). Si el sensor apareciera en
    # 0x77 con "i2cdetect -y 1", cambiar a bme680.I2C_ADDR_SECONDARY.
    sensor = bme680.BME680(bme680.I2C_ADDR_PRIMARY)

    # Sobremuestreo (oversampling): promedia varias medidas internas para
    # reducir el ruido. Más oversampling = medida más estable pero más lenta.
    sensor.set_humidity_oversample(bme680.OS_2X)
    sensor.set_pressure_oversample(bme680.OS_4X)
    sensor.set_temperature_oversample(bme680.OS_8X)
    # Filtro IIR: suaviza picos bruscos y transitorios en las lecturas.
    sensor.set_filter(bme680.FILTER_SIZE_3)


def _acota(valor, minimo, maximo):
    """Mantiene un valor dentro de un rango [minimo, maximo]."""
    return max(minimo, min(maximo, valor))


def datos_simulados():
    """Genera una medida ambiental plausible con deriva suave.

    En vez de valores al azar independientes en cada lectura, se parte del
    valor anterior y se le suma una pequeña variación. Así la serie simulada
    se parece a una señal real (cambia poco a poco) y sirve para validar el
    flujo completo —buffer, MQTT, topic, client_id— sin el sensor físico.
    """
    _sim["temp"] = _acota(_sim["temp"] + random.uniform(-0.3, 0.3), 15.0, 30.0)
    _sim["hum"] = _acota(_sim["hum"] + random.uniform(-0.8, 0.8), 30.0, 70.0)
    _sim["pres"] = _acota(_sim["pres"] + random.uniform(-0.2, 0.2), 1000.0, 1025.0)
    return (round(_sim["temp"], 2), round(_sim["hum"], 2), round(_sim["pres"], 2))


def leer_medida():
    """Devuelve (temp, hum, pres), o None si aún no hay una medida válida.

    Encapsula el origen del dato: en modo normal lee el BME680; en modo
    --fake devuelve datos simulados. El resto del script no necesita saber
    de dónde viene la medida.
    """
    if args.fake:
        return datos_simulados()
    if sensor.get_sensor_data():   # True cuando hay una medida válida lista
        return (round(sensor.data.temperature, 2),
                round(sensor.data.humidity, 2),
                round(sensor.data.pressure, 2))
    return None


# =====================================================================
#  CONFIGURACIÓN REMOTA (panel de control -> este script)
# =====================================================================
# Intervalo de captura EN USO. Arranca con el valor de -i/--intervalo, pero
# se puede actualizar en caliente desde el panel de control (ver on_message).
intervalo_actual = args.intervalo


def on_connect(client, userdata, flags, reason_code, properties):
    """Al conectar (o reconectar), nos suscribimos al topic de configuración."""
    client.subscribe(CONFIG_TOPIC, qos=1)
    print(f"Suscrito a '{CONFIG_TOPIC}'")


def on_message(client, userdata, msg):
    """Actualiza el intervalo de captura al recibir un comando de configuración.

    Formato del payload (lo publica api.py, ver COMANDOS_CONFIG):
        {"command": "set_sensor_interval", "params": {"interval_seconds": N}, ...}
    """
    global intervalo_actual
    try:
        orden = json.loads(msg.payload)
    except json.JSONDecodeError:
        print(f"Mensaje recibido en '{CONFIG_TOPIC}' que no es JSON válido; se ignora.")
        return

    command = orden.get("command")
    cmd_id = orden.get("command_id", "?")
    print(f"Comando de configuración recibido [{cmd_id}]: {command}")

    if command != "set_sensor_interval":
        print(f"  -> Comando desconocido '{command}' en este topic; se ignora.")
        return

    try:
        nuevo_intervalo = float(orden["params"]["interval_seconds"])
    except (KeyError, TypeError, ValueError):
        print("  -> 'params.interval_seconds' ausente o inválido; se ignora.")
        return

    intervalo_actual = nuevo_intervalo
    print(f"  -> Intervalo de captura actualizado a {intervalo_actual}s")


# =====================================================================
#  CLIENTE MQTT
# =====================================================================
# El client_id identifica este proceso ante el broker de forma única.
client = mqtt.Client(client_id=CLIENT_ID,
                     callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

# Reconexión automática: si se cae la red, la librería reintenta sola,
# esperando entre 1 y 30 s de forma progresiva. No hay que hacer nada
# manualmente; cuando vuelva la WiFi, se reconecta y el buffer se vacía.
client.reconnect_delay_set(min_delay=1, max_delay=30)

# connect_async NO bloquea aunque el broker no esté disponible al arrancar.
# Combinado con loop_start(), la gestión de red corre en segundo plano.
print(f"Conectando al broker en {EC2_HOST} como '{CLIENT_ID}'...")
client.connect_async(EC2_HOST, PORT, 60)
client.loop_start()

print(f"Dominio '{DOMINIO}' -> topic '{TOPIC}'")
print(f"Captura cada {args.intervalo}s con buffer local. (Ctrl+C para salir)")


# =====================================================================
#  FUNCIÓN: guardar() — CAPTURA (siempre activa)
# =====================================================================
def guardar():
    """Lee el origen de datos y almacena la medida en el buffer local.

    Esto ocurre SIEMPRE, haya red o no. La hora se toma en el momento
    exacto de la captura (no del envío) y en formato ISO 8601 con zona
    horaria, para que un dato que se envíe con retraso se guarde luego
    con su instante real y no con el de la reconexión.
    """
    medida = leer_medida()
    if medida is None:     # aún no hay lectura válida del sensor
        return
    temp, hum, pres = medida
    db.execute(
        "INSERT INTO lecturas (ts, temp, hum, pres) VALUES (?,?,?,?)",
        (datetime.now().astimezone().isoformat(), temp, hum, pres))
    db.commit()


# =====================================================================
#  FUNCIÓN: reenviar() — ENVÍO (oportunista)
# =====================================================================
def reenviar():
    """Vacía el buffer hacia MQTT, pero solo con conexión confirmada.

    Se publica con QoS 1 (el broker debe confirmar la recepción con un
    ACK). Una fila solo se marca como enviada TRAS esa confirmación; si
    no llega, permanece pendiente y se reintenta en el siguiente ciclo.
    Así nunca se da por enviado algo que no llegó de verdad.
    """
    # Si no hay conexión, no se intenta nada: los datos ya están a salvo
    # en disco y se enviarán cuando la red vuelva.
    if not client.is_connected():
        return

    # Se recuperan las filas pendientes (enviado=0), en orden y por lotes.
    filas = db.execute(
        "SELECT id, ts, temp, hum, pres FROM lecturas "
        "WHERE enviado=0 ORDER BY id LIMIT ?", (LOTE,)).fetchall()

    for id_, ts, temp, hum, pres in filas:
        # El JSON mantiene el mismo formato que espera el puente del EC2.
        payload = json.dumps({
            "temperatura": temp,
            "humedad": hum,
            "presion": pres,
            "timestamp": ts
        })
        try:
            info = client.publish(TOPIC, payload, qos=1)
            info.wait_for_publish(timeout=5)   # espera el ACK del broker
            if info.is_published():
                # Confirmado: se marca como enviado de forma definitiva.
                db.execute("UPDATE lecturas SET enviado=1 WHERE id=?", (id_,))
                db.commit()
                print(f"Enviado [{TOPIC}]: {payload}")
            else:
                # No confirmado: se para y se reintenta en el próximo ciclo.
                break
        except (ValueError, RuntimeError):
            # Error puntual de la cola de publicación: se reintenta luego.
            break


# =====================================================================
#  BUCLE PRINCIPAL
# =====================================================================
# En cada ciclo: primero se captura (a disco), luego se intenta reenviar.
# La captura nunca depende del resultado del envío.
try:
    while True:
        guardar()
        reenviar()
        time.sleep(intervalo_actual)

except KeyboardInterrupt:
    # Cierre ordenado al pulsar Ctrl+C.
    print("\nDetenido por el usuario.")
    client.loop_stop()
    client.disconnect()
    db.close()