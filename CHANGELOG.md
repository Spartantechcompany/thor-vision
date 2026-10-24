# Changelog

Generado desde `app/release_notes.json` (la misma fuente que muestra el dashboard). Versionado semántico: mayor = cambia el modelo de datos o el flujo; menor = funciones nuevas; parche = correcciones.

## v2.45.0 — Focos por red local, horarios y casas separadas (2026-10-24)

La página IoT es solo de la casa Patricio 10: los equipos de Coworking540 (Ahuehuete, Barra, Ceiba, Encino, Sabila, Co Working, Pasillo Entrada, Security Camera...) se sacaron y nunca entran en los controles de grupo ni en los horarios. Los equipos con más de 90 días desconectados van a una sección 'Retirados' al final y no cuentan como fallas. Nuevo panel de focos: brillo, color y blanco (cálido/neutro/frío) de todos los focos a la vez o de cada uno, y horarios (hora, días, focos, color o brillo) que se guardan en el servicio de control y se pueden probar al momento. Todo va por la red local porque el proyecto de la nube de Tuya agotó su cupo de equipos controlables. Las direcciones de los focos salen de la lista de clientes del UDM.

- **IoT · Retirados**: isRetired: desconectado y sin datos de la nube hace más de 90 días, o retired: true en iot.yml.
- **IoT · Control local y horarios**: lan_bulbs.py + video_service.py (oapc-devops): /bulbs, /schedules; solo focos listados en iot.yml.

## v2.44.1 — Todo el inventario de Tuya (2026-10-24)

La página IoT mostraba 29 de los 50 equipos de la cuenta: faltaban 21 (todos los interruptores de coworking, Sala de Juntas, Oficina, Podcast, pasillos y jardín, y cinco focos), ahora están todos, con su número real de botones. Arriba hay un resumen de conectados, desconectados y sin permiso. El Gateway Site lista sus equipos hijos con su estado y cuándo se vio cada uno; cada tarjeta muestra lo que sabe la nube (último dato, fecha de activación, IP de salida). Los equipos sin permiso en la nube (Garage Entrada anterior, Escalera Piso 1, cortina, Gateway 2do Piso) quedan marcados y sin control. El rechazo 'controllable device pool quota is insufficient' de Tuya se explica en español. El puente publica info y children en cada sincronización.

- **IoT · 52 equipos en iot.yml**: 23 interruptores, 13 focos, 5 sensores, 4 cámaras, gateways, panel, alarma, cortina, enchufe, abre-garage.
- **IoT · Hijos del gateway**: tuya_bridge: /v1.0/devices/{gw}/sub-devices; listener: children e info; página: lista con estado y último dato.

## v2.44.0 — IoT en 3D: cada apagador es una palanca (2026-10-24)

La página IoT dibuja cada interruptor como una placa de pared en SVG con una palanca por botón físico: tres apagadores son tres palancas independientes que se tocan directamente, sin botones de texto, y el estado dice cuántos están encendidos ('1 de 3 encendidos'). Los focos son un bulbo con halo y los enchufes un botón con domo, con brillo, sombra y animación suave (se respeta 'reducir movimiento'). Las palancas se mueven con animación al cambiar el estado en vez de redibujarse cada 5 s, responden al teclado y vuelven a su lugar si la orden falla. Inventario de Tuya al día: 5 focos nuevos (Podcast 1, 2 y 3, pizarrón y televisor 2) y 'Garage Switch' reemplaza al 'Garage Entrada' viejo, que ya no tiene permiso en la nube. El puente relee iot.yml en cada sincronización, así que un dispositivo nuevo entra sin reiniciarlo. Opcional: gang_names en iot.yml pone nombre a cada botón.

- **IoT · Placas 3D con palanca por botón**: plateSvg/bulbSvg/plugSvg en iot.html; sendGang con actualización optimista y reversa; patchCard actualiza sin rehacer.
- **IoT · Inventario al día**: 29 dispositivos en iot.yml (8 focos, 5 interruptores); tuya_bridge.cloud_sync relee la lista en cada ciclo.

## v2.43.1 — Línea de tiempo y visor más claros (2026-10-22)

Los botones de la línea de tiempo muestran 'Cargando…' al instante y un clic nuevo cancela la carga anterior (con 24 h o más cada cambio tardaba 2-3 s sin ninguna señal y se sentía muerto). El visor de alertas atenúa la imagen mientras carga, avisa si no llegó y reintenta una vez. Las páginas HTML llevan Cache-Control: no-cache para que Safari no conserve copias viejas. Probado en Chromium y WebKit (el motor de Safari): botones, imágenes de capturas y de visitas.

- **Panel · Cargando y reintento**: tlLoad con AbortController; _vaLightbox con estado de carga/error y un reintento; middleware NoCacheHTML.

## v2.43.0 — Viajes por timbre, videoportero unido a la entrada, Clef en los recorridos, calle ligera, identidades consolidadas y resumen de cada viaje (2026-10-21)

El viaje del videoportero lo abre el timbre (evento nativo del Dahua), no cualquiera que pase: con los 22 timbrazos de la semana habrían sido 6 alertas de viaje en vez de 248. Quien sale del videoportero y aparece en Garage frontal en menos de 4 minutos queda en el mismo recorrido, así el viaje lleva la cara del videoportero. En el enlazador de recorridos el cuerpo lo juzga Clef (AUC 0.84 contra 0.67 del embedding ReID; en 42 pares con rostro confirmado, precisión 0.64 y recall 0.50, antes 0.40 y 0.14), también para unir pedazos de la misma persona en una cámara. En Exterior 1 y 2 solo quien se acerca a la puerta (o a la zona nueva "seguimiento en calle") abre una visita completa; el resto se cuenta por hora como transeúnte. De noche se consolidan identidades: pares de "Persona #N" parecidos van a Clef y se fusionan solo si Clef e InsightFace coinciden (≥ 0.85 y ≥ 0.40), una fusión por persona por noche; lo demás queda como sugerencia. Al cerrar un viaje con alerta, una sola llamada con video resume quién llegó, por dónde y qué hizo.

- **Viajes · Disparo por timbre**: Disparador vto-timbre en topology.yml (entry_gap_s 300); DoorAlerts.on_ring abre el viaje.
- **Recorridos · Videoportero → Garage frontal y Clef**: door_links, door_link_s=240, door_min_s=15; JOURNEY_CLEF, JOURNEY_CLEF_MIN_P=0.7 (3 candidatos por visita).
- **Personas · Calle ligera**: VISIT_STREET_LIGHT_CAMS; zona seguimiento_calle; /api/street/passersby.
- **Personas · Consolidación de identidades**: app/vision/consolidate.py, 03:00 cada noche; POST /api/people/consolidate?dry=1. En prueba: 19 de 250 pares se fusionarían; con la regla doble, 15.
- **Viajes · Resumen de la película**: TRIP_SUMMARY_ENABLED; la escena de cada visita del recorrido como un video de hasta 8 cuadros; queda en el evento (activity/summary) y en trips.summary.

## v2.42.0 — Matrículas confiables, sin autos fantasma y viajes sin ruido (2026-10-21)

Matrículas: solo se dan por buenas con 2 lecturas iguales de confianza ≥ 0.7, 3 de ≥ 0.6 o una de ≥ 0.85; lo demás queda "por confirmar" con un botón para confirmarlo (las 7 lecturas viejas poco seguras pasaron a por confirmar). Se sigue leyendo cada 10 min mientras el auto siga ahí (antes se dejaba de intentar a los 20 min). El mismo auto se enlaza por placa, tolerando las confusiones típicas del OCR: partido en la misma cámara se fusiona, visto a la vez por Exterior 1 y 2 queda como gemelo, y si vuelve otro día se cuentan sus visitas. Estacionados: un auto solo se registra si aparece en al menos la mitad de los cuadros, y uno registrado que en 10 min aparece en menos del 15% se da por ido: eran carriles con tráfico que quedaban 'estacionados' durante horas. Viajes: un recorrido solo cuenta si pasa por la puerta o la calle, o si empieza en la cámara de entrada a lo mucho 2 min después del disparo. Reprocesando la semana, las alertas de viaje del videoportero bajan de 248 a 80: el 92% eran personas que ya estaban adentro.

- **Vehículos · Placas por confirmar y enlazadas**: plates.vote/guess/similar; columnas plate_guess, plate_guess_conf, merged_into, twin_id; /api/vehicles trae plate_guess, twin_id y other_visits.
- **Vehículos · Sin fantasmas de tráfico**: PARKED_PRESENCE_MIN=0.5, PARKED_PRESENCE_GONE=0.15 (ventana de 10 min), PLATES_SLOW_S=600.
- **Viajes · Solo quien llega por la puerta**: TRIP_ENTRY_GAP_S=120 y la cámara de entrada de topology.yml (entry_cams): quien aparece ahí tras el timbre cuenta como entrada.

## v2.41.0 — Prompt compacto medido (2026-10-21)

El texto fijo de cada cámara baja de ~970 a ~750 tokens (-22%, unos 13% menos por llamada): esquema JSON más corto, descripciones breves de cada código de alerta y sin frases repetidas. Se midió antes de activarlo con scripts/prompt_ab.py: las mismas imágenes y clips de las 10 cámaras con el prompt actual (dos veces, para medir cuánto varía el modelo por sí solo) y con el compacto. Sobre 40 entradas con cuadros limpios, el modelo coincide consigo mismo en 97% de personas y 100% de alertas, y el compacto en 97% y 95%; las 3 diferencias fueron casos ambiguos y en el único claro (una persona de pie en Cowork) acertó el compacto. VLM_PROMPT_COMPACT=false vuelve al texto anterior.

- **Modelo · Prompt compacto**: scenes.yml: alert_catalog_short; scenes.py: SCHEMA_COMPACT y render_parts(compact=). Promedio 970 -> 752 tokens de texto; el esquema y todos los códigos de alerta se conservan (prueba).
- **Herramientas · Prueba A/B de prompts**: scripts/prompt_ab.py (dentro del contenedor). Usa cuadros limpios de go2rtc: /api/snapshot dibuja encima los chips de detección y el texto del último análisis, y el modelo los lee, lo que falsea la comparación.

## v2.40.0 — Escena completa con menos llamadas (2026-10-21)

El modelo veía clips de apenas 1 a 2 segundos (30 cuadros seguidos del búfer) y el servidor solo tomaba ~4 cuadros de ellos. Ahora cada clip es la escena de los últimos 16 s en 8 cuadros a 480x270 (un cuadro cada 2 s, ~530 tokens, casi lo mismo que antes), esperando 4 s tras el inicio del movimiento para que la acción entre. Si la escena no cambió desde el último análisis (mismas personas en el mismo lugar, mismos objetos, imagen casi igual) no se llama al modelo; se vuelve a mirar a los 5 minutos. thor-vision manda como mucho 2 peticiones a la vez y, cuando el gateway contesta 503 por capacidad, abre una pausa común: la rutina espera y solo pasan las prioritarias (noche en zonas de alerta, armado, videoportero, personas en la calle). El texto fijo de cada cámara va antes del video para que el servidor lo reutilice de su caché, y la foto del heartbeat va a 640x360.

- **Modelo · Clip de escena de 16 s**: VLM_SCENE_FRAMES=8, VLM_SCENE_WINDOW_S=16, VLM_SCENE_WIDTH/HEIGHT=480x270, VLM_RISE_DELAY_S=4. Medido: 8 cuadros a 480x270 = 527 tokens; 10 cuadros de 1 s = 471 (el servidor muestrea a 2 fps).
- **Modelo · Escena igual sin llamada**: VLM_SAME_GATE, VLM_SAME_STALE_S=300, VLM_SAME_PIX=0.06, VLM_SAME_MOVE=0.08. /api/pipeline: vlm_same_scene_1h.
- **Modelo · Saturación del gateway**: VLM_MAX_CONCURRENCY=2; pausa 4-60 s tras un 503 de capacidad; rutina omitida (vlm_busy_skips_1h) y prioritarias con un solo reintento. /api/pipeline: gateway.

## v2.39.0 — Caras nítidas y alertas de día sin "persona nocturna" (2026-10-21)

Las miniaturas de rostro ya no se agrandan: se guardan a su tamaño real (máximo 160 px) y se prefiere la captura JPEG de la cámara sobre el cuadro del video. Una cara de menos de 40 px en el cuadro original no se guarda como miniatura, y al cerrar cada visita Clef revisa si la cara se ve bien: si está barrida, borrosa o no es una cara, la visita se queda solo con el cuerpo. Cowork y Sala de Juntas vuelven a su horario: el modo de alerta total del 9 de octubre estaba fijo en la configuración y daba unas 1,200 alertas de "persona nocturna" de día; para alertar por toda persona está el botón de armado, y de día esa alerta ahora se llama actividad del armado. En la vista de cámara la línea de tiempo incrustada ya no pide video ni capturas de las 11 cámaras, que dejaban sin conexión a la imagen de la alerta.

