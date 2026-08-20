#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 Exportar pesos YOLO de PyTorch (.pt) a ONNX
=====================================================================

Utilidad puntual para generar weights/best.onnx a partir de
weights/best.pt (el modelo que usa deteccion.py). No hace falta
ejecutar esto para que deteccion.py funcione (usa el .pt); es solo
para cuando se quiera tener también la versión .onnx (más ligera y
rápida de cargar, pensada para despliegue).

Cada vez que haya un weights/best.pt nuevo (reentrenamiento), vuelve
a ejecutar este script para regenerar weights/best.onnx a partir de él.

Requiere el paquete 'onnx' además de 'ultralytics' (ya en
requirements.txt):
    pip install onnx

Uso:
    python3 exportar_onnx.py                      # weights/best.pt -> weights/best.onnx
    python3 exportar_onnx.py --pesos otro.pt       # exportar otro fichero de pesos
    python3 exportar_onnx.py --imgsz 640           # tamaño de imagen (igual que el de entrenamiento)
    python3 exportar_onnx.py -h                    # ayuda con los valores por defecto
"""

import argparse
import os

from ultralytics import YOLO

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

parser = argparse.ArgumentParser(
    description='Exporta un fichero de pesos YOLO (.pt) a ONNX.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument('--pesos', default=os.path.join(BASE_DIR, 'weights', 'best.pt'),
                     help='Ruta del fichero .pt a exportar')
parser.add_argument('--imgsz', type=int, default=640,
                     help='Tamaño de imagen del export (debe coincidir con el usado en el entrenamiento)')
parser.add_argument('--opset', type=int, default=12,
                     help='Version del opset de ONNX')
args = parser.parse_args()

if not os.path.isfile(args.pesos):
    raise SystemExit(f'No encuentro "{args.pesos}".')

print(f'Cargando "{args.pesos}"...')
model = YOLO(args.pesos)

print(f'Exportando a ONNX (imgsz={args.imgsz}, opset={args.opset})...')
ruta_onnx = model.export(format='onnx', imgsz=args.imgsz, opset=args.opset)

print(f'Listo: "{ruta_onnx}"')
