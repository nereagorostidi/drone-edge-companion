"""
mision01.py — Mision de busqueda sobre el campo de Galapagar.

Barrido de 5 waypoints a 10 m y vuelta a casa (RTL) al terminar.

Se puede ejecutar de DOS formas, con la misma secuencia en ambas:

  1) Como MODULO, desde receptor.py (es lo normal en vuelo real).
     Cuando llega por MQTT el comando start_mission con
     params.mission = "mision01", el receptor llama a ejecutar(ctx) en un
     hilo aparte, REUTILIZANDO su propia conexion MAVLink. No se lanza un
     proceso nuevo ni se abre un segundo puerto: el receptor ya tiene
     ocupado el 14550, y dos procesos hablando a la vez con la Pixhawk
     harian falta mavproxy/mavlink-router y un aborto a base de kill.
     Aqui la mision es un hilo mas del receptor, asi que abortarla es
     simplemente activar un threading.Event.

  2) SUELTA, para probar contra Mission Planner / SITL:
         python3 mision01.py
     Abre su propia conexion (MAVLINK_CONN_MISION, por defecto
     udpin:127.0.0.1:14550). OJO: en ese caso receptor.py NO puede estar
     corriendo a la vez si comparten puerto.

La diferencia entre los dos modos esta encapsulada en ContextoMision: la
mision nunca llama a recv_match() ni a set_mode() directamente, sino a
las funciones que le pasa quien la ejecuta. Eso es lo que permite que en
el receptor respete su regla de "solo el hilo lector lee MAVLink".

Secuencia:
    subir waypoints -> GUIDED -> armar -> despegar -> AUTO -> seguir la
    mision hasta el RTL final.
"""

import os
import time
import threading
from pymavlink import mavutil


# =====================================================================
#  IDENTIDAD DE LA MISION (lo que lee receptor.py del registro MISIONES)
# =====================================================================
NOMBRE = "mision01"
DESCRIPCION = ("Barrido de 5 waypoints a 10 m sobre el campo de Galapagar; "
               "termina volviendo a casa (RTL)")


# =====================================================================
#  PLAN DE VUELO
# =====================================================================
# Waypoints tal cual salen de la tabla de Mission Planner:
#   (latitud, longitud, altitud_relativa_en_metros)
WAYPOINTS = [
    (40.5983246, -3.9986546, 10),
    (40.5984824, -3.9989001, 10),
    (40.5984702, -3.9993332, 10),
    (40.5987054, -3.9990342, 10),
    (40.5985221, -3.9986855, 10),
]

TAKEOFF_ALT = 10  # altitud de despegue en metros (relativa al home)

FRAME = mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT

# Tipos de mensaje MAVLink que esta mision necesita leer. receptor.py se
# suscribe a estos tipos ANTES de arrancar el hilo, para no perderse el
# primer MISSION_REQUEST del autopiloto.
TIPOS_MAVLINK = (
    "MISSION_REQUEST",       # el autopiloto pide el item N de la mision
    "MISSION_REQUEST_INT",   #   (variante _INT, segun firmware)
    "MISSION_ACK",           # confirma que ha recibido la mision entera
    "GLOBAL_POSITION_INT",   # altitud, para saber cuando ha despegado
    "MISSION_ITEM_REACHED",  # waypoint alcanzado, para seguir el progreso
)

TIMEOUT_PROTOCOLO = 5    # s de espera de cada MISSION_REQUEST / MISSION_ACK
TIMEOUT_DESPEGUE = 60    # s maximos para alcanzar la altitud de despegue
TIMEOUT_WAYPOINT = 180   # s maximos entre dos waypoints alcanzados


