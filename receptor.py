"""
receptor.py — Receptor de comandos a bordo (Raspberry Pi)

Hace de puente entre dos mundos:

  1) Al broker MQTT: se suscribe al topic de comandos de su dron y espera
     ordenes (armar, desarmar...) publicadas desde la nube, ya sea por la
     API REST del panel de control o por comandos.py desde terminal.

  2) Al autopiloto por MAVLink: ejecuta esas ordenes. La conexion depende
     de MAVLINK_MODE en el .env:
       - "sitl" (por defecto): Mission Planner reenvia por UDP y este
         script escucha en MAVLINK_CONN (por defecto udpin:0.0.0.0:14550).
       - "real": el Pixhawk esta conectado por TELEM3, y mavlink-router
         reparte ese puerto serie hacia MAVLINK_CONN_REAL_RECEPTOR
         (udpin:127.0.0.1:14550 por defecto).

La conexion MQTT es RESILIENTE: si el broker no resuelve por DNS o no
responde (p. ej. una caida temporal de Tailscale), el script NO se cae.
Se queda reintentando solo en segundo plano (con backoff) y lo avisa por
el log como WARNING/ERROR, pero sigue vivo — la conexion MAVLink con la
Pixhawk no depende de MQTT en ningun momento.

Uso:
    python3 receptor.py
    (se queda escuchando; Ctrl+C para salir)
"""

import os
import time
import json
import logging
import threading
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from pymavlink import mavutil


# =====================================================================
#  LOGGING
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("receptor")


# =====================================================================
#  CONFIGURACION (desde .env)
# =====================================================================
load_dotenv()

