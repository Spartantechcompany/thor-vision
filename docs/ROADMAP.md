# Roadmap

Estado a 2026-10-02 (v2.3.0). Cada línea indica qué falta y cómo sabremos que terminó.

## 1. Videoportero (timbre)
**Equipo:** panel exterior Dahua VTO y dos monitores interiores VTH. Expone HTTP con autenticación Digest, RTSP y el puerto propietario 37777.
**Plan:**
- Crear en el VTO un usuario dedicado de solo lectura; guardar su clave solo en el `.env` del servidor.
- Escuchar los eventos del equipo por `eventManager.cgi?action=attach` (llamada, llamada sin respuesta, apertura de puerta) y tomar una captura con `snapshot.cgi` en cada timbre.
- Tabla `doorbell_events` con hora, captura, resultado del reconocimiento facial del visitante y, si hubo, a quién llamó; aviso al dashboard y a Wazuh.
- Enlazar el timbre con las cámaras de la entrada (Escaleras de entrada y Exteriores) para recuperar los segundos previos del *pre-roll*.
**Hecho cuando:** un timbre real aparece en la línea de tiempo con su captura en menos de 5 s, y el visitante entra a la base "por revisar" si no se reconoce.

## 2. Personas: empleados, invitados y asistencia
**Hecho:** categorías `revisar` / `empleado` / `invitado`, modal de clasificación, parecidos y fusión, `/api/attendance` (+ CSV), retención por categoría.
**Falta:**
- Consolidación automática de sujetos duplicados. Hoy hay 2 pares de 70 % o más y 31 entre 55 y 70 %; la regla actual solo compara cada visita nueva contra el promedio de cada sujeto, nunca los sujetos entre sí.
- Reporte de asistencia con interfaz propia y reglas: entrada = primera detección en cámaras interiores; salida = última; tolerancia de huecos; fines de semana y festivos; envío programado.
- Alta de empleados con foto de referencia para no depender del azar de la primera detección.
- Invitados: solo cantidades y capturas, nunca reporte individual de asistencia.
**Hecho cuando:** el reporte del día coincide con la asistencia real de una semana de prueba, con menos de 1 persona mal clasificada.

## 3. Mascotas (Akamaru, Mojo-jojo y Gigi)
**Hecho:** recolector de recortes, página de etiquetado y "Entrenar y sugerir" (clasificador sobre características de ImageNet con validación cruzada).
**Falta:** reunir unas 60 etiquetas, hacer el ajuste fino real, y usar el modelo en vivo para que `animal` solo alerte con perros desconocidos.
**Hecho cuando:** 90 % de acierto en validación y cero alertas de `animal` por las tres mascotas durante una semana.

## 4. Vehículos
**Hecho:** registro de vehículos estacionados en Exterior 1 y 2 con llegada, permanencia y placa manual.
**Falta:** lectura automática de placas. Solo es viable en 1 o 2 lugares cercanos de la cámara de 5 MP y sobre el cuadro nativo (las capturas guardadas están reducidas); requiere un detector de placas y OCR con votación entre cuadros. Evaluar antes si se justifica.

## 5. Visión
- Comparar YOLOv8s contra YOLO26 con TensorRT en capturas reales; cambiar solo si gana.
- Dibujar cajas sobre las detecciones únicamente cuando hay una activa (panel en vivo y capturas), con interruptor por cámara.
- ReID de cuerpo y mapa de cámaras para enlazar personas sin rostro, empezando en modo observación.

## 6. Reportes y asistente
- Entrenar con SkillOpt el skill de reportes en periodos ocupados (hoy ninguna ronda superó al skill original) y construir el benchmark del chat.
- Darle al Vision Agent herramientas para consultar la base bajo demanda.

## 7. Plataforma
- Mover Umami a Coolify (respaldo hecho, pendiente de restaurar).
- Proteger `main`, exigir PR y CI, y publicar cada versión con su tag y su GitHub Release usando las notas de `app/release_notes.json`.

## Pendientes registrados el 2026-10-03

- **Zonas por cámara** (`/zonas`): ignorar (falsos positivos fijos, como la palmera de Escaleras entrada), estacionamiento con placa (solo Exterior 1 y 2) y descripción fija para el VLM; aprendizaje de los descartes de mascotas; overlay del recuadro de YOLO en las alertas para depuración automática.
- **Videoportero Dahua y sensor de puerta** (issue aparte; falta usuario de solo lectura en el VTO).
- **Ollama:** usar la GPU (`OLLAMA_LLM_LIBRARY=cuda_v13`), con aviso a NormaAI; retirar `gemma3:12b` y `qwen2.5:3b` si siguen sin uso.
- **Seguridad del host:** inventario de CVE antes de decidir JetPack 7.2.1 (`docs/JETPACK-7.2.1.md`).
- **Gobierno:** épica en Jira, espacio de Confluence y registro de riesgos de biometría (`docs/COBIT-JIRA.md`).
- Búsqueda semántica (e5) como segunda fase, solo para consultas conceptuales.