# =====================================================================
#  CONTEXTO — todo lo que la mision necesita del proceso que la ejecuta
# =====================================================================
class ContextoMision:
    """Las "manos y ojos" que la mision usa para hablar con el autopiloto.

    receptor.py construye uno de estos con SU conexion MAVLink, SU cambio
    de modo y un Event para abortar; el modo suelto (__main__) construye
    otro equivalente con una conexion propia. La mision no sabe cual de
    los dos la esta ejecutando.

    Campos:
        master       conexion pymavlink ya establecida (solo para ENVIAR).
        esperar      esperar(tipos, timeout) -> mensaje o None. Es la UNICA
                     via de lectura: en el receptor viene alimentada por su
                     hilo lector, nunca por recv_match() desde aqui.
        cambiar_modo cambiar_modo(nombre) -> bool (True si el autopiloto lo
                     confirma).
        armar        armar() -> bool (True si el autopiloto confirma armado).
        log          log(texto) para dejar traza de cada paso.
        abortado     threading.Event; si se activa, la mision se detiene en
                     el siguiente punto de control y NO vuelve a tocar el
                     modo de vuelo (lo controla ya quien la aborto).
    """

    def __init__(self, master, esperar, cambiar_modo, armar, log, abortado=None):
        self.master = master
        self.esperar = esperar
        self.cambiar_modo = cambiar_modo
        self.armar = armar
        self.log = log
        self.abortado = abortado or threading.Event()

    def abortada(self, punto):
        """True si hay que parar. Se consulta antes de cada paso que mueve
        el dron, para que un land/rtl/hold desde la web tenga efecto
        inmediato y la mision no le pise el modo de vuelo despues."""
        if self.abortado.is_set():
            self.log(f"Mision ABORTADA ({punto}). No se envian mas ordenes.")
            return True
        return False

    def esperar_atento(self, tipos, timeout, punto):
        """Como esperar(), pero vigilando el aborto mientras espera.

        Devuelve (mensaje, abortada). Sin esto, una espera larga (hasta
        TIMEOUT_WAYPOINT entre dos waypoints) dejaria la mision sorda a un
        land/rtl durante minutos; troceando la espera se entera en menos
        de medio segundo.
        """
        limite = time.time() + timeout
        while True:
            if self.abortada(punto):
                return None, True
            restante = limite - time.time()
            if restante <= 0:
                return None, False
            msg = self.esperar(tipos, min(restante, 0.5))
            if msg is not None:
                return msg, False

    def vaciar(self):
        """Descarta lo que haya pendiente de leer. Se usa antes de empezar
        un intercambio del protocolo de mision, para no confundir un
        mensaje viejo con la respuesta que toca."""
        while self.esperar(TIPOS_MAVLINK, 0.05) is not None:
            pass


# =====================================================================
#  PASOS DE LA MISION
# =====================================================================
def subir_mision(ctx, waypoints):
    """Sube la mision siguiendo el protocolo MAVLink (count + request loop).

    Devuelve el numero de items subidos, o None si el autopiloto no
    responde a tiempo.

    Los items son:
        seq 0      placeholder de home (el autopiloto lo sustituye por el real)
        seq 1..N   los waypoints del plan
        seq N+1    RTL, para que vuelva y aterrice al acabar
    """
    items = [(waypoints[0][0], waypoints[0][1], 0,
              mavutil.mavlink.MAV_CMD_NAV_WAYPOINT)]

    for lat, lon, alt in waypoints:
        items.append((lat, lon, alt, mavutil.mavlink.MAV_CMD_NAV_WAYPOINT))

    items.append((0, 0, 0, mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH))

    tgt_sys = ctx.master.target_system
    tgt_comp = ctx.master.target_component

    # Limpiamos cualquier mision anterior. El clear genera su propio
    # MISSION_ACK, asi que se le da un momento y se descarta todo lo
    # pendiente antes de empezar: si no, ese ack se colaria mas abajo
    # como si fuera el de nuestra mision.
    ctx.master.mav.mission_clear_all_send(tgt_sys, tgt_comp)
    time.sleep(0.5)
    ctx.vaciar()

    ctx.log(f"Subiendo mision: {len(items)} items "
            f"({len(waypoints)} waypoints + home + RTL) ...")
    ctx.master.mav.mission_count_send(tgt_sys, tgt_comp, len(items))

    # El autopiloto nos pide cada item por orden; se lo enviamos.
    for _ in range(len(items)):
        req = ctx.esperar(("MISSION_REQUEST", "MISSION_REQUEST_INT"),
                          TIMEOUT_PROTOCOLO)
        if req is None:
            ctx.log(f"  -> El autopiloto ha dejado de pedir items tras "
                    f"{TIMEOUT_PROTOCOLO}s. Mision NO subida.")
            return None

        seq = req.seq
        lat, lon, alt, cmd = items[seq]
        ctx.master.mav.mission_item_int_send(
            tgt_sys, tgt_comp,
            seq,
            FRAME,
            cmd,
            0,            # current
            1,            # autocontinue
            0, 0, 0, 0,   # param1-4 (sin uso para WAYPOINT/RTL)
            int(lat * 1e7),
            int(lon * 1e7),
            float(alt),
            mavutil.mavlink.MAV_MISSION_TYPE_MISSION,
        )
        ctx.log(f"  Item {seq} enviado")

    ack = ctx.esperar(("MISSION_ACK",), TIMEOUT_PROTOCOLO)
    if ack is None:
        ctx.log("  -> Sin MISSION_ACK del autopiloto; no se puede dar la "
                "mision por subida.")
        return None
    if ack.type != mavutil.mavlink.MAV_MISSION_ACCEPTED:
        ctx.log(f"  -> El autopiloto ha RECHAZADO la mision (ack={ack.type}).")
        return None

    ctx.log(f"Mision subida y aceptada ({len(items)} items).")
    return len(items)


