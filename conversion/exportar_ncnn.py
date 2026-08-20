#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 Exportar pesos YOLO de PyTorch (.pt) a NCNN
=====================================================================

Utilidad puntual para generar weights/best_ncnn_model/ a partir de
weights/best.pt (el modelo que usa deteccion.py). NCNN es un motor de
inferencia optimizado para CPUs ARM (Raspberry Pi incluida): suele ir
más rápido que ONNX Runtime en ese tipo de hardware.

A diferencia de exportar_onnx.py, esto NO genera un único fichero
sino una carpeta (model.ncnn.param + model.ncnn.bin + metadata.yaml),
que es lo que espera deteccion.py --runtime ncnn.

No hace falta ejecutar esto para que deteccion.py funcione con los
otros runtimes (pt/onnx/onnx-int8); es solo para cuando se quiera
además la versión NCNN.

Cada vez que haya un weights/best.pt nuevo (reentrenamiento), vuelve
a ejecutar este script para regenerar weights/best_ncnn_model/ a
partir de él.

Requiere el paquete 'ncnn' además de 'ultralytics' (ya en
requirements.txt):
    pip install ncnn

Uso (desde la raíz del repo):
    python3 conversion/exportar_ncnn.py                      # weights/best.pt -> weights/best_ncnn_model/
    python3 conversion/exportar_ncnn.py --pesos otro.pt       # exportar otro fichero de pesos
    python3 conversion/exportar_ncnn.py --imgsz 640           # tamaño de imagen (igual que el de entrenamiento)
    python3 conversion/exportar_ncnn.py -h                    # ayuda con los valores por defecto
"""

import argparse
import os

from ultralytics import YOLO

# weights/ vive en la raíz del repo, un nivel por encima de conversion/.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

parser = argparse.ArgumentParser(
    description='Exporta un fichero de pesos YOLO (.pt) a NCNN.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument('--pesos', default=os.path.join(BASE_DIR, 'weights', 'best.pt'),
                     help='Ruta del fichero .pt a exportar')
parser.add_argument('--imgsz', type=int, default=640,
                     help='Tamaño de imagen del export (debe coincidir con el usado en el entrenamiento)')
args = parser.parse_args()

if not os.path.isfile(args.pesos):
    raise SystemExit(f'No encuentro "{args.pesos}".')

print(f'Cargando "{args.pesos}"...')
model = YOLO(args.pesos)

print(f'Exportando a NCNN (imgsz={args.imgsz})...')
ruta_ncnn = model.export(format='ncnn', imgsz=args.imgsz)

print(f'Listo: "{ruta_ncnn}"')
