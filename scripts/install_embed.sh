#!/bin/sh
# Instala las dependencias de EmbeddingGemma 2 en data/pylibs-emb (volumen de datos; no toca la imagen ni torch).
# Uso: docker exec thor-vision sh /app/scripts/install_embed.sh   (o copiar el archivo adentro)
set -e
T=/app/data/pylibs-emb
pip install -q --target $T --no-deps sentence-transformers transformers huggingface_hub tokenizers safetensors regex hf-xet
pip install -q --target $T --upgrade httpx2 httpcore2
echo "listo: $(du -sh $T | cut -f1) en $T; el modelo (1.5 GB) se descarga solo la primera vez en data/models/hf"
