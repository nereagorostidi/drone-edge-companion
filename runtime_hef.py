#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 Runtime Hailo (.hef) para deteccion.py  --  YOLO de salida CRUDA
=====================================================================

Ultralytics 8.4.x trae un backend Hailo, pero SOLO sabe decodificar
HEFs exportados por el propio Ultralytics (`yolo export format=hailo`).
El .hef de este proyecto viene de otro pipeline (Hailo Model Zoo / DFC)
y saca los tensores del head YOLO sin postprocesar:

    input : input_layer1  UINT8  NHWC 640x640x3   (normalizacion on-chip)
    salida: 3 mapas de regresion  (64 canales = 4 lados x 16 bins DFL)
            3 mapas de clase        (1 canal -> 1 clase 'persona')
            en strides 8 / 16 / 32  ->  rejillas 80 / 40 / 20

Este modulo ejecuta el .hef con HailoRT (hailo_platform) y hace a mano
el postproceso YOLO (DFL -> dist2bbox -> sigmoide -> NMS). Devuelve
objetos que imitan lo justo del `Results` de Ultralytics que usa
deteccion.py:
    r.boxes         -> None-o-len y  .xywh / .conf  (con .cpu().numpy())
    r.orig_shape    -> (alto, ancho) del frame original
    r.plot()        -> frame BGR anotado

