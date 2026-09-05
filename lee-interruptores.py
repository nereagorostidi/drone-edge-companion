#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 lee-interruptores.py — Lee dos interruptores del mando por MAVLink
 Sistema SAR basado en dron — Raspberry Pi 5
=====================================================================

Conecta al autopiloto (por serie directo o por mavlink-router, igual
que test-estado.py) y entra en un bucle que, cada segundo, imprime el
estado de DOS canales RC del receptor:

    - Canal "arm"   (por defecto: canal 5 = rueda VrA)  ->  "arm arriba" / "arm abajo"
    - Canal "video" (por defecto: canal 6 = rueda VrB)  ->  "video on"   / "video off"

Pensado para usar las dos RUEDAS (VrA / VrB) del FS-i6X como si fueran
interruptores de 2 posiciones. Hay DOS umbrales con zona muerta en medio
(histeresis), asi que la rueda NO tiene que estar en el tope, basta con
acercarla a un extremo:

    pwm >= umbral-alto (1700)  ->  posicion "arriba" / "on"
    pwm <= umbral-bajo (1300)  ->  posicion "abajo"  / "off"
    entre los dos umbrales     ->  se mantiene la ultima posicion detectada
    pwm == 0 o sin datos       ->  "sin senal"
    antes del primer cruce     ->  "centro (mueve la rueda a un extremo)"

Solo lectura: NO arma, NO desarma, NO cambia parametros y NO pide
re-chequeos de pre-arm. Lo unico que hace contra el vehiculo es pedir
los streams de datos al conectar (necesario para recibir RC_CHANNELS).

-------------------------------------------------------------------
 ¿No sabes que numero de canal es cada interruptor?
-------------------------------------------------------------------
Arranca con --diagnostico: imprime los 10 primeros canales en crudo.
Mueve cada interruptor del mando y anota cual cambia. Luego fija
--canal-arm y --canal-video con esos numeros.

Uso:
    python3 lee-interruptores.py                       # serie directo (por defecto)
    python3 lee-interruptores.py --conexion router     # via mavlink-router (UDP)
    python3 lee-interruptores.py --canal-arm 5 --canal-video 6
    python3 lee-interruptores.py --diagnostico         # ver todos los canales
    (Ctrl+C para salir)
