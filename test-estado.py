#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 test-estado.py — Panel en vivo: batería, GPS y RC del Pixhawk
 Sistema SAR basado en dron — Raspberry Pi 5
=====================================================================

Conecta al autopiloto (por serie directo o por mavlink-router, igual
que test-arm-disarm.py) y muestra un panel que se refresca cada segundo
con:

    - BATERÍA: voltaje, corriente, porcentaje restante.
    - GPS: tipo de fix, satélites visibles, HDOP, posición.
    - RC: si el receptor está presente y sano, valores en crudo de los
      canales, y un aviso heurístico de si el throttle parece estar en
      una posición que bloquearía el armado (no al mínimo).
    - PRE-ARM: los mensajes STATUSTEXT reales que envía ArduPilot
      explicando qué falla para poder armar (p. ej. "PreArm: GPS: no
      fix"). El script pide activamente estos mensajes cada pocos
      segundos (MAV_CMD_RUN_PREARM_CHECKS), porque ArduPilot NO expone
      un booleano fiable de "listo para armar" en SYS_STATUS — es un
      hueco conocido y nunca implementado del firmware (ver issue
      ArduPilot/ardupilot #13534). Esta es la única fuente real de ese
      dato por MAVLink.

No modifica nada del vehículo salvo pedir el re-chequeo de pre-arm
(MAV_CMD_RUN_PREARM_CHECKS), que no arma ni cambia ningún parámetro: es
de solo lectura para todo lo demás.

AVISO sobre el chequeo de RC: es una comprobación HEURÍSTICA basada en
el valor en crudo del canal de throttle (por defecto, canal 3). No
sustituye a los pre-arm checks reales de ArduPilot — para ver el motivo
EXACTO de un rechazo de armado, usa test-arm-disarm.py, que captura los
mensajes STATUSTEXT del propio autopiloto.

Uso:
    python3 test-estado.py                    # serie directo (por defecto)
    python3 test-estado.py --conexion router   # vía mavlink-router (UDP)
    python3 test-estado.py --canal-throttle 3  # cambiar el canal si RCMAP_THROTTLE no es el 3
    (Ctrl+C para salir)
"""

import sys
import time
import shutil
import logging
import argparse

from pymavlink import mavutil


# =====================================================================
#  ARGUMENTOS DE LÍNEA DE COMANDOS
# =====================================================================
parser = argparse.ArgumentParser(
    description="Panel en vivo de batería, GPS y RC del Pixhawk, por serie o por mavlink-router")
parser.add_argument("--conexion", choices=["serial", "router"], default="serial",
                    help="'serial' (por defecto): puerto serie directo (--device/--baud). "
                         "'router': UDP contra mavlink-router (127.0.0.1:14550), sin tocar "
                         "el puerto serie — usa esto si mavlink-router ya está corriendo.")
parser.add_argument("--device", default="/dev/ttyAMA0",
                    help="Dispositivo serie, solo con --conexion serial (por defecto: /dev/ttyAMA0)")
parser.add_argument("--baud", type=int, default=57600,
                    help="Baudios, solo con --conexion serial (por defecto: 57600)")
parser.add_argument("--puerto-router", default="udpin:127.0.0.1:14550",
                    help="Endpoint UDP de mavlink-router, solo con --conexion router "
                         "(por defecto: udpin:127.0.0.1:14550)")
parser.add_argument("--sysid", type=int, default=1,
                    help="SYSID propio de este proceso (por defecto: 1, igual que el vehículo)")
parser.add_argument("--compid", type=int, default=None,
                    help="COMPID propio de este proceso. Por defecto: 191 con --conexion serial, "
                         "194 con --conexion router (para no coincidir con receptor.py=191 ni "
                         "vuelo.py=192 si están hablando con mavlink-router a la vez).")
parser.add_argument("--canal-throttle", type=int, default=3,
                    help="Número de canal RC que corresponde al throttle (por defecto: 3, "
                         "el estándar de ArduPilot; ajústalo si tu RCMAP_THROTTLE es distinto)")
parser.add_argument("--frecuencia", type=float, default=1.0,
                    help="Refresco del panel en segundos (por defecto: 1.0)")
args = parser.parse_args()

if args.compid is None:
    args.compid = 194 if args.conexion == "router" else 191


# =====================================================================
#  LOGGING (solo para errores de arranque; el panel usa print directo)
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("test-estado")


# =====================================================================
#  CONEXIÓN
# =====================================================================
def conectar():
    if args.conexion == "router":
        destino = args.puerto_router
        log.info("Conectando a mavlink-router en %s (sysid=%d, compid=%d) ...",
                  destino, args.sysid, args.compid)
        kwargs = {}
    else:
        destino = args.device
        log.info("Conectando a %s a %d baudios (sysid=%d, compid=%d) ...",
                  destino, args.baud, args.sysid, args.compid)
        kwargs = {"baud": args.baud}

    try:
        master = mavutil.mavlink_connection(
            destino, source_system=args.sysid, source_component=args.compid, **kwargs)
    except Exception as e:
        log.error("No se ha podido abrir la conexión: %s", e)
        if args.conexion == "serial":
            log.error("¿No tendrás mavlink-router corriendo? Prueba --conexion router, "
                       "o sudo systemctl stop mavlink-router.service")
        else:
            log.error("¿Está mavlink-router realmente arrancado? "
                       "sudo systemctl status mavlink-router.service")
        sys.exit(1)

    log.info("Conexión abierta. Esperando heartbeat (timeout 15s) ...")
    hb = master.wait_heartbeat(timeout=15)
    if hb is None:
        log.error("No ha llegado ningún heartbeat en 15 segundos.")
        if args.conexion == "serial":
            log.error("¿No tendrás mavlink-router corriendo y quedándose con el puerto? "
                       "Prueba --conexion router, o para mavlink-router primero.")
        else:
            log.error("¿mavlink-router está corriendo y TELEM3 recibe datos del Pixhawk? "
                       "Revisa: journalctl -u mavlink-router.service -f")
        sys.exit(1)

    log.info("Heartbeat recibido: sistema=%d, componente=%d",
              master.target_system, master.target_component)

    # Pide explícitamente todos los streams de datos a 4 Hz — sin esto,
    # el autopiloto puede no enviar por su cuenta todo lo que necesitamos.
    master.mav.request_data_stream_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL, 4, 1)

    return master


def pedir_prearm_checks(master):
    """Pide al autopiloto que vuelva a evaluar y enviar por STATUSTEXT el
    motivo de cualquier fallo de pre-arm, sin esperar a su ciclo automático
    de ~30s. ArduPilot no expone un booleano fiable de "listo para armar"
    en SYS_STATUS (bug conocido, nunca implementado) — los STATUSTEXT
    "PreArm: ..." son la única fuente real de este dato.
    """
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_RUN_PREARM_CHECKS,
        0, 0, 0, 0, 0, 0, 0, 0,
    )


# =====================================================================
#  INTERPRETACIÓN DE MENSAJES
# =====================================================================
FIX_TYPE_NOMBRES = {
    0: "SIN GPS", 1: "SIN FIX", 2: "FIX 2D", 3: "FIX 3D",
    4: "DGPS", 5: "RTK FLOAT", 6: "RTK FIJO", 7: "ESTÁTICO", 8: "PPP",
}

SENSOR_RC_RECEIVER = mavutil.mavlink.MAV_SYS_STATUS_SENSOR_RC_RECEIVER


def resumen_bateria(msgs):
    sys_status = msgs.get("SYS_STATUS")
    bat = msgs.get("BATTERY_STATUS")

    if sys_status is None and bat is None:
        return ["  (todavía sin datos de batería)"]

    lineas = []
    if sys_status is not None:
        v = sys_status.voltage_battery
        i = sys_status.current_battery
        pct = sys_status.battery_remaining
        v_txt = f"{v/1000:.2f} V" if v != 65535 else "N/D"
        i_txt = f"{i/100:.1f} A" if i != -1 else "N/D"
        pct_txt = f"{pct} %" if pct != -1 else "N/D"
        lineas.append(f"  Voltaje: {v_txt}   Corriente: {i_txt}   Restante: {pct_txt}")
        if pct != -1 and pct < 20:
            lineas.append("  ⚠ Batería por debajo del 20% — no volar.")
    return lineas


def resumen_gps(msgs):
    gps = msgs.get("GPS_RAW_INT")
    if gps is None:
        return ["  (todavía sin datos de GPS)"]

    fix_txt = FIX_TYPE_NOMBRES.get(gps.fix_type, f"desconocido ({gps.fix_type})")
    sats = gps.satellites_visible if gps.satellites_visible != 255 else "N/D"
    hdop = gps.eph / 100.0 if gps.eph != 65535 else None
    lat = gps.lat / 1e7
    lon = gps.lon / 1e7

    lineas = [f"  Fix: {fix_txt}   Satélites: {sats}" +
              (f"   HDOP: {hdop:.2f}" if hdop is not None else "")]
    if gps.fix_type >= 3:
        lineas.append(f"  Posición: {lat:.7f}, {lon:.7f}")
    else:
        lineas.append("  ⚠ Sin fix 3D todavía — ArduPilot normalmente no arma sin GPS "
                       "(salvo que ARMING_CHECK lo excluya).")
    return lineas


def resumen_rc(msgs, canal_throttle):
    sys_status = msgs.get("SYS_STATUS")
    rc = msgs.get("RC_CHANNELS")

    lineas = []

    if sys_status is not None:
        presente = bool(sys_status.onboard_control_sensors_present & SENSOR_RC_RECEIVER)
        sano = bool(sys_status.onboard_control_sensors_health & SENSOR_RC_RECEIVER)
        if not presente:
            lineas.append("  ⚠ El autopiloto no reporta ningún receptor RC presente.")
        elif not sano:
            lineas.append("  ⚠ Receptor RC presente pero marcado como NO SANO "
                           "(posible failsafe o pérdida de señal).")
        else:
            lineas.append("  Receptor RC: presente y sano.")

    if rc is None:
        lineas.append("  (todavía sin datos de canales RC)")
        return lineas

    canales = [rc.chan1_raw, rc.chan2_raw, rc.chan3_raw, rc.chan4_raw,
               rc.chan5_raw, rc.chan6_raw, rc.chan7_raw, rc.chan8_raw]
    etiquetas = ["ROLL", "PITCH", "THR", "YAW", "AUX1", "AUX2", "AUX3", "AUX4"]
    fila = "  " + "   ".join(f"{et}={val}" for et, val in zip(etiquetas, canales))
    lineas.append(fila)

    rssi = rc.rssi
    if rssi == 255:
        lineas.append("  RSSI: no reportado por este receptor/protocolo (normal en muchos "
                       "receptores PPM/SBUS sin RSSI_TYPE configurado — no es un problema si "
                       "los valores de canal de arriba cambian con la emisora).")
    elif rssi == 0:
        lineas.append("  ⚠ RSSI en 0 — si además los canales de arriba no se mueven al mover "
                       "la emisora, sí podría ser pérdida de señal real. Si los canales cambian "
                       "con normalidad, probablemente sea el mismo caso de \"no reportado\".")
    else:
        lineas.append(f"  RSSI: {rssi}")

    idx = canal_throttle - 1
    if 0 <= idx < len(canales):
        val_thr = canales[idx]
        if val_thr == 0:
            lineas.append(f"  ⚠ Canal {canal_throttle} (throttle) en 0 — no hay señal RC "
                           "real en ese canal, revisa --canal-throttle.")
        elif val_thr > 1200:
            lineas.append(f"  ⚠ Throttle (canal {canal_throttle}) = {val_thr}, no parece "
                           "estar al mínimo — esto suele BLOQUEAR el armado en ArduPilot. "
                           "Baja el stick de gas del todo.")
        else:
            lineas.append(f"  Throttle (canal {canal_throttle}) = {val_thr} — parece al "
                           "mínimo, correcto para armar.")

    return lineas


def resumen_general(master, msgs):
    modo = master.flightmode or "DESCONOCIDO"
    armado = master.motors_armed()
    lineas = [f"  Modo: {modo}   Armado: {'SÍ' if armado else 'no'}"]
    return lineas


# =====================================================================
#  BUCLE PRINCIPAL — panel que se refresca
# =====================================================================
def limpiar_pantalla():
    print("\033[H\033[J", end="")


def main():
    master = conectar()
    print()  # deja el log de conexión visible antes del primer refresco

    prearm_msgs = []       # últimos STATUSTEXT relacionados con pre-arm
    ultima_peticion = 0.0
    INTERVALO_PETICION = 6.0  # cada cuántos segundos forzar un re-chequeo

    try:
        while True:
            ahora = time.time()
            if ahora - ultima_peticion >= INTERVALO_PETICION:
                pedir_prearm_checks(master)
                ultima_peticion = ahora

            # Drena todo lo recibido en esta vuelta, sin bloquear, y de paso
            # captura cualquier STATUSTEXT que llegue (no queda en .messages,
            # solo se ve el último de cada tipo, así que hay que cogerlo aquí).
            while True:
                msg = master.recv_match(blocking=False)
                if msg is None:
                    break
                if msg.get_type() == "STATUSTEXT":
                    texto = msg.text.strip()
                    if texto and (not prearm_msgs or prearm_msgs[-1] != texto):
                        prearm_msgs.append(texto)
                        prearm_msgs[:] = prearm_msgs[-5:]  # solo los 5 últimos

            msgs = master.messages

            ancho = shutil.get_terminal_size((80, 24)).columns
            limpiar_pantalla()
            print("=" * ancho)
            print(" ESTADO DEL PIXHAWK — Ctrl+C para salir".ljust(ancho))
            print("=" * ancho)

            print("\nGENERAL")
            for l in resumen_general(master, msgs):
                print(l)

            print("\nBATERÍA")
            for l in resumen_bateria(msgs):
                print(l)

            print("\nGPS")
            for l in resumen_gps(msgs):
                print(l)

            print("\nRC")
            for l in resumen_rc(msgs, args.canal_throttle):
                print(l)

            print("\nPRE-ARM (mensajes del autopiloto)")
            if prearm_msgs:
                for texto in prearm_msgs:
                    print(f"  {texto}")
            else:
                print("  (sin mensajes todavía — si no aparece nada en unos segundos, "
                      "puede que ya esté listo para armar, o que no reciba STATUSTEXT)")

            print("\n" + "=" * ancho)
            time.sleep(args.frecuencia)

    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")


if __name__ == "__main__":
    main()