Solo funciona en la Raspberry Pi con el Hailo conectado y HailoRT
instalado. Fuera de la Pi, importar HailoYolo lanza ImportError con un
mensaje claro (deteccion.py solo lo importa con --runtime hef).
"""

from __future__ import annotations

import os
from contextlib import ExitStack

import cv2
import numpy as np


# ------------------------------------------------------------------
#  Shims: imitan lo justo de los objetos de Ultralytics
# ------------------------------------------------------------------
class _Arr:
    """Envuelve un ndarray para que .cpu().numpy() funcione igual que un tensor torch."""

    def __init__(self, a):
        self._a = np.asarray(a)

    def cpu(self):
        return self

    def numpy(self):
        return self._a

    def __len__(self):
        return len(self._a)

    def __iter__(self):
        return iter(self._a)


class _Boxes:
    def __init__(self, xywh, conf):
        self._xywh = np.asarray(xywh, dtype=np.float32).reshape(-1, 4)
        self._conf = np.asarray(conf, dtype=np.float32).reshape(-1)

    def __len__(self):
        return len(self._xywh)

    @property
    def xywh(self):
        return _Arr(self._xywh)

    @property
    def conf(self):
        return _Arr(self._conf)


class _Result:
    def __init__(self, frame_bgr, xywh, conf, names):
        self.orig_img = frame_bgr
        self.orig_shape = frame_bgr.shape[:2]          # (alto, ancho)
        self.boxes = _Boxes(xywh, conf)
        self._names = names or {0: "persona"}

    def plot(self):
        """Frame BGR con las cajas dibujadas (equivalente basico a Results.plot())."""
        img = self.orig_img.copy()
        for (cx, cy, w, h), c in zip(self.boxes.xywh.numpy(), self.boxes.conf.numpy()):
            x1, y1 = int(cx - w / 2), int(cy - h / 2)
            x2, y2 = int(cx + w / 2), int(cy + h / 2)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 2)
            etq = f"{self._names.get(0, 'obj')} {c:.2f}"
            cv2.putText(img, etq, (x1, max(12, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2, cv2.LINE_AA)
        return img


class _DummyDataset:
    """Deja que el cierre de sesion de deteccion.py
    (model.predictor.dataset.close()) libere el VideoCapture al cortar con
    stop_recording / 'q'."""

    def __init__(self):
        self.cap = None

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class _DummyPredictor:
    def __init__(self):
        self.dataset = _DummyDataset()


# ------------------------------------------------------------------
#  Utilidades de postproceso (sin torch, solo numpy + cv2)
# ------------------------------------------------------------------
def _softmax(x, axis=-1):
    x = x - x.max(axis=axis, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=axis, keepdims=True)


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _to_hwc(arr):
    """Normaliza una salida Hailo a (H, W, C). Acepta (1,H,W,C) o (1,C,H,W)."""
    a = np.asarray(arr)
    if a.ndim == 4 and a.shape[0] == 1:
        a = a[0]
    if a.ndim != 3:
        a = a.reshape(a.shape[-3], a.shape[-2], a.shape[-1])
    if a.shape[0] == a.shape[1]:          # (H, W, C)
        return a
    if a.shape[1] == a.shape[2]:          # (C, H, W)
        return np.transpose(a, (1, 2, 0))
    return a


def _letterbox(img_bgr, size=640, color=(114, 114, 114)):
    """Redimensiona manteniendo proporcion y rellena hasta size x size (centrado)."""
    h0, w0 = img_bgr.shape[:2]
    s = min(size / h0, size / w0)
    nw, nh = round(w0 * s), round(h0 * s)
    resized = cv2.resize(img_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), color, dtype=np.uint8)
    px, py = (size - nw) // 2, (size - nh) // 2
    canvas[py:py + nh, px:px + nw] = resized
    return canvas, s, px, py


def _nms(boxes_xyxy, scores, iou_thr=0.45):
    """NMS clasico (1 clase) sobre numpy. Devuelve indices a conservar."""
    x1, y1, x2, y2 = boxes_xyxy.T
    areas = (x2 - x1).clip(0) * (y2 - y1).clip(0)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = (xx2 - xx1).clip(0) * (yy2 - yy1).clip(0)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thr]
    return np.asarray(keep, dtype=int)


# ------------------------------------------------------------------
#  Modelo Hailo
# ------------------------------------------------------------------
class HailoYolo:
    """Carga un directorio *_hailo_model/ (best.hef + metadata.yaml) y expone
    .predict(...) como generador de _Result, igual que YOLO(...).predict(stream=True)."""

    def __init__(self, folder, imgsz=640, names=None):
        try:
            from hailo_platform import (
                HEF,
                ConfigureParams,
                FormatType,
                HailoStreamInterface,
                InferVStreams,
                InputVStreamParams,
                OutputVStreamParams,
                VDevice,
            )
        except ImportError as e:
            raise ImportError(
                "El runtime 'hef' necesita HailoRT (hailo_platform), que solo esta en la "
                "Raspberry Pi con el AI Kit. Fuera de la Pi usa --runtime pt/onnx/ncnn."
            ) from e

        folder = str(folder)
        hef_file = None
        for root, _dirs, files in os.walk(folder):
            for f in files:
                if f.endswith(".hef"):
                    hef_file = os.path.join(root, f)
                    break
            if hef_file:
                break
        if hef_file is None:
            raise FileNotFoundError(f"No hay ningun .hef dentro de {folder}")

        self.imgsz = int(imgsz)
        self.names = names or self._leer_names(folder)
        self.predictor = _DummyPredictor()          # deteccion.py usa model.predictor.dataset.close()
        self._proj = np.arange(16, dtype=np.float32)  # bins de la DFL

        self._stack = ExitStack()
        self.hef = HEF(hef_file)
        self._in_name = self.hef.get_input_vstream_infos()[0].name
        self._out_infos = self.hef.get_output_vstream_infos()
        target = self._stack.enter_context(VDevice())
        cfg = ConfigureParams.create_from_hef(self.hef, interface=HailoStreamInterface.PCIe)
        ng = target.configure(self.hef, cfg)[0]
        self._stack.enter_context(ng.activate(ng.create_params()))
        in_params = InputVStreamParams.make(ng, format_type=FormatType.UINT8)
        out_params = OutputVStreamParams.make(ng, format_type=FormatType.FLOAT32)  # salida ya dequantizada
        self._infer = self._stack.enter_context(InferVStreams(ng, in_params, out_params))
        print(f"Loading {hef_file} for Hailo inference (postproceso YOLO propio, runtime_hef.py)...")

    def __del__(self):
        try:
            self._stack.close()
        except Exception:
            pass

    @staticmethod
    def _leer_names(folder):
        meta = os.path.join(folder, "metadata.yaml")
        if os.path.isfile(meta):
            try:
                import yaml
                with open(meta, encoding="utf-8") as f:
                    d = yaml.safe_load(f) or {}
                n = d.get("names")
                if isinstance(n, dict):
                    return {int(k): v for k, v in n.items()}
            except Exception:
                pass
        return {0: "persona"}

    # Firma compatible con model.predict(...) de Ultralytics; los kwargs de mas
    # (imgsz, augment, verbose, classes...) se ignoran a proposito.
    def predict(self, source=None, conf=0.25, vid_stride=1, stream=True, **kwargs):
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            raise RuntimeError(f"No se pudo abrir la fuente para Hailo: {source!r}")
        self.predictor.dataset.cap = cap
        idx = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if idx % max(1, int(vid_stride)) == 0:
                    yield self._infer_frame(frame, float(conf))
                idx += 1
        finally:
            cap.release()
            self.predictor.dataset.cap = None

    def _infer_frame(self, frame_bgr, conf):
        lb, s, px, py = _letterbox(frame_bgr, self.imgsz)
        rgb = cv2.cvtColor(lb, cv2.COLOR_BGR2RGB)
        inp = np.ascontiguousarray(rgb[None], dtype=np.uint8)   # (1, 640, 640, 3)

        out = self._infer.infer({self._in_name: inp})
        maps = [_to_hwc(out[i.name]) for i in self._out_infos]
        regs = sorted((m for m in maps if m.shape[-1] == 64), key=lambda m: -m.shape[0])
        clss = sorted((m for m in maps if m.shape[-1] == 1), key=lambda m: -m.shape[0])

        vacio = (np.zeros((0, 4), np.float32), np.zeros((0,), np.float32))
        if not regs or len(regs) != len(clss):
            return _Result(frame_bgr, *vacio, self.names)

        cajas, scores = [], []
        for reg, cls in zip(regs, clss):
            g = reg.shape[0]
            stride = self.imgsz / g
            dist = (_softmax(reg.reshape(g * g, 4, 16), axis=-1) * self._proj).sum(axis=-1)  # (g*g, 4) lados en celdas
            ys, xs = np.divmod(np.arange(g * g), g)
            ax = xs.astype(np.float32) + 0.5
            ay = ys.astype(np.float32) + 0.5
            x1 = (ax - dist[:, 0]) * stride
            y1 = (ay - dist[:, 1]) * stride
            x2 = (ax + dist[:, 2]) * stride
            y2 = (ay + dist[:, 3]) * stride
            cajas.append(np.stack([x1, y1, x2, y2], axis=1))
            scores.append(_sigmoid(cls.reshape(g * g)))

        cajas = np.concatenate(cajas, axis=0)
        scores = np.concatenate(scores, axis=0)

        m = scores >= conf
        cajas, scores = cajas[m], scores[m]
        if len(cajas) == 0:
            return _Result(frame_bgr, *vacio, self.names)

        keep = _nms(cajas, scores, iou_thr=0.45)
        cajas, scores = cajas[keep], scores[keep]

        # Deshacer el letterbox -> pixeles del frame original
        h0, w0 = frame_bgr.shape[:2]
        cajas[:, [0, 2]] = ((cajas[:, [0, 2]] - px) / s).clip(0, w0)
        cajas[:, [1, 3]] = ((cajas[:, [1, 3]] - py) / s).clip(0, h0)

        cx = (cajas[:, 0] + cajas[:, 2]) / 2
        cy = (cajas[:, 1] + cajas[:, 3]) / 2
        ww = cajas[:, 2] - cajas[:, 0]
        hh = cajas[:, 3] - cajas[:, 1]
        xywh = np.stack([cx, cy, ww, hh], axis=1).astype(np.float32)
        return _Result(frame_bgr, xywh, scores.astype(np.float32), self.names)
