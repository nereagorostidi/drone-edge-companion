#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
======================================================================
 benchmark_inferencia.py
 Benchmark de INFERENCIA PURA para los runtimes de deteccion.py
======================================================================

Compara pt / onnx / onnx-int8 / ncnn / hef sobre el MISMO video, con
los MISMOS parametros, midiendo FPS, latencia, CPU, RAM y tasas de
deteccion (numero de detecciones, frames con deteccion, confianza
media). A diferencia de deteccion.py, este script NO escribe video de
salida, NO abre ventana de preview, NO envia nada por MQTT y NO emite
en RTSP: el bucle por frame se reduce a "leer frame -> inferir ->
anotar metricas", nada mas.

AISLAMIENTO POR SUBPROCESO
---------------------------
Cada runtime corre en un subproceso propio (este mismo script,
invocado con --runtime <uno> --worker). Motivo: si los 5 runtimes se
cargaran uno detras de otro en el MISMO proceso Python, la memoria
reservada por PyTorch/ONNX Runtime/HailoRT del runtime anterior podria
no liberarse limpiamente (fragmentacion del allocator, contexto de
dispositivo Hailo, cachés de ONNX Runtime...) y sesgar al alza la RAM
medida en los runtimes siguientes. Con un subproceso por runtime, cada
medicion de CPU/RAM arranca de un proceso nuevo y limpio.

SOBRE --augment
----------------
deteccion.py llama a model.predict(..., augment=args.augment), con
--augment=True por defecto para pt/onnx/onnx-int8/ncnn (Ultralytics
implementa TTA de verdad: varias pasadas por frame combinadas).
runtime_hef.HailoYolo.predict(...) IGNORA ese parametro a proposito
(se absorbe en **kwargs) — con 'hef' siempre se hace una sola pasada.
Si se compara con --augment true tal cual el comportamiento por
defecto de deteccion.py, hef arrastra una ventaja de carga de trabajo
(1 pasada) frente a los demas (varias pasadas). Por eso este script
usa augment=False para los 5 POR DEFECTO: compara con igual trabajo
por frame. Pasa --augment si quieres reproducir el comportamiento por
defecto de deteccion.py (afecta a pt/onnx/onnx-int8/ncnn; hef no
cambia).

Uso
----
    pip install psutil    # unica dependencia nueva

    # los 5 runtimes sobre el mismo video, parametros de la tabla 8.5
    python3 benchmark_inferencia.py sample.mp4 --conf 0.5 --vid-stride 6

    # solo un subconjunto
    python3 benchmark_inferencia.py sample.mp4 --runtimes pt ncnn hef

    # reproducir el comportamiento por defecto de deteccion.py (augment=True
    # en los que lo soportan)
    python3 benchmark_inferencia.py sample.mp4 --augment

Colocar este script en la MISMA carpeta que deteccion.py y
runtime_hef.py (usa weights/ y el propio runtime_hef.py desde ahi).
======================================================================
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time

try:
    import psutil
except ImportError:
    sys.exit("Falta psutil. Instala con:  pip install psutil")

try:
    import cv2
except ImportError:
    sys.exit("Falta opencv-python. Instala con:  pip install opencv-python")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

NOMBRE_PESOS = {
    'pt': 'best.pt',
    'onnx': 'best.onnx',
    'onnx-int8': 'best.int8.onnx',
    'ncnn': 'best_ncnn_model',
    'hef': 'best_hailo_model',
}
ES_CARPETA = {'pt': False, 'onnx': False, 'onnx-int8': False, 'ncnn': True, 'hef': True}
TODOS_LOS_RUNTIMES = list(NOMBRE_PESOS.keys())


