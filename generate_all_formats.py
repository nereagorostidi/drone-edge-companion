#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 generate_all_formats.py -- ONNX / ONNX-INT8 / NCNN / HEF para 'persona'
=====================================================================

Pipeline de compilacion del unico modelo de este repo: deteccion de
personas (YOLO11n) para el dron de busqueda y rescate. Parte de un
best.pt ya entrenado (por defecto weights/best.pt) y genera, dentro de
weights/, todos los formatos que sabe cargar deteccion.py:

    weights/best.onnx                 --runtime onnx
    weights/best.int8.onnx            --runtime onnx-int8
    weights/best_ncnn_model/          --runtime ncnn
    weights/best_hailo_model/best.hef --runtime hef  (+ metadata.yaml)

Solo entrena si se pide explicitamente con --fresh; por defecto
convierte/compila sin tocar los pesos.

Este script SOLO sirve para el dataset de personas (dataset/, una sola
clase 'persona'): no acepta distintos datasets ni modelos por --tag,
a proposito, para no repetir la generalidad multi-modelo que tenia la
version original de este pipeline (pensada para dos proyectos, solar y
persona, y que vivia fuera de este repo).

Requiere el Dataflow Compiler de Hailo (DFC), que SOLO existe en
x86_64 (nunca en la Raspberry Pi) y vive en su propio entorno virtual,
separado del que usa deteccion.py en produccion:

    python3.11 -m venv compiler_env
    source compiler_env/bin/activate
    pip install vendor/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl
    pip install -r requirements-compile.txt

(el wheel del DFC no se distribuye por pip: hay que descargarlo de la
Developer Zone de Hailo y colocarlo en vendor/, ver requirements-compile.txt)

Antes de sobreescribir weights/, hace una copia de seguridad completa
en weights_prev/ (se sobreescribe en cada ejecucion: es un backup de
"la version anterior", no un historial).

Uso:
    python generate_all_formats.py                        # compila desde weights/best.pt
    python generate_all_formats.py --weights otro/best.pt  # compila desde otros pesos (y los deja en weights/best.pt)
    python generate_all_formats.py --skip-hef              # solo onnx/int8/ncnn, sin compilar a Hailo (rapido, para iterar)
    python generate_all_formats.py --fresh --epochs 150    # entrena un YOLO11n nuevo sobre dataset/ en vez de partir de un .pt
    python generate_all_formats.py -h                      # ayuda con los valores por defecto
