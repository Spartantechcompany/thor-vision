# Evaluación: JetPack 7.2.1 (Jetson Linux 39.2.1)

Evaluación del 2026-10-03. Es una decisión de infraestructura del host Thor, no del código de `thor-vision`.

## Estado actual de Thor (medido)

- Jetson AGX Thor Developer Kit, **Jetson Linux 38.4.0 (JetPack 7.1)**, kernel 6.8.12-tegra, driver 580, **CUDA 13.0**.
- Sistema operativo del host: **Ubuntu 26.04.1** con Python 3.14 (NVIDIA entrega Ubuntu 24.04 en JetPack 7.x: el host ya no coincide con el rootfs de NVIDIA).
- NVIDIA Container Toolkit 1.18.1. Docker 29.1.3 con runtime `nvidia`.
- `apt`: 1 paquete actualizable y **0 de seguridad pendientes**. 2,708 paquetes instalados; 129 ya no están disponibles para descarga.

## Qué trae 39.2.1

- Jetson Linux 39.2.1 (11-ago-2026): kernel 6.8 (el mismo), Ubuntu 24.04, **CUDA 13.2.2**, cuDNN 9.20, TensorRT 10.16.2, DeepStream 9.1, Container Toolkit 1.19 (con la imagen ISO).
- Novedades: emulación de T3000 y skills de video para agentes (no nos aplican). Correcciones de errores conocidos y de vulnerabilidades (esto es lo que justifica evaluar).

## La nota sobre SBSA y CUDA unificado: ¿aplica?

Sí, pero es evolución y no una ruptura. Thor ya es SBSA desde JetPack 7. Lo nuevo es que la instalación de CUDA es la misma que en cualquier servidor ARM. Para nosotros implica:

- Los paquetes de `pypi.jetson-ai-lab.io/sbsa/cu130` (onnxruntime, opencv) seguirán siendo los correctos, aunque con CUDA 13.2 habrá que confirmar cuáles tienen versión `cu132`.
- **Dependencia real a vigilar:** `docker-compose.yml` monta la carpeta de CUDA del host (`/usr/local/cuda-13.0/targets/sbsa-linux/lib`) para las librerías NPP que usa el wheel de OpenCV con GStreamer. Con CUDA 13.2 esa ruta cambia y habría que actualizarla y probar.
- El decodificador por hardware (`nvv4l2decoder`) y las 10 cámaras deben revalidarse completas.
- Ollama: hoy fuerza `cuda_jetpack6` (incorrecto para Thor); con CUDA 13.x se usa `cuda_v13`. Se puede corregir sin actualizar el sistema.

## Qué implica actualizar

No es un `apt upgrade`: pasar de 38.4 a 39.2.1 es una versión mayor de Jetson Linux y requiere **reflasheo** (imagen ISO o instalación manual, que cambió por SBSA). Reflashear **borra el disco del sistema**.

- Hay 219 GB usados y 19 contenedores, incluidas bases de datos (`labene-db`, de la que depende también el CMDB oficial `oxm-api`, NormaAI y su Qdrant, Umami) y el stack NOC que alimenta los dashboards de otros agentes.
- Requiere ventana de mantenimiento (horas), respaldo completo de volúmenes y configuraciones, y plan de restauración.
- No hay una segunda Thor para probar antes. La CUDA 13.2 no se puede probar en contenedores sobre el driver actual sin actualizar el host.
- El host está en Ubuntu 26.04: el rootfs de NVIDIA es 24.04, así que habría que decidir qué se conserva.

## Cómo decidir por CVE (antes de reflashear)

1. **Inventario:** correr `ubuntu-security-status` / `pro security-status`, revisar el boletín de seguridad de NVIDIA para la versión 38.4 (kernel, bootloader, drivers) y escanear las 19 imágenes de contenedor (por ejemplo con Trivy). Muchas CVE están en las imágenes y se corrigen reconstruyéndolas, sin tocar el host.
2. **Clasificar** por exposición real: Thor no es accesible desde Internet; el riesgo es interno.
3. **Decidir** solo si hay CVE explotable en el host que NVIDIA corrija únicamente en 39.2.1.

## Recomendación

No actualizar todavía. Primero el inventario de CVE (paso 1), que se puede hacer sin riesgo. Si se justifica, hacerlo como proyecto planeado: ventana, respaldo en el Synology, prueba de restauración, y revalidación de captura (NVDEC), CUDA, NormaAI, CMDB y NOC.
