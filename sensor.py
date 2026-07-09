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

Uso:
    python3 sensor.py              # captura cada 30 s (por defecto)
    python3 sensor.py -i 5         # captura cada 5 s
    python3 sensor.py --intervalo 60
"""

import os
import time
import json
import sqlite3
import argparse
import bme680
from datetime import datetime
from dotenv import load_dotenv
import paho.mqtt.client as mqtt


# =====================================================================
#  CARGA DE VARIABLES DE ENTORNO
# =====================================================================
# Se leen desde un archivo .env situado en el mismo directorio del
# script (o el que systemd defina como WorkingDirectory).
load_dotenv()


# =====================================================================
#  ARGUMENTOS DE LÍNEA DE COMANDOS
# =====================================================================
# Permite parametrizar el intervalo de captura sin editar el código.
# argparse genera además una ayuda automática con "python3 sensor.py -h".
parser = argparse.ArgumentParser(
    description="Lectura y envío MQTT del sensor BME680 con buffer local")
parser.add_argument("-i", "--intervalo", type=float, default=30.0,
                    help="Segundos entre cada lectura (por defecto: 30)")
args = parser.parse_args()


# =====================================================================
#  CONFIGURACIÓN
# =====================================================================
# Todas las variables sensibles y configurables se cargan desde el .env,
# nunca hardcodeadas en el código. Esto permite versionar el script en
# público sin exponer credenciales ni infraestructura.
EC2_HOST = os.getenv("EC2_HOST")                              # IP o dominio del broker
PORT = int(os.getenv("MQTT_PORT", 1883))                      # Puerto MQTT
TOPIC = os.getenv("MQTT_TOPIC")                               # Topic de publicación
DB = os.getenv("BUFFER_DB", "/home/nerea/drone-edge-companion/buffer.db")
LOTE = int(os.getenv("LOTE", 50))                             # Filas por ciclo

# Validación temprana: si falta alguna variable obligatoria, el script
# falla con un mensaje claro en lugar de con errores crípticos más tarde.
required = {"EC2_HOST": EC2_HOST, "MQTT_TOPIC": TOPIC}
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
#  INICIALIZACIÓN DEL SENSOR BME680
# =====================================================================
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


# =====================================================================
#  CLIENTE MQTT
# =====================================================================
client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

# Reconexión automática: si se cae la red, la librería reintenta sola,
# esperando entre 1 y 30 s de forma progresiva. No hay que hacer nada
# manualmente; cuando vuelva la WiFi, se reconecta y el buffer se vacía.
client.reconnect_delay_set(min_delay=1, max_delay=30)

# connect_async NO bloquea aunque el broker no esté disponible al arrancar.
# Combinado con loop_start(), la gestión de red corre en segundo plano.
print(f"Conectando al bróker en {EC2_HOST}...")
client.connect_async(EC2_HOST, PORT, 60)
client.loop_start()

print(f"Captura cada {args.intervalo}s con buffer local. (Ctrl+C para salir)")


# =====================================================================
#  FUNCIÓN: guardar() — CAPTURA (siempre activa)
# =====================================================================
def guardar():
    """Lee el sensor y almacena la medida en el buffer local.

    Esto ocurre SIEMPRE, haya red o no. La hora se toma en el momento
    exacto de la captura (no del envío) y en formato ISO 8601 con zona
    horaria, para que un dato que se envíe con retraso se guarde luego
    con su instante real y no con el de la reconexión.
    """
    if sensor.get_sensor_data():   # True cuando hay una medida válida lista
        db.execute(
            "INSERT INTO lecturas (ts, temp, hum, pres) VALUES (?,?,?,?)",
            (datetime.now().astimezone().isoformat(),
             round(sensor.data.temperature, 2),
             round(sensor.data.humidity, 2),
             round(sensor.data.pressure, 2)))
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
        # El JSON mantiene el mismo formato que espera el servidor EC2,
        # por lo que NO hay que modificar el puente del EC2.
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
                print(f"Enviado: {payload}")
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
        time.sleep(args.intervalo)

except KeyboardInterrupt:
    # Cierre ordenado al pulsar Ctrl+C.
    print("\nDetenido por el usuario.")
    client.loop_stop()
    client.disconnect()
    db.close()
