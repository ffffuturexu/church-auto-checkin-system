from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class ProcessMetrics:
    pid: int
    load_avg_1m: float | None
    load_avg_5m: float | None
    load_avg_15m: float | None
    rss_mb: float | None
    cpu_seconds: float | None
    gpu_name: str | None
    gpu_temperature_c: float | None
    gpu_utilization_percent: float | None
    gpu_memory_used_mib: float | None
    gpu_memory_total_mib: float | None
    gpu_power_usage_w: float | None
    gpu_power_limit_w: float | None


def get_process_metrics() -> ProcessMetrics:
    load_avg_1m = load_avg_5m = load_avg_15m = None
    try:
        load_avg_1m, load_avg_5m, load_avg_15m = os.getloadavg()
    except (AttributeError, OSError):
        pass

    rss_mb = None
    cpu_seconds = None
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_seconds = float(usage.ru_utime + usage.ru_stime)
        rss_mb = float(usage.ru_maxrss) / 1024.0
    except Exception:
        pass

    gpu_metrics = _get_gpu_metrics()

    return ProcessMetrics(
        pid=os.getpid(),
        load_avg_1m=load_avg_1m,
        load_avg_5m=load_avg_5m,
        load_avg_15m=load_avg_15m,
        rss_mb=rss_mb,
        cpu_seconds=cpu_seconds,
        gpu_name=gpu_metrics.get("gpu_name"),
        gpu_temperature_c=gpu_metrics.get("gpu_temperature_c"),
        gpu_utilization_percent=gpu_metrics.get("gpu_utilization_percent"),
        gpu_memory_used_mib=gpu_metrics.get("gpu_memory_used_mib"),
        gpu_memory_total_mib=gpu_metrics.get("gpu_memory_total_mib"),
        gpu_power_usage_w=gpu_metrics.get("gpu_power_usage_w"),
        gpu_power_limit_w=gpu_metrics.get("gpu_power_limit_w"),
    )


def _get_gpu_metrics() -> dict[str, Any]:
    query = "name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,power.limit"
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={query}",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return {}

    output = (completed.stdout or "").strip()
    if not output:
        return {}

    first_line = output.splitlines()[0].strip()
    parts = [part.strip() for part in first_line.split(",")]
    if len(parts) < 7:
        return {}

    return {
        "gpu_name": parts[0] or None,
        "gpu_temperature_c": _to_float(parts[1]),
        "gpu_utilization_percent": _to_float(parts[2]),
        "gpu_memory_used_mib": _to_float(parts[3]),
        "gpu_memory_total_mib": _to_float(parts[4]),
        "gpu_power_usage_w": _to_float(parts[5]),
        "gpu_power_limit_w": _to_float(parts[6]),
    }


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None