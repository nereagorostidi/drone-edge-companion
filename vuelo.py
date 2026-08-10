#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 Captura y envío resiliente de la telemetría de vuelo (Pixhawk)
 Sistema SAR basado en dron — Raspberry Pi 5 (nodo edge)
=====================================================================

Este script publica la telemetría de vuelo (posición, actitud, batería,
GPS y modo) en sar/{dron_id}/vuelo y, además, escribe en cada ciclo el
fichero posicion_actual.json con la última posición conocida del dron,
que es lo que el proceso de detección (deteccion.py) leerá para adjuntar
la posición a cada alerta de persona.

Sigue la MISMA plantilla que los demás colectores: captura, guarda en un
buffer local SQLite y reenvía por MQTT con la lógica "store-and-forward".

*** VERSIÓN PROVISIONAL CON DATOS SIMULADOS ***
El Pixhawk todavía no está montado, así que de momento los datos se
generan de forma simulada, centrados en el campo de vuelo de Galapagar
(LECMUAV090). Cuando el Pixhawk esté disponible, solo hay que sustituir
la función datos_simulados() por una lectura real por MAVLink (pymavlink);
el resto del script (buffer, reenvío, fichero de posición) no cambia.

Este proceso cubre el DOMINIO VUELO.

Uso:
    python3 vuelo.py                  # captura cada 1 s (por defecto)
    python3 vuelo.py -i 0.3           # captura ~3 veces por segundo

Variables de entorno (.env):
    DRON_ID     identificador del dron (p. ej. dron01)   [obligatoria]
    EC2_HOST    IP o dominio del broker MQTT             [obligatoria]
    MQTT_PORT   puerto MQTT (por defecto 1883)
    BUFFER_DB   ruta del buffer SQLite (por defecto: vuelo.db)
    POS_FILE    ruta del fichero de posición compartido
    LOTE        filas enviadas por ciclo (por defecto 50)
