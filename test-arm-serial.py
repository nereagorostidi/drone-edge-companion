#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 test-arm-disarm.py — Prueba de armado/desarmado por TELEM3 (serie)
 Sistema SAR basado en dron — Raspberry Pi 5
=====================================================================

Conecta DIRECTAMENTE al Pixhawk por el puerto serie de TELEM3
(/dev/ttyAMA0 en esta Raspberry Pi — NO /dev/serial0, que en esta placa
apunta a un UART distinto que no está cableado a TELEM3), sin pasar por
mavlink-router ni por UDP. Es una prueba de un único proceso hablando
directo con el autopiloto, pensada para validar el ciclo completo de
armado antes de meter receptor.py/vuelo.py y mavlink-router de por medio.

Secuencia:
    1. Conecta por serie y espera el heartbeat del autopiloto.
    2. Pide confirmación explícita antes de armar (los motores pueden
       girar de verdad).
    3. Arma los motores (respetando los pre-arm checks) y confirma que
       el autopiloto lo aceptó. Si lo rechaza, muestra el motivo
       (STATUSTEXT del autopiloto, p. ej. "PreArm: GPS fix") y pregunta
       si se quiere FORZAR el armado saltándose esos chequeos —
       equivalente al botón "Force Arm" de Mission Planner, con su
       propia confirmación explícita aparte.
    4. Espera 10 segundos, mostrando una cuenta atrás.
    5. Desarma los motores y confirma que el autopiloto lo aceptó.

AVISO DE SEGURIDAD — leer antes de ejecutar:
    - Las hélices deben estar DESMONTADAS, o el dron sujeto/atado en un
      banco de pruebas, tal y como se describe en el documento del
      proyecto (sección 7, "Procedimiento de prueba y validación").
    - Si mavlink-router está corriendo, tiene el puerto serie abierto
      para él solo y este script no podrá conectar. Pararlo antes:
          sudo systemctl stop mavlink-router.service
      Y volver a arrancarlo después de la prueba:
          sudo systemctl start mavlink-router.service

Uso:
    python3 test-arm-disarm.py
    python3 test-arm-disarm.py --device /dev/ttyAMA0 --baud 57600
    python3 test-arm-disarm.py --segundos 15
    python3 test-arm-disarm.py --sin-confirmar   # NO recomendado