- **Personas · Miniaturas sin zoom**: Recorte a tamaño real (máx. 160 px); mínimo de 40 px de cara (FACE_THUMB_MIN_PX); Clef quita las miniaturas con probabilidad < 0.18 de verse bien (FACE_CLEF_MIN, 0 lo apaga). Log FACE_Q.
- **Alertas · Horario normal en Cowork y Sala de Juntas**: Sala de Juntas: alerta de 00:00 a 06:00 entre semana y sin alerta nocturna el fin de semana; Cowork: 22:00 a 06:00. Con armado absoluto, una persona de día es actividad_armado, no persona_nocturna.
- **Cámara · La captura de la alerta carga**: El panel incrustado (?tl=) solo corre la línea de tiempo; el visor pausa la vista en vivo mientras está abierto y avisa si la captura no carga.

## v2.38.0 — Prompts por toma de cámara y video real otra vez (2026-10-21)

Cada cámara le dice al modelo desde dónde mira (TOMA), qué hay en cada parte del encuadre (ZONAS) y cómo se mueve normalmente la gente (FLUJO), y se le pide decir dónde ocurre algo y hacia dónde se mueve, empezando por lo más importante. El videoportero recibe su primer perfil. Con preserveVideoUrl activo en OmniRoute, los clips vuelven a mandarse como video (547 tokens contra 1009 de los cuadros en la misma prueba).

- **Prompts · Toma, zonas y flujo**: config/scenes.yml: toma, zonas y flujo para las 11 cámaras, escritos a partir de sus capturas de día. Ejemplo Exterior 1: muro con la puerta peatonal a la izquierda, banqueta al centro, calle a la derecha; el mismo tramo que Exterior 2.
- **Modelo · Video real**: VLM_VIDEO_AS_FRAMES ahora es false por defecto; true vuelve a mandar cuadros si el gateway deja de aceptar video.

## v2.37.0 — Identidad en dos etapas: InsightFace + Clef (2026-10-21)

Con el umbral actual (0.45) InsightFace solo reconocía al 12 % de las visitas de la misma persona en otro momento, y por eso se acumulaban cientos de personas 'por revisar'. Ahora, cuando el parecido queda en la zona gris (0.25-0.45), Clef compara la cara de la visita con la mejor cara del candidato al cerrar la visita.

- **Identidad · Reglas**: Clef >= 0.85: la visita pasa sola a esa persona y se borra la identidad temporal si quedó vacía. Clef 0.6-0.85: sugerencia en /identidades (pestaña Sugerencias, con el % de Clef). Nunca se mueve una visita de alguien con nombre. Todo queda en la tabla identity_checks.
- **Medición · Prueba con 160 pares**: AUC InsightFace 0.928, Clef 0.915, combinados 0.955. En una semana de visitas: ~37 uniones automáticas (0 entre dos personas con nombre) y el caso Oswaldo/Neto (Clef 0.74) habría quedado solo como sugerencia.

## v2.36.1 — El vigilante de cuadros ya no tumba el contenedor (2026-10-21)

El vigilante de v2.34.2 liberaba desde otro hilo la captura colgada; con FFmpeg (respaldo de cam-118) eso tumbó el proceso dos veces el 21-oct (01:16 y 01:19, sin apagado ordenado). Ahora solo libera capturas de GStreamer (seguro, probado con el videoportero) y las de FFmpeg llevan tiempos máximos propios de apertura (10 s) y lectura (8 s).

- **Captura · Sin caídas del contenedor**: FFmpeg: CAP_PROP_OPEN_TIMEOUT_MSEC y CAP_PROP_READ_TIMEOUT_MSEC; la lectura colgada regresa sola y el lector reconecta.
- **Diagnóstico · cam-118 y videoportero**: La red a cam-118 no pierde paquetes; sus fallas y el reinicio del videoportero (01:25) coinciden con la saturación de sesiones RTSP de la madrugada (TV con video en vivo, go2rtc y Thor a la vez). Con el modo ligero en TV y go2rtc como único cliente del videoportero no se han repetido.

## v2.36.0 — Auditoría: compresión, un bug que llenaba el log, ahorro en pestañas ocultas y autochequeo (2026-10-21)

Pasada adversarial con una batería fija (scripts/audit_battery.py) medida antes y después. Se corrige el borrado del volcado de depuración que fallaba en cada llamada (45 % de los avisos del log), se comprimen páginas y JSON (el panel y la línea de tiempo pesaban 220 y 640 KB), la línea de tiempo se guarda 10 s en caché, el panel deja de consultar con la pestaña oculta y abre los videos escalonados, y hay un nuevo /api/selfcheck.

- **Bug · Volcado de depuración**: Al rotar archivos intentaba borrar la carpeta 'latest' y fallaba: 4,956 de 12,150 avisos en 24 h.
- **Rendimiento · Gzip selectivo**: Páginas y JSON comprimidos; imágenes, clips y flujos no se tocan.
- **Panel · Pestaña oculta y precarga**: Los 13 temporizadores se saltan con la pestaña oculta y refrescan al volver; preconexión a go2rtc, prefetch de la vista de cámara al pasar el cursor, videos escalonados cada 250 ms.
- **Operación · /api/selfcheck**: Cámaras, modelo, modo sin VLM, Clef, go2rtc, gateway de IA, Tuya, disco y eventos recientes en un solo estado ok/aviso/falla.

## v2.35.0 — Íconos por tipo de equipo, puertas de enlace en /iot y ONVIF sin el videoportero duplicado (2026-10-21)

En /iot cada equipo Tuya muestra un ícono según lo que es (foco, interruptor de 1/2/3 botones, sensor de contacto, abre-portón, cortina, enchufe, gateway, panel, alarma) y su rótulo; se agregan las puertas de enlace Zigbee, el panel y la alarma con su estado en línea desde la nube. El videoportero deja de aparecer como 'cam-vto · 127.0.0.1:80' en la página ONVIF.

- **IoT · Tipos e íconos**: config/iot.yml lleva type (y gangs en interruptores) por dispositivo; la página agrupa en Puertas de enlace, Cámaras, Sensores de puertas/ventanas/garage, Focos, Interruptores y enchufes, Cortinas.
- **IoT · Gateways**: Gateway Site (en línea según la nube), Gateway 2do Piso (la nube no autoriza el equipo: sin datos), Panel de control P1 y Alarma Site (desconectada). El puente sincroniza su estado en línea cada 10 min.
- **ONVIF · Sin duplicado del videoportero**: Las cámaras que Thor lee por go2rtc (127.0.0.1) ya no se listan como equipos propios: el aparato real sigue siendo el endpoint 'vto' (192.168.0.3).

## v2.34.4 — El video llega al modelo como cuadros (2026-10-21)

OmniRoute descarta el video_url (el modelo recibía 33 tokens y respondía 'no hay video'), por lo que los análisis de video de las cámaras trabajaban solo con el texto y el recorte. Ahora la secuencia se manda como 6 imágenes en orden cronológico a 640x360, que el gateway sí respeta (misma descripción que directo a Spark01). VLM_VIDEO_AS_FRAMES=false vuelve al video.

- **Modelo · Secuencia como imágenes**: Prueba con un clip real: directo a Spark01 el video_url dio 547 tokens y descripción correcta; por el gateway 33 tokens y 'no hay video'; las mismas 8 imágenes por el gateway 1009 tokens y la misma descripción correcta.

## v2.34.3 — Toda visita guarda al menos el cuadro completo (2026-10-21)

Las alertas de persona nocturna de Garage frontal salían sin imagen: sus visitas no generaban recorte de cuerpo ni de cara. Ahora, si una visita no tiene escena, se guarda el cuadro con el recuadro de la persona en cuanto se detecta.

- **Alertas · Imagen garantizada**: En 24 h, 19 de 100 alertas persona_nocturna (modo sin VLM) y todas de cam-228 no traían captura; las demás alertas ya la traen (Tuya recuperó la suya con la clave nueva).

## v2.34.2 — Reconexión automática de cámaras con el video colgado (2026-10-21)

El videoportero llevaba unas 7 horas congelado en Thor y marcado como 'streaming': cap.read() se quedó bloqueado sin que nada lo detectara. Ahora un vigilante por cámara libera la captura si no llegan cuadros nuevos en 20 s (STREAM_STALL_S) y el lector reconecta solo.

- **Captura · Vigilante de cuadros**: Aplica a todas las cámaras. Si la captura se libera, el log dice 'sin cuadros nuevos en N s: se fuerza la reconexion'.

## v2.34.1 — El video en vivo se reinicia solo si se congela (2026-10-17)

Cuando una cámara se cae y go2rtc reconecta (el videoportero rechazó conexiones RTSP unos 11 minutos), el video del navegador se quedaba en el último cuadro. Un vigilante revisa cada 2.5 s si el video avanza y, si lleva 7.5 s sin avanzar, lo reinicia y vuelve a las fotos mientras reconecta.

- **Video · Vigilante de video congelado**: En el panel y en /camara/<id>: currentTime sin cambios 3 revisiones seguidas dispara el reintento.

## v2.34.0 — Presencia WiFi por teléfono, sugerencias con Clef, encabezado en una fila y TV ligera (2026-10-13)

Para el WiFi, el teléfono es la presencia: una laptop olvidada en la oficina ya no cuenta mientras el teléfono del empleado esté fuera. Los equipos que cambian de MAC pero conservan su nombre heredan al dueño, y Clef sugiere de quién es cada equipo a partir de su nombre. El lector de UniFi pasa a cada minuto. El encabezado queda en una sola fila y las Smart TV (Tizen, WebOS, Silk) usan el modo ligero sin video en vivo.

- **Presencia · El teléfono manda**: Rol por dispositivo (teléfono, laptop, otro; se adivina por el nombre y se corrige en /wifi). Con un teléfono asignado, solo los teléfonos cuentan. Sin teléfono, cuentan todos sus equipos.
- **Presencia · Clef sugiere dueño**: POST /api/wifi/suggest: Clef lee el nombre de cada equipo sin dueño y propone tipo y empleado (5 de 6 aciertos con dueño conocido; 'Ernesto Pastrana' no lo liga a 'Neto'). Nunca asigna solo.
- **Presencia · Cambio de MAC**: Un equipo nuevo con el mismo nombre (sin acentos ni mayúsculas) que uno ya asignado hereda al empleado y el rol. Los nombres genéricos tipo 'iPhone 49:56' no heredan.
- **Panel · Una fila y TV ligera**: Botones compactos en una sola fila (se desplaza sin barra si la ventana es angosta), menú Más posicionado bajo su botón. Smart TV: sin video en vivo, que saturaba la TV y las cámaras.
- **Viajes · Menos disparos del videoportero**: Solo las visitas de 4 s o más (o con nombre) abren un viaje; los rostros de 0-2 s ya no activaban refuerzos cada pocos minutos.

## v2.33.0 — Video en la vista de cámara, menú Más y videoportero por go2rtc (2026-10-13)

La página de cada cámara (/camara/<id>) también muestra video en vivo por WebRTC con respaldo a capturas; el panel suma un menú 'Más' con las páginas que no tenían enlace (Presencia, WiFi, Evaluación, Miniaturas, Conocimiento, Reglas, Zonas); y las cámaras de 5 MP solo usan video si el navegador las decodifica con fluidez (MediaCapabilities). El videoportero se lee a través de go2rtc para que su única sesión RTSP sirva a Thor y al navegador.

- **Panel · Menú Más**: Enlaces a /presencia, /wifi, /evaluacion, /galeria, /conocimiento, /reglas y /zonas desde el encabezado.
- **Video · Vista de cámara en vivo**: /camara/<id> usa WebRTC (go2rtc) y vuelve a capturas si falla; ?video=0 lo apaga. El doble clic sigue abriendo pantalla completa.
- **Video · Videoportero**: Admite una sola sesión RTSP (503 en la segunda): Thor ahora lee rtsp://127.0.0.1:8554/cam-vto y go2rtc es el único cliente del videoportero.

## v2.32.0 — Video en vivo en el panel (go2rtc, WebRTC) (2026-10-12)

Las tarjetas de cámara muestran video real por WebRTC desde go2rtc en vez de una foto cada 200 ms. Si una cámara no conecta en 8 s o se corta, vuelve sola a las fotos y reintenta a los 15 s. Sin audio ni controles de volumen.

- **Panel · Video en vivo**: Botón 'Video en vivo' sobre la cuadrícula (se recuerda por navegador); ?video=0 lo apaga y ?video=1 lo fuerza. Se pausa con la pestaña oculta. Las 11 fuentes (incluidos videoportero y exteriores a 5 MP) pasan por go2rtc, que solo se conecta a la cámara cuando alguien mira.

## v2.31.2 — Snapshots con el perfil MJPEG de mayor resolución (2026-10-12)