def esperar_altitud(ctx, altitud_objetivo):
    """Espera hasta alcanzar (casi) la altitud objetivo. True si llega."""
    limite = time.time() + TIMEOUT_DESPEGUE
    while time.time() < limite:
        msg, abortada = ctx.esperar_atento(
            ("GLOBAL_POSITION_INT",), limite - time.time(),
            "durante el despegue")
        if abortada:
            return False
        if msg is None:
            break
        alt = msg.relative_alt / 1000.0  # mm -> m
        if alt >= altitud_objetivo * 0.95:
            ctx.log(f"Altitud de despegue alcanzada ({alt:.1f} m).")
            return True

    ctx.log(f"  -> No se ha alcanzado la altitud de despegue en "
            f"{TIMEOUT_DESPEGUE}s. Mision detenida (el dron se queda en "
            f"GUIDED, usa hold/land/rtl desde la web).")
    return False


def ejecutar(ctx):
    """Ejecuta la mision completa. Devuelve True solo si termina el recorrido.

    Cada paso comprueba antes si la mision ha sido abortada: en cuanto
    llega un land/rtl/hold desde la web, esta funcion deja de enviar
    ordenes y sale, sin volver a tocar el modo de vuelo.
    """
    ctx.log(f"=== {NOMBRE}: {DESCRIPCION} ===")

    if ctx.abortada("antes de empezar"):
        return False

    n_items = subir_mision(ctx, WAYPOINTS)
    if n_items is None:
        return False

    # GUIDED para armar y despegar de forma controlada.
    if ctx.abortada("antes de entrar en GUIDED"):
        return False
    if not ctx.cambiar_modo("GUIDED"):
        ctx.log("  -> Mision abortada: no se ha podido entrar en GUIDED.")
        return False

    # Armar motores.
    if ctx.abortada("antes de armar"):
        return False
    if not ctx.armar():
        ctx.log("  -> Mision abortada: el autopiloto no ha armado. Revisa "
                "los STATUSTEXT del log (GPS, calibraciones, failsafe...).")
        return False

    # Despegue vertical a la altitud definida.
    if ctx.abortada("antes de despegar"):
        return False
    ctx.log(f"Despegando a {TAKEOFF_ALT} m ...")
    ctx.master.mav.command_long_send(
        ctx.master.target_system, ctx.master.target_component,
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0,
        0, 0, 0, 0, 0, 0, TAKEOFF_ALT)

    if not esperar_altitud(ctx, TAKEOFF_ALT):
        return False

    # AUTO: apuntamos al primer waypoint y arrancamos el recorrido.
    if ctx.abortada("antes de entrar en AUTO"):
        return False
    ctx.master.mav.mission_set_current_send(
        ctx.master.target_system, ctx.master.target_component, 1)
    if not ctx.cambiar_modo("AUTO"):
        ctx.log("  -> Mision detenida: no se ha podido entrar en AUTO (el "
                "dron se queda en el aire en GUIDED; usa hold/land/rtl).")
        return False
    ctx.log("Mision en marcha (AUTO).")

    # El ultimo item es el RTL; su seq es n_items - 1.
    ultimo_seq = n_items - 1

    while True:
        msg, abortada = ctx.esperar_atento(
            ("MISSION_ITEM_REACHED",), TIMEOUT_WAYPOINT,
            "durante el recorrido")
        if abortada:
            return False
        if msg is None:
            ctx.log(f"  -> Sin waypoints alcanzados en {TIMEOUT_WAYPOINT}s. "
                    f"Se deja de seguir la mision (el autopiloto sigue en "
                    f"AUTO por su cuenta).")
            return False
        ctx.log(f"Waypoint {msg.seq}/{ultimo_seq} alcanzado")
        if msg.seq == ultimo_seq:
            ctx.log("Ultimo punto alcanzado: el dron regresa a casa (RTL). "
                    "Mision completada.")
            return True


# =====================================================================
#  MODO SUELTO — python3 mision01.py (pruebas con Mission Planner / SITL)
# =====================================================================
def _contexto_suelto():
    """Monta un ContextoMision con conexion propia, para ejecutar el script
    a mano. Aqui si se puede leer con recv_match() directamente porque no
    hay ningun otro hilo compartiendo la conexion."""
    conexion = os.getenv("MAVLINK_CONN_MISION", "udpin:127.0.0.1:14550")
    print(f"Conectando al autopiloto en {conexion} ...")
    master = mavutil.mavlink_connection(conexion)
    master.wait_heartbeat()
    print(f"Conectado: sistema {master.target_system}, "
          f"componente {master.target_component}")

    def esperar(tipos, timeout):
        return master.recv_match(type=list(tipos), blocking=True,
                                 timeout=timeout)

    def cambiar_modo(modo):
        mapa = master.mode_mapping() or {}
        if modo not in mapa:
            print(f"Modo desconocido: {modo}")
            return False
        master.set_mode(mapa[modo])
        limite = time.time() + 5
        while time.time() < limite:
            if (master.flightmode or "").upper() == modo:
                print(f"Modo: {modo}")
                return True
            master.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
        return False

    def armar():
        master.arducopter_arm()
        master.motors_armed_wait()
        print("Motores armados")
        return True

    return ContextoMision(master, esperar, cambiar_modo, armar, print)


if __name__ == "__main__":
    ejecutar(_contexto_suelto())