# ======================================================================
#  MONITOR DE RECURSOS (CPU% y RAM del proceso actual, en un hilo aparte)
# ======================================================================
class MonitorRecursos:
    """Muestrea CPU% y RSS del proceso actual a intervalos fijos, desde que
    se llama a iniciar() hasta que se llama a detener(). Se arranca ANTES
    de cargar el modelo, para que el pico de RAM/CPU de la carga (a veces
    el mayor de toda la sesion, sobre todo en PyTorch) tambien cuente.

    CPU% sigue el convenio de psutil/top: 100% = 1 nucleo saturado, asi que
    en una Raspberry Pi de 4 nucleos el maximo teorico es 400%.
    """

    def __init__(self, intervalo=0.2):
        self.intervalo = intervalo
        self.proc = psutil.Process(os.getpid())
        self._cpu = []
        self._ram = []
        self._parar = threading.Event()
        self._hilo = None

    def _bucle(self):
        self.proc.cpu_percent(interval=None)  # arma el contador; se descarta
        while not self._parar.is_set():
            self._cpu.append(self.proc.cpu_percent(interval=None))
            self._ram.append(self.proc.memory_info().rss)
            self._parar.wait(self.intervalo)

    def iniciar(self):
        self._hilo = threading.Thread(target=self._bucle, daemon=True)
        self._hilo.start()

    def detener(self):
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(timeout=2)
        # La 1a muestra de CPU tras "armar" el contador es poco fiable.
        cpu = self._cpu[1:] or self._cpu
        ram = self._ram or [0]
        return {
            "cpu_media_pct": round(statistics.mean(cpu), 1) if cpu else 0.0,
            "cpu_pico_pct": round(max(cpu), 1) if cpu else 0.0,
            "ram_media_mb": round(statistics.mean(ram) / 1_048_576, 1),
            "ram_pico_mb": round(max(ram) / 1_048_576, 1),
        }