"""

import sys
import time
import logging
import argparse

from pymavlink import mavutil


# =====================================================================
#  ARGUMENTOS DE LINEA DE COMANDOS
# =====================================================================
parser = argparse.ArgumentParser(
    description="Lee dos interruptores del mando (canales RC) por MAVLink e "
                "imprime su estado en un bucle, por serie o por mavlink-router")
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
                    help="COMPID propio de este proceso. Por defecto: 195 con --conexion serial, "
                         "196 con --conexion router (para no coincidir con receptor.py=191, "
                         "vuelo.py=192 ni test-estado.py=191/194).")
parser.add_argument("--canal-arm", type=int, default=5,
                    help="Numero de canal RC del interruptor de ARMADO (por defecto: 5). "
                         "Si no lo sabes, usa --diagnostico.")
parser.add_argument("--canal-video", type=int, default=6,
                    help="Numero de canal RC del interruptor de VIDEO (por defecto: 6). "
                         "Si no lo sabes, usa --diagnostico.")
parser.add_argument("--umbral-alto", type=int, default=1700,
                    help="PWM por ENCIMA del cual la rueda cuenta como 'arriba/on' "
                         "(por defecto: 1700)")
parser.add_argument("--umbral-bajo", type=int, default=1300,
                    help="PWM por DEBAJO del cual la rueda cuenta como 'abajo/off' "
                         "(por defecto: 1300). Entre los dos umbrales se mantiene la "
                         "ultima posicion (histeresis: la rueda no tiene que llegar "
                         "al tope).")
parser.add_argument("--frecuencia", type=float, default=1.0,
                    help="Cada cuantos segundos imprimir el estado (por defecto: 1.0)")
parser.add_argument("--diagnostico", action="store_true",
                    help="En vez del estado de los dos canales, imprime los 10 primeros "
                         "canales RC en crudo mas el rango (min-max) visto en cada uno "
                         "desde que arranco — util para averiguar que numero es cada "
                         "interruptor: mueve todo y mira que canales tienen rango amplio.")
args = parser.parse_args()

if args.compid is None:
    args.compid = 196 if args.conexion == "router" else 195


# =====================================================================
#  LOGGING (solo para el arranque; el bucle usa print directo)
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("lee-interruptores")


# =====================================================================
#  CONEXION  (identico enfoque que test-estado.py)
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

    # Pide explicitamente los streams — sin esto el autopiloto puede no
    # enviar RC_CHANNELS por su cuenta.
    master.mav.request_data_stream_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_RC_CHANNELS, 5, 1)

    return master


# =====================================================================
#  LECTURA DE CANALES
# =====================================================================
def ultimo_rc_channels(master, timeout=2.0):
    """Devuelve el RC_CHANNELS mas reciente. Primero drena todo lo que
    haya en el buffer (para no ir con retraso), y si el buffer estaba
    vacio se queda esperando el siguiente hasta 'timeout' segundos."""
    msg = None
    while True:
        m = master.recv_match(type="RC_CHANNELS", blocking=False)
        if m is None:
            break
        msg = m
    if msg is None:
        msg = master.recv_match(type="RC_CHANNELS", blocking=True, timeout=timeout)
    return msg


def lista_canales(msg):
    """RC_CHANNELS trae chan1_raw .. chan18_raw en microsegundos."""
    return [getattr(msg, f"chan{i}_raw") for i in range(1, 19)]


def valor_canal(canales, n):
    if 1 <= n <= len(canales):
        return canales[n - 1]
    return None


class Rueda:
    """Traduce el PWM de una rueda/canal a 'arriba' o 'abajo' con dos
    umbrales y zona muerta en medio. En la zona muerta conserva la
    ultima posicion detectada (histeresis)."""

    def __init__(self, umbral_bajo, umbral_alto, texto_arriba, texto_abajo):
        self.umbral_bajo = umbral_bajo
        self.umbral_alto = umbral_alto
        self.texto_arriba = texto_arriba
        self.texto_abajo = texto_abajo
        self.posicion = None  # "arriba" | "abajo" | None (aun sin cruzar ningun umbral)

    def actualizar(self, pwm):
        if pwm is None or pwm == 0:
            return "sin senal"
        if pwm >= self.umbral_alto:
            self.posicion = "arriba"
        elif pwm <= self.umbral_bajo:
            self.posicion = "abajo"
        # entre los dos umbrales: no se toca self.posicion

        if self.posicion == "arriba":
            return self.texto_arriba
        if self.posicion == "abajo":
            return self.texto_abajo
        return "centro (mueve la rueda a un extremo)"


# =====================================================================
#  BUCLE PRINCIPAL
# =====================================================================
def main():
    master = conectar()
    print()  # separa el log de conexion de la primera linea de estado

    vistos_min = {}   # solo para --diagnostico: minimo visto por canal
    vistos_max = {}   # solo para --diagnostico: maximo visto por canal

    rueda_arm = Rueda(args.umbral_bajo, args.umbral_alto, "arm arriba", "arm abajo")
    rueda_video = Rueda(args.umbral_bajo, args.umbral_alto, "video on", "video off")

    if args.diagnostico:
        log.info("Modo diagnostico: mueve TODOS los sticks, switches y ruedas unos "
                 "segundos. Un canal 'vivo' tendra un rango (min-max) amplio; uno "
                 "congelado, rango 0.")

    try:
        while True:
            msg = ultimo_rc_channels(master)
            if msg is None:
                print("sin datos RC (¿receptor conectado y mando encendido?)")
                time.sleep(args.frecuencia)
                continue

            canales = lista_canales(msg)

            if args.diagnostico:
                partes = []
                for i, v in enumerate(canales[:10]):
                    n = i + 1
                    if v:  # ignora 0 (canal sin datos) para el rango
                        vistos_min[n] = min(vistos_min.get(n, v), v)
                        vistos_max[n] = max(vistos_max.get(n, v), v)
                    rango = vistos_max.get(n, 0) - vistos_min.get(n, 0)
                    marca = " *" if rango > 50 else "  "
                    partes.append(f"C{n}={v} (rango {rango}){marca}")
                print("   ".join(partes))
            else:
                arm_pwm = valor_canal(canales, args.canal_arm)
                video_pwm = valor_canal(canales, args.canal_video)
                arm_txt = rueda_arm.actualizar(arm_pwm)
                video_txt = rueda_video.actualizar(video_pwm)
                print(f"{arm_txt:<38} (C{args.canal_arm}={arm_pwm})     "
                      f"{video_txt:<38} (C{args.canal_video}={video_pwm})")

            time.sleep(args.frecuencia)

    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")


if __name__ == "__main__":
    main()
