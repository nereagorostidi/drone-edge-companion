"""
receptor.py — Receptor de comandos a bordo (Raspberry Pi)

Hace de puente entre dos mundos:

  1) Al broker MQTT: se suscribe al topic de comandos de su dron y espera
     ordenes (armar, desarmar...) publicadas desde la nube, ya sea por la
     API REST del panel de control o por comandos.py desde terminal.

  2) Al autopiloto por MAVLink: ejecuta esas ordenes (aqui, el simulador
     SITL: Mission Planner reenvia por UDP y este script escucha en
     udpin:0.0.0.0:14550).

Uso:
    python3 receptor.py
    (se queda escuchando; Ctrl+C para salir)
"""

import os
import json
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from pymavlink import mavutil


# =====================================================================
#  CONFIGURACION (desde .env)
# =====================================================================
load_dotenv()

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
DRONE_ID = os.getenv("DRONE_ID", "dron-01")

# Cadena de conexion MAVLink. Contra el simulador (SITL) es la misma que
# se valido en mision01.py: la Pi escucha y Mission Planner le reenvia.
MAVLINK_CONN = os.getenv("MAVLINK_CONN", "udpin:127.0.0.1:14550")

# El receptor escucha SOLO el topic de comandos de SU dron.
TOPIC = f"dronsar/{DRONE_ID}/comandos"


# =====================================================================
#  CONEXION MAVLINK (al autopiloto / simulador)
# =====================================================================
# Se conecta una sola vez, al arrancar, y se reutiliza para cada comando.
print(f"Conectando al autopiloto en {MAVLINK_CONN} ...")
master = mavutil.mavlink_connection(MAVLINK_CONN)
master.wait_heartbeat()
print(f"Autopiloto conectado: sistema {master.target_system}, "
      f"componente {master.target_component}")


# =====================================================================
#  ACCIONES MAVLINK  (la "traduccion" de cada comando)
# =====================================================================
def hacer_arm():
    """Arma los motores."""
    master.arducopter_arm()
    master.motors_armed_wait()
    print("  -> Dron ARMADO")


def hacer_disarm():
    """Desarma los motores."""
    master.arducopter_disarm()
    master.motors_disarmed_wait()
    print("  -> Dron DESARMADO")


# Diccionario que asocia cada 'command' del JSON con su funcion.
# Anadir un comando nuevo en el futuro = anadir una entrada aqui.
ACCIONES = {
    "arm": hacer_arm,
    "disarm": hacer_disarm,
}


# =====================================================================
#  CALLBACKS MQTT
# =====================================================================
def on_connect(client, userdata, flags, reason_code, properties):
    """Al conectar (o reconectar) al broker, nos suscribimos al topic."""
    print(f"Conectado al broker MQTT. Suscrito a '{TOPIC}'")
    client.subscribe(TOPIC, qos=1)


def on_message(client, userdata, msg):
    """Se ejecuta CADA VEZ que llega un comando por MQTT."""
    try:
        orden = json.loads(msg.payload)
    except json.JSONDecodeError:
        print("Mensaje recibido que no es JSON valido; se ignora.")
        return

    command = orden.get("command")
    cmd_id = orden.get("command_id", "?")
    print(f"Comando recibido [{cmd_id}]: {command}")

    accion = ACCIONES.get(command)
    if accion is None:
        print(f"  -> Comando desconocido '{command}'; se ignora.")
        return

    # Ejecuta la accion MAVLink correspondiente.
    accion()


# =====================================================================
#  BUCLE PRINCIPAL MQTT
# =====================================================================
client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

client.connect(MQTT_BROKER, MQTT_PORT, 60)

print("Receptor en marcha. Esperando comandos... (Ctrl+C para salir)")
try:
    client.loop_forever()      # se queda escuchando indefinidamente
except KeyboardInterrupt:
    print("\nDetenido por el usuario.")
    client.disconnect()
