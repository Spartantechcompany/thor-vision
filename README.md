# THOR Vision

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![NVIDIA](https://img.shields.io/badge/NVIDIA-Jetson%20AGX%20Thor-76b900?logo=nvidia&logoColor=white)](https://www.nvidia.com/en-us/autonomous-machines/embedded-systems/jetson-agx-thor/)
[![JetPack](https://img.shields.io/badge/JetPack-R36.4-blue)](https://developer.nvidia.com/embedded/jetpack)
[![CUDA](https://img.shields.io/badge/CUDA-12.8-76b900?logo=nvidia&logoColor=white)](https://developer.nvidia.com/cuda-toolkit)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)

Sistema de vigilancia inteligente que combina **YOLO** (personas), **InsightFace** (identidad) y un **VLM** remoto (semántica de la escena) sobre cámaras RTSP, con decodificación por hardware (GStreamer + NVDEC).

Corre sobre **NVIDIA Jetson AGX Thor** con Docker. El VLM se consume vía un gateway OpenAI-compatible. Ver [INTEGRATION.md](INTEGRATION.md) para el contrato de API y el flujo de trabajo.

---

## ¿Qué hace?

- Captura RTSP con GStreamer + NVDEC (fallback FFmpeg) y resolución nativa por cámara
- Movimiento nativo de la cámara (SUNAPI) para decidir cuándo llamar al VLM
- Pre-roll de 6 s en memoria para análisis retroactivo
- Tracker de visitas por cámara e identidad por rostro (sujetos, renombrar, fusionar)
- Análisis de escena por cámara con prompts editables (`config/scenes.yml`)
- Dashboard: grid, estado del pipeline, conexiones, personas, histórico, reportes y chat flotante
- Persistencia en SQLite con retención configurable; datos biométricos sin nombre caducan (`UNNAMED_TTL_DAYS`)

---

## Flujo

```
RTSP ─► GStreamer+NVDEC ─► FrameBuffer ─► pre-roll (JPEG 0.25 s × 6 s)
                               │
                  YOLO personas (0.5/2/4 fps según movimiento)
                               │
                  InsightFace (rostro) ─► VisitManager ─► subjects / person_visits
                               │
                  VLM (por movimiento + persona, o al cerrar la visita) ─► events
                               │
                       FastAPI :8080  (dashboard, API, WebSocket)
```

---

## Requisitos

- Jetson NVIDIA (Orin / Thor o compatible) con [JetPack 6.x](https://developer.nvidia.com/embedded/jetpack)
- [Docker](https://docs.docker.com/engine/install/) + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
- Un VLM accesible por una API compatible con OpenAI (`/v1/chat/completions`), local o vía gateway
- Cámaras IP con stream RTSP

---

## Setup

### 1. Clonar

```bash
git clone https://github.com/Oxm-Tech/thor-vision.git
cd thor-vision
```

### 2. Configurar cámaras

```bash
cp config/cameras.example.yml config/cameras.yml
nano config/cameras.yml   # agregar URLs RTSP y credenciales
```

### 3. Configurar docker-compose

```bash
cp docker-compose.example.yml docker-compose.yml
nano docker-compose.yml   # ajustar la URL del VLM (VLM_ENDPOINT)
```

### 4. Crear carpetas de datos

```bash
mkdir -p data/snapshots data/logs data/metadata
```

### 5. Levantar

```bash
make build
make up
```

Acceder en: `http://<IP_JETSON>:8080`

---

## Comandos (Makefile)

| Comando | Descripción |
|---|---|
| `make up` | Levantar en background |
| `make dev` | Levantar con logs en consola |
| `make down` | Detener |
| `make logs` | Ver logs en tiempo real |
| `make build` | Rebuildar imagen Docker |
| `make status` | Estado del servicio + health check |
| `make cameras` | Ver estado de cámaras vía API |
| `make reload` | Reiniciar sin rebuildar |
| `make clean` | Eliminar contenedor e imagen |

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `VLM_ENDPOINT` | `http://localhost:8003` | URL del VLM (antes `NEMOTRON_ENDPOINT`, aún aceptada) |
| `VLM_MODEL` | `thor-vision` | Modelo o alias a usar |
| `VLM_MIN_INTERVAL_S` | `30` | Cooldown mínimo entre análisis por cámara |
| `VLM_MAX_INTERVAL_S` | `120` | Heartbeat máximo sin movimiento |
| `VLM_MOTION_THRESHOLD` | `0.04` | Fracción de píxeles para disparar análisis (0–1) |
| `SNAPSHOT_PERIODIC_S` | `600` | Snapshot programado cada N segundos |
| `RETENTION_MAX_EVENTS` | `200000` | Máximo de eventos en SQLite |
| `RETENTION_MAX_SNAPSHOT_GB` | `50` | Máximo de GB de snapshots en disco |
| `RETENTION_MAX_AGE_DAYS` | `30` | Días máximos de retención de snapshots |
| `LOG_LEVEL` | `INFO` | Nivel de logging (`DEBUG`, `INFO`, `WARNING`) |

---

## Estructura del proyecto

```
thor-vision/
├── app/
│   ├── main.py                  ← FastAPI app + lifespan
│   ├── config.py                ← carga cameras.yml + settings.yml
│   ├── api/
│   │   ├── routes_chat.py       ← POST /api/chat (VLM)
│   │   ├── routes_history.py    ← GET /api/events, /api/snapshots
│   │   ├── routes_status.py     ← GET /api/status, /api/cameras
│   │   ├── routes_stream.py     ← GET /api/stream/{cam_id} (MJPEG)
│   │   ├── routes_vision.py     ← GET /api/detections
│   │   └── routes_ws.py         ← WS /ws/detections
│   ├── capture/
│   │   ├── manager.py           ← gestiona todos los RTSPReaders
│   │   ├── rtsp_reader.py       ← thread de captura por cámara
│   │   └── frame_buffer.py      ← buffer circular de frames
│   ├── vision/
│   │   ├── vlm_worker.py        ← disparo por movimiento/persona + análisis VLM
│   │   ├── vlm_analyzer.py      ← cliente HTTP al VLM (imagen/video + recorte)
│   │   ├── detection_store.py   ← estado en vivo de todas las cámaras
│   │   ├── face_db.py           ← base de datos de rostros conocidos
│   │   └── worker.py            ← worker genérico de visión
│   ├── storage/
│   │   ├── db.py                ← EventDB (SQLite WAL)
│   │   ├── snapshot_manager.py  ← guarda JPGs en disco
│   │   └── retention.py         ← hilo de retención automática
│   ├── pipeline/
│   │   ├── vision_processor.py  ← procesador de visión
│   │   ├── passthrough.py       ← modo passthrough (sin inferencia)
│   │   └── base_processor.py    ← clase base
│   ├── utils/
│   │   ├── logger.py            ← configuración de logging
│   │   └── gpu_probe.py         ← detecta GPU disponible
│   └── dashboard/
│       └── templates/
│           └── index.html       ← SPA del dashboard
├── config/
│   ├── cameras.example.yml      ← plantilla de cámaras
│   └── settings.example.yml     ← plantilla de settings
├── scripts/
│   └── check_cameras.sh         ← verifica conectividad de cámaras
├── Dockerfile
├── docker-compose.example.yml
├── requirements.txt
├── Makefile
└── .env.example
```

---

## Persistencia

Los datos se guardan en `./data/` (montado en `/app/data` dentro del contenedor):

```
data/
├── events.db          ← SQLite WAL (eventos del VLM + chat + index snapshots)
├── snapshots/
│   └── cam-01/
│       └── 20260605/
│           ├── 143022_alert.jpg
│           ├── 143100_periodic.jpg
│           └── 143242_people_change.jpg
├── logs/
└── metadata/
    └── faces.json     ← base de datos de rostros conocidos
```

---

## Stack tecnológico

| Componente | Tecnología | Docs |
|---|---|---|
| Framework API | [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) | REST + WebSocket |
| Visión computacional | [OpenCV](https://docs.opencv.org/) | Captura RTSP, motion detection |
| Detección de objetos | [Ultralytics YOLOv8](https://docs.ultralytics.com/) | Detección de personas |
| Reconocimiento facial | [InsightFace](https://github.com/deepinsight/insightface) + [ONNX Runtime](https://onnxruntime.ai/) | Identificación de rostros |
| VLM / Análisis semántico | cualquier modelo multimodal vía [OpenAI-compatible API](https://platform.openai.com/docs/api-reference) | Descripción de escenas |
| Persistencia | SQLite (WAL mode) | Eventos, snapshots, chat |
| Templates | [Jinja2](https://jinja.palletsprojects.com/) | Dashboard HTML |
| Configuración | [PyYAML](https://pyyaml.org/) | `cameras.yml`, `settings.yml` |
| Hardware | [NVIDIA Jetson](https://developer.nvidia.com/embedded-computing) + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) | Edge AI |

---

## Autores

| Autor | Rol |
|---|---|
| [Diego Reyes](https://github.com/Diegooxm) | Desarrollo principal, arquitectura |
| [Brayan Iván López Carlos](https://github.com/brayanlopez-oxm) | Desarrollo, integración de hardware |

**OXM Tech** — [oxmtech.com](https://oxmtech.com)
