# Set de pruebas multimodal: EmbeddingGemma 2 y Clef (2026-10-29)

`scripts/bench_multimodal.py` dentro del contenedor (`--only T3` repite solo video). Datos reales de los últimos 7 días (clips: 14 días).
**Límite principal:** en T1-T4 la "verdad" son las etiquetas que puso el propio modelo de visión (circular, y favorece a la búsqueda por descripción). T5 usa rostros confirmados. T6 no mide precisión real.

| Prueba | Resultado | Lectura |
|---|---|---|
| T1 escena por texto (881 capturas, 10 tipos) | captura P@10 **0.37**, descripción 0.79 (azar ~0.1) | buscar en la imagen funciona (3.7x el azar) pero la descripción gana |
| T2 buscar por foto (80 consultas) | P@10 **0.48** (azar 0.11); **95 %** del top 10 es de la misma cámara | encuentra "momentos parecidos en esta cámara", no el mismo hecho en otra |
| T3 video (220 clips, 3 tipos) | P@5 video **0.57**, captura 0.33, descripción 0.67 | el clip entiende mejor que una sola foto (+70 %) |
| T4 repetidas (200 + 200 pares seguidos, misma cámara) | AUC embeddings **0.50**; Clef **0.58** (0.39 s por par) | ni uno ni otro distingue "nada nuevo" en cuadros completos |
| T5 personas (14 empleados, 332 recortes) | AUC **0.64** (ReID actual 0.67, Clef 0.84, cara 0.93) | no sirve para seguimiento de personas |
| T6 audio | sintéticos 3/5 (silencio, campanilla, alarma); videoportero: señal casi nula (rms 0.000-0.0003) | la tubería funciona; el micrófono del videoportero no entrega audio útil hoy |

Tiempos en la GPU de Thor: imagen ~0.15 s, clip de 16 cuadros ~1.5 s, audio 4-8 s ~0.4 s.
