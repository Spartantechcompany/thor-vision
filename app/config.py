import os
from dataclasses import dataclass, field
from typing import List, Optional
import yaml


@dataclass
class CameraConfig:
    id: str
    name: str
    rtsp_url: str
    enabled: bool = True
    zone: str = "interior"
    priority: str = "normal"
    resolution: Optional[List[int]] = None
    # Grados de rotacion CW a aplicar al frame ya capturado (0/90/180/270).
    # Para camaras montadas rotadas o con stream nativo en vertical (ej.
    # cam-113 "Escaleras entrada": nativo 1080x1920, ver CLAUDE.md).
    rotate: int = 0
    # Mascara de privacidad: lista de poligonos [[x,y],...] normalizados 0-1 sobre el
    # frame final. Se rellenan de negro ANTES de entrar al buffer, asi ningun consumidor
    # (YOLO, caras, VLM, snapshots, preroll, stream) ve esos pixeles.
    privacy_mask: Optional[List] = None


@dataclass
class GlobalConfig:
    frame_width: int = 1280
    frame_height: int = 720
    snapshot_path: str = "/app/data/snapshots"
    log_path: str = "/app/data/logs"
    reconnect_interval_s: int = 5
    buffer_size: int = 30
    stream_fps: int = 15
    jpeg_quality: int = 75


@dataclass
class AppConfig:
    cameras: List[CameraConfig] = field(default_factory=list)
    global_cfg: GlobalConfig = field(default_factory=GlobalConfig)

    @property
    def enabled_cameras(self) -> List[CameraConfig]:
        return [c for c in self.cameras if c.enabled]


def load_config(cameras_path: str = None, settings_path: str = None) -> AppConfig:
    cameras_path = cameras_path or os.environ.get(
        "CAMERAS_CONFIG", "/app/config/cameras.yml"
    )
    with open(cameras_path) as f:
        raw = yaml.safe_load(f)

    global_raw = raw.get("global", {})
    global_cfg = GlobalConfig(
        frame_width=global_raw.get("frame_width", 1280),
        frame_height=global_raw.get("frame_height", 720),
        snapshot_path=global_raw.get("snapshot_path", "/app/data/snapshots"),
        log_path=global_raw.get("log_path", "/app/data/logs"),
        reconnect_interval_s=global_raw.get("reconnect_interval_s", 5),
        buffer_size=global_raw.get("buffer_size", 30),
        stream_fps=global_raw.get("stream_fps", 15),
        jpeg_quality=global_raw.get("jpeg_quality", 75),
    )

    cameras = [
        CameraConfig(
            id=c["id"],
            name=c["name"],
            rtsp_url=c["rtsp_url"],
            enabled=c.get("enabled", True),
            zone=c.get("zone", "interior"),
            priority=c.get("priority", "normal"),
            resolution=c.get("resolution"),
            rotate=c.get("rotate", 0),
            privacy_mask=c.get("privacy_mask"),
        )
        for c in raw.get("cameras", [])
    ]

    return AppConfig(cameras=cameras, global_cfg=global_cfg)
