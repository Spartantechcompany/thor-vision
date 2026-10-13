# Guía de datos y API de THOR Vision

Qué registra el sistema, dónde vive cada dato, cómo consultarlo y qué falta. La lista completa de rutas (generada del código) está en
[`API_REFERENCE.md`](API_REFERENCE.md); la documentación interactiva está en `http://<thor>:8080/docs`.

- **Base:** `http://192.168.0.10:8080`. Todas las marcas de tiempo son segundos epoch (decimales). Los filtros `since`/`until` también.
- **Sin autenticación** (solo red interna). Ver "Qué falta" más abajo.

## 1. Mapa de lo que se registra

| Dato | Dónde vive | Cómo se consulta | Retención |
|---|---|---|---|
| **VLM** (descripción de escena y alertas) | tabla `events` (`type='nemotron'`, JSON en `data`) | `GET /api/events`, `/api/events/aggregate`, `/api/timeline` | ~53 días hoy (tope por filas) |
| **Alertas de estacionados y del videoportero** | `events` con `data.source` = `parked_tracker` o `doorbell` | `GET /api/events?has_alert=1` | igual que `events` |
| **YOLO en vivo** (personas, objetos, rostros del cuadro actual) | memoria (no se guarda por cuadro) | `GET /api/detections`, `/api/detections/{cam}`, `/api/pipeline` | solo el último cuadro |
| **YOLO resumido** | dentro del evento VLM (`yolo_people`, `people_mismatch`) y en las visitas | `GET /api/events`, `/api/person-visits` | igual que su tabla |
| **Capturas** (JPG en disco + índice) | carpeta `data/snapshots/<cam>/<día>/` y tabla `snapshots` | `GET /api/snapshots`, `/api/snapshots/file/{id}`, `/api/snapshot/{cam}` (cuadro actual) | 30 días hoy (10 GB) |
| **Personas** (visitas, identidades, rostro y cuerpo) | `person_visits`, `subjects` | `GET /api/person-visits`, `/api/people`, `/api/subjects` | capturas 90 días; empleados no caducan |
| **Asistencia y ocupación** | calculadas de `person_visits` | `GET /api/attendance`, `/api/attendance.csv`, `/api/occupancy`, `/api/people/today` | — |
| **Vehículos estacionados** | tabla `parked_vehicles` (+ alertas en `events`) | `GET /api/vehicles` | 90 días tras irse |
| **Mascotas** (recortes y etiquetas) | tabla `pet_crops`, imágenes en `data/pets/` | `GET /api/pets/stats`, `/api/pets/crops` | sin tope de tiempo (máx. 4000) |
| **Chat** | tabla `chat_messages` | `POST /api/chat`, `POST /api/chat/stream` | 144 días hoy |
| **Reportes** | tabla `reports` | `GET /api/reports`, `POST /api/reports/generate` | sin tope |
| **ONVIF y eventos del videoportero** | tabla `onvif_events` | `GET /api/onvif/events` | 90 días |
| **Acciones sobre dispositivos** | tabla `device_actions` | `GET /api/devices/audit` | sin tope |
| **Zonas** | archivo `data/zones.json` | `GET /api/zones` | — |
| **Conocimiento del agente** | archivo `data/knowledge.md` | `GET /api/knowledge` (editable en `/conocimiento`) | — |
| **Acceso de equipos ONVIF** | archivo `data/onvif.json` (permisos 600, nunca sale por la API) | `/onvif` | — |

Volúmenes hoy: `events.db` 450 MB, 382 mil eventos, 65 mil capturas (10 GB), 30 mil visitas, 345 identidades, 804 recortes de mascotas.

## 2. Ejemplos de consulta

```bash
B=http://192.168.0.10:8080

# VLM: últimas alertas de una cámara
curl "$B/api/events?cam_id=cam-189&has_alert=1&limit=5"

# VLM: resumen por hora de las últimas 24 h (totales, por cámara, por tipo)
curl "$B/api/events/aggregate?hours=24"

# Alertas agrupadas para la línea de tiempo
curl "$B/api/timeline"

# YOLO en vivo de una cámara (personas, cajas, rostros; el cuadro actual)
curl "$B/api/detections/cam-189"

# Estado de las 3 etapas por cámara: movimiento -> YOLO -> VLM
curl "$B/api/pipeline"

# Capturas: índice y archivo
curl "$B/api/snapshots?cam_id=cam-189&limit=3"
curl -o captura.jpg "$B/api/snapshots/file/186623"
curl -o ahora.jpg "$B/api/snapshot/cam-189"        # cuadro actual

# Personas: visitas de una cámara, identidades por revisar, resumen del día
curl "$B/api/person-visits?cam_id=cam-vto&limit=3"
curl "$B/api/people?category=revisar&limit=10"
curl "$B/api/people/today"

# Vehículos estacionados
curl "$B/api/vehicles?state=parked"

# Chat (pregunta en lenguaje natural; usa los datos del periodo que se pida)
curl -X POST "$B/api/chat" -H 'Content-Type: application/json' -d '{"message":"¿cuántas alertas hubo hoy?"}'

# Búsqueda que usa el Vision Agent (hechos exactos, alertas, coincidencias por descripción, conocimiento de la casa)
curl -G "$B/searxng/search" --data-urlencode "q=persona con bolsa naranja"

# Eventos del videoportero (nativos de Dahua y ONVIF)
curl "$B/api/onvif/events?endpoint=vto&limit=20"
```

