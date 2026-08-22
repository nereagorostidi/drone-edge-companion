"""
Ejecuta la misión de búsqueda sobre el campo de Galapagar en el SITL.

Se conecta al autopiloto a través del reenvío UDP de Mission Planner
(SerialOutput -> UDP Outbound 0.0.0.0:14550), sube los 4 waypoints,
arma, despega en GUIDED y lanza la misión en AUTO. Al terminar, el dron
vuelve al punto de despegue y aterriza (RTL).
"""

import time
from pymavlink import mavutil

# ----------------------------------------------------------------------
# 1. Conexión (a través del reenvío UDP de Mission Planner)
# ----------------------------------------------------------------------
master = mavutil.mavlink_connection('udpin:0.0.0.0:14550')
master.wait_heartbeat()
print(f"Conectado: sistema {master.target_system}, "
      f"componente {master.target_component}")

tgt_sys = master.target_system
tgt_comp = master.target_component

# ----------------------------------------------------------------------
# 2. Waypoints de tu plan (los de la tabla de Mission Planner)
#    (latitud, longitud, altitud_relativa_en_metros)
# ----------------------------------------------------------------------
WAYPOINTS = [
    (40.5976331, -3.9999729, 100),
    (40.5970548, -3.9991575, 100),
    (40.5969977, -4.0009117, 100),
    (40.5976087, -4.0007669, 100),
]

TAKEOFF_ALT = 30  # altitud de despegue en metros (relativa al home)

FRAME = mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT


# ----------------------------------------------------------------------
# Funciones auxiliares
# ----------------------------------------------------------------------
def set_mode(mode_name):
    """Cambia el modo de vuelo y espera a que el autopiloto lo confirme."""
    mode_id = master.mode_mapping()[mode_name]
    master.mav.set_mode_send(
        tgt_sys,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        mode_id,
    )
    while True:
        hb = master.recv_match(type='HEARTBEAT', blocking=True, timeout=5)
        if hb and hb.custom_mode == mode_id:
            print(f"Modo: {mode_name}")
            return


def upload_mission(waypoints):
    """Sube la misión siguiendo el protocolo MAVLink (count + request loop)."""
    # Construimos la lista de items:
    #   seq 0      -> placeholder de home (el autopiloto lo sustituye por el real)
    #   seq 1..N   -> tus waypoints
    #   seq N+1    -> RTL, para que vuelva y aterrice al acabar
    items = [(waypoints[0][0], waypoints[0][1], 0,
              mavutil.mavlink.MAV_CMD_NAV_WAYPOINT)]

    for lat, lon, alt in waypoints:
        items.append((lat, lon, alt, mavutil.mavlink.MAV_CMD_NAV_WAYPOINT))

    items.append((0, 0, 0, mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH))

    # Limpiamos cualquier misión anterior
    master.mav.mission_clear_all_send(tgt_sys, tgt_comp)

    # Anunciamos cuántos items vamos a enviar
    master.mav.mission_count_send(tgt_sys, tgt_comp, len(items))

    # El autopiloto nos pide cada item por orden; se lo enviamos
    for _ in range(len(items)):
        req = master.recv_match(
            type=['MISSION_REQUEST', 'MISSION_REQUEST_INT'],
            blocking=True, timeout=5)
        seq = req.seq
        lat, lon, alt, cmd = items[seq]
        master.mav.mission_item_int_send(
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
        print(f"  Item {seq} enviado")

    ack = master.recv_match(type='MISSION_ACK', blocking=True, timeout=5)
    print(f"Misión subida (ack: {ack.type})")


def wait_altitude(target_alt):
    """Espera hasta alcanzar (casi) la altitud objetivo."""
    while True:
        msg = master.recv_match(type='GLOBAL_POSITION_INT', blocking=True)
        alt = msg.relative_alt / 1000.0  # mm -> m
        print(f"  Altitud: {alt:.1f} m")
        if alt >= target_alt * 0.95:
            print("Altitud de despegue alcanzada")
            return
        time.sleep(0.5)


# ----------------------------------------------------------------------
# 3. Secuencia principal
# ----------------------------------------------------------------------
# Subir la misión
upload_mission(WAYPOINTS)

# Modo GUIDED para armar y despegar de forma controlada
set_mode('GUIDED')

# Armar motores (en SITL puede tardar unos segundos hasta fijar el GPS)
master.arducopter_arm()
master.motors_armed_wait()
print("Motores armados")

# Despegue vertical a la altitud definida
master.mav.command_long_send(
    tgt_sys, tgt_comp,
    mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0,
    0, 0, 0, 0, 0, 0, TAKEOFF_ALT)
wait_altitude(TAKEOFF_ALT)

# Modo AUTO: apuntamos al primer waypoint y arrancamos el recorrido
master.mav.mission_set_current_send(tgt_sys, tgt_comp, 1)
set_mode('AUTO')
print("Misión en marcha. Observa el dron moverse en Mission Planner.")

# Monitorizamos qué waypoint va alcanzando
while True:
    msg = master.recv_match(type='MISSION_ITEM_REACHED',
                            blocking=True, timeout=120)
    if msg is None:
        print("Sin más eventos (misión terminada o timeout).")
        break
    print(f"Waypoint {msg.seq} alcanzado")
