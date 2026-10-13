import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional
import numpy as np


@dataclass
class FrameEntry:
    frame: np.ndarray
    timestamp: float = field(default_factory=time.monotonic)


class FrameBuffer:
    """
    Buffer circular thread-safe por cámara.
    maxlen=2 → siempre se tiene el frame más reciente, latencia < 200ms.
    """

    def __init__(self, maxlen: int = 2):
        self._buf: deque[FrameEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._frame_count = 0
        self._last_ts: float = 0.0
        self._fps: float = 0.0
        # Ventana de timestamps SOLO para fps — separada de _buf (que es
        # chico a propósito, maxlen=2, para latencia minima). Antes el fps
        # se calculaba como EWMA de 1/(gap entre 2 llegadas consecutivas):
        # con entrega en rafaga (normal en H.264 con Dynamic FPS/GOV — un
        # par de frames a 8-13ms de distancia cada ~4s, confirmado en vivo
        # con cam-113), un solo gap chico produce un "fps instantaneo" de
        # ~100 y desvia el promedio a valores sin sentido (68fps medidos
        # cuando la camara reportaba 1/30fps real). Una tasa por conteo en
        # una ventana de N llegadas es robusta a esas rafagas.
        self._fps_window: deque[float] = deque(maxlen=60)

    def put(self, frame: np.ndarray) -> None:
        now = time.monotonic()
        entry = FrameEntry(frame=frame, timestamp=now)
        with self._lock:
            self._buf.append(entry)
            self._frame_count += 1
            self._fps_window.append(now)
            if len(self._fps_window) >= 2:
                span = self._fps_window[-1] - self._fps_window[0]
                self._fps = (len(self._fps_window) - 1) / span if span > 0 else 0.0
            self._last_ts = now

    def get_latest(self) -> Optional[np.ndarray]:
        with self._lock:
            if not self._buf:
                return None
            return self._buf[-1].frame.copy()

    def peek_latest(self) -> Optional[FrameEntry]:
        """Ultimo frame SIN copiar (solo lectura; el lector nunca muta un frame ya publicado)."""
        with self._lock:
            return self._buf[-1] if self._buf else None

    def get_latest_entry(self) -> Optional[FrameEntry]:
        with self._lock:
            if not self._buf:
                return None
            e = self._buf[-1]
            return FrameEntry(frame=e.frame.copy(), timestamp=e.timestamp)

    def get_recent_frames(self, seconds: float) -> List[FrameEntry]:
        """
        Todos los frames del buffer cuyo timestamp cae dentro de los
        últimos `seconds`, en orden cronológico. Puede devolver menos
        frames de los esperados si el buffer aún no se llenó a ese
        alcance (arranque en frío) — el llamador debe tolerar clips cortos.
        """
        with self._lock:
            if not self._buf:
                return []
            cutoff = self._buf[-1].timestamp - seconds
            return [
                FrameEntry(frame=e.frame.copy(), timestamp=e.timestamp)
                for e in self._buf
                if e.timestamp >= cutoff
            ]

    def latency_ms(self) -> float:
        """Milisegundos desde el último frame recibido."""
        with self._lock:
            if self._last_ts == 0:
                return -1.0
            return (time.monotonic() - self._last_ts) * 1000

    @property
    def fps(self) -> float:
        with self._lock:
            return round(self._fps, 1)

    @property
    def frame_count(self) -> int:
        with self._lock:
            return self._frame_count

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            self._fps_window.clear()
            self._last_ts = 0.0
            self._fps = 0.0
