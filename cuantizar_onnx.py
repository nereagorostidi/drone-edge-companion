#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 Cuantizar un modelo ONNX (FP32 -> INT8, cuantizacion dinamica)
=====================================================================

Utilidad puntual para reducir el tamaño y acelerar la inferencia de
weights/best.onnx en CPU (pensado para la Raspberry Pi). Usa
cuantizacion dinamica (onnxruntime.quantization.quantize_dynamic): no
necesita imagenes de calibracion, a cambio de una precision algo
menos ajustada que la cuantizacion estatica.

No hace falta ejecutar esto para que deteccion.py --runtime onnx
funcione (usa weights/best.onnx tal cual); es solo para cuando se
quiera además una versión cuantizada más ligera.

Cada vez que se regenere weights/best.onnx (con exportar_onnx.py tras
un reentrenamiento), vuelve a ejecutar este script para cuantizar la
version nueva.

Uso:
    python3 cuantizar_onnx.py                        # weights/best.onnx -> weights/best.int8.onnx
    python3 cuantizar_onnx.py --entrada otro.onnx     # cuantizar otro fichero
    python3 cuantizar_onnx.py -h                      # ayuda con los valores por defecto
"""

import argparse
import os
import tempfile

from onnxruntime.quantization import QuantType, quantize_dynamic
from onnxruntime.quantization.shape_inference import quant_pre_process

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

parser = argparse.ArgumentParser(
    description='Cuantiza (FP32 -> INT8, dinamica) un modelo ONNX.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument('--entrada', default=os.path.join(BASE_DIR, 'weights', 'best.onnx'),
                     help='Ruta del .onnx a cuantizar')
parser.add_argument('--salida', default=None,
                     help='Ruta del .onnx cuantizado (por defecto, junto a la entrada con sufijo ".int8")')
args = parser.parse_args()

if not os.path.isfile(args.entrada):
    raise SystemExit(f'No encuentro "{args.entrada}".')

salida = args.salida
if salida is None:
    raiz, _ext = os.path.splitext(args.entrada)
    salida = f'{raiz}.int8.onnx'

with tempfile.TemporaryDirectory() as tmp:
    preprocesado = os.path.join(tmp, 'preprocesado.onnx')
    print(f'Preprocesando "{args.entrada}" (inferencia de formas, recomendado antes de cuantizar)...')
    quant_pre_process(args.entrada, preprocesado)

    print(f'Cuantizando (dinamica, INT8)...')
    quantize_dynamic(preprocesado, salida, weight_type=QuantType.QUInt8)

peso_original = os.path.getsize(args.entrada) / (1024 * 1024)
peso_cuantizado = os.path.getsize(salida) / (1024 * 1024)
print(f'Listo: "{salida}"')
print(f'  {peso_original:.1f} MB -> {peso_cuantizado:.1f} MB')
