import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class FaceDB:
    """Persistent face database backed by a JSON file with float32 embeddings."""

    def __init__(self, db_path: str, recognition_threshold: float = 0.40):
        self.db_path = Path(db_path)
        self.threshold = recognition_threshold
        self.thumbs_dir = self.db_path.parent / "face_thumbs"
        self._entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.db_path.exists():
            return
        try:
            with open(self.db_path) as f:
                raw = json.load(f)
            for e in raw:
                e["embedding"] = np.array(e["embedding"], dtype=np.float32)
            self._entries = raw
            logger.info("FaceDB loaded — %d faces from %s", len(self._entries), self.db_path)
        except Exception as exc:
            logger.warning("FaceDB load error: %s", exc)

    def _save(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for e in self._entries:
            d = dict(e)
            d["embedding"] = e["embedding"].tolist()
            data.append(d)
        with open(self.db_path, "w") as f:
            json.dump(data, f)

    def add(self, name: str, embedding: np.ndarray,
            thumb_jpeg: Optional[bytes] = None) -> str:
        face_id = str(uuid.uuid4())[:8]
        norm = np.linalg.norm(embedding)
        emb = embedding / norm if norm > 0 else embedding
        self._entries.append({
            "id": face_id,
            "name": name,
            "embedding": emb.astype(np.float32),
            "added_at": time.time(),
        })
        self._save()
        if thumb_jpeg:
            try:
                self.thumbs_dir.mkdir(parents=True, exist_ok=True)
                (self.thumbs_dir / f"{face_id}.jpg").write_bytes(thumb_jpeg)
            except Exception as exc:
                logger.warning("No se pudo guardar thumbnail de '%s': %s", name, exc)
        logger.info("FaceDB: registered '%s' (id=%s)", name, face_id)
        return face_id

    def thumb_path(self, face_id: str) -> Optional[Path]:
        p = self.thumbs_dir / f"{face_id}.jpg"
        return p if p.exists() else None

    def remove(self, face_id: str) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e["id"] != face_id]
        if len(self._entries) < before:
            self._save()
            (self.thumbs_dir / f"{face_id}.jpg").unlink(missing_ok=True)
            return True
        return False

    def recognize(self, embedding: np.ndarray) -> tuple[str, float]:
        """Return (name, cosine_similarity). name='Desconocido' if below threshold."""
        if not self._entries:
            return "Desconocido", 0.0
        norm = np.linalg.norm(embedding)
        emb = embedding / norm if norm > 0 else embedding
        best_sim = -1.0
        best_name = "Desconocido"
        for e in self._entries:
            sim = float(np.dot(emb, e["embedding"]))
            if sim > best_sim:
                best_sim = sim
                best_name = e["name"]
        if best_sim < self.threshold:
            return "Desconocido", best_sim
        return best_name, best_sim

    def list_faces(self) -> list[dict]:
        return [
            {"id": e["id"], "name": e["name"], "added_at": e["added_at"]}
            for e in self._entries
        ]

    def __len__(self) -> int:
        return len(self._entries)
