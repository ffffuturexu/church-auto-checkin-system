import asyncio
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.services import camera_diagnostics
from app.services.camera_diagnostics import CameraDiagnostics, _ffmpeg_event, _tcp_status


def test_tcp_status_only_includes_camera_socket_owned_by_process(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 554))],
    )
    output = (
        f'ESTAB 0 42 192.0.2.20:40000 192.0.2.10:554 users:(("python",pid={os.getpid()},fd=5))\n'
        "\trtt:12.5/2.0 retrans:0/3 bytes_retrans:20\n"
        'ESTAB 0 0 192.0.2.20:40001 192.0.2.10:554 users:(("other",pid=999999,fd=6))\n'
        "\trtt:1/1\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=output))

    status = _tcp_status("rtsp://user:secret@camera.example/live")
    assert status == {
        "applicable": True,
        "connections": [{
            "state": "ESTAB",
            "receive_queue_bytes": 0,
            "send_queue_bytes": 42,
            "rtt": "12.5/2.0",
            "retrans": "0/3",
            "bytes_retrans": "20",
        }],
    }
    assert "secret" not in str(status)


def test_tcp_status_reports_command_failure(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 554))],
    )

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("ss", 3)

    monkeypatch.setattr(subprocess, "run", fail)
    assert _tcp_status("rtsp://user:secret@camera.example/live") == {
        "applicable": True,
        "error": "TimeoutExpired",
    }


def test_ffmpeg_events_are_allowlisted_without_credentials():
    assert _ffmpeg_event("[ WARN] Stream timeout triggered after 30058.5 ms rtsp://user:secret@host/live") == (
        "stream timeout after 30058.5 ms"
    )
    assert _ffmpeg_event("[h264 @ 0x123] error while decoding MB 17 12, bytestream 2530") == (
        "h264 error while decoding MB 17 12, bytestream 2530"
    )
    assert _ffmpeg_event("[rtsp @ 0x123] rtsp://user:secret@host/live") is None
    assert _ffmpeg_event(
        "[tcp @ 0x123] Connection to tcp://user:secret@host:554 failed: Connection refused"
    ) == "tcp connection failed: Connection refused"


def test_diagnostics_writes_samples_and_stops(tmp_path, monkeypatch):
    @dataclass
    class Metrics:
        pid: int = 42

    state = SimpleNamespace(
        stream_running=True,
        camera_running=True,
        camera_stream_healthy=False,
        camera_last_frame_age_sec=30.0,
        camera_stats={"frames_captured": 3},
        recognition_running=True,
        recognition_last_debug_frame_age_sec=31.0,
        recognition_stats={"pending_requests": 0},
        frame_queue_size=0,
        event_queue_size=0,
        camera_source="rtsp://user:secret@camera.example/live",
    )
    runtime = SimpleNamespace(state=lambda: state, camera=SimpleNamespace(force_tcp=True))
    monkeypatch.setattr(camera_diagnostics, "_tcp_status", lambda source: {"applicable": True, "connections": []})
    monkeypatch.setattr(camera_diagnostics, "get_process_metrics", Metrics)

    async def idle_journal(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(CameraDiagnostics, "_journal_loop", idle_journal)

    async def run():
        diagnostics = CameraDiagnostics(runtime, tmp_path, interval_sec=0.01)
        await diagnostics.start()
        for _ in range(100):
            if "status " in (tmp_path / "camera.log").read_text():
                break
            await asyncio.sleep(0.01)
        await diagnostics.stop()

    asyncio.run(run())
    content = (tmp_path / "camera.log").read_text()
    status = next(line.split(" status ", 1)[1] for line in content.splitlines() if " status " in line)
    assert json.loads(status)["camera_last_frame_age_sec"] == 30.0
    assert "camera diagnostics stopped" in content
    assert "secret" not in content


def test_journal_events_are_recorded_and_reader_stops(tmp_path, monkeypatch):
    class Reader:
        def __init__(self):
            self.sent = False

        async def readline(self):
            if not self.sent:
                self.sent = True
                return json.dumps({
                    "MESSAGE": "[h264 @ 0x123] error while decoding MB 17 12, bytestream 2530",
                    "__REALTIME_TIMESTAMP": "1700000000000000",
                }).encode()
            await asyncio.Event().wait()

    class Process:
        def __init__(self):
            self.stdout = Reader()
            self.returncode = None

        def terminate(self):
            self.returncode = -15

        async def wait(self):
            return self.returncode

    process = Process()

    async def create_process(*args, **kwargs):
        return process

    async def idle_samples(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(CameraDiagnostics, "_sample_loop", idle_samples)

    async def run():
        diagnostics = CameraDiagnostics(SimpleNamespace(), tmp_path)
        await diagnostics.start()
        for _ in range(100):
            if "ffmpeg occurred_at=" in (tmp_path / "camera.log").read_text():
                break
            await asyncio.sleep(0.01)
        await diagnostics.stop()

    asyncio.run(run())
    content = (tmp_path / "camera.log").read_text()
    assert "occurred_at=2023-11-14T22:13:20+00:00 h264 error while decoding MB 17 12" in content
    assert process.returncode == -15


def test_application_lifespan_starts_and_stops_diagnostics(tmp_path, monkeypatch):
    import app.main as main

    @dataclass
    class Metrics:
        pid: int = 42

    class Cleanup:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        async def stop(self):
            pass

    async def idle_journal(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "FaceLibraryService", lambda *args: object())
    monkeypatch.setattr(main, "RecognitionLogCleanupService", Cleanup)
    monkeypatch.setattr(main, "ReceptionFeedCleanupService", Cleanup)
    monkeypatch.setattr(main, "CameraDiagnostics", lambda runtime: CameraDiagnostics(runtime, tmp_path))
    monkeypatch.setattr(CameraDiagnostics, "_journal_loop", idle_journal)
    monkeypatch.setattr(camera_diagnostics, "_tcp_status", lambda source: {"applicable": True, "connections": []})
    monkeypatch.setattr(camera_diagnostics, "get_process_metrics", Metrics)

    with TestClient(main.create_app()):
        for _ in range(100):
            if "status " in (tmp_path / "camera.log").read_text():
                break
            time.sleep(0.01)

    content = (tmp_path / "camera.log").read_text()
    assert "status " in content
    assert "camera diagnostics stopped" in content