EC2_HOST = os.getenv("EC2_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
DRON_ID = os.getenv("DRON_ID")

# Validacion temprana, igual que el resto de dominios.
faltan = [k for k, v in {"DRON_ID": DRON_ID, "EC2_HOST": EC2_HOST}.items() if not v]
if faltan:
    raise SystemExit(f"Faltan variables en el .env: {', '.join(faltan)}.")

# --- Seleccion de modo de conexion MAVLink ---
# "sitl": Mission Planner / SITL (loopback, red).
# "real": mavlink-router sobre TELEM3 (Pixhawk real).
MAVLINK_MODE = os.getenv("MAVLINK_MODE", "sitl").strip().lower()

if MAVLINK_MODE == "real":
    MAVLINK_CONN = os.getenv("MAVLINK_CONN_REAL_RECEPTOR", "udpin:127.0.0.1:14550")
else:
    # Se mantiene compatibilidad con la variable MAVLINK_CONN original.
    MAVLINK_CONN = os.getenv("MAVLINK_CONN", "udpin:127.0.0.1:14550")

# --- Identificacion MAVLink propia de este proceso ---
# Mismo SYSID que el vehiculo, y un COMPID propio dentro del rango
# reservado para companion computers (MAV_COMP_ID_ONBOARD_COMPUTER=191).
# Sin esto, pymavlink usaria por defecto sysid=255/compid=0 (los mismos
# que Mission Planner), haciendo indistinguibles los tres origenes.
MAVLINK_SYSID = int(os.getenv("MAVLINK_SYSID", 1))
MAVLINK_COMPID = int(os.getenv("MAVLINK_COMPID_RECEPTOR", 191))

# El receptor escucha SOLO el topic de comandos de SU dron. Misma variable
# DRON_ID que usan sensor.py/sistema.py/vuelo.py/deteccion.py.
TOPIC = f"dronsar/{DRON_ID}/comandos"


# =====================================================================
#  CONEXION MAVLINK (al autopiloto / simulador)
# =====================================================================
# Se conecta una sola vez, al arrancar, y se reutiliza para cada comando.
# Esta conexion es independiente de MQTT: si el broker falla, esto sigue
# funcionando igual (los comandos simplemente no llegarian hasta que MQTT
# se recupere, pero el proceso y el enlace con la Pixhawk no se ven afectados).
log.info(f"Conectando al autopiloto en {MAVLINK_CONN} "
         f"(modo={MAVLINK_MODE}, sysid={MAVLINK_SYSID}, compid={MAVLINK_COMPID}) ...")
master = mavutil.mavlink_connection(
    MAVLINK_CONN,
    source_system=MAVLINK_SYSID,
    source_component=MAVLINK_COMPID,
)
master.wait_heartbeat()
log.info(f"Autopiloto conectado: sistema {master.target_system}, "
         f"componente {master.target_component}")


# =====================================================================
#  HILO DEDICADO DE LECTURA MAVLINK
# =====================================================================
# Es el UNICO sitio de todo el programa que llama a master.recv_match().
# Mantiene el estado (modo de vuelo, armado/desarmado) siempre al dia, y
# registra en el log cualquier STATUSTEXT que envie el autopiloto en
# cualquier momento (no solo durante un intento de armado) — p. ej. los
# motivos de un pre-arm check ("PreArm: GPS: no fix"). El resto del
# programa (comandos MQTT, el latido periodico) solo LEE el estado ya
# cacheado (master.flightmode / master.motors_armed()), nunca vuelve a
# leer del puerto — asi se evita que dos hilos lean MAVLink a la vez.
def _hilo_lector_mavlink():
    while True:
        try:
            msg = master.recv_match(blocking=True, timeout=1)
        except Exception as e:
            log.error(f"Error leyendo MAVLink: {e}")
            time.sleep(1)
            continue
        if msg is not None and msg.get_type() == "STATUSTEXT":
            log.info(f"[STATUSTEXT autopiloto] {msg.text.strip()}")


threading.Thread(target=_hilo_lector_mavlink, daemon=True).start()


# =====================================================================
#  ACCIONES MAVLINK  (la "traduccion" de cada comando)
# =====================================================================
TIMEOUT_ARM_DISARM = 10  # segundos de espera de confirmacion del autopiloto


def hacer_arm():
    """Arma los motores, con log detallado de todo el proceso.

    Solo ENVIA el comando y consulta master.motors_armed() (estado ya
    cacheado por el hilo lector); no vuelve a leer el puerto MAVLink
    directamente.
    """
    log.info("Enviando comando ARM al autopiloto ...")
    t0 = time.time()
    master.arducopter_arm()

    limite = time.time() + TIMEOUT_ARM_DISARM
    while time.time() < limite and not master.motors_armed():
        time.sleep(0.2)

    if master.motors_armed():
        log.info(f"  -> Dron ARMADO (confirmado por el autopiloto en {time.time()-t0:.2f}s)")
    else:
        log.warning(f"  -> El autopiloto NO ha confirmado el armado tras {TIMEOUT_ARM_DISARM}s. "
                    f"Revisa los [STATUSTEXT autopiloto] del log para el motivo "
                    f"(GPS, calibraciones, failsafe...).")


def hacer_disarm():
    """Desarma los motores, con log detallado de todo el proceso.

    Solo ENVIA el comando y consulta master.motors_armed() (estado ya
    cacheado por el hilo lector); no vuelve a leer el puerto MAVLink
    directamente.
    """
    log.info("Enviando comando DISARM al autopiloto ...")
    t0 = time.time()
    master.arducopter_disarm()

    limite = time.time() + TIMEOUT_ARM_DISARM
    while time.time() < limite and master.motors_armed():
        time.sleep(0.2)

    if not master.motors_armed():
        log.info(f"  -> Dron DESARMADO (confirmado por el autopiloto en {time.time()-t0:.2f}s)")
    else:
        log.error(f"  -> El autopiloto SIGUE ARMADO tras {TIMEOUT_ARM_DISARM}s intentando "
                  f"desarmar. Desarma manualmente por RC o Mission Planner de inmediato.")


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
    if reason_code == 0:
        log.info(f"Conectado al broker MQTT ({EC2_HOST}). Suscrito a '{TOPIC}'")
        client.subscribe(TOPIC, qos=1)
    else:
        log.warning(f"Conexion al broker MQTT rechazada (reason_code={reason_code}). "
                    f"Se seguira reintentando en segundo plano.")


def on_connect_fail(client, userdata):
    """Se llama cuando un INTENTO de conexion falla (p. ej. DNS que no
    resuelve, o el broker no responde) — a diferencia de on_disconnect,
    que es para cortes tras una conexion ya establecida. Es el callback
    real que se dispara en el caso que nos ocupa (DNS de ec2-aws caido).
    """
    log.warning(f"No se ha podido conectar al broker MQTT ({EC2_HOST}:{MQTT_PORT}). "
                f"Reintentando solo en segundo plano; los comandos no llegaran hasta "
                f"que la conexion se recupere, pero el enlace con la Pixhawk sigue activo.")


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    """Se llama en cortes de una conexion que ya estaba establecida.
    paho reintenta solo, con el backoff configurado en
    reconnect_delay_set — el proceso NO se cae por esto.
    """
    log.warning(f"Desconectado del broker MQTT ({EC2_HOST}, reason_code={reason_code}). "
                f"Reintentando solo en segundo plano.")


def on_message(client, userdata, msg):
    """Se ejecuta CADA VEZ que llega un comando por MQTT."""
    try:
        orden = json.loads(msg.payload)
    except json.JSONDecodeError:
        log.warning("Mensaje recibido que no es JSON valido; se ignora.")
        return

    command = orden.get("command")
    cmd_id = orden.get("command_id", "?")
    log.info(f"Comando recibido [{cmd_id}]: {command}")

    accion = ACCIONES.get(command)
    if accion is None:
        log.warning(f"  -> Comando desconocido '{command}'; se ignora.")
        return

    # Ejecuta la accion MAVLink correspondiente.
    accion()


# =====================================================================
#  CLIENTE MQTT — conexion RESILIENTE (no bloqueante, con reintento solo)
# =====================================================================
client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_connect_fail = on_connect_fail
client.on_disconnect = on_disconnect
client.on_message = on_message
client.reconnect_delay_set(min_delay=1, max_delay=30)

# connect_async() NO resuelve DNS ni abre el socket aqui mismo: solo deja
# los parametros guardados. La conexion real (y sus reintentos) ocurren
# en el hilo de red que arranca loop_start(), asi que un fallo de DNS/red
# se queda como un WARNING en el log via on_disconnect, sin tirar el
# proceso — a diferencia de connect() + loop_forever(), que si son
# bloqueantes y propagarian la excepcion hacia arriba.
try:
    client.connect_async(EC2_HOST, MQTT_PORT, 60)
except Exception as e:
    log.error(f"No se ha podido iniciar la conexion al broker MQTT ({EC2_HOST}): {e}. "
              f"El script seguira funcionando y reintentando en segundo plano.")

client.loop_start()

log.info("Receptor en marcha. Esperando comandos... (Ctrl+C para salir)")

INTERVALO_LATIDO = 30  # segundos entre cada resumen de estado en el log
ultimo_latido = 0.0

try:
    while True:
        ahora = time.time()
        if ahora - ultimo_latido >= INTERVALO_LATIDO:
            modo = master.flightmode or "DESCONOCIDO"
            armado = master.motors_armed()
            mqtt_ok = client.is_connected()
            log.info(f"[latido] vivo — MAVLink: modo={modo} armado={armado} | "
                     f"MQTT: {'conectado' if mqtt_ok else 'SIN conexion'}")
            ultimo_latido = ahora

        time.sleep(1)
except KeyboardInterrupt:
    log.info("Detenido por el usuario.")
    client.loop_stop()
    client.disconnect()
