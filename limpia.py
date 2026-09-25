#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 Limpieza de los buffers store-and-forward (SQLite)
 Sistema SAR basado en dron — Raspberry Pi 5 (nodo edge)
=====================================================================

Cada dominio (ambiental, sistema, vuelo, deteccion) guarda sus lecturas
en un buffer SQLite local y las marca con enviado=1 cuando el broker
confirma la recepcion. Esas filas ya estan en el servidor y en el buffer
solo ocupan espacio en la tarjeta SD: este script las borra y libera el
espacio en disco.

Nunca toca las filas con enviado=0 (pendientes de enviar).

Borrar filas en SQLite NO reduce el tamano del fichero .db: las paginas
quedan libres pero reservadas. Por eso, tras borrar, se ejecuta VACUUM,
que reescribe la base de datos solo con lo que queda.

Los servicios siguen escribiendo en estos ficheros mientras se limpia, asi
que el borrado se hace por lotes con commit entre cada uno (el bloqueo de
escritura dura milisegundos, no todo el borrado) y se espera hasta 60 s a
que un servicio suelte la base de datos antes de rendirse.

Uso:
    python3 limpia.py               # limpia los cuatro buffers
    python3 limpia.py --dry-run     # solo cuenta lo que borraria, sin tocar nada

Rutas de los buffers: las mismas variables del .env que usan los
servicios (BUFFER_SENSOR, BUFFER_SISTEMA, BUFFER_VUELO, BUFFER_DB); las
rutas relativas se resuelven desde la carpeta del script. Un buffer cuyo
fichero no existe (p. ej. deteccion.db si nunca se ha usado) se salta.

Programacion semanal con cron: ver docs/servicios.md.
"""

import os
import sys
import sqlite3
import argparse
from datetime import datetime
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# dominio -> (variable del .env con la ruta, fichero por defecto)
BUFFERS = {
    "ambiental": ("BUFFER_SENSOR", "ambiental.db"),
    "sistema": ("BUFFER_SISTEMA", "sistema.db"),
    "vuelo": ("BUFFER_VUELO", "vuelo.db"),
    "deteccion": ("BUFFER_DB", "deteccion.db"),
}

LOTE_BORRADO = 5000       # filas por DELETE (un commit por lote)
ESPERA_BLOQUEO_S = 60     # espera maxima a que un servicio libere la base de datos


def log(texto):
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {texto}", flush=True)


def fmt_bytes(n):
    return f"{n / 1024 / 1024:.2f} MB" if n >= 1024 * 1024 else f"{n / 1024:.1f} KB"


def limpiar(dominio, ruta, dry_run):
    """Borra las filas enviado=1 de un buffer y compacta el fichero.

    Devuelve los bytes liberados. Lanza sqlite3.Error si algo falla.
    """
    tam_antes = os.path.getsize(ruta)
    db = sqlite3.connect(ruta, timeout=ESPERA_BLOQUEO_S)
    try:
        enviadas = db.execute("SELECT COUNT(*) FROM lecturas WHERE enviado=1").fetchone()[0]
        pendientes = db.execute("SELECT COUNT(*) FROM lecturas WHERE enviado=0").fetchone()[0]

        if dry_run:
            log(f"[{dominio}] (dry-run) se borrarian {enviadas} filas enviadas; "
                f"quedarian {pendientes} pendientes. Fichero actual: {fmt_bytes(tam_antes)}")
            return 0

        borradas = 0
        while True:
            cur = db.execute(
                "DELETE FROM lecturas WHERE id IN "
                "(SELECT id FROM lecturas WHERE enviado=1 LIMIT ?)", (LOTE_BORRADO,))
            db.commit()
            if cur.rowcount <= 0:
                break
            borradas += cur.rowcount

        if borradas:
            db.execute("VACUUM")   # libera de verdad el espacio en disco
    finally:
        db.close()

    tam_despues = os.path.getsize(ruta)
    log(f"[{dominio}] {borradas} filas enviadas borradas, {pendientes} pendientes conservadas. "
        f"{fmt_bytes(tam_antes)} -> {fmt_bytes(tam_despues)}")
    return tam_antes - tam_despues


def main():
    parser = argparse.ArgumentParser(
        description="Borra de los buffers SQLite las lecturas ya enviadas (enviado=1) y libera espacio")
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo muestra cuantas filas se borrarian, sin modificar nada")
    args = parser.parse_args()

    log("Inicio de la limpieza de buffers" + (" (dry-run)" if args.dry_run else ""))
    liberado = 0
    hubo_errores = False

    for dominio, (variable, por_defecto) in BUFFERS.items():
        ruta = os.path.join(BASE_DIR, os.getenv(variable, por_defecto))
        # Comprobar antes: sqlite3.connect() crearia un .db vacio si no existe.
        if not os.path.isfile(ruta):
            log(f"[{dominio}] {ruta} no existe; se salta.")
            continue
        try:
            liberado += limpiar(dominio, ruta, args.dry_run)
        except sqlite3.Error as e:
            hubo_errores = True
            log(f"[{dominio}] ERROR limpiando {ruta}: {e}")

    if not args.dry_run:
        log(f"Fin. Espacio liberado en total: {fmt_bytes(liberado)}")
    return 1 if hubo_errores else 0


if __name__ == "__main__":
    sys.exit(main())