El lector de snapshots descubre al primer uso el perfil MJPEG de mayor resolución de cada cámara y lo pide de forma explícita (Profile=N), sin depender del predeterminado. En las cámaras se borraron los perfiles de 800x448 y las dos exteriores quedaron con el mismo MJPEG de 1 fps y 4096 kbps.

- **Captura · Perfiles de las cámaras**: Streams de Thor: H.264 a 30 fps en las 10 cámaras (1920x1080 en las 8 internas y de garage, 2592x1944 en las exteriores). Snapshots: MJPEG de la misma resolución. Respaldos de lo borrado en data/profiles-800x448-backup-20261012.json.

## v2.31.1 — Snapshot SUNAPI con perfil MJPEG en las cámaras de 5 MP (2026-10-12)

Las QNO-8010R (Exterior 1 y 2) responden error 604 al pedir snapshot porque no tienen ningún perfil MJPEG. El lector de snapshots ahora busca un perfil MJPEG y, si existe, lo usa con Profile=N; si no, lo registra una sola vez en el log.

- **Captura · Snapshot a resolución completa**: En cuanto se cree un perfil MJPEG en la cámara (por ejemplo el perfil 3, que no se usa), la búsqueda de rostros en la calle podrá pedir cuadros de 5 MP en vez de depender del stream.

## v2.31.0 — Alerta por cruce de la línea virtual de la cámara (2026-10-12)

El cruce de la línea virtual IVA de las cámaras Hanwha (evento VideoAnalytics.Passing por SUNAPI) genera la alerta cruce_linea cuando YOLO confirma una persona, con captura y clip.

- **Alertas · cruce_linea**: Flanco de subida del evento de cruce con enfriamiento de 15 s; 3 s después se revisa que YOLO vea una persona (los autos y hojas no alertan). Severidad media de noche. Hoy la línea está en Exterior 1 (a lo largo del muro, ambas direcciones).

## v2.30.1 — Garage frontal cuenta como salida de las mascotas (2026-10-12)

Garage frontal pasa a contar como 'fuera de la casa' para la presencia de mascotas, junto a Exterior 1, Exterior 2 y el videoportero.

- **Mascotas · Garage frontal = salida**: Una mascota vista en Garage frontal tras haber estado dentro genera mascota_salida; al volver a una cámara interna, mascota_regreso.

## v2.30.0 — Presencia de mascotas, empleados e invitados (2026-10-12)

Nuevo tablero /presencia con quién está ahora: empleados (cámara o WiFi), invitados del día y mascotas (en casa o fuera, con última cámara y hora). Las mascotas conocidas generan aviso al salir de la casa y al regresar.

- **Mascotas · En casa / fuera**: Cada perro que ve YOLO se identifica con el clasificador de recortes (Akamaru, Gigi, Mojo-jojo, confianza mínima 0.7). Exterior 1, Exterior 2, Garage frontal y el videoportero cuentan como fuera.. Alertas mascota_salida y mascota_regreso con la foto; GET /api/pets/presence.
- **Presencia · Tablero /presencia**: GET /api/presence/now junta empleados (cámara + WiFi), invitados y mascotas; se actualiza cada 20 s. Un empleado cuenta como 'en oficina' si lo ve una cámara interna o su dispositivo está en el WiFi.

## v2.29.0 — Modo sin VLM: alertas por reglas sobre visitas confirmadas (2026-10-12)

Cuando el modelo de visión lleva más de 5 minutos sin responder, persona nocturna y merodeo se generan con reglas sobre las visitas que el tracker ya confirmó (YOLO + Clef), con captura y la marca 'modo sin VLM'. Se apaga solo cuando el VLM vuelve.

- **Alertas · Persona nocturna sin VLM**: De noche, en cámaras con night_person: alert (scenes.yml), una persona que no es personal conocido ni empleado genera alerta con su captura y la ropa detectada. En calle solo si se queda 30 s.
- **Alertas · Merodeo en calle**: Una persona que permanece 150 s o más en Exterior 1 o 2. Enfriamiento de 120 s por cámara y tipo.
- **Validación · Simulación de 24 h**: Sobre las visitas reales de un día habría dado ~50 alertas (38 de merodeo en calle y 12 nocturnas en interiores). GET /api/yolo-rules muestra si está activo, por qué y cuántas lleva. YOLO_RULES=always|off lo fuerza.

## v2.28.0 — Clips de video en las alertas (2026-10-12)

Las alertas de merodeo en la puerta peatonal y de apertura del portón del garage guardan un clip MP4 (6 s antes y 6 s después, de las cámaras de la zona) además de la captura. El historial muestra un botón ▶ junto al evento.

- **Alertas · Clip por alerta**: Se arma con el anillo de pre-roll que ya existía (4 cuadros por segundo, resolución nativa reducida a 1280 px), codificado en H.264; no agrega decodificación. Hasta 3 cámaras por evento, 2 codificaciones a la vez.
- **API · Rutas de clips**: GET /api/clips/recent, GET /api/clips/file/{id} y POST /api/clips/now?cam=... para grabar un clip a demanda. CLIP_TTL_DAYS (90 por defecto) borra los viejos cada hora; CLIPS_ENABLED=false lo apaga.

## v2.27.0 — Merodeo en la puerta peatonal y presencia por WiFi (2026-10-12)

Nueva zona 'puerta_peatonal' que se dibuja en /zonas: una persona real (confirmada por Clef) que se queda 20 s o más frente a la puerta genera una alerta de merodeo con captura. La asistencia suma la presencia WiFi de cada empleado (UniFi, solo lectura) y se conservan solo las mejores fotos por empleado y cámara.

- **Alertas · Merodeo en la puerta**: Alerta merodeo_puerta (alta de noche). No alertan los empleados conocidos ni lo que Clef no confirma como persona (árboles junto a la pared). DOOR_DWELL_S ajusta los segundos.
- **Asistencia · Presencia WiFi**: El lector de UniFi en oapc-devops envía cada 3 min los clientes WiFi a /api/wifi/clients; en /wifi se asigna cada dispositivo a un empleado. /api/attendance y el CSV traen wifi_primera, wifi_ultima y wifi_minutos aunque la cámara no vea a la persona.
- **Privacidad · Fotos de empleados**: Se conservan las 5 mejores (cara primero) por empleado y cámara; las imágenes de visitas con más de 7 días se descartan (EMPLOYEE_KEEP_BEST, EMPLOYEE_PRUNE_DAYS). Las filas quedan para asistencia e identidad.

## v2.26.0 — Viajes disparados por la puerta del garage y el videoportero (2026-10-12)

Cuando se abre la puerta peatonal del garage o llega alguien al videoportero se abre un viaje: las cámaras del recorrido suben a más fps, se reanalizan los segundos previos y, al vencer la ventana, queda una alerta con la dirección (entra o sale), la cadena de cámaras y un mosaico.

- **Viajes · Recorrido por evento**: config/topology.yml > trips define, por disparador, las cámaras que lo siguen y cuáles son 'de adentro'. Entrada = calle > garage > escaleras/Site; salida = al revés. Alerta 'viaje_puerta' y GET /api/trips.
- **Viajes · Personas separadas**: Se respetan los viajes por persona que ya enlaza el sistema: dos personas a la vez no se funden. Primero las entradas/salidas; el movimiento interno se cuenta aparte. El timbre sin nadie que entre no genera alerta.
- **Validación · Repetición con datos reales**: La apertura del garage del 11-oct 15:30 se reproduce como 'entrada: Exterior 1 > Exterior 2 > Exterior 1 > Garage frontal' más 24 movimientos internos.

## v2.25.0 — Clef-flash: verificador de personas (2026-10-12)

Clef-flash (Cloudflare, 9B, Apache-2.0) corre local en Thor como servidor de decisiones y confirma que cada visita sin cara es una persona real; los árboles, postes y sombras que YOLO tomaba por persona se descartan antes de generar alertas.

- **Tracker · Verificación de persona con Clef**: Cada visita sin cara se confirma en segundo plano con el mejor recorte de cuerpo (~0.4 s). Probabilidad de persona < 0.20 = falso (CLEF_PERSON_MIN_P). En una muestra de 48 visitas de Exterior 1 y 2 los árboles quedaron entre 0.03 y 0.19 y las personas desde 0.56.
- **Infra · Servidor Clef aparte**: Contenedor thor-clef (deploy/docker-compose.clef.yml) con la misma imagen, solo en 127.0.0.1:20140; carga en 20 s. Si no responde, el tracker sigue con sus reglas (pausa automática tras 3 fallos). Estado en GET /api/clef.
- **Identidad · Clef como árbitro de 'misma persona'**: Medido con 18 sujetos nombrados: AUC 0.76 (embedding de cuerpo 0.71, combinados 0.80). Útil para confirmar fusiones sugeridas, no para fusionar solo.

## v2.24.6 — Recuadro verde fino, cajas lejanas y repo oficial (2026-10-11)

Las capturas de visita dibujan el recuadro con el verde del panel y 1 px, y ahora muestran a todas las personas del cuadro (la de la visita resaltada); una persona que se aleja ya no se descarta como 'caja minúscula' por el tamaño de su último cuadro; el mensaje de error de mascotas se limpia solo; y se fija Oxm-Tech como repositorio oficial con el fork como respaldo.

- **Capturas · Recuadro #76B900 de 1 px**: Antes salía turquesa por tener los canales BGR al revés y de 2 px. Las demás personas del cuadro se dibujan apagadas, así una escena con dos personas muestra a las dos.
- **Tracker · Caja minúscula = toda la vida del track**: El filtro de cajas diminutas en exteriores usaba la última caja: peatones reales que se alejan (hasta 120 detecciones) quedaban marcados como falsos. Ahora cuenta el alto máximo que tuvo.
- **Mascotas · Error de carga**: El aviso 'Error al cargar recortes' se quedaba pegado tras un fallo pasajero; ahora se limpia al cargar bien.
- **Proyecto · Repo oficial**: Oxm-Tech/thor-vision es el oficial; Spartantechcompany/thor-vision queda como copia de respaldo. README con el enlace al perfil del autor.

## v2.24.5 — Vuelta a Qwen, entradas Tuya sin falsos y créditos (2026-10-11)

El modelo de visión vuelve a llamarse Qwen (Athena se retira mientras el backend siga caído); el movimiento de una cámara Tuya sin persona ya no genera 'entrada sin captura' (tiene su propia alerta); los avisos de puertas nombran las cámaras revisadas; y el README incluye a Oswaldo A. Pérez Castillo y la asistencia de Claude Code.

- **Modelo · Qwen de nuevo**: NEMOTRON_MODEL=thor-vision y los textos del panel vuelven a Qwen.
- **Alertas · Entrada sin captura solo para sensores**: Garage, Recepción y Comedor; las cámaras Tuya ya no (generaban 10 al día por mascotas y sombras). El texto lista las cámaras revisadas en vez de solo la primera.
- **Proyecto · Créditos**: Autores del README actualizados.

## v2.24.4 — El modelo de visión ahora es Athena (2026-10-10)

El modelo de visión pasa a llamarse Athena: NEMOTRON_MODEL=Athena en docker-compose.yml (el alias anterior thor-vision dejó de aceptar imágenes en el gateway: 'capability_mismatch'), y el panel, la animación, las reglas y la documentación dicen Athena en lugar de Qwen.

- **Modelo · Nombre en la API**: /api/vlm devuelve model=Athena; model_real se llena con lo que responde el backend en la primera llamada correcta.
- **Panel · Chips y textos**: El chip de la tarjeta de cada cámara dice ATHENA y el resumen 'Athena última hora'.

## v2.24.3 — Línea de tiempo con ventanas anchas sin huecos (2026-10-10)

Con rangos de varios días la línea de tiempo se quedaba con los últimos 4,000 eventos y mostraba 0 alertas en lo más viejo (por eso Garage frontal y 'todas las cámaras' salían vacías entre el 07 y el 08). Ahora, cuando hay más de 4,000, se agrupa por cámara, tipo y tramo de tiempo, mostrando un punto por grupo con su cantidad, y los contadores suman todo.

- **Línea de tiempo · Sin huecos en rangos anchos**: Respuesta con total, thinned y n por grupo; el resumen avisa 'ventana ancha: se muestra un evento por grupo'. Ventanas cortas, igual que antes.
- **Línea de tiempo · Tráfico de calle**: El límite de visitas de calle subió de 600 a 3,000.

## v2.24.2 — Página de búsqueda con comparación lado a lado (2026-10-10)

Nueva página /buscar (botón BUSCAR del encabezado): la misma frase buscada por palabras (antes) y por palabras + significado (ahora) con las capturas de cada resultado, y búsqueda de personas por apariencia. La API de búsqueda declara UTF-8 para que los acentos no se rompan en algunos visores.

- **Búsqueda · Comparación lado a lado**: Dos columnas con el mismo texto, capturas y cámara; ejemplos con un clic.
- **API · UTF-8 declarado**: Content-Type: application/json; charset=utf-8 en /api/search/semantic.

