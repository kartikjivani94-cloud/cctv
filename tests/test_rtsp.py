"""RTSP live-gateway unit tests plus an optional MediaMTX/ffmpeg integration test."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tarfile
import time
import urllib.request
from datetime import time as dtime
from pathlib import Path
from urllib.error import URLError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.library import Camera
from app.routes import router
from app.rtsp import advertised_urls, ffmpeg_publish_cmd, publish_path, rtsp_path
from app.timefeed import FeedClock

ROOT = Path(__file__).resolve().parents[1]
MEDIAMTX_VERSION = "1.20.1"
TOOLS_BIN = ROOT / "tools" / "mediamtx"


class DummySettings:
    timezone = "Asia/Kolkata"
    drift_tolerance_seconds = 2.5
    stream_chunk_size = 256 * 1024
    rtsp_public_host = "cameras.example.gov"
    rtsp_port = 8554
    webrtc_port = 8889
    rtsp_path_prefix = "stream"
    hls_live_via_proxy = True


class FakeGateway:
    def is_publishing(self, cam_id: str) -> bool:
        return cam_id == "1"


def test_ffmpeg_publish_cmd_is_realtime_copy():
    cmd = ffmpeg_publish_cmd(
        ffmpeg_bin="ffmpeg",
        video_path="/videos/01_bridge.mp4",
        dest_url="rtsp://127.0.0.1:8554/stream/1",
        offset=3600.5,
        transport="tcp",
    )
    assert "-re" in cmd
    assert cmd.index("-re") < cmd.index("-i")
    assert cmd[cmd.index("-stream_loop") + 1] == "-1"
    assert cmd.index("-stream_loop") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "3600.500"
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-an" in cmd
    assert cmd[-1] == "rtsp://127.0.0.1:8554/stream/1"


def test_ffmpeg_omits_ss_at_start_of_file():
    cmd = ffmpeg_publish_cmd(
        ffmpeg_bin="ffmpeg",
        video_path="cam.mp4",
        dest_url="rtsp://127.0.0.1:8554/stream/2",
        offset=0.1,
    )
    assert "-ss" not in cmd


def test_advertised_urls_and_path():
    assert rtsp_path("7") == "stream/7"
    urls = advertised_urls(
        "7",
        public_host="10.0.0.8",
        rtsp_port=8554,
        webrtc_port=8889,
        path_prefix="stream",
        hls_via_proxy=True,
    )
    assert urls["rtsp_url"] == "rtsp://10.0.0.8:8554/stream/7"
    assert urls["webrtc_url"] == "http://10.0.0.8:8889/stream/7/whep"
    assert urls["hls_live_url"] == "/live/stream/7/index.m3u8"


def test_publish_path_prefers_processed_file(tmp_path: Path):
    src = tmp_path / "raw.mkv"
    processed = tmp_path / "processed.mp4"
    src.write_bytes(b"x")
    processed.write_bytes(b"y")
    cam = Camera(
        id="1", number=1, name="Camera 1", location="Bridge", slug="bridge",
        source_path=src, serve_path=processed, duration=60.0,
        video_codec="hevc", container="mkv", size=1, servable=True,
    )
    assert publish_path(cam) == processed


def test_api_exposes_rtsp_when_gateway_is_live(tmp_path: Path):
    vid = tmp_path / "cam.mp4"
    vid.write_bytes(os.urandom(64 * 1024))
    app = FastAPI()
    app.include_router(router)
    cam = Camera(
        id="1", number=1, name="Camera 1", location="Janpath", slug="janpath",
        source_path=vid, serve_path=vid, duration=43200.0,
        video_codec="h264", container="mp4", size=vid.stat().st_size, servable=True,
    )
    app.state.settings = DummySettings()
    app.state.cameras = [cam]
    app.state.cameras_by_id = {"1": cam}
    app.state.clock = FeedClock("Asia/Kolkata", dtime(21, 0), 43200.0, True)
    app.state.rtsp = FakeGateway()

    client = TestClient(app)
    listed = client.get("/api/cameras").json()["cameras"][0]
    assert listed["delivery"] == "rtsp"
    assert listed["rtsp_url"] == "rtsp://cameras.example.gov:8554/stream/1"

    ingest = client.get("/api/ingest").json()["cameras"][0]
    assert ingest["live"] is True
    assert ingest["rtsp_url"].endswith("/stream/1")

    state = client.get("/api/cameras/1/state").json()
    assert state["hls_live_url"] == "/live/stream/1/index.m3u8"
    assert state["stream_url"] == "/stream/1"


def _have_ffmpeg() -> bool:
    try:
        return subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, timeout=10
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ensure_mediamtx() -> Path:
    if TOOLS_BIN.exists() and os.access(TOOLS_BIN, os.X_OK):
        return TOOLS_BIN
    import platform

    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64"
    url = (
        f"https://github.com/bluenviron/mediamtx/releases/download/"
        f"v{MEDIAMTX_VERSION}/mediamtx_v{MEDIAMTX_VERSION}_{system}_{arch}.tar.gz"
    )
    TOOLS_BIN.parent.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / "tools" / "_mediamtx.tgz"
    try:
        urllib.request.urlretrieve(url, tmp)
    except (URLError, OSError) as exc:
        pytest.skip(f"could not download MediaMTX: {exc}")
    with tarfile.open(tmp) as tar:
        try:
            tar.extract("mediamtx", path=TOOLS_BIN.parent, filter="data")
        except TypeError:
            tar.extract("mediamtx", path=TOOLS_BIN.parent)
    tmp.unlink(missing_ok=True)
    TOOLS_BIN.chmod(0o755)
    return TOOLS_BIN


def _wait_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_live_rtsp_is_realtime_not_file_burst(tmp_path: Path):
    """Publish a short MP4 with -re and prove clients ingest at native FPS."""
    if not _have_ffmpeg():
        pytest.skip("ffmpeg not installed")

    mtx = _ensure_mediamtx()
    video = tmp_path / "clip.mp4"
    make = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
            "-t", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "ultrafast", "-tune", "zerolatency",
            str(video),
        ],
        capture_output=True, timeout=30,
    )
    assert make.returncode == 0, make.stderr.decode(errors="replace")

    rtsp_port = _free_port()
    hls_port = _free_port()
    api_port = _free_port()
    cfg = tmp_path / "mediamtx.yml"
    cfg.write_text(
        "\n".join([
            "logLevel: warn",
            f"rtspAddress: :{rtsp_port}",
            "rtspTransports: [tcp]",
            f"hlsAddress: :{hls_port}",
            "hlsAlwaysRemux: yes",
            "webrtc: no",
            "api: yes",
            f"apiAddress: :{api_port}",
            "authInternalUsers:",
            "  - user: any",
            '    pass: ""',
            "    permissions:",
            "      - action: publish",
            "      - action: read",
            "      - action: playback",
            "      - action: api",
            "paths:",
            "  all_others:",
            "    source: publisher",
        ]) + "\n"
    )

    mtx_proc = subprocess.Popen(
        [str(mtx), str(cfg)],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert _wait_port("127.0.0.1", rtsp_port), "MediaMTX did not bind RTSP"
        dest = f"rtsp://127.0.0.1:{rtsp_port}/stream/1"
        cmd = ffmpeg_publish_cmd(
            ffmpeg_bin="ffmpeg",
            video_path=str(video),
            dest_url=dest,
            offset=0.0,
            transport="tcp",
        )
        pub = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 20
            ready = False
            while time.monotonic() < deadline:
                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                        "-show_entries", "stream=codec_name,codec_type",
                        "-of", "json", dest,
                    ],
                    capture_output=True, text=True, timeout=8,
                )
                if probe.returncode == 0:
                    data = json.loads(probe.stdout or "{}")
                    codecs = [s.get("codec_name") for s in data.get("streams") or []]
                    if "h264" in codecs:
                        ready = True
                        break
                time.sleep(0.4)
            assert ready, "RTSP stream never became readable"

            t0 = time.monotonic()
            grab = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp", "-i", dest,
                    "-t", "2", "-f", "null", "-",
                ],
                capture_output=True, timeout=20,
            )
            elapsed = time.monotonic() - t0
            assert grab.returncode == 0, grab.stderr.decode(errors="replace")
            # Without -re this would finish in milliseconds. Native 2s of
            # video must take about 2s of wall clock.
            assert elapsed >= 1.5, f"RTSP ingest was not real-time ({elapsed:.2f}s for 2s of video)"
            assert elapsed < 12.0, f"RTSP ingest stalled ({elapsed:.2f}s)"
        finally:
            pub.terminate()
            try:
                pub.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pub.kill()
    finally:
        mtx_proc.terminate()
        try:
            mtx_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mtx_proc.kill()
