"""
receptor.py — Receptor de comandos a bordo (Raspberry Pi)

Hace de puente entre dos mundos:

  1) Al broker MQTT: se suscribe al topic de comandos de su dron y espera
     ordenes (arm, disarm, takeoff, hold, land, rtl, start_mission)
     publicadas desde la nube, ya sea por la API REST del panel de control
     o por comandos.py desde terminal.

  2) Al autopiloto por MAVLink: ejecuta esas ordenes. La conexion depende
     de MAVLINK_MODE en el .env:
       - "sitl" (por defecto): Mission Planner reenvia por UDP y este
         script escucha en MAVLINK_CONN (por defecto udpin:0.0.0.0:14550).
       - "real": el Pixhawk esta conectado por TELEM3, y mavlink-router
         reparte ese puerto serie hacia MAVLINK_CONN_REAL_RECEPTOR
         (udpin:127.0.0.1:14550 por defecto).

El comando start_mission (boton "Iniciar mision" de la web) lleva en
params.mission el NOMBRE de una mision. Ese nombre se busca en el
diccionario MISIONES de aqui abajo, que lo traduce a un modulo Python ya
importado; nunca se ejecuta el texto que llega por MQTT. La mision se
ejecuta en un hilo de este mismo proceso, reutilizando esta conexion
MAVLink (no se lanza otro proceso ni se abre otro puerto), y cualquier
land/rtl/hold posterior la aborta antes de mandar su propio comando.

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
import queue
import logging
import threading
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from pymavlink import mavutil

import mision01


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
#
# Una mision en curso SI necesita mensajes concretos (MISSION_REQUEST,
# MISSION_ACK, MISSION_ITEM_REACHED...), que no se pueden sacar del cache
# porque importa recibirlos en orden y en su momento. Para eso esta
# SuscripcionMavlink: el hilo lector sigue siendo el unico que lee del
# puerto, y va dejando COPIAS de los tipos pedidos en una cola que lee el
# hilo de la mision.
_suscripciones = []
_lock_suscripciones = threading.Lock()

# Tope de la cola de cada suscripcion. GLOBAL_POSITION_INT llega varias
# veces por segundo: si por lo que sea nadie la vacia, se tiran los
# mensajes nuevos en vez de comerse la memoria de la Pi.
MAX_COLA_SUSCRIPCION = 2000


class SuscripcionMavlink:
    """Cola de mensajes MAVLink de ciertos tipos, alimentada por el hilo lector.

    Se usa como context manager para que la suscripcion se de de baja
    siempre, aunque la mision falle a mitad:

        with SuscripcionMavlink(("MISSION_ACK",)) as sub:
            msg = sub.esperar(("MISSION_ACK",), timeout=5)
    """

    def __init__(self, tipos):
        self.tipos = set(tipos)
        self.cola = queue.Queue(maxsize=MAX_COLA_SUSCRIPCION)

    def __enter__(self):
        with _lock_suscripciones:
            _suscripciones.append(self)
        return self

    def __exit__(self, *excepcion):
        with _lock_suscripciones:
            if self in _suscripciones:
                _suscripciones.remove(self)
        return False

    def _entregar(self, msg):
        """Llamado SOLO por el hilo lector."""
        if msg.get_type() not in self.tipos:
            return
        try:
            self.cola.put_nowait(msg)
        except queue.Full:
            pass

    def esperar(self, tipos, timeout):
        """Devuelve el primer mensaje de alguno de 'tipos', o None si se
        agota el tiempo. Los mensajes de otros tipos que haya por delante
        en la cola se descartan por el camino (asi el chorro de
        GLOBAL_POSITION_INT no la atasca)."""
        tipos = set(tipos)
        limite = time.time() + timeout
        while True:
            restante = limite - time.time()
            if restante < 0:
                return None
            try:
                msg = self.cola.get(timeout=max(restante, 0.01))
            except queue.Empty:
                return None
            if msg.get_type() in tipos:
                return msg


def _hilo_lector_mavlink():
    while True:
        try:
            msg = master.recv_match(blocking=True, timeout=1)
        except Exception as e:
            log.error(f"Error leyendo MAVLink: {e}")
            time.sleep(1)
            continue
        if msg is None:
            continue
        if msg.get_type() == "STATUSTEXT":
            log.info(f"[STATUSTEXT autopiloto] {msg.text.strip()}")
        # Reparto a quien este ejecutando una mision (normalmente, nadie).
        if _suscripciones:
            with _lock_suscripciones:
                for sub in _suscripciones:
                    sub._entregar(msg)


threading.Thread(target=_hilo_lector_mavlink, daemon=True).start()


# =====================================================================
#  ACCIONES MAVLINK  (la "traduccion" de cada comando)
# =====================================================================
TIMEOUT_ARM_DISARM = 10   # segundos de espera de confirmacion del autopiloto
TIMEOUT_MODO = 5          # segundos de espera de confirmacion de cambio de modo
ALTITUD_DESPEGUE_DEF = 10  # metros (AGL) si el comando takeoff no trae params.altitude


def hacer_arm():
    """Arma los motores, con log detallado de todo el proceso.

    Solo ENVIA el comando y consulta master.motors_armed() (estado ya
    cacheado por el hilo lector); no vuelve a leer el puerto MAVLink
    directamente. Devuelve True si el autopiloto confirma el armado (lo
    usa la mision para no seguir adelante si no ha armado).
    """
    log.info("Enviando comando ARM al autopiloto ...")
    t0 = time.time()
    master.arducopter_arm()

    limite = time.time() + TIMEOUT_ARM_DISARM
    while time.time() < limite and not master.motors_armed():
        time.sleep(0.2)

    if master.motors_armed():
        log.info(f"  -> Dron ARMADO (confirmado por el autopiloto en {time.time()-t0:.2f}s)")
        return True

    log.warning(f"  -> El autopiloto NO ha confirmado el armado tras {TIMEOUT_ARM_DISARM}s. "
                f"Revisa los [STATUSTEXT autopiloto] del log para el motivo "
                f"(GPS, calibraciones, failsafe...).")
    return False


def hacer_disarm():
    """Desarma los motores, con log detallado de todo el proceso.

    Solo ENVIA el comando y consulta master.motors_armed() (estado ya
    cacheado por el hilo lector); no vuelve a leer el puerto MAVLink
    directamente.
    """
    # Desarmar con una mision en marcha seria dejar al hilo mandando
    # ordenes a un dron parado: se corta primero.
    abortar_mision_en_curso("disarm desde la web")

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


def cambiar_modo(modo):
    """Cambia el modo de vuelo del autopiloto y espera su confirmacion.

    NO lee del puerto MAVLink: solo ENVIA el cambio y consulta
    master.flightmode (estado ya cacheado por el hilo lector). Devuelve
    True si el autopiloto confirma el modo dentro de TIMEOUT_MODO.
    """
    modo = modo.upper()
    mapa = master.mode_mapping() or {}
    if modo not in mapa:
        log.error(f"  -> El autopiloto no reconoce el modo '{modo}'. "
                  f"Modos disponibles: {sorted(mapa)}")
        return False

    log.info(f"Enviando cambio de modo -> {modo} ...")
    master.set_mode(mapa[modo])

    limite = time.time() + TIMEOUT_MODO
    while time.time() < limite:
        if (master.flightmode or "").upper() == modo:
            log.info(f"  -> Modo {modo} confirmado por el autopiloto.")
            return True
        time.sleep(0.2)

    log.warning(f"  -> El autopiloto NO ha confirmado el modo {modo} tras "
                f"{TIMEOUT_MODO}s (modo actual: {master.flightmode}). "
                f"Revisa los [STATUSTEXT autopiloto] del log para el motivo.")
    return False


# =====================================================================
#  MISIONES  (comando start_mission)
# =====================================================================
# La web publica {"command": "start_mission", "params": {"mission": "mision01"}}.
# Ese nombre NO se ejecuta nunca como texto: solo sirve de clave en este
# diccionario, que lo traduce a un modulo Python ya importado arriba. Si
# el nombre no esta aqui, el comando se ignora (la API ya lo valida con su
# lista blanca, pero se vuelve a comprobar a bordo).
#
# Anadir una mision nueva = crear misionNN.py con la misma interfaz que
# mision01 (NOMBRE, TIPOS_MAVLINK, ejecutar(ctx)) y anadirla aqui.
MISIONES = {
    mision01.NOMBRE: mision01,
}

# La mision corre en un hilo de ESTE proceso y reutiliza la conexion
# MAVLink de arriba. No se lanza un proceso aparte a proposito: el puerto
# de esta conexion ya esta ocupado, dos procesos hablando con la Pixhawk
# necesitarian mavlink-router, y abortar seria matar un proceso a mitad de
# vuelo. Asi, abortar es solo activar un Event.
_mision_en_curso = None                # threading.Thread de la mision activa
_mision_abortar = threading.Event()    # se activa para pedirle que pare
_lock_mision = threading.Lock()        # evita lanzar dos a la vez

TIMEOUT_ABORTO_MISION = 5   # s de cortesia para que el hilo de mision salga

# --- Preflight: condiciones minimas para arrancar una mision ---
# Una mision arma y despega sola, asi que no se lanza a ciegas. Se puede
# desactivar con PREFLIGHT_MISION=0 en el .env para pruebas de banco.
PREFLIGHT_MISION = os.getenv("PREFLIGHT_MISION", "1").strip().lower() not in ("0", "false", "no")
MIN_FIX_TYPE = 3      # 3 = fix 3D
MIN_SATELITES = 6

# Banderas del bitmask de EKF_STATUS_REPORT. Se escriben como numeros para
# no depender de que el dialecto MAVLink cargado las exponga por nombre.
EKF_ATTITUDE = 1
EKF_VELOCITY_HORIZ = 2
EKF_POS_HORIZ_ABS = 16
EKF_UNINITIALIZED = 1024


def _preflight_ok():
    """Comprueba GPS y EKF antes de arrancar una mision.

    Lee del cache de mensajes que mantiene el hilo lector (master.messages),
    no del puerto. Devuelve True si se puede volar.
    """
    if not PREFLIGHT_MISION:
        log.warning("  -> PREFLIGHT_MISION=0: se arranca la mision SIN "
                    "comprobar GPS/EKF. Solo para pruebas en banco.")
        return True

    gps = master.messages.get("GPS_RAW_INT")
    if gps is None:
        log.error("  -> Mision no arrancada: todavia no ha llegado ningun "
                  "GPS_RAW_INT del autopiloto.")
        return False

    if gps.fix_type < MIN_FIX_TYPE:
        log.error(f"  -> Mision no arrancada: sin fix 3D "
                  f"(fix_type={gps.fix_type}, se necesita >= {MIN_FIX_TYPE}).")
        return False

    sats = gps.satellites_visible if gps.satellites_visible != 255 else 0
    if sats < MIN_SATELITES:
        log.error(f"  -> Mision no arrancada: solo {sats} satelites "
                  f"(se necesitan >= {MIN_SATELITES}).")
        return False

    ekf = master.messages.get("EKF_STATUS_REPORT")
    if ekf is not None:
        necesarias = EKF_ATTITUDE | EKF_VELOCITY_HORIZ | EKF_POS_HORIZ_ABS
        if ekf.flags & EKF_UNINITIALIZED:
            log.error("  -> Mision no arrancada: el EKF aun se esta inicializando.")
            return False
        if (ekf.flags & necesarias) != necesarias:
            log.error(f"  -> Mision no arrancada: el EKF no tiene todavia "
                      f"actitud + velocidad + posicion absoluta "
                      f"(flags={ekf.flags}).")
            return False

    log.info(f"  -> Preflight OK (fix_type={gps.fix_type}, satelites={sats}).")
    return True


def abortar_mision_en_curso(motivo):
    """Pide a la mision activa (si la hay) que pare, y la espera un poco.

    Se llama ANTES de mandar hold/land/rtl/land: asi el hilo de la mision
    ya no vuelve a tocar el modo de vuelo y no le pisa el comando que
    viene de la web. Si el hilo tarda mas de la cuenta en salir da igual,
    porque en cuanto ve el Event deja de enviar ordenes.
    """
    hilo = _mision_en_curso
    if hilo is None or not hilo.is_alive():
        return

    log.warning(f"Abortando la mision en curso ({motivo}) ...")
    _mision_abortar.set()
    hilo.join(timeout=TIMEOUT_ABORTO_MISION)
    if hilo.is_alive():
        log.warning(f"  -> El hilo de la mision sigue cerrandose tras "
                    f"{TIMEOUT_ABORTO_MISION}s; ya no enviara ordenes, se "
                    f"continua con el comando.")
    else:
        log.info("  -> Mision detenida.")


def _ejecutar_mision(modulo):
    """Cuerpo del hilo de mision: prepara el contexto y la ejecuta.

    La suscripcion se abre AQUI, antes de la primera orden, para no
    perderse el primer MISSION_REQUEST del autopiloto, y se cierra sola al
    salir del with (aunque la mision falle a mitad).
    """
    def log_mision(texto):
        log.info(f"[{modulo.NOMBRE}] {texto}")

    try:
        with SuscripcionMavlink(modulo.TIPOS_MAVLINK) as sub:
            ctx = mision01.ContextoMision(
                master=master,
                esperar=sub.esperar,
                cambiar_modo=cambiar_modo,
                armar=hacer_arm,
                log=log_mision,
                abortado=_mision_abortar,
            )
            completada = modulo.ejecutar(ctx)
    except Exception as e:
        log.exception(f"[{modulo.NOMBRE}] La mision ha fallado con una "
                      f"excepcion: {e}. El dron sigue en el modo en el que "
                      f"estuviera; usa hold/land/rtl desde la web.")
        return

    if completada:
        log.info(f"[{modulo.NOMBRE}] Mision terminada correctamente.")
    else:
        log.warning(f"[{modulo.NOMBRE}] Mision terminada SIN completar el "
                    f"recorrido (revisa el log de arriba).")


def hacer_start_mission(params):
    """Arranca la mision que venga en params['mission'], si es conocida."""
    global _mision_en_curso

    nombre = params.get("mission")
    modulo = MISIONES.get(nombre) if isinstance(nombre, str) else None
    if modulo is None:
        log.warning(f"  -> Mision desconocida '{nombre}'; se ignora. "
                    f"Disponibles: {sorted(MISIONES)}")
        return

    with _lock_mision:
        if _mision_en_curso is not None and _mision_en_curso.is_alive():
            log.warning(f"  -> Ya hay una mision en curso; se ignora "
                        f"'{nombre}'. Aborta con hold/land/rtl antes de "
                        f"lanzar otra.")
            return

        log.info(f"Preparando mision '{nombre}': {modulo.DESCRIPCION}")
        if not _preflight_ok():
            return

        _mision_abortar.clear()
        _mision_en_curso = threading.Thread(
            target=_ejecutar_mision, args=(modulo,),
            name=f"mision-{nombre}", daemon=True)
        _mision_en_curso.start()

    log.info(f"  -> Mision '{nombre}' lanzada.")


def hacer_takeoff(params):
    """Despegue vertical a params['altitude'] metros (AGL).

    Secuencia: GUIDED -> armar (si no lo esta ya) -> MAV_CMD_NAV_TAKEOFF.
    Es fire-and-forget: no bloquea esperando alcanzar la altitud (el
    [latido] del log y vuelo.py reflejan la altura real).
    """
    # Un despegue manual toma el control en GUIDED: si habia una mision
    # volando, se para primero para que no se peleen por el modo.
    abortar_mision_en_curso("llega un takeoff manual")

    try:
        altitud = float(params.get("altitude"))
    except (TypeError, ValueError):
        altitud = ALTITUD_DESPEGUE_DEF
        log.warning(f"  -> takeoff sin 'altitude' valido; se usa {altitud} m por defecto.")

    if not cambiar_modo("GUIDED"):
        log.error("  -> Despegue abortado: no se pudo entrar en GUIDED.")
        return

    if not master.motors_armed():
        hacer_arm()
        if not master.motors_armed():
            log.error("  -> Despegue abortado: el autopiloto no ha armado.")
            return

    log.info(f"Enviando MAV_CMD_NAV_TAKEOFF a {altitud} m ...")
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0,
        0, 0, 0, 0, 0, 0, altitud)


# hold / land / rtl son ademas la forma de ABORTAR una mision desde la
# web: primero se para el hilo de la mision y despues se manda el cambio
# de modo, para que la mision no lo pise un segundo despues.
def hacer_hold(params=None):
    """'Mantener posicion' -> modo LOITER (requiere GPS con fix 3D)."""
    abortar_mision_en_curso("hold desde la web")
    cambiar_modo("LOITER")


def hacer_land(params=None):
    """Aterrizar en la vertical actual -> modo LAND."""
    abortar_mision_en_curso("land desde la web")
    cambiar_modo("LAND")


def hacer_rtl(params=None):
    """Volver a casa y aterrizar -> modo RTL."""
    abortar_mision_en_curso("rtl desde la web")
    cambiar_modo("RTL")


# Diccionario que asocia cada 'command' del JSON con su funcion. Todas las
# acciones reciben 'params' (el dict params del JSON MQTT), aunque la
# mayoria lo ignore; lo usan takeoff (params['altitude']) y start_mission
# (params['mission']).
# Anadir un comando nuevo en el futuro = anadir una entrada aqui.
ACCIONES = {
    "arm": lambda params: hacer_arm(),
    "disarm": lambda params: hacer_disarm(),
    "takeoff": hacer_takeoff,
    "hold": hacer_hold,
    "land": hacer_land,
    "rtl": hacer_rtl,
    "start_mission": hacer_start_mission,
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
    params = orden.get("params") or {}
    cmd_id = orden.get("command_id", "?")
    log.info(f"Comando recibido [{cmd_id}]: {command} params={params}")

    accion = ACCIONES.get(command)
    if accion is None:
        log.warning(f"  -> Comando desconocido '{command}'; se ignora.")
        return

    # Ejecuta la accion MAVLink correspondiente, pasandole los params del JSON.
    accion(params)


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
            en_mision = (_mision_en_curso is not None
                         and _mision_en_curso.is_alive())
            log.info(f"[latido] vivo — MAVLink: modo={modo} armado={armado} | "
                     f"MQTT: {'conectado' if mqtt_ok else 'SIN conexion'} | "
                     f"mision: {'en curso' if en_mision else 'ninguna'}")
            ultimo_latido = ahora

        time.sleep(1)
except KeyboardInterrupt:
    log.info("Detenido por el usuario.")
    client.loop_stop()
    client.disconnect()