## v2.24.1 — Búsqueda semántica sin esperas (2026-10-10)

Las consultas ya no esperan detrás del lote del indexador (bajó de ~10 s a un instante): el modelo se suelta entre lotes de 128, y no consulta internet al cargar una vez descargado.

- **Búsqueda · Lotes pequeños**: El indexador vectoriza en lotes de 128 textos o 32 imágenes liberando el modelo entre ellos.
- **Búsqueda · Indexado con pausas**: Lotes de 500 eventos con 3 s de pausa mientras haya pendiente (antes sin pausa).
- **Búsqueda · Consultas con prioridad**: El indexador cede el modelo mientras haya una consulta de usuario y la matriz de vectores se actualiza solo con las filas nuevas.

## v2.24.0 — Búsqueda semántica con EmbeddingGemma 2 (2026-10-10)

El Vision Agent y la API buscan eventos mezclando el índice de palabras con EmbeddingGemma 2 (P@10 0.45 → 0.58 y MRR 0.55 → 0.86 sobre 45,654 descripciones reales), y encuentran personas por cómo se ven en los recortes de cuerpo (P@10 0.16 → 0.29 por color de ropa). Corre en la GPU de Thor, fuera de la imagen.

- **Búsqueda · Híbrido FTS + embeddings**: GET /api/search/semantic?q=…&mode=hybrid|semantic|fts; el buscador del Vision Agent usa la mezcla y, si el modelo no está, cae al FTS de siempre.
- **Búsqueda · Personas por apariencia**: GET /api/search/semantic?kind=visit; el agente también devuelve recortes parecidos a 'persona con chamarra roja' (pista visual, no identificación).
- **Operación · Instalación fuera de la imagen**: Dependencias en data/pylibs-emb (scripts/install_embed.sh); EMBED_ENABLED=false lo apaga. Índice en la tabla emb_index; GET /api/search/status. Benchmark en docs/BENCHMARK-embeddinggemma2.md.

## v2.23.1 — Banner de armado absoluto en todas las pantallas (2026-10-09)

Mientras el armado absoluto está activo, una franja roja fija arriba de cada pantalla (panel, TV y vista de cámara) lo indica con la hora de activación, y el título de la pestaña empieza con 🔴 ARMADO.

- **Seguridad · Estado siempre visible**: Franja roja y título de pestaña; se actualiza cada 10 s sin recargar.

## v2.23.0 — Armado absoluto (2026-10-09)

Nuevo interruptor ARMAR en el encabezado: con el armado absoluto, toda persona, a cualquier hora y en cualquier cámara (empleados conocidos incluidos), es alerta, y cada visita que cierra genera su propia alerta con captura. El cambio de estado queda en la línea de tiempo y el estado sobrevive a los reinicios.

- **Seguridad · Interruptor global**: GET/POST /api/arming (normal | absoluto); botón ARMAR/ARMADO en el panel, con confirmación.
- **Seguridad · Sin excepciones**: Ignora horario, fin de semana, personas conocidas exentas y el amarillo de empleado conocido.
- **Línea de tiempo · Grupo Armado absoluto**: Alertas 'actividad_armado' (una por visita, con captura) y 'estado_armado' (activación/desactivación) en rojo.

## v2.22.3 — Mosaico de todas las cámaras en la alerta del garage (2026-10-08)

La alerta de apertura y cierre de una zona con varias cámaras (garage: frontal, posterior y exteriores) guarda un mosaico 2x2 con la vista de todas, con borde verde en las que ven una persona.

- **IoT · Mosaico de zona**: Un dispositivo con 2 o más cámaras en 'related' guarda una sola captura con todas; con una sola, se elige la que ve a la persona.

## v2.22.2 — Capturas de puertas desde la cámara que ve a la persona (2026-10-08)

Las alertas de apertura y cierre de puertas, ventanas y garage eligen, entre las cámaras de la zona, la que ve a una persona en ese instante (la puerta del garage no está en el campo de Garage frontal); el cierre también lleva captura y la duración sale en segundos cuando es menor a un minuto; y el tráfico de calle exige 6 detecciones.

- **IoT · Cámara que sí ve**: Se consulta YOLO en las cámaras 'related' del dispositivo y se captura la que tiene personas; si ninguna, la asignada.
- **IoT · Cierre con captura y segundos**: 'Se cerró garage (estuvo abierta 34 s)' en lugar de '0 min'.
- **Personas · Tráfico de calle más estricto**: 6 detecciones mínimas para mostrarse en la línea de tiempo.

## v2.22.1 — Plantas ya no cuentan como visitas y grabadora de cajas (2026-10-08)

En interiores, una detección breve (menos de 15 s) e inmóvil que se repite 3 veces en el mismo punto sin cara (una planta o un bulto) se marca falsa; y se guardan las cajas de persona de cada cámara por 7 días para repetir otros trackers sobre los mismos datos.

- **Personas · Fantasmas en interiores**: Escaleras entrada generaba una 'visita' de 4 s cada pocos minutos sobre una palma, y Garage frontal sobre un bulto en el piso.
- **Calidad · Grabadora de cajas**: data/eval/tracks-AAAAMMDD.jsonl con tiempo, cámara y cajas; sirve para comparar ByteTrack y BoT-SORT con los recorridos de prueba. EVAL_TRACK_LOG=false lo apaga.

## v2.22.0 — Conjunto de evaluación de personas (2026-10-08)

Nueva página /evaluacion: cada 20 minutos se guarda un cuadro por cámara con las cajas de persona de YOLO; tú marcas las falsas y las personas que faltaron, y el informe calcula precisión y recall por cámara. Sirve de base para comparar trackers y decidir si hace falta reentrenar.

- **Calidad · Etiquetado con clics**: Tocar una caja la marca falsa; arrastrar marca una persona no vista. Atajos: Enter guarda, S omite, Z deshace.
- **Calidad · Informe de precisión y recall**: GET /api/eval/report por cámara y total. Los cuadros sin personas se muestrean 1 de cada 4 para no inflar la muestra.

## v2.21.1 — Interruptores piden captura y empleado conocido en amarillo (2026-10-08)

Mover un interruptor reanaliza los últimos segundos de las cámaras de esa zona (para capturar la cara de quien lo movió), y una 'persona nocturna' que ya es un empleado con nombre sale amarilla como advertencia, no roja.

- **IoT · Interruptor → captura**: Cada luz tiene 'cams' en config/iot.yml (cocina, acceso site/jardín, taller con garage posterior, cowork, garage frontal); un cambio dispara la ráfaga de pre-roll en esas cámaras. 'ignore_codes' omite botones (la fuente del jardín).
- **Línea de tiempo · Empleado conocido en amarillo**: Nuevo grupo 'Empleado conocido (de noche)': la alerta sigue visible pero como advertencia.

## v2.21.0 — Enlace Tuya con ventana previa y menos visitas partidas (2026-10-08)

Un aviso de entrada Tuya ahora también cuenta una visita que ya estaba en cuadro hasta 20 s antes; la Tuya del garage (que apunta a las escaleras) se enlaza con Escaleras P2, Escaleras entrada y Garage frontal; y una persona ya no se parte en varias visitas por detecciones duplicadas o saltos del seguimiento.

- **Viajes · Ventana previa de 20 s**: Resuelve los falsos 'entrada sin captura' cuando la persona ya estaba en cámara al llegar el aviso (Recepción, garage).
- **Viajes · Varias cámaras por aviso**: config/topology.yml admite una lista; tuya-garaje y tuya-escaleras-p2 usan las cámaras de las escaleras.
- **Personas · Menos fragmentación**: Una visita necesita 4 detecciones (o una cara); se eliminan cajas duplicadas contenidas en otra y una detección sin pareja se une al track cercano visto hace menos de 4 s.
- **IoT · Registro completo**: /api/iot/events?sync=true&limit=500 devuelve todo, incluidas sincronizaciones.

## v2.20.6 — Menos visitas fantasma en la calle (2026-10-08)

Una detección de 1 a 3 cuadros en Exterior 1 o 2 ya no cuenta como visita aunque dure segundos, y la línea de tiempo exige al menos 4 detecciones para mostrar tráfico de calle (esas visitas salían sin captura).

- **Personas · Detecciones sueltas descartadas**: En exteriores, tracks con 3 detecciones o menos se marcan falsos; también se ocultan del histórico.

## v2.20.5 — Sensores de puertas: solo cambios de estado (2026-10-08)

Se acabaron los avisos repetidos 'lleva N min abierta'; cada puerta o ventana avisa al abrirse y al cerrarse, y el cierre anota cuánto duró abierta (en horas y minutos).

- **IoT · Sin avisos repetidos**: alert_open_minutes queda en 0 en config/iot.yml; poner un número reactiva el aviso periódico.
- **IoT · Duración al cerrar**: 'Se cerró … (estuvo abierta 5 h 20 min)'. Los avisos repetidos anteriores se ocultan del historial.

## v2.20.4 — Rostros sin deformar (2026-10-08)

Los recortes de rostro eran el cuadro del detector (no cuadrado) reducido a un cuadrado, así que se estiraban; ahora se recortan siempre cuadrados, rellenando el borde cuando la cara toca el límite del cuadro, y las miniaturas del panel no se estiran.

- **Personas · Recorte cuadrado**: Los rostros nuevos se guardan sin estirar. Los ya guardados no se corrigen solos (el recorte original no se conserva).
- **Panel · Miniaturas sin estirar**: Las imágenes de rostros y cuerpos usan 'cover' (rostros) y 'cover' alineado arriba (cuerpos) en los paneles.

## v2.20.3 — Reglas con nombres y definiciones completas (2026-10-08)

La página de reglas muestra nombres de dispositivos Tuya en vez de ids, nombra el grupo de sensores sin cámara y define los tipos que no genera el modelo.

- **Reglas · Nombres y definiciones**: Visita y timbre del videoportero, entrada sin captura, movimiento y ruido Tuya y sin tipo ya tienen definición y origen correctos; los dispositivos Tuya salen por su nombre.

## v2.20.2 — Dispositivos Tuya en la línea de tiempo de cada cámara (2026-10-08)

La vista de una cámara muestra también los sensores y cámaras Tuya de su zona; los tres sensores sin carril ya tienen el suyo; y los filtros de la línea de tiempo vuelven a empezar con todo visible.

- **Línea de tiempo · Tuya dentro de cada cámara**: Cocina muestra Puerta Comedor, Ventana y la cámara del jardín; Garage frontal la Tuya del garage y el abre-puertas; etc. (según 'related' de config/iot.yml).
- **Línea de tiempo · Carriles nuevos**: Oficina Abr Puerta Cristal, Cocina Ventana y Oficina Adm ventana tienen carril propio.
- **Línea de tiempo · Filtros reiniciados**: Los grupos Tuya y Sensores estaban ocultos por una preferencia guardada del navegador; se reinicia para que todo aparezca activado.

## v2.20.1 — Análisis de reglas guardados y tarjetas IoT parejas (2026-10-08)

'Analizar con el modelo' guarda cada análisis y lo vuelve a mostrar al abrir la página; las tarjetas de IoT tienen el mismo tamaño dentro de cada sección.

- **Reglas · Análisis guardados**: Cada análisis queda en la base con fecha, días y muestras; al abrir /reglas aparece el último y el botón dice 'Volver a analizar'. Historial en /api/rules/analyses?type=…
- **IoT · Tarjetas del mismo tamaño**: Cada sección (cámaras, puertas y ventanas, luces) es su propia cuadrícula con filas iguales y los botones alineados abajo, con áreas táctiles de 36 px.

## v2.20.0 — Control de luces y cortina desde la página IoT (2026-10-08)

Cada luz o interruptor (de 1, 2 o 3 botones, o foco) se enciende y apaga desde /iot, y la cortina se abre, detiene o cierra. El servicio de Tuya valida cada orden contra una lista permitida y registra quién y cuándo.

- **IoT · Botones por interruptor**: Los interruptores de 2 y 3 botones muestran un botón por cada uno; los focos y el enchufe uno solo.
- **IoT · Cortina**: Abrir, detener y cerrar la cortina S.J derecho.
- **Seguridad · Lista permitida y registro**: Solo luces y cortinas, solo códigos de interruptor, brillo y cortina, 1 orden por segundo por dispositivo; cada orden queda en control.log. Sin autenticación hasta la fase de hardening.

## v2.19.2 — Videoportero con visitas de 15 s y aviso rojo suave (2026-10-08)

Pasar frente al videoportero menos de 15 s ya no es visita (era el 83% de las 798 alertas de la semana, también en el histórico de la línea de tiempo), y el contador de personas cambia con un desvanecido en vez de saltar.

