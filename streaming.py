#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emisor de vídeo hacia MediaMTX (RTSP vía ffmpeg) — dominio DETECCION.

Recibe fotogramas ya anotados (los que deteccion.py saca de r.plot()) y
los reenvía, en baja resolución, a un servidor MediaMTX por RTSP. Está
pensado como un EXTRA que nunca debe tumbar la detección: si ffmpeg no
arranca, se cae la red o MediaMTX no responde, el emisor se marca como
inactivo y deteccion.py sigue funcionando igual (vídeo local + alertas).
"""

import subprocess
from urllib.parse import quote
import cv2


class EmisorRTSP:
    def __init__(self, host, usuario, password, path,
                 ancho=640, alto=360, fps=12):
        # quote() escapa la contraseña por si tiene símbolos raros (@, :, /...)
        # que romperían el formato de la URL RTSP.
        self.url = (f"rtsp://{usuario}:{quote(password, safe='')}"
                    f"@{host}:8554/{path}")
        self.ancho = ancho
        self.alto = alto
        self.fps = fps
        self.proc = None
        self.activo = False

    def abrir(self):
        """Lanza el proceso ffmpeg que publica en MediaMTX."""
        cmd = [
            'ffmpeg', '-loglevel', 'error', '-y',
            # --- Entrada: frames crudos BGR desde stdin (los de OpenCV) ---
            '-f', 'rawvideo', '-pix_fmt', 'bgr24',
            '-s', f'{self.ancho}x{self.alto}',
            '-r', str(self.fps),
            '-use_wallclock_as_timestamps', '1',  # marca cada frame por hora de llegada
            '-i', '-',
            # --- Salida: H264 ligero para tiempo real ---
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-b:v', '400k', '-maxrate', '500k', '-bufsize', '800k',
            '-pix_fmt', 'yuv420p', '-g', str(self.fps * 2),
            '-f', 'rtsp', '-rtsp_transport', 'tcp',
            self.url,
        ]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            self.activo = True
            print("  Streaming iniciado hacia MediaMTX.")
        except (OSError, ValueError) as e:
            print(f"  (aviso: no se pudo iniciar el streaming: {e})")
            self.proc = None
            self.activo = False

    def enviar(self, frame):
        """Empuja un frame anotado al stream (lo redimensiona si hace falta)."""
        if not self.activo or self.proc is None:
            return
        try:
            if frame.shape[1] != self.ancho or frame.shape[0] != self.alto:
                frame = cv2.resize(frame, (self.ancho, self.alto))
            self.proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, OSError, ValueError):
            # El streaming se cayó: lo desactivamos y seguimos SIN él.
            print("  (aviso: se perdió el streaming; la detección continúa.)")
            self.activo = False

    def cerrar(self):
        """Cierra ordenadamente el proceso ffmpeg."""
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.activo = False