"""

import os
import time
import json
import math
import random
import sqlite3
import argparse
from datetime import datetime
from dotenv import load_dotenv
import paho.mqtt.client as mqtt


# =====================================================================
#  CARGA DE VARIABLES DE ENTORNO
# =====================================================================
load_dotenv()


# =====================================================================
#  ARGUMENTOS DE LÍNEA DE COMANDOS
# =====================================================================
parser = argparse.ArgumentParser(
    description="Envío MQTT de la telemetría de vuelo con buffer local")
parser.add_argument("-i", "--intervalo", type=float, default=1.0,
                    help="Segundos entre cada lectura (por defecto: 1)")
args = parser.parse_args()


# =====================================================================
#  CONFIGURACIÓN
# =====================================================================
DOMINIO = "vuelo"                                            # dominio de este proceso
DRON_ID = os.getenv("DRON_ID")                               # identificador del dron
EC2_HOST = os.getenv("EC2_HOST")                             # IP o dominio del broker
PORT = int(os.getenv("MQTT_PORT", 1883))                     # Puerto MQTT
DB = os.getenv("BUFFER_VUELO", f"./{DOMINIO}.db")
LOTE = int(os.getenv("LOTE", 50))                            # Filas por ciclo

# Fichero de posición compartido con deteccion.py. Es la ÚNICA pieza que
# comparten dos procesos; al ser un fichero (no una base de datos) no hay
# bloqueos que gestionar.
POS_FILE = os.getenv("POS_FILE", "./posicion_actual.json")

# El topic y el client_id se construyen a partir del identificador del dron.
TOPIC = f"sar/{DRON_ID}/{DOMINIO}"
CLIENT_ID = f"{DRON_ID}-{DOMINIO}"

# Validación temprana de las variables obligatorias.
required = {"DRON_ID": DRON_ID, "EC2_HOST": EC2_HOST}
missing = [k for k, v in required.items() if not v]
if missing:
    raise RuntimeError(f"Faltan variables de entorno: {', '.join(missing)}")


# =====================================================================
#  PUNTO DE REFERENCIA — CAMPO DE VUELO DE GALAPAGAR (LECMUAV090)
# =====================================================================
# La simulación parte de este punto y va derivando alrededor, como si el
# dron se moviese sobre el campo.
BASE_LAT = 40.5979097
BASE_LON = -3.9988503
BASE_ALT = 880.0        # ver NOTA sobre altitud más abajo
BASE_RUMBO = 0.0

# NOTA sobre BASE_ALT: 880 m es la altitud del campo sobre el nivel del mar
# (MSL). En el esquema, alt_rel es la altura RELATIVA al despegue (AGL), que
# en Galapagar está limitada a 120 m. Aquí se usa 880 tal cual se pidió; si
# se quiere una alt_rel realista (AGL), basta con poner BASE_ALT = 0 (o el
# valor de crucero deseado) y la simulación arrancará desde ahí.

# Radio máximo de deambulación alrededor del centro (~150 m), en grados.
_M_POR_GRADO_LAT = 111320.0
LIM_LAT = 150.0 / _M_POR_GRADO_LAT
LIM_LON = 150.0 / (_M_POR_GRADO_LAT * math.cos(math.radians(BASE_LAT)))


# =====================================================================
#  BUFFER LOCAL — LA FUENTE DE VERDAD
# =====================================================================
# El dominio de vuelo tiene MUCHOS campos (15). En vez de una columna por
# campo, se guarda el JSON completo del mensaje en una sola columna
# 'payload'. Es más limpio para este dominio y no cambia nada aguas abajo:
# el buffer solo tiene que almacenar y reenviar, no consultar campo a campo.
db = sqlite3.connect(DB)
db.execute("""CREATE TABLE IF NOT EXISTS lecturas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,                 -- hora de captura en formato ISO 8601
    payload TEXT,            -- JSON completo del dominio vuelo
    enviado INTEGER DEFAULT 0)""")
db.commit()


# =====================================================================
#  GENERACIÓN DE DATOS SIMULADOS
# =====================================================================
# Estado de la simulación. Se parte del punto de referencia y en cada
# lectura se le aplica una pequeña variación (deriva suave), para que la
# serie se parezca a un vuelo real y no a ruido.
_sim = {
    "lat": BASE_LAT, "lon": BASE_LON, "alt": BASE_ALT,
    "rumbo": BASE_RUMBO, "bat_v": 25.2, "bat_pct": 100.0,
}


def _acota(valor, minimo, maximo):
    """Mantiene un valor dentro de un rango [minimo, maximo]."""
    return max(minimo, min(maximo, valor))


def _mover(lat, lon, dnorte_m, deste_m):
    """Desplaza una posición (lat, lon) unos metros al norte y al este."""
    dlat = dnorte_m / _M_POR_GRADO_LAT
    dlon = deste_m / (_M_POR_GRADO_LAT * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def datos_simulados():
    """Genera una lectura de telemetría de vuelo plausible.

    Cuando el Pixhawk esté disponible, esta función se sustituye por una
    lectura real por MAVLink; el resto del script no cambia.
    """
    # Movimiento horizontal: paseo aleatorio de unos metros, acotado a un
    # radio de ~150 m alrededor del centro del campo.
    lat, lon = _mover(_sim["lat"], _sim["lon"],
                      random.uniform(-3, 3), random.uniform(-3, 3))
    _sim["lat"] = _acota(lat, BASE_LAT - LIM_LAT, BASE_LAT + LIM_LAT)
    _sim["lon"] = _acota(lon, BASE_LON - LIM_LON, BASE_LON + LIM_LON)

    _sim["alt"] = _acota(_sim["alt"] + random.uniform(-1.5, 1.5),
                         BASE_ALT - 15, BASE_ALT + 15)
    _sim["rumbo"] = (_sim["rumbo"] + random.uniform(-15, 15)) % 360
    _sim["bat_v"] = max(21.0, _sim["bat_v"] - 0.02)     # descarga lenta
    _sim["bat_pct"] = max(0.0, _sim["bat_pct"] - 0.1)

    return {
        "lat": round(_sim["lat"], 7),
        "lon": round(_sim["lon"], 7),
        "alt_rel": round(_sim["alt"], 1),
        "vel_terreno": round(random.uniform(0, 8), 1),
        "rumbo": round(_sim["rumbo"], 1),
        "roll": round(random.uniform(-8, 8), 1),
        "pitch": round(random.uniform(-8, 8), 1),
        "yaw": round((_sim["rumbo"] + random.uniform(-3, 3)) % 360, 1),
        "bateria_v": round(_sim["bat_v"], 2),
        "bateria_pct": int(_sim["bat_pct"]),
        "corriente_a": round(random.uniform(10, 18), 1),
        "satelites": random.randint(12, 16),
        "fix_type": 3,
        "modo": "AUTO",
        "armado": True,
    }


def escribir_posicion(lat, lon, alt_rel, ts):
    """Escribe la última posición conocida en posicion_actual.json.

    La escritura es ATÓMICA: se vuelca primero a un fichero temporal y
    luego se renombra, para que deteccion.py nunca lea un fichero a medio
    escribir. El campo 'ts' permite comprobar aguas abajo si la posición
    está fresca. Se hace en cada ciclo, haya red o no.
    """
    datos = {"lat": lat, "lon": lon, "alt_rel": alt_rel, "ts": ts}
    tmp = POS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(datos, f)
    os.replace(tmp, POS_FILE)      # renombrado atómico


# =====================================================================
#  CLIENTE MQTT
# =====================================================================
client = mqtt.Client(client_id=CLIENT_ID,
                     callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
client.reconnect_delay_set(min_delay=1, max_delay=30)

print(f"Conectando al broker en {EC2_HOST} como '{CLIENT_ID}'...")
client.connect_async(EC2_HOST, PORT, 60)
client.loop_start()

print(f"Dominio '{DOMINIO}' -> topic '{TOPIC}'  (datos SIMULADOS: Galapagar)")
print(f"Posición compartida en: {POS_FILE}")
print(f"Captura cada {args.intervalo}s con buffer local. (Ctrl+C para salir)")


# =====================================================================
#  FUNCIÓN: guardar() — CAPTURA (siempre activa)
# =====================================================================
def guardar():
    """Genera una lectura de vuelo, la guarda en el buffer y actualiza el
    fichero de posición para el proceso de detección.
    """
    m = datos_simulados()
    ts = datetime.now().astimezone().isoformat()

    # 1) Actualizar la posición compartida (lo más crítico para detección).
    escribir_posicion(m["lat"], m["lon"], m["alt_rel"], ts)

    # 2) Guardar el mensaje completo en el buffer local.
    payload = json.dumps({**m, "timestamp": ts})
    db.execute("INSERT INTO lecturas (ts, payload) VALUES (?,?)", (ts, payload))
    db.commit()


# =====================================================================
#  FUNCIÓN: reenviar() — ENVÍO (oportunista)
# =====================================================================
def reenviar():
    """Vacía el buffer hacia MQTT, solo con conexión confirmada (QoS 1)."""
    if not client.is_connected():
        return

    filas = db.execute(
        "SELECT id, payload FROM lecturas WHERE enviado=0 ORDER BY id LIMIT ?",
        (LOTE,)).fetchall()

    for id_, payload in filas:
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