- **Videoportero · Visita desde 15 s**: El mínimo sube de 8 a 15 s; el timbre y las personas con nombre siguen alertando siempre. Las visitas viejas de menos de 15 s pasan a tráfico de calle.
- **Panel · Aviso rojo suave**: El texto del contador se desvanece al cambiar, marca ⚠ junto a la cámara y late con un brillo discreto.

## v2.19.1 — Línea de tiempo con carriles Tuya y eventos IoT sin ruido (2026-10-07)

Las alertas de puertas, garage y cámaras Tuya aparecen en su propio carril de la línea de tiempo, y la lista de eventos de IoT ya no muestra sincronizaciones ni interruptores internos.

- **Línea de tiempo · Carril por dispositivo Tuya**: Las alertas de sensores y cámaras Tuya van al carril del dispositivo, no al de la cámara de captura.
- **IoT · Eventos sin ruido**: Se ocultan las sincronizaciones y los códigos internos de luces; quedan los eventos reales y encendidos/apagados.

## v2.19.0 — Cámara Tuya de Escaleras P2 y sección de luces (2026-10-07)

La nueva cámara Tuya de las escaleras del segundo piso queda integrada (alertas de movimiento con captura de Escaleras P2 y aviso de viaje), y la página IoT agrupa cámaras, sensores y una nueva sección con el estado de las luces.

- **Tuya · Cámara Escaleras P2 (Tuya)**: Movimiento y ruido generan alerta con la captura de Escaleras P2; su aviso anticipa a esa cámara en los viajes.
- **IoT · Sección de luces**: Estado encendida/apagada de 9 luces e interruptores (solo lectura). No generan alertas; los cambios quedan en la lista de eventos.
- **IoT · Página agrupada**: Cámaras Tuya, puertas/ventanas/garage y luces en secciones separadas.

## v2.18.4 — Buscador del Vision Agent reparado y selector de identidades (2026-10-07)

El buscador devolvía error 500 por una cámara sin nombre; mover capturas ahora ofrece solo personas con nombre; el contador aparece sin duplicar la cámara; objetos diminutos lejanos ya no cuentan como personas en exteriores.

- **Vision Agent · Error 500 corregido**: Una cámara sin nombre rompía los hechos de búsqueda.
- **Identidades · Selector con nombres**: Al mover capturas se elige de una lista filtrable de personas con nombre.
- **Panel · Alerta sin duplicar**: El contador marca ⚠ junto a la cámara en vez de repetirla.
- **Personas · Cajas diminutas**: En exteriores, cajas de menos del 7% del alto del cuadro se descartan.
- **VLM · Textos limpios**: Las series de signos de interrogación en las descripciones se reducen a '…'.
- **Umami · 400 de sesión duplicada**: El identify y la vista de página creaban la misma sesión a la vez; ahora los eventos esperan 3 s a que la primera vista cree la sesión.

## v2.18.3 — Cowork sin sillas como personas y sin alertas de batería (2026-10-07)

Cowork valida personas con esqueleto (una silla cercana a la planta contaba como persona), se quita la línea de pantallas, y la batería baja de los sensores ya no es una alerta.

- **Personas · Cowork con esqueleto**: Las detecciones de YOLO sin esqueleto en Cowork se descartan (como Cocina y Sala de Juntas).
- **IoT · Batería baja ya no alerta**: Se ve en la tarjeta del sensor; deja de aparecer en la lista de alertas.
- **Panel · Sin línea de pantallas**: Se quita el análisis de monitores de Cowork del panel y se apaga en el servidor.
- **Personas · Parpadeos de YOLO**: Visitas de 1-3 cuadros en cámaras exteriores se descartan al cerrarse.

## v2.18.2 — Sin falsos positivos de árbol, Umami corregido y menos tráfico (2026-10-07)

Objetos fijos que YOLO toma por persona (un árbol en Exterior 2) ya no generan visitas; el error 400 de Umami se debía a una llamada mal formada; las cámaras fuera de pantalla no piden imágenes; la API ya no muestra 'nemotron' y las tarjetas IoT no muestran notas de viajes.

- **Personas · Objetos fijos descartados**: Si 3 o más tracks sin cara e inmóviles caen en el mismo punto de una cámara exterior en 6 h, se marcan como falsos (fp) y no generan alerta de tráfico ni consulta al VLM.
- **Umami · Error 400 resuelto**: identify() en Umami v2.15 recibe solo un objeto; se enviaba (id, datos). Ahora va un objeto con device_id.
- **Panel · Menos tráfico del navegador**: Las tarjetas fuera de pantalla o con la pestaña oculta no piden imagen; las del panel usan 800 px en vez de 1280.
- **API · Clave vlm**: /api/detections y el WebSocket exponen 'vlm' en lugar de 'nemotron'.
- **IoT · Tarjetas más limpias**: Se quitan las notas de topología y 'por confirmar'; los viajes siguen usando esos datos internamente.

## v2.18.1 — TV en filas fijas sin parpadeo, miniaturas ligeras y sensores sin eventos falsos (2026-10-07)

El muro /tv sigue el orden pedido y no parpadea; las listas cargan miniaturas reducidas (antes cientos de MB); la vista de cámara abre a 1920 px con 5 MP bajo demanda; y la sincronización de la nube ya no genera eventos ni alertas falsas de puertas.

- **TV · Filas fijas y sin parpadeo**: 3 columnas: Exterior 1, Exterior 2 y Videoportero; Garage frontal, Escaleras entrada y Acceso Site; Garage posterior, Cowork y Sala de Juntas; Escaleras P2 y Cocina. Cada imagen se precarga y se cambia al terminar. Las alertas inferiores piden datos sin caché y muestran la hora de actualización.
- **Rendimiento · Miniaturas ligeras**: Capturas y escenas aceptan ?w= (reducción al vuelo); la lista de la vista de cámara usa miniaturas de 160 px perezosas, 40 a la vez con Ver más. Antes cargaba todas las capturas completas (el inspector mostraba ~450 MB).
- **Cámaras · HD de 1920 px y 5 MP bajo demanda**: Las exteriores entregan 2592x1944 (5 MP) y enviar eso cada segundo era lo lento; ahora la vista abre a 1920 px y el botón 5 MP pide la nativa completa.
- **IoT · Sin eventos falsos ni cierres ocultos**: El estado de la nube que el puente publica cada 10 min se guarda como sincronización (no como evento ni alerta). Cerrar una puerta o ventana ahora deja una entrada informativa con cuánto estuvo abierta.
- **Analítica · Eventos de Umami sin ráfagas**: Un mismo evento se envía a lo más cada 2 s y se descartan valores vacíos o largos.

## v2.18.0 — Modo TV para Fire TV / Silk y modo ligero (2026-10-07)

Página /tv con un muro de cámaras ligero y navegable con el control remoto; el panel completo se abre solo en ese modo desde Silk o Fire TV, y los equipos con poca memoria usan 1 fps, miniaturas de 640 px y sin efectos.

- **TV · /tv: muro de cámaras para pantallas con control remoto**: Todas las cámaras en una cuadrícula (columnas según el ancho), miniaturas de 480 px a ~0.7 fps escalonadas, borde amarillo cuando hay personas y rojo si hay alerta, barra con estado, personas y reloj, y las últimas alertas abajo. Cada mosaico se enfoca con el D-pad y Enter abre la vista de esa cámara. Se recarga solo cada 6 h para liberar memoria.
- **TV · Detección automática**: Desde Silk, Fire TV y navegadores de TV el panel redirige a /tv (el botón Panel completo o ?full=1 lo evita). Hay también un icono Modo TV en el encabezado.
- **Rendimiento · Modo ligero**: Con poca memoria (deviceMemory ≤ 2), Silk o ?lite=1: sin desenfoques ni animaciones, snapshots a 1 fps y 640 px, encabezado sin posición fija e iconos en una sola fila. Los snapshots aceptan ?w= para miniaturas.

## v2.17.0 — Sensores Tuya con estado en vivo e integración con viajes (2026-10-07)

La página de IoT muestra cada sensor como tarjeta con icono y estado actual (abierta, cerrada, sin conexión), batería, función y cámaras relacionadas; las puertas y el garage anticipan entradas para los viajes.

- **IoT · Tarjetas con icono y estado**: Cada puerta, ventana, garage y cámara Tuya se ve con su icono (cambia a 'abierta'), estado en grande, hace cuánto cambió, batería y enlaces a las cámaras relacionadas. Recepción: inicia el viaje de Escaleras entrada; Puerta Comedor: conecta la Cocina con la Tuya del jardín; Garage: la Tuya del garage, Garage frontal y posterior, y Exterior 1 y 2.
- **IoT · Estado actual desde la nube**: El puente consulta a la nube de Tuya el estado de puertas y ventanas al arrancar y cada 10 min y lo publica como retenido (sin generar alertas), para que no queden en 'sin datos' hasta su siguiente cambio.
- **Viajes · Aperturas como aviso de entrada**: Abrir la puerta de recepción, la del comedor o el garage abre una ventana de 60 s esperando una visita en la cámara asociada; si no llega, queda 'entrada probable sin captura'.

## v2.16.0 — Viajes entre cámaras, Tuya del garage como aviso de entrada y recolección de marcha (2026-10-06)

Las visitas de una misma persona se enlazan entre cámaras en un viaje; si en algún punto se ve su rostro, las demás visitas quedan como sugerencia con confirmación en bloque. La cámara Tuya del garage anticipa la entrada y se recolecta, sin usarla aún, el vector de marcha a partir de la postura.

- **Seguimiento · Viajes**: Cada visita cerrada se enlaza al viaje más compatible según tiempo entre cámaras (mapa en config/topology.yml), cuerpo (ReID), ropa e identidad por rostro; si dos viajes son igual de compatibles no se enlaza. Identidades > Viajes muestra los recorridos con visitas por confirmar y permite aceptarlos o rechazarlos.
- **Tuya · Entrada anunciada por la Tuya del garage**: El movimiento de la cámara Tuya del garage abre una ventana de 60 s en la que se espera una visita en Garage frontal, reanaliza el pre-roll de esa cámara y, si no llega nadie, registra una alerta de baja severidad 'entrada probable sin captura'.
- **Marcha · Recolección de rasgos de marcha**: Para cada visita en movimiento de cámaras interiores se muestrean ~3 s a 12 fps del búfer, se estima la postura con YOLO26-pose en la GPU y se guarda un vector de 8 rasgos normalizado por el torso. No se usa para identificar: /api/gait/eval mide su AUC con visitas de rostro confirmado.
- **API · /api/journeys y /api/gait/eval**: Lista de viajes, aceptar/rechazar, estadísticas (visitas recuperadas por recorrido, entradas por Tuya) y evaluación de la marcha.

## v2.15.0 — Rostros: videoportero más sensible y mapa de dónde salen las mejores capturas (2026-10-06)

El detector de rostros del videoportero usa entrada de 960 px y umbral 0.45 (medido sobre recortes reales: 54% a 88% de detección), y cada visita guarda en qué parte del encuadre salió su mejor rostro para ver dónde conviene captar.

- **Reconocimiento · Detector del videoportero**: InsightFace con det_size 960 y det_thresh 0.45 solo en el videoportero (cámara de solo rostros, sin riesgo de falsos por gente de fondo). Las demás cámaras conservan 640/0.6 y la validación contra la persona de YOLO.
- **Capturas · Dónde salen las mejores capturas**: Cada visita guarda la posición normalizada de su mejor rostro. Identidades > Capturas muestra por cámara la calidad mediana/p90 y un mapa de calor del encuadre; /api/presence/capture-quality da los mismos datos.
- **Revisión · Máscara de Sala de Juntas retirada**: La máscara de privacidad de esa cámara se puso por una suposición equivocada el 30-sep y se quitó; la zona vuelve a verse y analizarse.

## v2.14.0 — Vehículos en polígono con placa, capturas por vehículo, Umami con identidad y móvil (2026-10-06)

Los vehículos del polígono (los que importan) generan alertas y se leen con OCR y, de respaldo, con el VLM; los demás quedan como informativos; cada vehículo muestra sus capturas de llegada y salida. Umami identifica el dispositivo y registra eventos con propiedades, y el panel se adapta solo al tamaño y al tacto.

- **Vehículos · En polígono vs informativos**: Todo vehículo quieto más de 3 min se registra; solo los dentro del polígono generan alertas (llegada, cada hora, salida) y lectura de placa. La tabla indica cuál es cuál.
- **Vehículos · Placa con OCR y VLM**: Si el OCR no lee la placa del recorte nativo tras 3 intentos, el VLM la lee del recorte; se acepta con dos lecturas iguales y formato de placa (letras y números).
- **Vehículos · Capturas de llegada y salida**: Al pulsar la miniatura del vehículo se abre el visor con las capturas de la llegada, de cada hora y de la salida.
- **Cámaras · Vista nativa**: La vista de cada cámara usa por defecto la resolución nativa (2592x1944 en las exteriores) con su rótulo; el botón Nativa la cambia por una vista ligera.
- **Analítica · Umami con identidad y eventos**: Cada dispositivo se identifica con un id estable y propiedades (nombre, sistema, navegador, formato, táctil); se registran eventos de uso con propiedades y el iframe de la línea de tiempo ya no cuenta como vista.
- **Interfaz · Móvil automático e indicadores**: El panel detecta tamaño y tacto (celular, tablet, escritorio) y ajusta encabezado, iconos y cuadrícula; TEMP y DISCO muestran el valor con su unidad en una línea, sin deformar la tarjeta. El timeline de eventos muestra fecha y hora.