"""

import os
import sys
import glob
import json
import shutil
import time
import logging
import argparse
import platform
from datetime import datetime

import cv2
import numpy as np

# _letterbox: la MISMA funcion de preprocesado que usa runtime_hef.py en la
# Raspberry Pi para cada frame real. Es fundamental calibrar la cuantizacion
# INT8 con imagenes preprocesadas exactamente igual que en produccion (mismo
# aspect ratio conservado, mismo padding gris) -- calibrar con un resize
# simple (que deforma la imagen y no tiene barras de padding) le da al
# compilador de Hailo estadisticas de activacion que no se corresponden con
# lo que el modelo ve de verdad en vuelo, y eso degrada la cuantizacion.
from runtime_hef import _letterbox

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(BASE_DIR, "build")          # logs, calib.npy, entrenamientos --fresh (gitignored)
WEIGHTS_DIR = os.path.join(BASE_DIR, "weights")
WEIGHTS_BACKUP_DIR = os.path.join(BASE_DIR, "weights_prev")  # backup del weights/ anterior (gitignored)
DATASET_DIR = os.path.join(BASE_DIR, "dataset")
DATA_YAML = os.path.join(DATASET_DIR, "data.yaml")
VAL_DIR = os.path.join(DATASET_DIR, "valid", "images")

CANONICAL_PT = os.path.join(WEIGHTS_DIR, "best.pt")
IMG_SIZE = 640
HW_ARCH = "hailo8"
NUM_CALIB_SAMPLES_DEFAULT = 100

T_INICIO_GLOBAL = time.time()

# ==========================================
# ARGUMENTOS
# ==========================================
parser = argparse.ArgumentParser(
    description="Genera weights/best.onnx, weights/best.int8.onnx, weights/best_ncnn_model/ "
                "y weights/best_hailo_model/best.hef a partir de un best.pt, para el modelo "
                "de deteccion de personas de este repo.",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument(
    "--weights", type=str, default=CANONICAL_PT,
    help="Pesos de partida (solo lectura). Si no es ya weights/best.pt, se copia ahi "
         "para que sea el best.pt canonico que usa deteccion.py."
)
parser.add_argument("--fresh", action="store_true",
                     help="Entrena un YOLO11n nuevo desde cero sobre dataset/ en vez de partir de --weights")
parser.add_argument("--epochs", type=int, default=100, help="Epocas maximas en modo --fresh")
parser.add_argument("--patience", type=int, default=20, help="Paciencia (early stopping) en modo --fresh")
parser.add_argument("--batch", type=int, default=16, help="Tamano de batch en modo --fresh")
parser.add_argument("--calib-samples", type=int, default=NUM_CALIB_SAMPLES_DEFAULT,
                     help="Numero de imagenes de dataset/valid/images usadas para calibrar el HEF")
parser.add_argument("--skip-hef", action="store_true",
                     help="No compila el .hef (se salta el paso mas lento, ~10-20 min). "
                          "Util para iterar rapido sobre onnx/int8/ncnn.")
args = parser.parse_args()

# ==========================================
# LOGS
# ==========================================
os.makedirs(BUILD_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
TIMESTAMPED_LOG_FILE = os.path.join(BUILD_DIR, f"pipeline_{timestamp}.log")
LATEST_LOG_FILE = os.path.join(BUILD_DIR, "pipeline_latest.log")

logger = logging.getLogger("ExportPipeline")
logger.setLevel(logging.INFO)
logger.handlers.clear()
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

for path in (TIMESTAMPED_LOG_FILE, LATEST_LOG_FILE):
    fh = logging.FileHandler(path, mode="w", encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)


def get_file_size_mb(path):
    if os.path.isfile(path):
        return f"{os.path.getsize(path) / (1024 * 1024):.2f} MB"
    elif os.path.isdir(path):
        total = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs)
        return f"{total / (1024 * 1024):.2f} MB"
    return "0 MB"


def contar_imagenes(carpeta):
    if not os.path.isdir(carpeta):
        return 0
    return len(glob.glob(os.path.join(carpeta, "*.jpg"))
               + glob.glob(os.path.join(carpeta, "*.jpeg"))
               + glob.glob(os.path.join(carpeta, "*.png")))


logger.info("============================================================")
logger.info("  generate_all_formats.py -- modelo 'persona'")
logger.info("============================================================")
logger.info(f"  Fecha/hora:              {datetime.now().isoformat(timespec='seconds')}")
logger.info(f"  Python:                  {platform.python_version()}")
logger.info(f"  Modo:                    {'entrenamiento --fresh' if args.fresh else 'a partir de pesos existentes'}")
if not args.fresh:
    logger.info(f"  Pesos de partida:        {args.weights}")
else:
    logger.info(f"  epochs={args.epochs}  patience={args.patience}  batch={args.batch}")
logger.info(f"  dataset/:                {DATASET_DIR}")
logger.info(f"  weights/ (salida):       {WEIGHTS_DIR}")
logger.info(f"  Tamano de imagen:        {IMG_SIZE}x{IMG_SIZE}")
logger.info(f"  Arquitectura Hailo:      {HW_ARCH}")
logger.info(f"  Saltar compilacion HEF:  {args.skip_hef}")
logger.info("============================================================")

# ---------------------------------------------------------
# [Paso 1/6] Comprobaciones previas
# ---------------------------------------------------------
logger.info("\n--- [Paso 1/6] Comprobaciones previas ---")
problemas = []

if not os.path.isfile(DATA_YAML):
    problemas.append(f"No existe data.yaml: {DATA_YAML}")
else:
    logger.info(f"  OK  data.yaml encontrado: {DATA_YAML}")

n_calib_disponibles = contar_imagenes(VAL_DIR)
if n_calib_disponibles == 0:
    if not args.skip_hef:
        problemas.append(f"No hay imagenes .jpg/.jpeg/.png en: {VAL_DIR}")
else:
    logger.info(f"  OK  {n_calib_disponibles} imagenes disponibles para calibracion en {VAL_DIR} "
                f"(se usaran hasta {args.calib_samples})")

if args.fresh:
    logger.info("  OK  Modo --fresh: no hace falta comprobar --weights")
elif not os.path.isfile(args.weights):
    problemas.append(f"No existe el fichero de pesos: {args.weights}")
else:
    logger.info(f"  OK  Pesos de partida encontrados: {args.weights} ({get_file_size_mb(args.weights)})")

if problemas:
    logger.error("Se han encontrado problemas antes de empezar, abortando:")
    for p in problemas:
        logger.error(f"  - {p}")
    sys.exit(1)

logger.info("  Todas las comprobaciones previas OK, continuando...")

from ultralytics import YOLO
from onnxruntime.quantization import quantize_dynamic, QuantType

# ---------------------------------------------------------
# [Paso 2/6] Backup de weights/ y modelo PyTorch (.pt) canonico
# ---------------------------------------------------------
logger.info("\n--- [Paso 2/6] Backup de weights/ y preparacion del .pt ---")

os.makedirs(WEIGHTS_DIR, exist_ok=True)
if os.listdir(WEIGHTS_DIR):
    if os.path.isdir(WEIGHTS_BACKUP_DIR):
        shutil.rmtree(WEIGHTS_BACKUP_DIR)
    shutil.copytree(WEIGHTS_DIR, WEIGHTS_BACKUP_DIR)
    logger.info(f"✓ Copia de seguridad de weights/ (version anterior) en: {WEIGHTS_BACKUP_DIR}")
else:
    logger.info("  weights/ esta vacio, no hace falta backup.")

if args.fresh:
    logger.info("Modo --fresh activado: entrenando un YOLO11n nuevo desde cero sobre dataset/...")
    logger.info(f"  epochs={args.epochs}  patience={args.patience}  batch={args.batch}  imgsz={IMG_SIZE}  seed=42")

    train_project_dir = os.path.join(BUILD_DIR, "runs")
    train_run_name = f"train_{timestamp}"
    t0 = time.time()

    train_model = YOLO("yolo11n.pt")
    train_model.train(
        data=DATA_YAML,
        epochs=args.epochs,
        patience=args.patience,
        imgsz=IMG_SIZE,
        batch=args.batch,
        seed=42,
        deterministic=True,
        project=train_project_dir,
        name=train_run_name,
    )

    trained_best_path = os.path.join(train_project_dir, train_run_name, "weights", "best.pt")
    assert os.path.exists(trained_best_path), f"El entrenamiento no genero {trained_best_path}"
    logger.info(f"✓ Entrenamiento completado en {time.time() - t0:.2f}s -> {trained_best_path}")

    shutil.copy2(trained_best_path, CANONICAL_PT)
    logger.info(f"  Copiado a: {CANONICAL_PT}")

elif os.path.abspath(args.weights) != os.path.abspath(CANONICAL_PT):
    logger.info(f"Copiando {args.weights} a {CANONICAL_PT} (pasa a ser el best.pt canonico)...")
    shutil.copy2(args.weights, CANONICAL_PT)
else:
    logger.info(f"  Usando directamente {CANONICAL_PT} (ya es el best.pt canonico).")

assert os.path.exists(CANONICAL_PT), "Fallo al preparar weights/best.pt"
model = YOLO(CANONICAL_PT)
logger.info(f"✓ PyTorch listo en: {CANONICAL_PT} ({get_file_size_mb(CANONICAL_PT)})")
logger.info(f"  Clases configuradas ({len(model.names)}): {model.names}")

if len(model.names) != 1 or model.names.get(0) != "persona":
    logger.warning(
        f"  ⚠ Se esperaba un modelo de 1 sola clase 'persona' y este tiene: {model.names}. "
        "Este pipeline (nodos de salida, metadata.yaml del HEF) asume ese caso concreto."
    )

# Este pipeline asume YOLO11 (los nodos de salida que se le pasan al
# compilador de Hailo -- model.23/cv2.x/cv3.x -- son los de la cabeza de
# deteccion de YOLO11). En modo --fresh esto esta garantizado porque se
# parte de "yolo11n.pt", pero con --weights el checkpoint puede ser
# cualquier cosa: se intenta detectar la arquitectura real para avisar si
# no cuadra.
arquitectura_detectada = None
try:
    ckpt = getattr(model, "ckpt", None) or {}
    train_args = ckpt.get("train_args", {}) if isinstance(ckpt, dict) else {}
    arquitectura_detectada = train_args.get("model")
    if not arquitectura_detectada:
        yaml_info = getattr(model.model, "yaml", {}) or {}
        arquitectura_detectada = yaml_info.get("yaml_file") or yaml_info.get("scale")
except Exception as e:
    logger.warning(f"  No se pudo inspeccionar automaticamente la arquitectura del checkpoint: {e}")

if arquitectura_detectada:
    logger.info(f"  Arquitectura detectada en el checkpoint: {arquitectura_detectada}")
    if "yolo11" not in str(arquitectura_detectada).lower():
        logger.warning(
            f"  ⚠ El checkpoint no parece ser YOLO11 (se detecto: {arquitectura_detectada}). "
            "Este pipeline usa nodos de salida (model.23/cv2.x/cv3.x) especificos de la cabeza "
            "de YOLO11 -- con otra familia de modelo (p.ej. YOLOv8) la compilacion a Hailo "
            "probablemente fallara o dara un HEF incorrecto. Revisa --weights antes de continuar."
        )
else:
    logger.warning(
        "  No se ha podido confirmar automaticamente que el checkpoint sea YOLO11n -- "
        "si tienes dudas, comprueba a mano con: YOLO('weights/best.pt').model.yaml"
    )

# ---------------------------------------------------------
# [Paso 3/6] Exportacion ONNX FP32
# ---------------------------------------------------------
logger.info("\n--- [Paso 3/6] Exportando a ONNX FP32 (opset=11) ---")
t0 = time.time()
onnx_exported_path = model.export(format="onnx", imgsz=IMG_SIZE, opset=11, simplify=True, dynamic=False)

final_onnx_path = os.path.join(WEIGHTS_DIR, "best.onnx")
if os.path.abspath(onnx_exported_path) != os.path.abspath(final_onnx_path) and os.path.exists(onnx_exported_path):
    shutil.move(onnx_exported_path, final_onnx_path)

assert os.path.exists(final_onnx_path), "Fallo al exportar ONNX FP32"
logger.info(f"✓ ONNX FP32 listo: {final_onnx_path} ({get_file_size_mb(final_onnx_path)}) en {time.time() - t0:.2f}s")

# ---------------------------------------------------------
# [Paso 4/6] Cuantizacion ONNX INT8 Dinamica (generica, de referencia)
# ---------------------------------------------------------
logger.info("\n--- [Paso 4/6] Cuantizando grafo a ONNX INT8 ---")
t0 = time.time()
onnx_int8_path = os.path.join(WEIGHTS_DIR, "best.int8.onnx")
quantize_dynamic(model_input=final_onnx_path, model_output=onnx_int8_path, weight_type=QuantType.QUInt8)
assert os.path.exists(onnx_int8_path), "Fallo al generar ONNX INT8"
logger.info(f"✓ ONNX INT8 listo: {onnx_int8_path} ({get_file_size_mb(onnx_int8_path)}) en {time.time() - t0:.2f}s")

# ---------------------------------------------------------
# [Paso 5/6] Formato Edge NCNN
# ---------------------------------------------------------
logger.info("\n--- [Paso 5/6] Exportando a formato NCNN ---")
t0 = time.time()
final_ncnn_dir = os.path.join(WEIGHTS_DIR, "best_ncnn_model")
try:
    ncnn_exported_dir = model.export(format="ncnn", imgsz=IMG_SIZE)
    if os.path.abspath(ncnn_exported_dir) != os.path.abspath(final_ncnn_dir) and os.path.exists(ncnn_exported_dir):
        if os.path.exists(final_ncnn_dir):
            shutil.rmtree(final_ncnn_dir)
        shutil.move(ncnn_exported_dir, final_ncnn_dir)
    logger.info(f"✓ NCNN listo: {final_ncnn_dir} ({get_file_size_mb(final_ncnn_dir)}) en {time.time() - t0:.2f}s")
except Exception as e:
    logger.error(f"Fallo exportando NCNN (no es critico, se continua): {e}")

if args.skip_hef:
    logger.info("\n--skip-hef activado: no se compila el .hef. Terminando aqui.")
    logger.info(f"PIPELINE COMPLETADO (parcial) ✓ -- weights/ actualizado salvo best_hailo_model/")
    sys.exit(0)

# ---------------------------------------------------------
# [Paso 6/6] Calibracion y Compilacion a HEF (Hailo-8)
# ---------------------------------------------------------
logger.info("\n--- [Paso 6/6] Compilando a HEF para Hailo-8 ---")
from hailo_sdk_client import ClientRunner

calib_path = os.path.join(BUILD_DIR, "calib.npy")
hailo_dir = os.path.join(WEIGHTS_DIR, "best_hailo_model")
hef_path = os.path.join(hailo_dir, "best.hef")
os.makedirs(hailo_dir, exist_ok=True)

# 1. Dataset de calibracion -- preprocesado IDENTICO al que hace
# runtime_hef.py con cada frame real (letterbox + BGR->RGB), para que las
# estadisticas de activacion usadas al cuantizar se correspondan con lo que
# el modelo ve de verdad en produccion.
image_files = (
    glob.glob(os.path.join(VAL_DIR, "*.jpg"))
    + glob.glob(os.path.join(VAL_DIR, "*.jpeg"))
    + glob.glob(os.path.join(VAL_DIR, "*.png"))
)[:args.calib_samples]

if not image_files:
    raise RuntimeError(f"No se encontraron imagenes en: {VAL_DIR}")

logger.info(f"Procesando {len(image_files)} imagenes para el set de calibracion (letterbox {IMG_SIZE}x{IMG_SIZE})...")
t0 = time.time()
calib_imgs = []
for i, f in enumerate(image_files, start=1):
    img_bgr = cv2.imread(f)
    if img_bgr is None:
        logger.warning(f"  No se pudo leer, se omite: {f}")
        continue
    lb_bgr, _s, _px, _py = _letterbox(img_bgr, IMG_SIZE)
    lb_rgb = cv2.cvtColor(lb_bgr, cv2.COLOR_BGR2RGB)
    calib_imgs.append(lb_rgb)
    if i % 20 == 0 or i == len(image_files):
        logger.info(f"  ...{i}/{len(image_files)} imagenes de calibracion procesadas")

calib_array = np.array(calib_imgs, dtype=np.uint8)
np.save(calib_path, calib_array)
logger.info(f"✓ Dataset calibracion guardado: {calib_path} (forma: {calib_array.shape}) en {time.time() - t0:.2f}s")

# 2. Pipeline Hailo DFC
logger.info("Inicializando ClientRunner(hw_arch='hailo8')...")
runner = ClientRunner(hw_arch=HW_ARCH)

yolo_end_nodes = [
    "/model.23/cv2.0/cv2.0.2/Conv",
    "/model.23/cv3.0/cv3.0.2/Conv",
    "/model.23/cv2.1/cv2.1.2/Conv",
    "/model.23/cv3.1/cv3.1.2/Conv",
    "/model.23/cv2.2/cv2.2.2/Conv",
    "/model.23/cv3.2/cv3.2.2/Conv",
]

logger.info("-> [DFC 1/4] Traduciendo grafo ONNX con 6 ramas convolucionales...")
t0 = time.time()
runner.translate_onnx_model(final_onnx_path, start_node_names=["images"], end_node_names=yolo_end_nodes)
logger.info(f"✓ Traduccion completada en {time.time() - t0:.2f}s")

logger.info("-> [DFC 2/4] Aplicando script ALLS de optimizacion y mapeo...")
# optimization_level=0 se salta casi todas las tecnicas de cuantizacion que
# preservan precision (correccion de bias, equalizacion entre capas...): la
# rama de clasificacion queda tan mal calibrada que, tras aplicar sigmoid,
# ninguna prediccion supera nunca un umbral de confianza razonable, aunque
# el modelo en punto flotante funcione bien. optimization_level=2 es el
# nivel intermedio que recomienda Hailo para produccion.
alls_config = (
    "normalization1 = normalization([0.0, 0.0, 0.0], [255.0, 255.0, 255.0])\n"
    "model_optimization_flavor(optimization_level=2)\n"
)
logger.info(f"  ALLS:\n{alls_config}")
runner.load_model_script(alls_config)

logger.info("-> [DFC 3/4] Cuantizando pesos y activaciones a INT8 (puede tardar varios minutos)...")
t0 = time.time()
runner.optimize(calib_array)
logger.info(f"✓ Cuantizacion INT8 completada en {time.time() - t0:.2f}s")

logger.info("-> [DFC 4/4] Mapeando clusters y compilando binario HEF (puede tardar varios minutos)...")
t0 = time.time()
hef_binary = runner.compile()
with open(hef_path, "wb") as f:
    f.write(hef_binary)

assert os.path.exists(hef_path) and os.path.getsize(hef_path) > 0, "Fallo al generar archivo .hef"
logger.info(f"✓ Binario HEF listo: {hef_path} ({get_file_size_mb(hef_path)}) en {time.time() - t0:.2f}s")

# ---------------------------------------------------------
# metadata.yaml junto al .hef -- lo unico que lee runtime_hef.py de aqui
# son las 'names', para las etiquetas del video anotado.
# ---------------------------------------------------------
metadata_path = os.path.join(hailo_dir, "metadata.yaml")
names_yaml = "\n".join(f"  {idx}: {name}" for idx, name in sorted(model.names.items()))
metadata_contenido = f"""# Metadatos del modelo Hailo para deteccion.py --runtime hef
#
# Este best.hef NO lo exporta Ultralytics: viene del pipeline Hailo DFC
# (generate_all_formats.py, este mismo repo) y saca los tensores del head
# YOLO SIN postprocesar:
#   - 3 mapas de regresion  80x80x64 / 40x40x64 / 20x20x64   (64 = 4 lados x 16 bins DFL)
#   - 3 mapas de clase       80x80xN  / 40x40xN  / 20x20xN    (N = numero de clases)
#   - strides 8 / 16 / 32 ,  entrada UINT8 NHWC {IMG_SIZE}x{IMG_SIZE}x3 (normalizacion on-chip)
# Lo decodifica runtime_hef.py (DFL -> dist2bbox -> sigmoide -> NMS).
#
# De aqui solo se lee 'names' (para las etiquetas del video anotado).
# Generado automaticamente por generate_all_formats.py -- no editar a mano.
task: detect
names:
{names_yaml}
imgsz: {IMG_SIZE}
hw_arch: {HW_ARCH}
origen: compilado desde {os.path.relpath(CANONICAL_PT, BASE_DIR)}
"""
with open(metadata_path, "w", encoding="utf-8") as f:
    f.write(metadata_contenido)
logger.info(f"✓ Metadatos: {metadata_path}")

shutil.copy2(TIMESTAMPED_LOG_FILE, os.path.join(BUILD_DIR, "last_pipeline_run.log"))

# ---------------------------------------------------------
# RESUMEN FINAL
# ---------------------------------------------------------
tiempo_total = time.time() - T_INICIO_GLOBAL
logger.info("\n" + "=" * 60)
logger.info("  RESUMEN")
logger.info("============================================================")
logger.info(f"  Modo:                    {'entrenamiento --fresh (' + str(args.epochs) + ' epochs max)' if args.fresh else 'a partir de pesos existentes'}")
if not args.fresh:
    logger.info(f"  Pesos de partida:        {args.weights}")
logger.info(f"  Imagenes de calibracion: {len(calib_imgs)}")
logger.info(f"  Tiempo total:            {tiempo_total:.2f}s ({tiempo_total / 60:.1f} min)")
logger.info("")
logger.info(f"  Clases del modelo generado ({len(model.names)}):")
for idx in sorted(model.names.keys()):
    logger.info(f"    [{idx}] {model.names[idx]}")
logger.info("============================================================")
logger.info(f"  weights/ actualizado (version anterior en {WEIGHTS_BACKUP_DIR}):")
for item in sorted(os.listdir(WEIGHTS_DIR)):
    item_p = os.path.join(WEIGHTS_DIR, item)
    logger.info(f" • {item:25s} -> {get_file_size_mb(item_p):>12s}")
logger.info("============================================================")
logger.info(f"Log de esta sesion: {TIMESTAMPED_LOG_FILE}")
logger.info("PIPELINE COMPLETADO ✓ -- listo para 'git add weights/ && git commit' y desplegar en la Pi")