def info_video(path):
    """Frames totales, FPS nativos y duracion (s) del video de muestra,
    leidos directamente del contenedor (metadata), sin decodificar nada.
    Falla rapido y con un mensaje claro si el video no existe/no abre."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FileNotFoundError(f"No se pudo abrir el video: {path}")
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    cap.release()
    duracion = frames / fps if fps > 0 else 0.0
    return frames, fps, duracion


def percentil(valores, p):
    """Percentil p (0-100) por interpolacion lineal, sin numpy."""
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    k = (len(ordenados) - 1) * (p / 100)
    piso, techo = int(k), min(int(k) + 1, len(ordenados) - 1)
    if piso == techo:
        return ordenados[piso]
    return ordenados[piso] + (ordenados[techo] - ordenados[piso]) * (k - piso)


# ======================================================================
#  CARGA DE MODELO  (mismas rutas/convenciones que deteccion.py)
# ======================================================================
def cargar_modelo(runtime, weights_dir, imgsz):
    path = os.path.join(weights_dir, NOMBRE_PESOS[runtime])
    existe = os.path.isdir(path) if ES_CARPETA[runtime] else os.path.isfile(path)
    if not existe:
        raise FileNotFoundError(f"No encuentro los pesos de '{runtime}' en: {path}")
    if runtime == 'hef':
        sys.path.insert(0, BASE_DIR)  # para 'import runtime_hef' desde aqui
        from runtime_hef import HailoYolo
        return HailoYolo(path, imgsz=imgsz)
    from ultralytics import YOLO
    return YOLO(path)


# ======================================================================
#  BENCHMARK DE UN SOLO RUNTIME  (esto es lo que corre en el subproceso)
# ======================================================================
def benchmark_runtime(runtime, weights_dir, video, imgsz, conf, vid_stride, augment, warmup):
    monitor = MonitorRecursos(intervalo=0.2)
    monitor.iniciar()  # antes de cargar el modelo: la carga tambien cuenta

    modelo = cargar_modelo(runtime, weights_dir, imgsz)

    # Firma identica para los 5: Ultralytics YOLO.predict(...) la usa toda;
    # HailoYolo.predict(...) ignora a proposito imgsz/augment/verbose/classes
    # (ver runtime_hef.py), pero acepta la llamada sin romperse.
    resultados = modelo.predict(
        source=video,
        imgsz=imgsz,
        conf=conf,
        classes=[0],
        vid_stride=vid_stride,
        stream=True,
        verbose=False,
        augment=augment,
    )

    latencias_s = []
    confianzas = []
    frames_con_deteccion = 0
    t_anterior = time.perf_counter()
    n = 0

    # OJO: no se llama a r.plot() (dibujar cajas) ni se escribe nada a
    # disco/red — es justo el overhead que se quiere excluir de la medida.
    for r in resultados:
        t_ahora = time.perf_counter()
        latencias_s.append(t_ahora - t_anterior)
        t_anterior = t_ahora
        n += 1

        if r.boxes is not None and len(r.boxes) > 0:
            frames_con_deteccion += 1
            confianzas.extend(round(float(c), 2) for c in r.boxes.conf.cpu().numpy())

        print(f"\r[{runtime}] frame {n}", end="", flush=True, file=sys.stderr)
    print(file=sys.stderr)

    recursos = monitor.detener()

    # Se descartan los 'warmup' primeros frames de las metricas de latencia
    # (primer frame = calentamiento del motor), igual que hace deteccion.py.
    validas = latencias_s[warmup:] if len(latencias_s) > warmup else latencias_s
    media_s = statistics.mean(validas) if validas else 0.0
    lat_ms = [t * 1000 for t in validas]

    return {
        "runtime": runtime,
        "frames_procesados": n,
        "frames_descartados_warmup": min(warmup, len(latencias_s)),
        "tiempo_total_s": round(sum(latencias_s), 2),
        "fps_medio": round(1.0 / media_s, 2) if media_s > 0 else 0.0,
        "latencia_media_ms": round(media_s * 1000, 2),
        "latencia_p95_ms": round(percentil(lat_ms, 95), 2),
        **recursos,
        "detecciones_totales": len(confianzas),
        "frames_con_deteccion": frames_con_deteccion,
        "confianza_media": round(statistics.mean(confianzas), 2) if confianzas else 0.0,
        "params": {"imgsz": imgsz, "conf": conf, "vid_stride": vid_stride, "augment": augment},
    }


# ======================================================================
#  DRIVER: lanza un subproceso por runtime y monta la tabla final
# ======================================================================
def lanzar_subproceso(runtime, a):
    cmd = [
        sys.executable, os.path.abspath(__file__),
        a.video,
        "--runtime", runtime,
        "--worker",
        "--weights-dir", a.weights_dir,
        "--imgsz", str(a.imgsz),
        "--conf", str(a.conf),
        "--vid-stride", str(a.vid_stride),
        "--warmup", str(a.warmup),
    ]
    if a.augment:
        cmd.append("--augment")

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"\n[{runtime}] FALLO:\n{proc.stderr.strip()[-2000:]}\n")
        return None
    for linea in proc.stdout.splitlines():
        if linea.startswith("RESULTADO_JSON:"):
            return json.loads(linea[len("RESULTADO_JSON:"):])
    print(f"\n[{runtime}] el worker no devolvio RESULTADO_JSON. stdout:\n{proc.stdout[-1000:]}")
    return None


def imprimir_tabla(filas):
    cols = [
        ("runtime", "Runtime"),
        ("frames_procesados", "Frames"),
        ("tiempo_total_s", "Tiempo total(s)"),
        ("fps_medio", "FPS"),
        ("latencia_media_ms", "Lat.media(ms)"),
        ("latencia_p95_ms", "Lat.p95(ms)"),
        ("cpu_media_pct", "CPU media(%)"),
        ("cpu_pico_pct", "CPU pico(%)"),
        ("ram_pico_mb", "RAM pico(MB)"),
        ("detecciones_totales", "Deteccs."),
        ("confianza_media", "Conf.media"),
    ]
    anchos = {k: max(len(h), max((len(str(f.get(k, ''))) for f in filas), default=0)) for k, h in cols}
    cab = " | ".join(h.ljust(anchos[k]) for k, h in cols)
    print("\n" + cab)
    print("-" * len(cab))
    for f in filas:
        print(" | ".join(str(f.get(k, '')).ljust(anchos[k]) for k, h in cols))


def main():
    p = argparse.ArgumentParser(
        description="Benchmark de inferencia pura (sin MQTT/RTSP/video/preview) para deteccion.py",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("video", nargs="?", default="samples/video_ej2.mp4",
                    help="Ruta del video de muestra (el MISMO para los 5 runtimes). "
                         "Se resuelve desde el directorio ACTUAL de la terminal, no desde el script")
    p.add_argument("--runtimes", nargs="+", default=TODOS_LOS_RUNTIMES, choices=TODOS_LOS_RUNTIMES,
                    help="Runtimes a comparar")
    p.add_argument("--weights-dir", default=os.path.join(BASE_DIR, "weights"))
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--conf", type=float, default=0.5)
    p.add_argument("--vid-stride", type=int, default=1,
                    help="1 = analiza TODOS los frames (recomendado para benchmark de rendimiento: "
                         "cada frame medido es un frame realmente inferido, sin huecos). Solo subirlo "
                         "a 2+ si el video es tan largo que el runtime mas lento (normalmente PT) tarda "
                         "demasiado en completarse")
    p.add_argument("--warmup", type=int, default=1,
                    help="Frames iniciales descartados de las metricas de latencia/FPS")
    p.add_argument("--augment", action="store_true",
                    help="TTA en pt/onnx/onnx-int8/ncnn (hef lo ignora siempre). "
                         "Por defecto DESACTIVADO en los 5 para comparar con igual trabajo por frame")
    p.add_argument("--output", default="benchmark_resultados.json")
    # Flags internas del modo worker (un solo runtime, sin relanzar subproceso)
    p.add_argument("--runtime", choices=TODOS_LOS_RUNTIMES, help=argparse.SUPPRESS)
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.worker:
        try:
            resultado = benchmark_runtime(
                args.runtime, args.weights_dir, args.video, args.imgsz,
                args.conf, args.vid_stride, args.augment, args.warmup,
            )
        except Exception as e:
            print(f"ERROR en runtime '{args.runtime}': {e}", file=sys.stderr)
            sys.exit(1)
        print("RESULTADO_JSON:" + json.dumps(resultado))
        return

    frames_video, fps_video, duracion_video = info_video(args.video)
    frames_a_analizar = frames_video // args.vid_stride

    print("=" * 60)
    print(f" Video          : {args.video}")
    print(f" Frames totales : {frames_video}  ({fps_video:.2f} FPS nativos)")
    print(f" Duracion       : {duracion_video:.1f} s")
    print(f" vid-stride     : {args.vid_stride}  -> se analizaran ~{frames_a_analizar} frames")
    print(f" conf (umbral)  : {args.conf}")
    print(f" imgsz / augment: {args.imgsz} / {args.augment}")
    print(f" Runtimes       : {', '.join(args.runtimes)}")
    print("=" * 60)
    print("Cada runtime corre en su propio subproceso (aislamiento de CPU/RAM).\n")

    filas = []
    for rt in args.runtimes:
        print(f"--- {rt} ---")
        r = lanzar_subproceso(rt, args)
        if r:
            filas.append(r)
            print(f"[{rt}] {r['fps_medio']} FPS | {r['tiempo_total_s']} s total | {r['latencia_media_ms']} ms | "
                  f"CPU media {r['cpu_media_pct']}% | RAM pico {r['ram_pico_mb']} MB | "
                  f"{r['detecciones_totales']} deteccs. (conf. media {r['confianza_media']})")

    if filas:
        imprimir_tabla(filas)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(filas, f, ensure_ascii=False, indent=2)
        print(f"\nResultados guardados en {args.output}")
    else:
        print("\nNingun runtime completo el benchmark.")


if __name__ == "__main__":
    main()