"""

import sys
import time
import logging
import argparse
from datetime import datetime

from pymavlink import mavutil


# =====================================================================
#  ARGUMENTOS DE LÍNEA DE COMANDOS
# =====================================================================
parser = argparse.ArgumentParser(
    description="Prueba de armado/desarmado del Pixhawk por TELEM3 (serie directo)")
parser.add_argument("--device", default="/dev/ttyAMA0",
                    help="Dispositivo serie (por defecto: /dev/ttyAMA0)")
parser.add_argument("--baud", type=int, default=57600,
                    help="Baudios (por defecto: 57600, debe coincidir con SERIALx_BAUD)")
parser.add_argument("--sysid", type=int, default=1,
                    help="SYSID propio de este proceso (por defecto: 1, igual que el vehículo)")
parser.add_argument("--compid", type=int, default=191,
                    help="COMPID propio de este proceso (por defecto: 191, MAV_COMP_ID_ONBOARD_COMPUTER)")
parser.add_argument("--segundos", type=int, default=10,
                    help="Segundos armado antes de desarmar (por defecto: 10)")
parser.add_argument("--sin-confirmar", action="store_true",
                    help="Salta la confirmación manual antes de armar (NO recomendado)")
parser.add_argument("--forzar-directo", action="store_true",
                    help="Si el ARM normal falla, fuerza directamente sin volver a preguntar "
                         "(sigue pidiendo la confirmación 'FORZAR' de seguridad)")
args = parser.parse_args()


# =====================================================================
#  LOGGING — todo por pantalla, con hora exacta de cada paso
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("test-arm-disarm")


def confirmar_seguridad():
    """Pide confirmación explícita antes de armar. No se puede saltar
    sin pasar --sin-confirmar a propósito.
    """
    log.warning("=" * 70)
    log.warning("AVISO DE SEGURIDAD: este script va a ARMAR los motores.")
    log.warning("Confirma que las hélices están DESMONTADAS, o que el dron")
    log.warning("está firmemente sujeto/atado en un banco de pruebas.")
    log.warning("=" * 70)
    respuesta = input("Escribe exactamente CONFIRMO para continuar: ").strip()
    if respuesta != "CONFIRMO":
        log.error("Confirmación no recibida ('%s'). Abortando sin tocar el autopiloto.", respuesta)
        sys.exit(1)
    log.info("Confirmación recibida. Continuando.")


def conectar(device, baud, sysid, compid):
    """Conecta por serie directo al autopiloto y espera el heartbeat."""
    log.info("Conectando a %s a %d baudios (sysid=%d, compid=%d) ...",
              device, baud, sysid, compid)
    try:
        master = mavutil.mavlink_connection(
            device,
            baud=baud,
            source_system=sysid,
            source_component=compid,
        )
    except Exception as e:
        log.error("No se ha podido abrir el puerto serie: %s", e)
        log.error("¿No tendrás mavlink-router corriendo? Tiene el puerto abierto "
                   "para él solo. Prueba: sudo systemctl stop mavlink-router.service")
        sys.exit(1)

    log.info("Puerto abierto. Esperando heartbeat del autopiloto (timeout 15s) ...")
    hb = master.wait_heartbeat(timeout=15)
    if hb is None:
        log.error("No ha llegado ningún heartbeat en 15 segundos.")
        log.error("¿No tendrás mavlink-router corriendo? Aunque el puerto se haya "
                   "podido abrir, si otro proceso ya lo tiene tomado no llegará nada "
                   "por aquí. Prueba: sudo systemctl stop mavlink-router.service")
        log.error("Si mavlink-router ya está parado, revisa cableado (TX/RX/GND) y "
                   "SERIALx_PROTOCOL/BAUD en ArduPilot.")
        sys.exit(1)

    log.info("Heartbeat recibido. Autopiloto: sistema=%d, componente=%d",
              master.target_system, master.target_component)
    log.info("Tipo de vehículo: %s | Autopiloto: %s",
              mavutil.mavlink.enums["MAV_TYPE"][hb.type].name,
              mavutil.mavlink.enums["MAV_AUTOPILOT"][hb.autopilot].name)
    return master


MAGIC_FORZAR_ARM = 21196  # Valor especial que ArduPilot exige en param2 para saltarse
                           # los pre-arm checks (el mismo "Force Arm" de Mission Planner).


def _drenar_mensajes(master, tiempo_max=1):
    """Lee todos los mensajes que lleguen durante 'tiempo_max' segundos.

    Además de refrescar el estado de armado (vía HEARTBEAT), registra en el
    log cualquier STATUSTEXT del autopiloto — es el mecanismo por el que
    ArduPilot explica POR QUÉ rechaza un armado (p. ej. "PreArm: GPS fix").
    """
    limite = time.time() + tiempo_max
    while time.time() < limite:
        msg = master.recv_match(blocking=True, timeout=max(0.0, limite - time.time()))
        if msg is None:
            break
        if msg.get_type() == "STATUSTEXT":
            log.warning("  [STATUSTEXT autopiloto] %s", msg.text)


def esperar_modo_y_estado(master, timeout=5):
    """Registra el modo de vuelo actual y el estado de armado antes de tocar nada."""
    master.recv_match(type="HEARTBEAT", blocking=True, timeout=timeout)
    modo = master.flightmode or "DESCONOCIDO"
    armado = master.motors_armed()
    log.info("Estado actual -> modo: %s | armado: %s", modo, armado)
    if armado:
        log.warning("El vehículo YA estaba armado antes de empezar la prueba.")
    return modo, armado


def armar(master, timeout=10):
    """Envía el comando de armado normal (respeta los pre-arm checks) y
    espera confirmación del autopiloto, mostrando cualquier STATUSTEXT
    que explique un posible rechazo.
    """
    log.info("Enviando comando ARM (respetando pre-arm checks) ...")
    t0 = time.time()
    master.arducopter_arm()

    log.info("Esperando confirmación de armado (timeout %ds) ...", timeout)
    limite = time.time() + timeout
    while time.time() < limite and not master.motors_armed():
        _drenar_mensajes(master, tiempo_max=1)

    if not master.motors_armed():
        log.error("El autopiloto NO ha aceptado el armado en %d segundos.", timeout)
        log.error("Si arriba no ha aparecido ningún [STATUSTEXT autopiloto] con el "
                   "motivo, revisa igualmente ARMING_CHECK y los prerrequisitos "
                   "habituales (GPS fix, calibraciones, failsafes) en Mission Planner.")
        return False

    log.info("ARMADO confirmado por el autopiloto (%.2fs).", time.time() - t0)
    return True


def armar_forzado(master, timeout=10):
    """Envía el comando de armado FORZADO, saltándose los pre-arm checks
    (equivalente al botón 'Force Arm' de Mission Planner). Usa
    MAV_CMD_COMPONENT_ARM_DISARM con el valor mágico 21196 en param2.
    """
    log.warning("Enviando comando ARM FORZADO (saltando pre-arm checks) ...")
    t0 = time.time()
    master.mav.command_long_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
        0,          # confirmation
        1,          # param1: 1 = armar
        MAGIC_FORZAR_ARM,  # param2: valor mágico para forzar
        0, 0, 0, 0, 0,
    )

    log.info("Esperando confirmación de armado forzado (timeout %ds) ...", timeout)
    limite = time.time() + timeout
    while time.time() < limite and not master.motors_armed():
        _drenar_mensajes(master, tiempo_max=1)

    if not master.motors_armed():
        log.error("El autopiloto NO ha aceptado el armado NI SIQUIERA FORZADO en "
                   "%d segundos. Esto ya no es un simple pre-arm check — revisa la "
                   "conexión, el failsafe de radio, o si hay algún bloqueo por "
                   "software (safety switch físico, si tu Pixhawk lo tiene).", timeout)
        return False

    log.info("ARMADO FORZADO confirmado por el autopiloto (%.2fs).", time.time() - t0)
    return True


def desarmar(master, timeout=10):
    """Envía el comando de desarmado y espera confirmación del autopiloto."""
    log.info("Enviando comando DISARM ...")
    t0 = time.time()
    master.arducopter_disarm()

    limite = time.time() + timeout
    while time.time() < limite and master.motors_armed():
        _drenar_mensajes(master, tiempo_max=1)

    if master.motors_armed():
        log.error("El autopiloto sigue ARMADO tras %d segundos intentando desarmar.", timeout)
        log.error("¡Atención! Desarma manualmente desde la emisora RC o Mission "
                   "Planner de inmediato.")
        return False

    log.info("DESARMADO confirmado por el autopiloto (%.2fs).", time.time() - t0)
    return True


def cuenta_atras(segundos):
    """Muestra en el log una cuenta atrás mientras el vehículo está armado."""
    log.info("Vehículo armado. Esperando %d segundos antes de desarmar ...", segundos)
    for restante in range(segundos, 0, -1):
        log.info("  ... %d s", restante)
        time.sleep(1)


def confirmar_forzar():
    """Pide una segunda confirmación, más explícita, antes de forzar el
    armado saltándose los pre-arm checks. Nunca se salta con --sin-confirmar
    (ese flag solo afecta a la primera confirmación).
    """
    log.warning("=" * 70)
    log.warning("El comando ARM normal no ha funcionado porque no cumple algún")
    log.warning("chequeo de pre-armado (ARMING_CHECK). ¿Quieres FORZAR el ARM,")
    log.warning("saltándote esos chequeos? Es lo mismo que 'Force Arm' en Mission")
    log.warning("Planner: los motores pueden arrancar aunque algo no esté bien")
    log.warning("(por ejemplo, sin fix GPS o sin calibrar).")
    log.warning("=" * 70)
    respuesta = input("Escribe exactamente FORZAR para continuar, o cualquier otra cosa para abortar: ").strip()
    return respuesta == "FORZAR"


def main():
    log.info("=" * 70)
    log.info("test-arm-disarm.py — inicio %s", datetime.now().isoformat(timespec="seconds"))
    log.info("=" * 70)

    master = conectar(args.device, args.baud, args.sysid, args.compid)
    esperar_modo_y_estado(master)

    if not args.sin_confirmar:
        confirmar_seguridad()
    else:
        log.warning("Confirmación de seguridad SALTADA (--sin-confirmar). Continuando bajo tu responsabilidad.")

    armado_ok = armar(master)
    if not armado_ok:
        if args.forzar_directo:
            proceder_forzado = True
        else:
            proceder_forzado = confirmar_forzar()

        if not proceder_forzado:
            log.error("Prueba ABORTADA: no se ha podido armar (normal) y no se ha "
                       "confirmado el armado forzado. No se intenta desarmar (no "
                       "hacía falta, nunca llegó a armar).")
            sys.exit(2)

        armado_ok = armar_forzado(master)
        if not armado_ok:
            log.error("Prueba ABORTADA: tampoco se ha podido armar de forma forzada. "
                       "No se intenta desarmar (no hacía falta, nunca llegó a armar).")
            sys.exit(2)

    try:
        cuenta_atras(args.segundos)
    except KeyboardInterrupt:
        log.warning("Interrumpido por el usuario (Ctrl+C). Desarmando inmediatamente.")

    desarmado_ok = desarmar(master)

    log.info("=" * 70)
    if armado_ok and desarmado_ok:
        log.info("PRUEBA COMPLETADA CON ÉXITO: armado y desarmado confirmados por el autopiloto.")
    else:
        log.error("PRUEBA COMPLETADA CON ERRORES. Revisa los mensajes anteriores.")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
