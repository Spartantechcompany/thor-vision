import logging
import subprocess
import time

logger = logging.getLogger(__name__)


_cpu_prev = None
_gpu_cache = (0.0, 0)


def _cpu_usage() -> float:
    global _cpu_prev
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    prev, _cpu_prev = _cpu_prev, (idle, total)
    if not prev or total <= prev[1]:
        return 0.0
    return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def _gpu_util() -> int:
    global _gpu_cache
    now = time.time()
    if now - _gpu_cache[0] < 2.0:
        return _gpu_cache[1]
    val = _gpu_cache[1]
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3).stdout.strip().splitlines()
        val = int(out[0].strip())
    except Exception:
        try:
            with open("/sys/devices/gpu.0/load") as f:
                val = int(f.read().strip()) // 10
        except Exception:
            pass
    _gpu_cache = (now, val)
    return val


def get_system_metrics() -> dict:
    """Lee métricas del sistema desde /proc y /sys (sin dependencias extra)."""
    metrics = {
        "cpu_pct": 0.0,
        "ram_used_gb": 0.0,
        "ram_total_gb": 0.0,
        "ram_pct": 0.0,
        "temp_c": 0.0,
        "gpu_pct": 0,
        "disk_used_gb": 0.0,
        "disk_total_gb": 0.0,
        "disk_pct": 0.0,
    }

    # CPU: uso real entre dos lecturas de /proc/stat
    try:
        metrics["cpu_pct"] = _cpu_usage()
    except Exception:
        pass

    # RAM
    try:
        with open("/proc/meminfo") as f:
            lines = {l.split(":")[0]: int(l.split()[1]) for l in f if ":" in l}
        total_kb = lines.get("MemTotal", 0)
        avail_kb = lines.get("MemAvailable", 0)
        used_kb = total_kb - avail_kb
        metrics["ram_total_gb"] = round(total_kb / 1024 / 1024, 1)
        metrics["ram_used_gb"] = round(used_kb / 1024 / 1024, 1)
        metrics["ram_pct"] = round(used_kb / total_kb * 100, 1) if total_kb else 0
    except Exception:
        pass

    # Temperatura (Tegra thermal zones)
    try:
        import glob
        temps = []
        for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                with open(path) as f:
                    temps.append(int(f.read().strip()) / 1000)
            except Exception:
                pass
        if temps:
            metrics["temp_c"] = round(max(temps), 1)
    except Exception:
        pass

    # GPU: Thor no expone /sys/devices/gpu.0/load; nvidia-smi si da la utilizacion
    metrics["gpu_pct"] = _gpu_util()

    # Disco
    try:
        import shutil
        usage = shutil.disk_usage("/app")
        metrics["disk_total_gb"] = round(usage.total / 1e9, 1)
        metrics["disk_used_gb"] = round(usage.used / 1e9, 1)
        metrics["disk_pct"] = round(usage.used / usage.total * 100, 1)
    except Exception:
        pass

    return metrics


def get_compute_backend() -> str:
    """Detecta si CUDA está disponible y soporta SM_11.0. Retorna 'cuda' o 'cpu'."""
    try:
        import torch
        if torch.cuda.is_available():
            major, minor = torch.cuda.get_device_capability()
            if major >= 11 and float(torch.version.cuda or 0) >= 13.0:
                logger.info("GPU Blackwell SM_%d.%d detectada — usando CUDA", major, minor)
                return "cuda"
            logger.warning("GPU detectada pero CUDA no compatible con SM_%d.%d", major, minor)
    except ImportError:
        pass
    return "cpu"
