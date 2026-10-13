#!/usr/bin/env bash
# Instala el lector de placas en el volumen de datos de thor-vision, sin tocar la imagen ni arrastrar dependencias.
# (--no-deps: numpy, OpenCV con GStreamer y onnxruntime-gpu de la imagen no se reemplazan.)
set -euo pipefail
docker exec thor-vision pip install -q --no-deps --target /app/data/pylibs fast-alpr fast-plate-ocr open-image-models
docker exec thor-vision python3 -c "import sys; sys.path.append('/app/data/pylibs'); import fast_alpr; print('fast-alpr ok')"
echo "Listo. Reinicia thor-vision para que PLATES_ENABLED=true lo use (los modelos se descargan solos la primera vez, ~10 MB)."
