# Contenido de esta carpeta

Ficheros generados por `generate_all_formats.py` a partir del dataset y los pesos
de `/home/andres/hailo_workspace/person`. Puede haber mas de un modelo aqui (uno
por cada `--tag` usado), cada uno con sus ficheros identificados por ese tag como
prefijo -- por ejemplo, los de la ultima ejecucion llevan el prefijo
`personas_yolo11n`.

## Lo esencial para desplegar en la Raspberry Pi

| Fichero | Que es | Hace falta en la Pi? |
|---|---|---|
| `<tag>_yolo11n.hef` | Binario compilado para el acelerador Hailo-8. Es el modelo en si, lo que ejecuta `hailort`/`hailortcli`. | Si, imprescindible |
| `<tag>_yolo11n_labels.txt` | Nombres de las clases, uno por linea, en el mismo orden que usa el modelo (indice 0, 1, 2...). | Si, para traducir el indice de clase a un nombre legible en el postprocesado |
| `<tag>_yolo11n_metadata.json` | Lo mismo que labels.txt en formato JSON, mas el tag, la ruta de los pesos de origen, el tamano de imagen (imgsz) y la arquitectura Hailo usada. | Opcional, mismo proposito que labels.txt pero mas completo |
| `<tag>_yolo11n_paquete.zip` | Zip con exactamente los tres ficheros anteriores, listo para enviar por correo o copiar a la Pi de un solo golpe. | Comodo, no imprescindible |

## Ficheros intermedios (pasos previos a la compilacion; no hacen falta en la Pi)

| Fichero | Que es |
|---|---|
| `<tag>_yolo11n_best.pt` | Copia de los pesos de PyTorch de partida (o del entrenamiento `--fresh`). Se aisla aqui para no depender de la carpeta original. |
| `<tag>_yolo11n.onnx` | Exportacion a ONNX en punto flotante (FP32); paso intermedio hacia la compilacion a Hailo. |
| `<tag>_yolo11n_int8.onnx` | Version del ONNX cuantizada a INT8 con las herramientas genericas de ONNX Runtime (no es la cuantizacion de Hailo; es solo una exportacion de referencia/comparacion). |
| `<tag>_yolo11n_calib.npy` | Array numpy con las imagenes de calibracion usadas para la cuantizacion INT8 del compilador de Hailo. Se guarda por si hace falta reproducir la compilacion; puede pesar bastante (decenas o cientos de MB). |
| `<tag>_yolo11n_pipeline_run.log` | Copia completa del log de esa ejecucion del pipeline (los mismos mensajes que salen por pantalla). |

## Carpeta `<tag>_yolo11n_ncnn/`

Es una exportacion **alternativa** del modelo a formato NCNN (otro runtime de
inferencia para dispositivos embebidos, sin relacion con Hailo). Se genera por si
algun dia hiciera falta correr el modelo en un dispositivo sin acelerador Hailo,
pero para desplegar en la Raspberry Pi con el Hailo-8 no se necesita nada de esta
carpeta.

| Fichero | Que es |
|---|---|
| `model.ncnn.param` | Definicion de la arquitectura de la red en formato NCNN (texto). |
| `model.ncnn.bin` | Pesos del modelo en formato binario NCNN. Junto al `.param`, es el equivalente NCNN del `.hef`. |
| `model_ncnn.py` | Script de ejemplo que genera Ultralytics automaticamente, con codigo para cargar y ejecutar este modelo NCNN desde Python. |
| `metadata.yaml` | Metadatos del export que usa la propia Ultralytics para reconocer este modelo NCNN si se vuelve a cargar con `YOLO(...)`. No tiene relacion con el `metadata.json` de mas arriba. |
| `__pycache__/` | Cache de bytecode de Python generada automaticamente durante la conversion. Sin ninguna utilidad, se puede borrar sin problema. |

---
*Este README se regenera automaticamente cada vez que se ejecuta el pipeline
(generate_all_formats.py), asi que sigue siendo valido aunque mas adelante se
generen mas modelos (con otros --tag) en esta misma carpeta.*
