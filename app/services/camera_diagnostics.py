from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from app.core.process_metrics import get_process_metrics
from app.services.runtime_pipeline import RuntimePipeline

logger = logging.getLogger(__name__)


def _tcp_status(source: str | int) -> dict:
    if not isinstance(source, str) or not source.lower().startswith(("rtsp://", "rtsps://")):
        return {"applicable": False}
    try:
        url = urlsplit(source)
        if not url.hostname:
            return {"applicable": True, "error": "RTSP hostname missing"}
        port = url.port or (322 if url.scheme.lower() == "rtsps" else 554)
        addresses = {
            ipaddress.ip_address(item[4][0].split("%")[0])
            for item in socket.getaddrinfo(url.hostname, port, type=socket.SOCK_STREAM)
        }
        result = subprocess.run(
            ["ss", "-H", "-t", "-i", "-n", "-p"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return {"applicable": True, "error": type(exc).__name__}

    connections = []
    current = None
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[0] in {
            "ESTAB", "SYN-SENT", "SYN-RECV", "FIN-WAIT-1", "FIN-WAIT-2",
            "TIME-WAIT", "CLOSE-WAIT", "LAST-ACK", "CLOSING", "CLOSED",
        }:
            current = None
            try:
                address, remote_port = fields[4].rsplit(":", 1)
                remote = ipaddress.ip_address(address.strip("[]").split("%")[0])
                if remote not in addresses or int(remote_port) != port:
                    continue
            except ValueError:
                continue
            if not re.search(rf"\bpid={os.getpid()}(?:,|\))", line):
                continue
            current = {"state": fields[0], "receive_queue_bytes": int(fields[1]), "send_queue_bytes": int(fields[2])}
            connections.append(current)
        elif current is not None:
            for key, value in re.findall(
                r"\b(rtt|rto|retrans|bytes_retrans|lost|unacked|cwnd):([^\s]+)", line
            ):
                current[key] = value
    return {"applicable": True, "connections": connections}


def _ffmpeg_event(message: str) -> str | None:
    timeout = re.search(r"Stream timeout triggered after ([\d.]+) ms", message, re.I)
    if timeout:
        return f"stream timeout after {timeout.group(1)} ms"
    codec = re.search(r"\[(h264|hevc|h265|rtsp|tcp|udp)\s+@", message, re.I)
    if not codec:
        return None
    connection_error = re.search(
        r"Connection to .+ failed: (Connection timed out|Connection refused|No route to host|Network is unreachable)",
        message,
        re.I,
    )
    if connection_error:
        return f"{codec.group(1).lower()} connection failed: {connection_error.group(1)}"
    for pattern in (
        r"error while decoding MB \d+ \d+, bytestream -?\d+",
        r"cabac decode of qscale diff failed at \d+ \d+",
        r"left block unavailable for requested intra4x4 mode -?\d+",
        r"Overread VUI by \d+ bits",
        r"method (?:DESCRIBE|SETUP|PLAY) failed: \d+",
        r"RTP: missed \d+ packets",
        r"non-existing PPS \d+ referenced",
        r"Invalid NAL unit \d+",
        r"decode_slice_header error",
    ):
        match = re.search(pattern, message, re.I)
        if match:
            return f"{codec.group(1).lower()} {match.group(0)}"
    return None


class CameraDiagnostics:
    def __init__(self, runtime: RuntimePipeline, log_dir: Path | None = None, interval_sec: float = 10.0) -> None:
        self.runtime = runtime
        self.log_dir = log_dir or Path(__file__).resolve().parents[2] / "logs"
        self.interval_sec = interval_sec
        self._logger = logging.Logger("camera_diagnostics", logging.INFO)
        self._handler: RotatingFileHandler | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        try:
            self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._handler = RotatingFileHandler(
                self.log_dir / "camera.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
        except OSError:
            logger.exception("camera diagnostics could not initialize")
            return
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self._logger.addHandler(self._handler)
        self._logger.info("camera diagnostics started pid=%s", os.getpid())
        self._tasks = [
            asyncio.create_task(self._sample_loop(), name="camera-diagnostics-sample"),
            asyncio.create_task(self._journal_loop(), name="camera-diagnostics-journal"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._handler is not None:
            self._logger.info("camera diagnostics stopped")
            self._logger.removeHandler(self._handler)
            self._handler.close()
            self._handler = None

    def _snapshot(self) -> dict:
        state = self.runtime.state()
        return {
            "stream_running": state.stream_running,
            "camera_running": state.camera_running,
            "camera_stream_healthy": state.camera_stream_healthy,
            "camera_last_frame_age_sec": state.camera_last_frame_age_sec,
            "camera_stats": state.camera_stats,
            "recognition_running": state.recognition_running,
            "recognition_last_debug_frame_age_sec": state.recognition_last_debug_frame_age_sec,
            "recognition_stats": state.recognition_stats,
            "frame_queue_size": state.frame_queue_size,
            "event_queue_size": state.event_queue_size,
            "rtsp_tcp_requested": self.runtime.camera.force_tcp,
            "tcp": _tcp_status(state.camera_source),
            "system": asdict(get_process_metrics()),
        }

    async def _sample_loop(self) -> None:
        while True:
            try:
                snapshot = await asyncio.to_thread(self._snapshot)
                self._logger.info("status %s", json.dumps(snapshot, default=str, ensure_ascii=False))
            except Exception:
                logger.exception("camera diagnostics status sample failed")
                self._logger.exception("camera diagnostics status sample failed")
            await asyncio.sleep(self.interval_sec)

    async def _journal_loop(self) -> None:
        while True:
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    "journalctl", "-q", "-f", "-n", "0", "-o", "json", f"_PID={os.getpid()}",
                    stdout=asyncio.subprocess.PIPE,
                    limit=1024 * 1024,
                )
                assert process.stdout is not None
                while line := await process.stdout.readline():
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        logger.warning("camera diagnostics received invalid journal entry")
                        continue
                    message = record.get("MESSAGE")
                    if isinstance(message, str) and (event := _ffmpeg_event(message)):
                        timestamp = record.get("__REALTIME_TIMESTAMP")
                        try:
                            occurred_at = datetime.fromtimestamp(int(timestamp) / 1_000_000, timezone.utc).isoformat()
                        except (TypeError, ValueError, OverflowError, OSError):
                            occurred_at = "unknown"
                        self._logger.warning("ffmpeg occurred_at=%s %s", occurred_at, event)
                code = await process.wait()
                logger.warning("camera diagnostics journal reader exited with code %s; retrying", code)
                self._logger.warning("journal reader exited with code %s; retrying", code)
            except OSError:
                logger.exception("camera diagnostics journal reader unavailable; retrying")
                self._logger.error("journal reader unavailable; retrying")
            finally:
                if process is not None and process.returncode is None:
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except ProcessLookupError:
                        pass
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
            await asyncio.sleep(30)