## Actualización 2026-10-03 (v2.5.0)

- **Hecho:** `/zonas`, alertas de vehículos estacionados (llegada, cada hora, salida), operador ONVIF `/onvif`, videoportero como cámara de solo rostros, skill de reportes v2.
- **Placas (solo vehículos estacionados dentro de las zonas de estacionamiento):** la prueba con `fast-alpr` (detector + OCR ONNX, 0.07–0.09 s por cuadro) no detectó placas en los autos que hoy están fuera de las zonas. Falta validar con un auto dentro de la zona y a resolución nativa. Si funciona, correr el OCR una vez al registrar el vehículo y guardar texto y confianza en `parked_vehicles` (con la placa en el metadato de la alerta). Agregar la dependencia a la imagen requiere cuidar el wheel de OpenCV con GStreamer.
- **Videoportero:** mapear con una prueba real los eventos de timbre y puerta (hoy se guardan todos en `onvif_events`), crear la tabla de llamadas con captura del momento, relación con la persona detectada y aviso. Audio: descartado por ahora.
- **Gestión remota por ONVIF (siguiente):** lectura de red, usuarios y perfiles; con confirmación y bitácora: hora y NTP, reinicio, perfiles de video (resolución, fps, tasa de bits), control de puerta del videoportero. Modo solo lectura por defecto por equipo.
- **Descartado:** conteo de personas de Hanwha (`/opensdk/WiseAI/search/objectcounting/check` devuelve 404 en cam-189, 191, 113 y 228). Open Platform: requiere ser socio (STEP) y la serie Q tiene 225 MB de RAM; no se desarrolla nada por ahora.

## Actualización 2026-10-03 (v2.6.0)

- **Gestión de dispositivos hecha** (`/dispositivos`): Hanwha por SUNAPI genérico sobre `attributes.cgi/cgis`; Dahua/ONVIF por ONVIF. Siguiente: más modelos de acciones guiadas por marca (por ejemplo un asistente para sincronizar la hora de todos los equipos o aplicar el mismo perfil de video a un grupo), aviso automático de firmware antiguo y comparación contra el boletín de seguridad del fabricante.
- **Firmware:** 8 de 10 cámaras Hanwha son LND-6010R con firmware 1.03 de 2020; las 2 QNO-8010R tienen 1.42.01 de 2024. Evaluar actualización (requiere archivo del fabricante; la actualización nunca se hace desde el dashboard).
- **Placas:** ver `scripts/install_alpr.sh`. Validar con un auto estacionado dentro de la zona; si la lectura nocturna es pobre, probar un recorte a resolución nativa con mejor detector (`PLATES_DETECTOR`).

## Actualización 2026-10-03 (v2.7.0)

- **Puerta Tuya (192.168.10.205):** responde y tiene el puerto local 6668 abierto (protocolo local de Tuya, alcanzable desde Thor por el router). Falta el id del dispositivo y su clave local (se obtienen con el proyecto de Tuya IoT / `tinytuya wizard`). Con eso: conexión persistente para el estado de la puerta, alerta al abrirse con captura de las cámaras cercanas y, con confirmación, apertura remota desde `/dispositivos`.
- **Timbre:** afinar `DOORBELL_CODES` con una pulsación real viendo `onvif_events` (`dahua/*`).
- **Avatar de Gigi:** las mejores 10 capturas son de Cocina-Patio (62–165 px, resolución nativa del recorte). Para más calidad hace falta fotografiarla de cerca; el coleccionador de mascotas ya guarda crops nativos.

## Decisiones del 2026-10-04

- **Hardening pendiente (no ahora):** accesos por niveles, incluida la API y el dashboard (hoy sin autenticación, solo red interna y sin usuarios), y respaldos programados de `events.db` y de los archivos de `data/`. Se hace en una fase de hardening, no antes.
- **Tuya:** el proyecto de la nube de Tuya ya tiene IoT Core, Authorization Token Management, Smart Home Basic Service, Device Status Notification (mensajes MQTT con los cambios de estado) e IoT Video Live Stream (WebRTC/RTSP/HLS; 5 GB mensuales de flujo incluidos, luego 0.15 USD/GB). Opciones de integración: (a) agente de operación en oapc-devops por la API de la nube, (b) eventos de estado por el servicio de notificaciones, (c) conexión local con tinytuya para la puerta. Los dispositivos Zigbee no tienen IP propia: se controlan por su puerta de enlace.

## Plan de precisión de personas y video (2026-10-08)

Origen: dos recorridos reales de prueba (02:05 y 02:33) con registro de eventos, visitas, avisos Tuya y viajes. Conclusión medida: el detector YOLO ve a las personas; lo que falla es el **seguimiento** (una persona se parte en varias visitas), la **identidad** (cara con mal ángulo en escaleras, cowork y garage frontal) y el **enlace entre cámaras** (ReID de cuerpo con AUC 0.73).