## 3. Esquemas

**Evento del VLM (`events.data`, `schema: 2`)** — `people`, `persons[{desc, action, where}]`, `vehicles`, `activity` (frase descriptiva),
`scene`, `relevant`, `alerts[]`, `alert_types[]` (códigos del catálogo de `config/scenes.yml`), `severity` (`none|low|medium|high`),
`confidence`, `yolo_people`, `people_mismatch`; metadatos internos: `_ms` (latencia), `_model`, `_trigger` (`cam-motion`, `idle`…), `_media_kind` (`video|image`).

**Alerta de vehículo estacionado** — como un evento del VLM más `source: "parked_tracker"` y
`parked: {vehicle_id, state: arrived|hourly|left, class, duration_s, first_ts, plate}`; `alert_types: ["vehiculo_detenido"]`.

**Alerta del videoportero** — `source: "doorbell"`, `alert_types: ["visita_videoportero"]` (visita con rostro) o `["timbre_videoportero"]`
(evento nativo de Dahua) y `doorbell: {kind, visit_id, subject_id, duration_s | code, action}`; la captura se enlaza por `snapshots.event_id`.

**Detección en vivo (`/api/detections/{cam}`)** — `person_count`, `person_bboxes`, `faces[{bbox, name, confidence, thumb}]`, `objects` (conteo por clase),
`yolo_persons`, `yolo_ts`, `frame_w`, `frame_h`, `inference_ms`.

**Visita de persona** — `subject_id`, `cam_id`, `start_ts`, `end_ts`, `hits`, `status`, `static`, `fp`, `known_name`, `vlm_desc`; las imágenes se piden aparte
(`/api/person-visits/{id}/face|body`).

## 4. Cómo seguir una captura desde una pregunta

1. `POST /api/chat` o la búsqueda del Vision Agent devuelven resultados con `url`/`img_src` hacia `/api/snapshots/file/{id}` cuando el evento tiene captura.
2. Con el `id` del evento (`/api/events`) se llega a su captura: `GET /api/snapshots?...` y comparar `event_id`.
3. Los eventos sin captura (la mayoría de las descripciones periódicas) lo indican en el resultado.

## 5. Qué falta (revisión del 2026-10-03)

1. **Respaldos.** Solo hay respaldos programados del CMDB y de Umami. `events.db` (450 MB), `zones.json`, `onvif.json`, `knowledge.md`, las etiquetas de mascotas y los nombres y categorías de las personas (trabajo manual) **no tienen respaldo programado**. Proponer un respaldo diario con `VACUUM INTO` y copia de los archivos de `data/` al Synology, con rotación.
2. **Autenticación.** La API y el dashboard no piden credenciales: cualquier equipo de la red puede escribir zonas, reiniciar cámaras (modo gestión) o ver identidades. Mínimo recomendado: clave de API para las rutas de escritura y la gestión de dispositivos, o acceso por un proxy con usuario.
3. **YOLO no deja historial.** Solo existe el cuadro actual y lo que resume el VLM o las visitas. Si se quiere analítica (aforo por minuto, tipos de objeto, falsos positivos por zona), falta una tabla compacta de conteos por cámara y minuto.
4. **Uso de tokens y costos.** El evento guarda latencia y modelo, no los tokens; el consumo real solo se ve en el gateway.
5. **Salud del sistema como métricas.** No hay endpoint `/metrics` (Prometheus); Grafana depende de los logs. Convendría exponer cuadros por segundo, cola de YOLO, latencia del VLM, estado de los escuchas (ONVIF/Dahua) y edad del último evento.
6. **Exportación.** Solo existe el CSV de asistencia; falta exportar eventos y alertas por rango (CSV/JSON) para auditoría.
7. **Capturas de alertas del VLM.** Muchas descripciones no tienen captura; las alertas de estacionados y del videoportero sí la llevan.
8. **Retención.** Hoy `events` conserva ~53 días y las capturas 30; conviene decidir plazos explícitos por tipo (alertas más tiempo que descripciones periódicas).
9. **Pruebas de la API.** Hay pruebas de las funciones internas, pero no pruebas de contrato de las rutas (códigos de respuesta y formas JSON); la guía anterior se verificó a mano.
