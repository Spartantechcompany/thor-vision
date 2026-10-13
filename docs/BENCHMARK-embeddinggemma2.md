# Benchmark: EmbeddingGemma 2 contra el buscador actual (2026-10-10)

Datos reales de Thor, ejecutado con `scripts/bench_embed.py` dentro del contenedor, GPU de Thor, vectores de 256 dimensiones.

## Texto: buscar eventos por lo que dice la descripcion
45,654 descripciones de los ultimos 7 dias, 17 consultas en lenguaje natural (9 tipos de alerta, 2 redacciones cada uno, solo tipos con 30 o mas casos).
Verdad = tipo de alerta que el propio sistema asigno al evento (circular: lo decidio el mismo modelo de lenguaje).

| Metodo | P@10 | MRR@10 |
|---|---|---|
| **Antes:** FTS5 (textindex) | 0.450 | 0.548 |
| Embeddings solos | **0.622** | 0.806 |
| **Despues:** FTS + embeddings (RRF) | 0.578 | **0.861** |

- Los embeddings ganan con parafrasis ("alguien forzando una puerta": FTS 0.4, embeddings 0.9; "alguien revisando la puerta de un coche": 0.0 contra 0.9).
- FTS gana con palabras exactas ("un perro o gato dentro de la casa": 0.7 contra 0.3; "persona tirada o caida en el piso": 0.8 contra 0.5). Por eso se dejo la mezcla.
- Vectorizar las 45,654 descripciones tomo 97 s (~470 textos/s).

## Imagen: buscar personas por la ropa
3,000 recortes de cuerpo de visitas reales (3 dias). Consulta "persona con camiseta de color X"; verdad = color de la prenda superior calculado por bodyid (aproximado).
Antes: texto de la descripcion del VLM de la visita (solo 1,483 de 3,000 tienen descripcion).

| Metodo | P@10 promedio |
|---|---|
| **Antes:** FTS sobre la descripcion | 0.163 |
| **Despues:** embedding de la imagen | 0.287 |

Por color (embedding contra el azar): rojo 0.40 (azar 0.04), rosa 0.50 (0.03), blanco 0.30 (0.03), negro 0.40 (0.18), azul 0.30 (0.20), gris 0.30 (0.40, peor que el azar), morado 0.10, cafe 0.00.
Util como pista visual para colores poco comunes; no es una identificacion y es debil con grises y cafes. Vectorizar 3,000 imagenes tomo 445 s (~7 por segundo): se indexa en segundo plano, 150 por ciclo.

## Limites
- Verdades derivadas del propio sistema, no etiquetadas por una persona.
- Consultas escritas por nosotros; 17 de texto y 8 colores es una muestra chica.
- No se midio video ni audio.
