# Contrato de integración y flujo de trabajo

Documento vivo para quien desarrolla sobre THOR Vision (Thor en producción, forks y ramas en GitHub).

## Reglas de trabajo

- `main` es lo que corre en producción. Entra solo por PR con una aprobación.
- Una rama por cambio: `feat/<tema>`, `fix/<tema>`. Commits pequeños, un tema por commit.
- CI (`.github/workflows/ci.yml`): compila, `ruff` (imports faltantes y nombres indefinidos), valida las plantillas y busca secretos con gitleaks.
- Nunca al repo: `.env`, `config/cameras.yml` (trae credenciales RTSP), `data/`, `docker-compose.yml` real, ni datos biométricos (caras, cuerpos, embeddings).
- Despliegue: en Thor, `git pull` y `docker restart thor-vision` (`./app` está montado de solo lectura). Un cambio en `docker-compose.yml` requiere `docker compose up -d`.

## Superficie estable (no romper sin avisar)

**API HTTP** (FastAPI, puerto 8080):

| Área | Endpoints |
|---|---|
| Estado | `/api/health`, `/api/cameras`, `/api/pipeline`, `/api/connections`, `/api/system`, `/api/vlm` |
| Eventos | `/api/events`, `/api/events/aggregate`, `PATCH /api/events/{id}/review` |
| Personas | `/api/subjects` (+`/stats`, `PATCH`, `/merge`, `DELETE`, `/face`, `/body`), `/api/person-visits` (+`/face`, `/body`) |
| Rostros | `/api/faces`, `/api/face-sightings` (legado) |
| Imágenes | `/api/snapshot/{cam}`, `/api/snapshots`, `/stream/{cam}` |
| Chat y reportes | `/api/chat`, `/api/reports`, `POST /api/reports/generate` |
| Tiempo real | `WS /ws/events` |

`/api/visits` son los accesos al dashboard, no las visitas de personas (esas son `/api/person-visits`).

**Base de datos** (`data/events.db`, SQLite): `events`, `snapshots`, `subjects`, `person_visits`, `face_sightings`, `reports`, `chat_messages`, `dashboard_visits`. Los cambios de esquema se hacen con `ALTER TABLE` idempotente; nada de borrar columnas.

**Evento del VLM** (`events.type = "nemotron"`, `schema: 2`): `people, persons[], vehicles, activity, scene, relevant, alerts, alert_types, severity, confidence, yolo_people, people_mismatch`. El tipo `"nemotron"`, la clave JSON `nemotron` de `/api/detections` y el directorio `data/debug/nemotron/` se conservan por compatibilidad con datos ya guardados; renombrarlos requiere una migración.

**Variables de entorno** (ver `docker-compose.example.yml`): `VLM_ENDPOINT/MODEL/API_KEY` (los `NEMOTRON_*` antiguos siguen funcionando como respaldo), `NATIVE_MOTION_ENABLED`, `PREROLL_ENABLED`, `FACE_*`, `UNNAMED_TTL_DAYS`, `RETENTION_*`, `UMAMI_*`.

## Cómo añadir una integración

1. Lee de la API; si necesitas un dato nuevo, añade un endpoint `GET` en `app/api/` en vez de leer la base directamente.
2. Si necesitas escribir en la base, agrega una tabla nueva (no modifiques las existentes) y documéntala aquí.
3. Abre un PR que actualice este archivo si cambia el contrato.

## Pendiente de diseño

- Correlación de identidades sin rostro (ReID de cuerpo + mapa de cámaras): ver el issue correspondiente.

## Ampliación del contrato (v2.3.0)

**Endpoints nuevos**

| Área | Endpoints |
|---|---|
| Versión | `GET /api/version`, `GET /api/releases` |
| Personas | `GET /api/people?category=`, `PATCH /api/people/{id}/category`, `POST /api/people/bulk-category`, `GET /api/people/{id}/similar`, `GET /api/people/duplicates`, `GET /api/people/overview` |
| Asistencia | `GET /api/attendance?date=`, `GET /api/attendance.csv?date=` |
| Vehículos | `GET /api/vehicles`, `GET /api/vehicles/{id}/image`, `PATCH /api/vehicles/{id}` (placa y nota) |
| Mascotas | `GET /api/pets/stats`, `GET /api/pets/crops`, `POST /api/pets/crops/{id}/label`, `POST /api/pets/suggest`, `GET /api/pets/model` |
| Chat | `POST /api/chat/stream` (SSE), `POST /api/chat/feedback` |
| Línea de tiempo | `GET /api/timeline` |
| Búsqueda del Vision Agent | `GET /searxng/search` (formato compatible con SearXNG, usado por Morphic) |

**Tablas nuevas o ampliadas:** `subjects.category`, `parked_vehicles`, `pet_crops` (con `pred_label` y `pred_conf`), `chat_feedback`.

**Variables nuevas:** `YOLO_OBJECTS_ENABLED`, `YOLO_OBJECT_CONF`, `PARKED_CAMS`, `PARKED_MIN_S`, `PARKED_GONE_S`, `PET_COLLECT`, `PET_MAX_TOTAL`, `GUEST_TTL_DAYS`, `CHAT_MODEL`, `CHAT_API_KEY`, `REPORT_MODEL`, `REPORT_API_KEY`.
