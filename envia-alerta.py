#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 envia-alerta.py — Envia un mensaje de alerta (STATUSTEXT) a la GCS
 Sistema SAR basado en dron — Raspberry Pi 5
=====================================================================

Conecta al autopiloto (por serie directo o por mavlink-router, igual
que test-estado.py y lee-interruptores.py) y envia por MAVLink un
mensaje STATUSTEXT. Ese mensaje aparece en el panel de mensajes de
cualquier estacion de tierra conectada (Mission Planner, QGroundControl,
mavproxy...). NO llega al mando RC (el FS-i6X no muestra texto) — esto
es a proposito: la alerta va SOLO a la GCS.

Por defecto el texto es "DEFECTO ENCONTRADO" y la severidad ALERT.

El mensaje se envia varias veces seguidas (--repetir) porque MAVLink no
garantiza la entrega: un unico STATUSTEXT se puede perder por el enlace.

Solo hace eso: conectar, esperar heartbeat y emitir el STATUSTEXT. No
arma, no cambia parametros, no pide streams ni re-chequeos.

Uso:
    python3 envia-alerta.py                              # serie directo, texto por defecto
    python3 envia-alerta.py --conexion router            # via mavlink-router (UDP)
    python3 envia-alerta.py --texto "OTRA COSA"          # texto personalizado
    python3 envia-alerta.py --severidad critical         # otra severidad
    python3 envia-alerta.py --repetir 5 --intervalo 0.5  # 5 envios cada 0.5 s
"""

import sys
import time
import logging
import argparse

from pymavlink import mavutil


# =====================================================================
#  ARGUMENTOS DE LINEA DE COMANDOS
# =====================================================================
SEVERIDADES = {
    "emergency": mavutil.mavlink.MAV_SEVERITY_EMERGENCY,
    "alert":     mavutil.mavlink.MAV_SEVERITY_ALERT,
    "critical":  mavutil.mavlink.MAV_SEVERITY_CRITICAL,
    "error":     mavutil.mavlink.MAV_SEVERITY_ERROR,
    "warning":   mavutil.mavlink.MAV_SEVERITY_WARNING,
    "notice":    mavutil.mavlink.MAV_SEVERITY_NOTICE,
    "info":      mavutil.mavlink.MAV_SEVERITY_INFO,
    "debug":     mavutil.mavlink.MAV_SEVERITY_DEBUG,
}

parser = argparse.ArgumentParser(
    description="Envia un STATUSTEXT de alerta a la GCS por MAVLink "
                "(serie directo o mavlink-router). No llega al mando RC.")
parser.add_argument("--texto", default="DEFECTO ENCONTRADO",
                    help='Texto de la alerta (por defecto: "DEFECTO ENCONTRADO"). '
                         "MAVLink lo recorta a 50 caracteres.")
parser.add_argument("--severidad", choices=list(SEVERIDADES), default="alert",
                    help="Severidad MAVLink del mensaje (por defecto: alert). "
                         "alert/critical/warning suelen resaltarse en rojo/amarillo "
                         "en la GCS.")
parser.add_argument("--repetir", type=int, default=3,
                    help="Cuantas veces enviar el mensaje seguido (por defecto: 3). "
                         "MAVLink no garantiza la entrega; repetir sube la probabilidad "
                         "de que llegue.")
parser.add_argument("--intervalo", type=float, default=1.0,
                    help="Segundos entre repeticiones (por defecto: 1.0)")

parser.add_argument("--conexion", choices=["serial", "router"], default="serial",
                    help="'serial' (por defecto): puerto serie directo (--device/--baud). "
                         "'router': UDP contra mavlink-router (127.0.0.1:14550), sin tocar "
                         "el puerto serie — usa esto si mavlink-router ya esta corriendo.")
parser.add_argument("--device", default="/dev/ttyAMA0",
                    help="Dispositivo serie, solo con --conexion serial (por defecto: /dev/ttyAMA0)")
parser.add_argument("--baud", type=int, default=57600,
                    help="Baudios, solo con --conexion serial (por defecto: 57600)")
parser.add_argument("--puerto-router", default="udpin:127.0.0.1:14550",
                    help="Endpoint UDP de mavlink-router, solo con --conexion router "
                         "(por defecto: udpin:127.0.0.1:14550)")
parser.add_argument("--sysid", type=int, default=1,
                    help="SYSID propio de este proceso (por defecto: 1, igual que el vehiculo)")
parser.add_argument("--compid", type=int, default=None,
                    help="COMPID propio de este proceso. Por defecto: 197 con --conexion "
                         "serial, 198 con --conexion router (para no chocar con receptor.py, "
                         "vuelo.py, test-estado.py ni lee-interruptores.py).")
parser.add_argument("--sin-heartbeat", action="store_true",
                    help="No esperar heartbeat antes de enviar (mas rapido, pero no "
                         "confirma que el enlace este vivo).")
args = parser.parse_args()

if args.compid is None:
    args.compid = 198 if args.conexion == "router" else 197


# =====================================================================
#  LOGGING
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("envia-alerta")


# =====================================================================
#  CONEXION  (mismo enfoque que test-estado.py, sin pedir streams)
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
        log.error("No se ha podido abrir la conexion: %s", e)
        if args.conexion == "serial":
            log.error("¿No tendras mavlink-router corriendo? Prueba --conexion router, "
                      "o sudo systemctl stop mavlink-router.service")
        else:
            log.error("¿Esta mavlink-router realmente arrancado? "
                      "sudo systemctl status mavlink-router.service")
        sys.exit(1)

    if args.sin_heartbeat:
        # Sin heartbeat no conocemos el target; usamos el SYSID configurado
        # y componente 1 (autopiloto). STATUSTEXT es de emision, no necesita
        # un target concreto para que la GCS lo vea.
        master.target_system = args.sysid
        master.target_component = 1
        return master

    log.info("Conexion abierta. Esperando heartbeat (timeout 15s) ...")
    hb = master.wait_heartbeat(timeout=15)
    if hb is None:
        log.error("No ha llegado ningun heartbeat en 15 segundos.")
        if args.conexion == "serial":
            log.error("¿No tendras mavlink-router corriendo y quedandose con el puerto? "
                      "Prueba --conexion router, o para mavlink-router primero.")
        else:
            log.error("¿mavlink-router esta corriendo y recibe datos del Pixhawk? "
                      "Revisa: journalctl -u mavlink-router.service -f")
        sys.exit(1)

    log.info("Heartbeat recibido: sistema=%d, componente=%d",
             master.target_system, master.target_component)
    return master


# =====================================================================
#  ENVIO
# =====================================================================
def enviar_alerta(master, texto, severidad):
    # STATUSTEXT.text son 50 bytes como maximo.
    datos = texto.encode("utf-8")[:50]
    master.mav.statustext_send(severidad, datos)


def main():
    master = conectar()

    severidad = SEVERIDADES[args.severidad]
    texto = args.texto

    log.info('Enviando alerta: "%s"  (severidad=%s, %d envio(s))',
             texto, args.severidad, args.repetir)

    for i in range(args.repetir):
        enviar_alerta(master, texto, severidad)
        log.info("  envio %d/%d hecho", i + 1, args.repetir)
        if i < args.repetir - 1:
            time.sleep(args.intervalo)

    # Da un instante a la capa de transporte para vaciar el buffer antes de salir.
    time.sleep(0.3)
    log.info("Listo. Revisa el panel de mensajes de tu GCS.")


if __name__ == "__main__":
    main()
