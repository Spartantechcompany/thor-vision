# Propuesta: detección continua + alertas del VLM (borrador 2026-10-06)

## Idea
Separar dos trabajos que hoy se mezclan:

1. **Registro continuo (barato, siempre encendido, sin VLM):** YOLO + seguimiento + identidad. Responde "quién/qué estuvo, dónde y cuándo".
2. **Alertas por escena (VLM, como hoy):** Qwen sigue leyendo la escena y generando alertas; al generarlas **consulta el registro continuo** para saber quién está presente, en vez de adivinarlo.

## Qué datos tenemos hoy (24 h, 2026-10-05)
| Cámara | Visitas | Con rostro | Con identidad | Con nombre |
|---|---|---|---|---|
| Exterior 1 | 3,307 | 197 (6%) | 36 | 6 |
| Exterior 2 | 2,706 | 244 (9%) | 31 | 6 |
| Videoportero | 319 | 319 | 84 | 16 |
| Cocina | 739 | 179 | 28 | 24 |
| Garage frontal | 740 | 64 | 27 | 27 |

La identidad depende **solo del rostro**; en la calle el rostro casi no se ve. Estamos tirando el resto de lo que YOLO puede dar.

## Qué se puede extraer de YOLO y complementos (sin VLM)
- **Ya lo usamos:** caja, clase (persona, auto, moto, bici, perro, mochila…), seguimiento, tiempo quieto/en movimiento.
- **Casi gratis:** dirección y velocidad (entra/sale/pasa), color dominante de ropa (superior/inferior) del recorte, estatura relativa, bolso/mochila/bicicleta asociados.
- **Modelo extra pequeño:** *ReID de cuerpo* (OSNet/similar, ~1-2 ms por persona en la GPU): un vector por persona que permite reconocerla **de espalda, de lejos y entre cámaras** sin rostro. Es lo que más rinde.
- **Pose (YOLOv8-pose):** puntos del cuerpo; sirve para "persona agachada/en el suelo" y como base de **marcha** (forma de caminar). La marcha por sí sola es débil con estas cámaras (ángulo alto, baja resolución, de noche en blanco y negro): se usaría solo como un voto más, nunca como identidad.
- **Placas:** solo con zoom/cuadro nativo y poca distancia; hoy 0 de 113 vehículos tienen placa.

## Cómo se decide quién es (fusión de pruebas)
Cada persona-seguimiento acumula votos: rostro (fuerte) + cuerpo ReID (medio) + ropa del día (medio, caduca en horas) + recorrido (viene del garage → jardín → cocina) (débil) + timbre/sensor cercano (contexto). Se asigna a una identidad solo si la suma supera un umbral; si no, queda **"sin identificar" pero registrada** (cumple "todas las personas que entren deben quedar registradas").

Con la ropa: si Exterior 2 ve "chaqueta roja, pantalón azul" a las 00:05 y 40 s después el videoportero reconoce a El Mani con esa misma ropa, la ropa **se asocia a su identidad para esa jornada** y las visitas previas sin nombre se proponen (no se fusionan solas) como suyas.

## Recorrido de entrada a la oficina (lo que describes)
Acceso Site (garage → jardín) → cámara Tuya Jardín → puerta de cristal de la cocina **o** puerta de cristal de Escaleras entrada. Garage frontal y Tuya Garaje son los puntos de entrada. Se modela como una **cadena de zonas**: si una persona sale de una zona y aparece en la siguiente dentro de N segundos, es la misma persona aunque no haya rostro. Esto da asistencia fiable (entrada = cruzar la cadena completa).

## Tráfico de calle (nivel 1: solo conteos)
Para Exterior 1, Exterior 2 y videoportero: contar personas y vehículos que **pasan** (cruce de línea o zona, dirección, hora) y guardar una captura por cruce, **sin descripción del VLM**. Salida: gráficas "personas/autos por hora" y totales por día. Costo: ~0 en VLM.

## Vehículos estacionados (nivel 2)
- Hoy el polígono usa el centro de la caja de YOLO; un auto grande o parcialmente fuera del polígono se pierde. Propuesta: contar el vehículo dentro si **≥60% de su área** cae en el polígono, y mostrar en `/zonas` los vehículos que YOLO ve para validar el dibujo.
- Alerta al estacionarse en el polígono (ya existe "llegó") + **un cuadro nativo** (SUNAPI snapshot) del vehículo para leer placa; el VLM solo se usa como lector de placa de respaldo cuando el OCR no la saca.
- Los autos que pasan sin quedarse solo suman al conteo (nivel 1).

## Timbre
Al sonar: ráfaga de ~6 cuadros del videoportero en 3 s, se queda con el rostro de mejor calidad, y se pide una descripción al VLM de las cámaras exteriores de los 90 s previos para ligar ropa ↔ identidad.

## Tuya
Eventos guardados, pero hoy no se ven grabaciones (la nube de Tuya las guarda cifradas, y el video en vivo cuenta contra la cuota). Propuesta: icono de estado en tiempo real por sensor (puerta/ventana/garage) en el panel; captura por evento (ya existe). **Control (luces, garage)** implica salir del modo solo lectura: requiere decisión explícita, lista de dispositivos permitidos y registro de auditoría (lo mismo que ya existe en Dispositivos). Se mueve a la fase de hardening.

## Fases propuestas
1. Registro continuo + ReID de cuerpo + color de ropa (tabla `presence`, sin cambiar las alertas). Medir: % de visitas con identidad antes/después.
2. Cadena de zonas y asistencia basada en ella; barra de frecuencia por persona.
3. Conteos de calle + gráficas; polígono por área y validación visual en `/zonas`; placa con cuadro nativo.
4. Timbre con ráfaga + ligado de ropa; sugerencias por ropa.
5. Página de reglas e identidades con metadatos (qué regla disparó cada alerta, ropa, cámaras, horarios esperados).

## Decisiones que necesito
- ¿OK a agregar un modelo ReID de cuerpo (descarga ~10 MB, corre en la GPU de Thor)?
- Horarios esperados por empleado (para marcar "debería estar / se quedó").
- Tuya en modo control: ¿qué dispositivos y cuándo (fase de hardening)?