Orden propuesto:
1. **Conjunto de evaluación:** 200–300 cuadros etiquetados (día/noche, interior/exterior) + los recorridos de prueba como verdad de terreno. Métricas: mAP, precisión y recall por imagen (`model.val()`), visitas por persona real, identidades por persona.
2. **Tracker estándar de Ultralytics, una instancia por cámara:** empezar con ByteTrack (base) y BoT-SORT con `gmc_method: none` y ReID (`yolo26n-reid.onnx`); comparar contra el tracker casero con los recorridos.
3. **Identidad:** aceptar caras de peor ángulo cuando cuerpo y tiempo ya apuntan a la misma persona; revisar el umbral de coseno 0.45 con datos reales.
4. **ReID de cuerpo propio:** entrenar con visitas cuya cara ya identifica a la persona (autoetiquetado entre cámaras).
5. **Reentrenar YOLO** solo si (1) demuestra que el detector es el límite; con negativos difíciles de las exteriores nocturnas. Evaluar inferencia por teselas (SAHI) para personas lejanas en las cámaras de 5 MP.
6. **Video en el navegador:** servir el H.264 original (p. ej. go2rtc, WebRTC/MSE) en vez de ~5 JPEG por segundo por cámara; el navegador lo decodifica por hardware y Thor no recodifica. JPEG por hardware (nvJPEG en Jetson Thor) solo para las capturas guardadas, si el CPU lo pide.
7. **Placas** en Garage frontal: función nueva (hoy solo exteriores 1 y 2).

Pendiente de decisión: retención de recortes de personas para entrenar (hoy 30 días sin nombre), cuota de nube Tuya para evidencia al abrir Recepción, alerta de movimiento en Garage frontal.

### Revisión de repositorios externos (2026-10-08, solo lectura)
- **Advanced-YOLO-Tracker, yolov8-person-tracking-trails:** nada que Ultralytics no tenga. Ideas útiles: tabla de remapeo "ID del tracker → ID real" y estelas de trayectoria por track. No adoptar.
- **face-recognition_yolo_insightface:** mismo esquema que el nuestro, sin calidad de rostro ni seguimiento. Descartar.
- **yolov5-face:** licencia **GPL-3.0**; evitar. SCRFD de InsightFace rinde igual o mejor y no es GPL. El cuello de botella no es detectar la cara sino su calidad y ángulo.
- **arcface_torch:** base de nuestro embedding. Vía futura: afinar un modelo propio con caras de nuestras cámaras y licencia limpia.
- **Automatic-License-Plate-Recognition-using-YOLOv8:** tutorial offline, valida formato de 7 caracteres (UK/EU) y usa EasyOCR en CPU; peor que fast-alpr. Solo copiar la idea de votar la mejor lectura por track con validación de formato mexicano.
- **Licencias a revisar (pendiente de verificar con la fuente):** los pesos buffalo_* de InsightFace se declaran solo para investigación no comercial; y `ultralytics` es AGPL-3.0. Conviene validarlo antes de cualquier uso comercial por OXM.

### Comparación de trackers sobre cajas grabadas (2026-10-09)
`scripts/track_replay.py` repite el tracker de producción y ByteTrack (Ultralytics 8.4.152) sobre `data/eval/tracks-*.jsonl` (últimas `HOURS` horas, por defecto 5) y mide, sin etiquetas: pistas, visitas (≥4 detecciones), máximo de personas simultáneas, y **pares de visitas simultáneas con cajas parecidas** (duplicadas). Con 3 h de actividad de la oficina el tracker actual **no es peor que ByteTrack**: 4–5 veces menos duplicadas en todas las cámaras medidas (Escaleras entrada 9 vs 26–31; Acceso Site 35 vs 145–161; Exterior 2 8 vs 20–23) y menos pistas en Escaleras entrada, Garage posterior, Acceso Site y Exterior 1. Límites: la grabadora no guarda confianza ni imagen (se usó 0.6 fijo y sin ReID) y sin etiquetas no se mide si se funden personas distintas. BoT-SORT con ReID no se pudo probar. Conclusión: no migrar a ByteTrack; el siguiente paso es fusionar visitas de la misma persona con la apariencia del cuerpo (`body_emb`) y validar con un recorrido de verdad de terreno.

### Modelo de visión "Athena" (2026-10-10)
`NEMOTRON_MODEL=Athena`. Verificado en el gateway: `GET /v1/models/Athena` → 404 `model_not_found`; las llamadas con ese nombre responden 500 `native generation failed` (también solo texto); los combos `thor-vision` y `THOR-chat` apuntan a `qwen38-27b`, que el backend ya no sirve (404), y `thor-vision` no declara visión (`capability_mismatch`). Hasta que el gateway registre Athena con visión no hay descripciones. `scripts/probe_vlm.py` sondea texto, imágenes, video y streaming cuando responda.