## v2.13.1 — Exteriores a 5 MP, YOLO de mayor tamaño y timbres con captura (2026-10-06)

Exterior 2 pasa a 2592x1944 sin deformar la imagen, las cámaras exteriores detectan personas con YOLO a 1280 px, los timbres viejos muestran al visitante cercano y el encabezado queda centrado.

- **Cámaras · Exterior 2 a 2592x1944**: La cámara ya entrega 5 MP en su perfil H.264; la configuración declaraba 1920x1080 y la imagen 4:3 se aplastaba en 16:9. Ahora se declara la resolución real.
- **Detección · YOLO a 1280 px en exteriores**: Las personas lejanas (por ejemplo junto al portón) no se veían a 640 px. La GPU de Thor tiene holgura, por lo que las cámaras exteriores usan 1280 px (YOLO_HIRES_IMGSZ).
- **Línea de tiempo · Timbres sin captura**: Los timbres anteriores a la corrección muestran la captura del visitante que el videoportero registró a menos de 90 s.
- **Interfaz · Iconos centrados**: Segunda fila centrada, con botones más grandes, degradado y elevación al pasar el mouse.

## v2.13.0 — Alertas más limpias: timbre aparte, tránsito, mascotas conocidas y capturas con recuadro (2026-10-06)

Se separan en la línea de tiempo el timbre, las cámaras Tuya y los sensores; el videoportero distingue visitas de simple tránsito; se dejan de alertar mascotas conocidas, personal identificado en el site y personas con bicicleta; y las capturas de calle muestran el cuadro completo con el recuadro de la persona.

- **Línea de tiempo · Categorías separadas**: Timbre (propia), Cámaras Tuya (movimiento/ruido), Sensores (puertas, batería, consumo) y Tráfico calle y visitas. Las alertas de puerta de un sensor ya no se mezclan con las que ve la cámara.
- **Videoportero · Visita real vs tránsito**: Solo es visita (alerta) si hubo timbre cerca, dura 8 s o más, o es alguien con nombre; el resto es tránsito y queda como tráfico. Las 656 alertas anteriores de 0 s se reclasifican como tráfico en la línea de tiempo.
- **Alertas · Menos falsos positivos**: Mascotas reconocidas por el clasificador (Akamaru, Mojo, Gigi) ya no generan 'animal'; el personal identificado no dispara 'acceso no autorizado' en Acceso Site; una persona con bicicleta o moto vista desde arriba no se reporta como 'tirada en el suelo'. El evento guarda los objetos de YOLO (yolo_objects) para auditar.
- **Tráfico · Capturas con recuadro**: Las visitas de calle (Exterior 1/2 y videoportero) de 8 s o más, o con identidad, guardan el cuadro completo con el recuadro de la persona; la línea de tiempo lo muestra en lugar de solo la miniatura.
- **Identidades · Duplicados sin falsos pares**: Ya no se proponen como duplicados dos personas con nombres distintos, y 'No son la misma' descarta el par. En Asistencia, las personas sin nombre tienen 'Nombrar / unir'.
- **Vehículos · Polígono por área**: Verificado con el auto azul de Exterior 2: antes quedaba fuera (base fuera del polígono), ahora entra (61% de su área dentro).
- **Cámaras · Escaleras P2 a 1920x1080**: La cámara ya entrega H264 1920x1080 a 30 fps y la configuración declara esa resolución.

## v2.12.0 — Fase 1: identidad por cuerpo, color de ropa y validación por esqueleto (2026-10-06)

Cada visita cierra con un embedding de cuerpo (GPU) y el color de su ropa, y las que no tienen rostro reciben una sugerencia de quién podrían ser; las personas dudosas de YOLO se validan con un esqueleto (YOLO26-pose).

- **Reconocimiento · Identidad por cuerpo (sugerencias)**: Modelo de re-identificación de personas (ONNX en la GPU, ~3 ms) sobre la mejor captura de cuerpo de cada visita. Con rostro confirmado separa a las personas con AUC 0.73: es una señal moderada, por eso solo sugiere (parecido alto con alguien con nombre visto en las últimas 12 h y ropa compatible) y nunca asigna ni fusiona.
- **Reconocimiento · Color de ropa por visita**: Color de la parte de arriba y de abajo; se guarda con la visita y se muestra en la línea de tiempo de calle y en la galería.
- **Detección · Validación por esqueleto**: YOLO26-pose confirma las personas de YOLO en cámaras interiores (puntos del cuerpo visibles). En Cocina y Sala de Juntas descarta las que no tienen esqueleto (sillas, perros, reflejos que se contaban como persona); en el resto solo mide.
- **API · /api/presence/stats y backfill**: Porcentaje de visitas identificadas por rostro y con sugerencia por cámara, estadísticas de los modelos y procesamiento de visitas anteriores.
- **Cámaras · HD a resolución nativa**: La captura HD de la vista de cámara usa la resolución nativa (hasta 2560 px) también en las cámaras de 5 MP.

## v2.11.0 — Reglas de alertas, modo oscuro fijo y línea de tiempo sincronizada (2026-10-06)

Página de reglas con datos reales y análisis con el modelo, modo oscuro en todas las páginas, brillo que sigue al mouse y la lista de cada cámara sincronizada con su línea de tiempo.

- **Alertas · Página /reglas**: Por cada tipo de alerta: definición, cómo se genera, cámaras donde aplica, alertas reales, porcentaje de ruido según 👍/👎 y botón para que el modelo proponga una definición mejor, líneas de ignorar/enfocar y filtros programables. Nada se aplica solo.
- **Interfaz · Siempre oscuro**: Las páginas secundarias cambiaban a blanco con el modo claro del sistema; ahora son oscuras siempre.
- **Interfaz · Iconos con etiqueta y brillo**: Los iconos de la segunda fila llevan su nombre y las tarjetas y paneles tienen un brillo que sigue al mouse.
- **Cámaras · Lista sincronizada y Preguntar al agente**: Las alertas de la cámara siguen al rango y las categorías de su línea de tiempo (se retiran los controles duplicados); Preguntar al Vision Agent abre el panel principal con la pregunta.
- **Panel · Menos ruido en el timeline de eventos**: Por defecto solo alertas de las categorías activas; las observaciones sin alerta se activan con una casilla.
- **Vehículos · Polígono por área**: Un vehículo cuenta dentro del polígono si su base o al menos 35% de su área está dentro. Si no hay vehículos estacionados, la ventana muestra el historial.

## v2.10.1 — Barra superior, línea de tiempo por cámara y horario habitual (2026-10-06)

Encabezado en dos filas, indicador de personas con la cámara que las ve o alerta, línea de tiempo completa dentro de cada cámara y horarios habituales aprendidos del historial.

- **Panel · Barra superior en dos filas**: Métricas, IA, personas y reloj (a la derecha) arriba; todos los iconos en una segunda fila.
- **Panel · Indicador de personas con cámara**: Muestra en qué cámaras hay personas y, en rojo, cuál está generando una alerta.
- **Cámaras · Línea de tiempo completa por cámara**: La vista de cámara incrusta la línea de tiempo del panel (zoom de 15 min a 7 d, selección de rango, categorías) filtrada a esa cámara. La captura HD lleva nombre y hora.
- **Histórico · Capturas con visor**: En el histórico de eventos la captura se abre en el visor (con X) en vez de otra pestaña.
- **Asistencia · Horario habitual aprendido**: Por empleado se calcula la hora a la que suele llegar y salir (mediana de 30 días, entre semana y fin de semana) y se marca si debería estar o se quedó tarde, sin capturar horarios a mano.

## v2.10.0 — Galería de miniaturas, sugerencias con selección y línea propia por cámara (2026-10-06)

Se pueden validar y corregir las miniaturas de cada persona y mascota, las sugerencias de identidad se revisan y se unen por selección con opción de rechazo, y cada cámara tiene su propia línea de tiempo.

- **Identidades · Galería /galeria**: Todas las miniaturas de una persona (o de cada mascota, etiquetadas y sugeridas por el modelo) con selección múltiple: mover a otra identidad, sacar a una nueva, eliminar o reetiquetar. El rostro promedio se recalcula al mover.
- **Identidades · Sugerencias seleccionables y "No es"**: Nueva pestaña Sugerencias con las parejas parecidas (foto contra foto), selección múltiple y Unir seleccionadas; "No son la misma" (también en cada tarjeta) descarta la sugerencia para siempre. Se retira el botón que unía todo de golpe.
- **Identidades · Nombres repetidos se unen solos**: Al poner un nombre que ya existe, ambas identidades se funden.
- **Cámaras · Línea propia, HD y navegación**: La página de cada cámara tiene su línea de tiempo (histograma y puntos por tipo, clic abre la captura), imagen HD y el visor navegable. En el visor principal ← → recorren las alertas de la misma cámara y ↑ ↓ todas; las capturas se amplían.
- **Ocupación · Menos ruido**: Las personas sin clasificar con visitas menores a 8 s ya no cuentan en la ocupación.

## v2.9.1 — Capturas del timbre, Tuya recuperado y vista por cámara (2026-10-06)

Se corrige el fallo que dejaba sin captura al timbre y detuvo los eventos de Tuya, y se agrega una página por cámara con sus alertas navegables con el teclado.

- **Corrección · Timbre sin captura y hilo MQTT caído**: peek_latest devuelve un FrameEntry y se trataba como imagen: el timbre quedaba sin captura y, al revisar una puerta abierta, el hilo MQTT de Tuya moría en silencio y dejaba de recibir eventos. Se corrige, y los errores al procesar un mensaje ya no matan el hilo.
- **Línea de tiempo · Visor con navegación**: El visor de capturas avanza con ← → (todas las alertas visibles en orden de tiempo) y ↑ ↓ (solo la misma cámara), con botones y deslizando con el dedo. Las alertas sin captura se muestran con el aviso en lugar de no abrir.
- **Cámaras · Vista de una sola cámara (/camara/<id>)**: Imagen en vivo grande y las alertas de esa cámara con filtro por rango y tipo y el mismo visor navegable. Se abre con doble clic en la tarjeta, con el botón ⤢ (para el móvil) o desde la cámara en Identidades.
- **Identidades · Cámaras como enlace y últimas características**: Las cámaras de cada persona abren su vista, y la tarjeta muestra la última descripción del VLM (ropa y rasgos).

## v2.9.0 — Correlación de eventos, tráfico de calle y limpieza de ruido (2026-10-04)

El timbre, las cámaras Tuya y los autos estacionados ahora dejan captura; el tráfico de la calle y el videoportero queda como registro separado de la ocupación interior, y las personas nuevas se sugieren contra las ya identificadas.

- **Videoportero · Sin ruido y con captura**: Se dejan de guardar los latidos del equipo (registro SIP y hora NTP, ~1,400 filas al día). El timbre toma una captura del propio videoportero (snapshot.cgi) cuando el flujo aún no trae cuadro.
- **Tuya · Movimiento y ruido con alerta y captura**: Los mensajes de las cámaras Tuya se decodifican (movimiento / ruido fuerte), ya no se guardan duplicados y cada cámara genera una alerta con captura (una por 120 s). La captura usa el servicio de video y respeta el medidor de consumo.
- **Alertas · Autos estacionados con captura**: Las alertas de llegada, permanencia cada hora y salida de un vehículo guardan la captura de la cámara.
- **Línea de tiempo · Categorías nuevas, todas/ninguna y tráfico de calle**: Botón Todas/Ninguna para afinar las categorías; categorías renombradas y nuevas (Tráfico calle y videoportero, Tuya y sensores). Las personas de Exterior 1 y 2 quedan como registro. Las descripciones repetidas se colapsan en la lista.
- **Identidades · Calle separada, sugerencias y asistencia real**: Quien solo pasó por la calle o el videoportero va a la pestaña Calle y no a Por revisar; quien entra pasa a revisar con la sugerencia de a quién se parece y un botón para unir las coincidencias. La asistencia ya no cuenta el videoportero como presencia y marca la salida cuando lo ven en la calle.
- **API · /api/correlation**: Todo lo ocurrido alrededor de un momento o alerta (timbre, personas, alertas, puertas, cámaras Tuya) en orden.
- **ONVIF · Certificados propios y videoportero**: Las cámaras con certificado propio (Escaleras P2) ya responden en la prueba ONVIF y el videoportero ya no aparece duplicado con un nombre sin resolver.

