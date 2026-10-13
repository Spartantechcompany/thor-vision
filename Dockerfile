FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev python3-venv \
    build-essential g++ \
    libglib2.0-0 libsm6 libxext6 libxrender-dev libgomp1 libgl1 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libtesseract5 libwebpdemux2 libtbb12 \
    # ^ libs que opencv-contrib-python-rolling (mas abajo) necesita al importar
    # y que ubuntu:24.04 no trae por default (confirmado con ldd sobre el .so
    # real, no supuesto): tesseract (modulo text de contrib), webp demux, TBB.
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt --extra-index-url https://pypi.jetson-ai-lab.io/sbsa/cu130

# opencv-python-headless (PyPI) no trae GStreamer compilado — este wheel del
# mismo indice que ya usa onnxruntime-gpu si lo trae (GStreamer+NVDEC+cuDNN).
# Paso separado + --force-reinstall --no-deps: ultralytics/insightface jalan
# su propio opencv-python transitivo durante el pip install de arriba, y si
# no se reinstala despues, ese wheel generico pisa este en silencio (mismo
# namespace cv2/, sin ningun error).
RUN pip install --force-reinstall --no-deps \
    opencv-contrib-python-rolling==4.13.0 \
    --extra-index-url https://pypi.jetson-ai-lab.io/sbsa/cu130
# NO hacer `pip uninstall opencv-python` despues de esto: su RECORD lista los
# mismos archivos cv2/ que --force-reinstall acaba de escribir (mismo
# namespace de paquete) — desinstalarlo borraria el binario bueno que
# acabamos de instalar. Queda un residuo cosmetico en `pip show opencv-python`
# (parece instalado aunque ya no manda) — inofensivo, se documenta y ya.

# El wheel de arriba necesita libcudnn.so.9 en tiempo de import; ya se instala
# transitivamente (torch/onnxruntime-gpu -> nvidia-cudnn-cu13), solo falta
# apuntar LD_LIBRARY_PATH a su ruta dentro del venv.
ENV LD_LIBRARY_PATH=/opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:${LD_LIBRARY_PATH}

COPY app/ ./app/
COPY config/ ./config/

RUN mkdir -p /app/data/snapshots /app/data/logs /app/data/metadata /app/data/models

# Pre-download YOLOv8n weights (~6 MB)
RUN python3 -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" 2>/dev/null || true

# Pre-download InsightFace buffalo_sc models (~100 MB) into /app/data/models
ENV INSIGHTFACE_HOME=/app/data/models
RUN python3 -c "\
from insightface.app import FaceAnalysis; \
app = FaceAnalysis(name='buffalo_sc', providers=['CPUExecutionProvider']); \
app.prepare(ctx_id=0, det_size=(320,320))" 2>/dev/null || true

ENV CAMERAS_CONFIG=/app/config/cameras.yml
ENV LOG_LEVEL=INFO

EXPOSE 8080

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