## v2.8.0 — IoT Tuya: puertas, cámaras y consumo de video (2026-10-04)

thor-vision recibe los cambios de puertas, ventanas y garage de Tuya por MQTT (solo lectura) y alerta con captura; las cámaras Tuya se pueden ver bajo demanda con un medidor de consumo de la cuota de video de la nube.

- **IoT · Puertas, ventanas y garage (Tuya por MQTT)**: Un puente en oapc-devops recibe los cambios de estado (servicio de mensajes de Tuya y conexión local con los dispositivos con IP: abre-puertas de garage y cámaras) y los publica en el broker MQTT; thor-vision los guarda, muestra su estado en /iot y alerta con captura de la cámara asociada cuando algo se abre, si sigue abierto más de 10 minutos y cuando la batería del sensor baja del 15 %. Todo de solo lectura.
- **IoT · Video de las cámaras Tuya con medidor de consumo**: En /iot cada cámara Tuya tiene captura bajo demanda y video en vivo (hasta 3 minutos por sesión). Un medidor muestra el consumo estimado de la cuota mensual de la nube (5 GB), avisa al 50, 80 y 95 % (alerta en thor-vision) y bloquea nuevos flujos al 100 %. El estimado se calibra con el uso real del portal de Tuya.

Notas:
- Pendiente: probar con un evento real de movimiento y de puerta (aún no llega ninguno).
- Pendiente: confirmar la cámara asociada a cada sensor (config/iot.yml).

## v2.7.0 — Videoportero con alertas y conocimiento del agente (2026-10-03)

El videoportero genera alertas (visitas con rostro y, con la cuenta admin, eventos nativos del timbre) y el Vision Agent consulta un conocimiento de la casa editable en lugar de datos escritos a mano en su prompt.

- **Videoportero · Alerta por cada visita con rostro**: Cuando alguien se detiene frente al videoportero y se captura su rostro, se guarda una alerta (visita_videoportero) con la captura, enlazada a la persona detectada; aparece en la línea de tiempo, el chat y los reportes.
- **Videoportero · Eventos nativos de Dahua (timbre, llamada, puerta)**: Con la cuenta admin se escucha el canal propio del equipo (eventManager attach), porque el ONVIF del videoportero no anuncia el timbre. Todo evento se guarda por 90 días y los códigos de timbre o llamada (DOORBELL_CODES) generan una alerta con captura. Los códigos exactos se afinan con una prueba real.
- **Vision Agent · Conocimiento de la casa (/conocimiento)**: Un markdown editable con las mascotas (Akamaru shiba, Mojo-jojo negro, Gigi shar pei), reglas de personas y de cámaras, más datos vivos (cámaras y zonas), se entrega como resultado de búsqueda con prioridad. Sustituye al dato escrito a mano en el prompt que seguía diciendo que Gigi era un pug.

Correcciones:
- El prompt del Vision Agent ya no trae razas ni nombres escritos a mano: el agente debe tomar los hechos de la casa de los resultados 'Conocimiento de la casa'.

Notas:
- Pendiente: confirmar con una pulsación real los códigos de timbre de Dahua.
- Pendiente: puerta Tuya (192.168.10.205, puerto 6668 abierto): requiere el id y la clave local del dispositivo para escuchar su estado y vincularlo con las cámaras.

## v2.6.0 — Gestión de dispositivos y placas (2026-10-03)

Cada marca de cámara se puede consultar y administrar desde el dashboard (Hanwha por SUNAPI, Dahua y cualquier equipo ONVIF por ONVIF), siempre en solo lectura por defecto, con confirmaciones y bitácora. Los vehículos estacionados dentro de las zonas ahora se leen solos (placa).

- **Dispositivos · Gestión de dispositivos (/dispositivos)**: Lista todos los equipos con marca, modelo y firmware, y para cada uno ofrece Resumen, Gestión y Auditoría. Hanwha: el árbol completo de funciones que publica la propia cámara (sistema, red, seguridad, video, imagen, eventos, grabación…), con lectura de cada una y formularios de modificación generados desde los tipos declarados por el equipo (listas, rangos, sí/no). Dahua y otros ONVIF: red, DNS, NTP, nombre, perfiles de video, puertas y relés, con acciones (NTP, perfil de video, reiniciar, abrir puerta).
- **Dispositivos · Seguridad de la gestión**: Todo equipo arranca en solo lectura; para modificar hay que activar el modo gestión del equipo. Las acciones sensibles (red, usuarios, reinicio, perfiles de video que Thor consume, abrir puerta) piden escribir el id del equipo; restaurar de fábrica, actualizar firmware, copias de configuración y certificados nunca se permiten desde aquí. Los valores se validan contra los tipos del propio equipo y toda acción queda en una bitácora con los secretos enmascarados.
- **Dispositivos · Inventario de firmware**: El resumen muestra modelo, firmware y hora de cada equipo y avisa cuando el firmware es antiguo: hoy 8 de las 10 cámaras son LND-6010R con firmware de 2020.
- **Vehículos · Placas de vehículos estacionados**: Dentro de las zonas de estacionamiento, cada vehículo registrado se lee una vez por minuto (hasta 20 lecturas) sobre el recorte nativo y se vota entre lecturas; lo escrito a mano siempre manda. Se guarda el texto, la confianza, el origen (auto o manual) y el recorte de la placa, y la placa aparece en las alertas del vehículo. El lector (fast-alpr) se instala en el volumen de datos con scripts/install_alpr.sh, sin tocar la imagen.

Correcciones:
- El escucha ONVIF del videoportero se caía tras 8 segundos sin eventos; ahora espera más que la consulta del equipo.
- El videoportero ya no aparece duplicado como cámara Hanwha en la gestión de dispositivos.

Notas:
- Probado en vivo solo en lectura; las modificaciones están cubiertas por pruebas y por la política, pero no se aplicó ninguna a un equipo real.
- Pendiente: probar placas con un auto estacionado dentro de la zona (a 43 px de ancho la lectura de un auto lejano de noche sale con baja confianza).
- Pendiente: el control de puertas del videoportero respondió con un error al listar puertas; hay que revisar el servicio en el equipo.

## v2.5.0 — Zonas, ONVIF y videoportero (2026-10-03)

Las zonas por cámara se dibujan desde el dashboard, hay un operador ONVIF para las cámaras y el videoportero, el videoportero entra al dashboard como cámara de solo rostros, los vehículos estacionados generan alertas y el reporte usa el skill optimizado.

- **Zonas · Zonas por cámara (/zonas)**: Se dibujan polígonos sobre la imagen de cada cámara con cuatro tipos: ignorar objetos (falsos positivos fijos como una palmera), ignorar todo, estacionamiento (solo se registran vehículos dentro) y descripción fija para el VLM. Se guardan en data/zones.json y se aplican en segundos, sin reiniciar.
- **Vehículos · Alertas de vehículos estacionados**: Cada vehículo estacionado dentro de una zona de estacionamiento genera una alerta al llegar, otra cada hora mientras siga ahí (con el tiempo acumulado) y otra al irse. Son eventos con alerta del tipo vehiculo_detenido y metadato del vehículo (id, clase, duración, placa si se capturó), así que salen en la línea de tiempo, el chat y los reportes.
- **ONVIF · Operador ONVIF (/onvif)**: Lista las cámaras y equipos agregados (videoportero), sondea modelo, firmware, hora, perfiles de video con su URI de stream, servicios y usuarios, y escucha eventos de forma continua (timbre, puerta, movimiento) guardándolos por 90 días. El usuario ONVIF de cada equipo solo se guarda si el equipo lo acepta y nunca sale por la API.
- **Videoportero · Videoportero como cámara de solo rostros**: El Dahua VTO entra al dashboard como cam-vto (1920x1080, 30 fps, decodificación por hardware) con detección de rostros únicamente: sin detección de objetos, VLM ni movimiento nativo. cameras.yml no lleva claves: apunta a onvif://vto/<perfil> y la clave se resuelve del operador ONVIF, así que la rotación aplica sola.
- **Reportes · Skill de reportes optimizado (v2)**: El reporte usa el skill de la ronda 2 de SkillOpt: acierto 0.89 contra 0.59 del original en 22 casos no vistos, con reportes más cortos y completos.
- **Dashboard · Resumen del pipeline en Conexiones**: El resumen de llamadas a Qwen pasó a la ventana de Conexiones a las cámaras y se quitaron los textos sobre credenciales.

Correcciones:
- El chat clásico del dock se retiró (usaba una ventana fija de 6 h y no la búsqueda por descripción).
- Las coincidencias de la búsqueda del Vision Agent enlazan a la captura real del evento cuando existe.

Notas:
- Pendiente: mapear los eventos del timbre y la puerta del videoportero y guardar las llamadas con captura.
- Pendiente: lectura de placas solo para vehículos estacionados dentro de las zonas dibujadas (prueba de factibilidad hecha; falta un auto en la zona para validar).
- Pendiente: gestión remota de cámaras por ONVIF (hora, red, perfiles de video, reinicio) con confirmación y bitácora.
- Descartado: el conteo de personas de Hanwha (WiseAI objectcounting) no existe en las cámaras probadas (404).

## v2.4.0 — Identidades, búsqueda y calidad (2026-10-03)

La gestión de personas pasa a una página propia (/identidades) con bandeja por revisar, empleados, invitados, ocupación, asistencia y duplicados. El Vision Agent ahora encuentra eventos por su descripción, y el repositorio gana pruebas y reglas automáticas que evitan que vuelvan los fallos que ya tuvimos.

- **Personas · Página propia: Identidades**: Reemplaza los modales mezclados por una página /identidades con pestañas: Por revisar, Empleados, Invitados, Ocupación, Asistencia y Duplicados. Cada persona se clasifica con un clic o en lote, se nombra con ✎ y muestra sus parecidos con el botón 'Es la misma'. El resumen del día (empleados presentes, invitados, por revisar) queda arriba.
- **Personas · Mismos nombres se unifican**: Las identidades con el mismo nombre se fusionan en una sola (conserva la categoría más fuerte y el historial) con un botón en Duplicados. La asistencia agrupa por nombre.
- **Personas · Cámaras exteriores sin ruido**: Exterior 1 y 2 siguen reconociendo a quien ya está registrado, pero no crean identidades nuevas ni cuentan en la asistencia; su actividad queda como alertas. El Garage frontal (acceso peatonal) sí cuenta para llegada y salida.
- **Ocupación · Pestaña de ocupación**: Personas ahora en cámara, empleados presentes con minutos promedio, pico del día y personas distintas por hora (empleados, invitados, por revisar). Solo cámaras interiores y Garage frontal; ignora visitas de pocos segundos sin identidad.
- **Vision Agent · Búsqueda por descripción**: Un índice de texto (FTS5, dentro de la misma events.db) permite preguntar 'persona con bolsa naranja' o 'perro con correa en Exterior 1' y obtener las capturas con cámara y hora. En una prueba de 24 preguntas la búsqueda por palabras acertó más (0.80 de precisión en los 5 primeros) que los embeddings multilingües (0.62).
- **Datos · Retención de capturas a 90 días**: Las capturas de rostro y cuerpo se borran a los 90 días en todas las categorías (CAPTURE_TTL_DAYS); el registro y la asistencia de los empleados se conservan.
- **Calidad · Pruebas y reglas automáticas en el CI**: 28 pruebas de contrato sobre lo que ya falló en producción, un tope que impide agregar excepciones genéricas o silenciosas (la deuda actual está fijada y solo puede bajar) y reglas ast-grep contra llamadas HTTP sin timeout y consumidores de frames sin tope.
- **Versiones · Versión sincronizada con git**: La versión que muestra el dashboard se toma de la primera entrada de release_notes.json; un chequeo del CI exige que el CHANGELOG esté regenerado y, al publicar un tag vX.Y.Z, que coincida con la versión y crea el GitHub Release con estas notas. El dashboard muestra además el commit desplegado.

Correcciones:
- La limpieza por antigüedad de face_sightings tenía las comillas mal en 'Desconocido' y 'Sin rostro' y podía fallar la rotación.
- La ocupación 'ahora' contaba visitas que quedaron abiertas tras reiniciar; ahora solo cuenta lo visto en los últimos 2 minutos.

Notas:
- Pendiente: zonas por cámara (ignorar, estacionamiento con placa, descripción fija) y aprendizaje de los descartes de mascotas.
- Pendiente: videoportero Dahua y sensor de puerta (issue aparte).
- Pendiente de decisión: Ollama usa la CPU por una variable del servicio (cuda_jetpack6 en lugar de cuda_v13); afecta a NormaAI.

## v2.3.0 — Personas: empleados e invitados (2026-10-02)

Las personas detectadas se reparten en tres bases (por revisar, empleados de OXM e invitados), con búsqueda de parecidos y la base del reporte de asistencia. Las mascotas ya tienen nombre y el sistema aprende a reconocerlas.

- **Personas · Tres bases: por revisar, empleados e invitados**: Toda persona nueva queda automáticamente 'por revisar'. Desde el modal de clasificación se deriva a Empleado u Invitado con un clic, o se regresa a revisión. Las personas que ya tienen nombre aparecen con la sugerencia 'Empleado'. Las categorías aplican a todas las cámaras.
- **Personas · Parecidos y fusión**: Cada persona muestra quiénes se le parecen, con porcentaje y botón 'Es la misma', y hay una vista de pares dudosos. Se encontraron 2 pares de 70 % o más y 31 entre 55 y 70 %: sujetos existentes que el sistema nunca volvió a comparar entre sí.
- **Asistencia · Base del reporte de asistencia**: /api/attendance y /api/attendance.csv calculan por día, solo para empleados y con las cámaras interiores, la primera y última vez que se vio a cada uno, los minutos en la oficina (huecos menores a 10 minutos cuentan como presencia) y las cámaras. A los invitados solo se les reportan cantidades y capturas.
- **Datos · Retención por categoría**: Los empleados no caducan. Los invitados se conservan 90 días (GUEST_TTL_DAYS) y las personas por revisar sin nombre 30 días. El recorte automático por tope de visitas ya no borra empleados ni invitados.
- **Mascotas · Akamaru, Mojo-jojo y Gigi**: Las tres mascotas tienen nombre propio en el etiquetado y en las reglas de escena (Akamaru es el shiba café, Mojo-jojo el perro negro pequeño y Gigi el shar pei, más grande que Akamaru).
- **Mascotas · Entrenar y sugerir**: Con las etiquetas puestas, el botón 'Entrenar y sugerir' entrena un clasificador sobre las características de una red ImageNet, mide su acierto con validación cruzada y propone etiqueta para el resto de los recortes, para que solo se confirme. El ajuste fino completo llega con unas 60 etiquetas.

Notas:
- Videoportero: identificado como Dahua VTO (el panel exterior y dos monitores VTH interiores). La captura del timbre queda pendiente de credenciales de un usuario dedicado en el equipo.
- Pendiente: consolidación automática de sujetos duplicados y el reporte de asistencia con interfaz propia.

## v2.2.0 — Vision Agent, vehículos y mascotas (2026-10-02)

El asistente pasa a ser Vision Agent completo (Morphic en español sobre tus cámaras), YOLO reconoce objetos y se estrenan el registro de vehículos estacionados y el etiquetado de mascotas.

- **Vision Agent · Asistente completo sobre tus datos**: El botón del chat abre Morphic auto-hospedado, en español, con fuentes citadas, capturas, preguntas relacionadas e historial. No busca en la web: consulta alertas, personas, capturas y vehículos de THOR. Los botones de ejemplo (Alertas, Personas, Cámaras, Capturas, Reportes) son preguntas que el sistema sabe responder.
- **Visión · YOLO reconoce objetos, no solo personas**: En la misma pasada detecta autos, motos, camiones, autobuses, bicicletas, perros, gatos, mochilas, bolsos y maletas, sin costo extra medible. Las cajas quedan guardadas y se le dan al VLM como pista para describir mejor la escena. Se apaga con YOLO_OBJECTS_ENABLED=false.
- **Alertas · Menos falsas 'persona en el suelo'**: Una captura reveló que el VLM inventaba personas caídas en una oficina vacía. Ahora la alerta solo pasa si YOLO ve una persona con forma horizontal; si no, se descarta y queda la descripción.
- **Vehículos · Registro de vehículos estacionados**: En Exterior 1 y 2, todo vehículo quieto más de 3 minutos queda registrado con miniatura, hora de llegada, permanencia y estado (estacionado o se fue). La placa se puede capturar a mano; la lectura automática no está incluida porque solo es viable en los 1 o 2 lugares más cercanos de la cámara de 5 MP.
- **Mascotas · Etiquetado de las tres mascotas**: El sistema recolecta recortes de perros y gatos y ofrece una página para etiquetarlos (negro, shiba, pug, otro). Con unas 15 etiquetas por mascota se podrá entrenar el reconocimiento, para que la alerta de animal solo salte con perros desconocidos.
- **Línea de tiempo · Curvas dinámicas**: El carril General dibuja áreas apiladas suaves por tipo de alerta, con el tramo en curso punteado, burbuja con el valor al pasar el mouse y animación al cargar.
- **Reportes · Reportes completos**: El tope de 900 tokens cortaba los reportes de periodos con muchas alertas y se perdían las secciones 3 y 4. Subió a 1,600 tokens con 120 s de espera.
- **Plataforma · Versión y notas de release**: La versión actual aparece en la barra inferior; al pulsarla se abre este historial con lo que trae cada release.

Correcciones:
- El entrenamiento de prompts ya no puntúa como cero una falla del gateway: reintenta con espera y se detiene.
- La cola de entrenamiento comprueba que el gateway y el cupo de Claude respondan antes de empezar.

Notas:
- Pendiente: lectura automática de placas, reconocimiento de mascotas por nombre, y dibujar cajas sobre las detecciones solo cuando existan.

## v2.1.0 — Reglas por cámara y asistente con memoria (2026-10-01)

Cada cámara tiene su propio horario y criterio de alerta, los reportes y el chat usan cifras exactas de la base y aparece la línea de tiempo de alertas.

- **Cámaras · Vista de conexiones**: El botón de conexiones muestra por cámara la IP, el puerto y la ruta RTSP (sin credenciales), el decodificador usado, la resolución nativa, el estado, los fps y el estado de SUNAPI (movimiento y snapshots).
- **Cámaras · Rótulo, fecha y hora en la imagen**: Las 10 cámaras muestran su nombre real, fecha y hora grabados en el video. La hora sincroniza por NTP.
- **Reglas · Horarios y criterios por cámara**: Sala de Juntas alerta solo de 00:00 a 06:00 entre semana y no los fines de semana; Cocina de 00:00 a 07:00; Escaleras de entrada vigila merodeo y actividad en fin de semana; Acceso Site alerta con cualquier persona. En exteriores un auto solo alerta si lo fuerzan, no por recargarse en él.
- **Reglas · Personas y mascotas conocidas**: En cocina y garages las personas con nombre reconocido por rostro no generan alerta nocturna ni de acceso, y las tres mascotas de la casa no generan alerta de animal.
- **Alertas · Uso de teléfono en exteriores**: Nueva alerta cuando alguien mira o usa su teléfono afuera, aunque a lo lejos solo se vea una luz en la mano.
- **Reportes y chat · Hechos exactos del periodo**: Los conteos por cámara, tipo de alerta, personas y visitas se calculan en la base, con comparación contra el periodo anterior, y el modelo solo redacta. Ya no se cortan en 200 alertas ni se inventan cifras.
- **Chat · Memoria por sesión y capturas**: El chat recuerda la conversación, entiende 'esas alertas', adjunta las capturas de las alertas con las personas conocidas presentes y acepta rangos escritos como 'entre 21:00 y 22:00'.
- **Modelos · DeepSeek para chat y reportes**: El chat y los reportes usan DeepSeek con su propia llave del gateway; el análisis de cámaras sigue con Qwen.
- **Vision Agent · Chat en streaming con evidencia**: Respuesta que aparece mientras se genera, con proceso consultado, evidencia citada [E#], capturas, preguntas relacionadas y calificación 👍/👎.
- **Línea de tiempo · Alertas por cámara**: Carril general y un carril por cámara con zoom +/−, desplazamiento con las flechas y selección de un rango para preguntarle al asistente.
- **Interfaz · Chat flotante, íconos y accesibilidad**: El chat es un botón flotante desplegable; los íconos son SVG, hay foco visible, se respeta 'reducir movimiento' y Esc cierra los modales. Se agrega una animación que explica el pipeline.
- **Plataforma · Repositorio y CI**: El código se sincroniza con GitHub con revisión por PR, escaneo de secretos y chequeo estático, y las variables NEMOTRON_* pasan a VLM_* conservando las anteriores.

Correcciones:
- cam-191 volvía a FFmpeg por software tras una caída y no regresaba a NVDEC: ahora reintenta.
- La detección de caras del flujo en vivo fallaba en silencio por un nombre sin definir (face_yaw).
- La alerta 'fuera de horario' se disparaba de día.

## v2.0.0 — Personas, escenas y movimiento nativo (2026-09-30)

El sistema deja de analizar a ciegas: usa el movimiento de la propia cámara, entiende cada escena y sigue a las personas con identidad entre visitas.

- **Cámaras · Movimiento nativo por SUNAPI**: El detector de movimiento de las cámaras Hanwha disparó el análisis en las 10 cámaras. El 67 % de las llamadas al modelo eran latidos sin actividad; ahora el latido es cada 15 minutos.
- **VLM · Prompt y alertas por cámara**: config/scenes.yml define dónde está cada cámara, qué importa, qué ignorar y qué alertas permite. El resultado se valida y se sanea, y no se llama al modelo si YOLO no vio una persona.
- **Personas · Visitas e identidades**: Cada persona seguida en una cámara es una visita con su mejor rostro y cuerpo. Los rostros se agrupan en sujetos 'Persona #N' que se pueden renombrar, fusionar o borrar, con modal de Personas e identidades.
- **Captura · Pre-roll y análisis retroactivo**: Se guardan 6 segundos de cuadros a resolución nativa por cámara; al detectar a alguien se reanalizan los cuadros anteriores para recuperar el momento real de llegada.
- **Rostros · Caza de rostros**: Mientras una persona no tiene buen rostro, YOLO sube a 4 fps y se piden snapshots completos a la cámara, filtrando por nitidez y pose.
- **Descripciones · Descripciones siempre descriptivas**: Se acabó el 'Sin actividad relevante': cada evento describe la escena aunque no haya alerta, y el VLM recibe un recorte ampliado de las personas.
- **Datos · Retención de datos biométricos**: Los datos de personas sin nombre caducan a los 30 días; los nombrados se conservan.
- **Dashboard · Indicadores y medidores**: Cada tarjeta muestra movimiento, YOLO y última consulta al VLM. El medidor de GPU usa nvidia-smi y el de CPU el uso real.

Correcciones:
- El VLM fallaba 9 % de las veces por exceder el contexto: el video se reescala a 640x360.

Notas:
- Nemotron se renombra a VLM en el código; los tipos guardados en la base se conservan.

## v1.2.0 — Captura por hardware y rostros (2026-09-15)

La decodificación pasa al hardware del Jetson y el sistema empieza a recordar rostros y a clasificar alertas.

- **Captura · GStreamer con NVDEC**: La decodificación de video pasó de FFmpeg por software a hardware: el CPU del contenedor bajó de 434 % a 47 %.
- **Captura · Resolución nativa**: Las 10 cámaras se procesan a su resolución real; cam-113 recibe su formato vertical.
- **Rostros · Miniaturas e historial**: Los rostros detectados se guardan como miniatura y hay un historial consultable.
- **Alertas · Triage 👍/👎 y reportes**: Cada alerta se puede marcar importante o ruido, y un reporte generado por IA resume el periodo usando ese criterio.
- **Observabilidad · Consumo de IA en Grafana**: El dashboard separa errores del gateway por código y muestra latencias.

Correcciones:
- Los fps reportados eran ráfagas y no la tasa real; ahora se calculan en una ventana.
- Rostros falsos (cuadros y reflejos): se exige confianza del detector y una persona de YOLO debajo.
- El video enviado al VLM se limita a 10 cuadros.

## v1.1.0 — Dashboard operativo (2026-09-11)

El dashboard gana configuración, histórico y chat con memoria del pasado.

- **Dashboard · Rediseño y panel de configuración**: Nuevo diseño verde NVIDIA, histórico con filtros por rango y cámara, grid movible y línea de eventos al fondo.
- **Chat · Chat con histórico real**: La ventana de tiempo se infiere de la pregunta; hasta 12 horas se lista evento por evento y más allá se resume.
- **Cámaras · Nombres y zonas reales**: Las cámaras tienen nombre y zona, y Cowork suma un pipeline de detalle de pantallas.
- **Auditoría · Quién consulta el sitio**: Cada acceso al dashboard queda registrado con IP y navegador, además de la analítica con Umami.
- **Modelos · Gateway LLM**: El análisis pasa por un gateway con reintentos ante fallas transitorias.

## v1.0.0 — Lanzamiento inicial (2026-06-05)

Primera versión publicada de THOR Vision.

- **Captura · RTSP multicámara**: Un hilo por cámara, con buffer circular en memoria.
- **Análisis · Escena con modelo multimodal**: El modelo solo se consulta cuando hay movimiento.
- **Dashboard · Cuadrícula en vivo y chat**: Estado de las cámaras, snapshots y preguntas en lenguaje natural.
- **Datos · Persistencia y retención**: Eventos y snapshots en SQLite y disco, con borrado automático.

Notas:
- Autor del lanzamiento inicial: Diego Reyes.
