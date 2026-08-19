"""Real-time RTSP publisher: static files -> MediaMTX at native framerate.

Hackathon AI clients (OpenCV, GStreamer, DeepStream) need a live RTP/RTSP
edge, not an HTTP byte-range MP4. Each camera is published with ffmpeg:

    ffmpeg -re -ss <wall-clock-offset> -stream_loop -1 -i <file> \\
           -c:v copy -f rtsp rtsp://<gateway>/stream/<id>

``-re`` paces the file at its native FPS (1s of video takes 1s to send).
``-c:v copy`` muxes existing H.264/H.265 NAL units into RTP with no re-encode.
MediaMTX then fans the same live path out as RTSP, WebRTC, and HLS.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from .config import Settings
from .library import Camera
from .timefeed import FeedClock

logger = logging.getLogger("cctv.rtsp")


def publish_path(cam: Camera) -> Optional[Path]:
    """File ffmpeg should read. Prefer a repaired MP4; fall back to the source."""
    if cam.serve_path is not None and cam.serve_path.exists():
        return cam.serve_path
    src = cam.source_path
    if src.exists() and cam.video_codec in {"h264", "hevc", "h265"}:
        return src
    return None


def rtsp_path(cam_id: str, prefix: str = "stream") -> str:
    return f"{prefix.strip('/')}/{cam_id}"


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def ffmpeg_publish_cmd(
    *,
    ffmpeg_bin: str,
    video_path: str,
    dest_url: str,
    offset: float = 0.0,
    transport: str = "tcp",
) -> List[str]:
    """Build the ffmpeg command that turns a file into a live RTSP publish.

    Input flags (``-re``, ``-ss``, ``-stream_loop``) must sit before ``-i``.
    Video is stream-copied; audio is dropped because many DVR tracks are
    PCM/PCMA which RTSP/WebRTC cannot carry without a transcode.
    """
    cmd = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-re",
    ]
    if offset and offset > 0.5:
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += [
        "-stream_loop", "-1",
        "-i", video_path,
        "-map", "0:v:0",
        "-c:v", "copy",
        "-an",
        "-f", "rtsp",
        "-rtsp_transport", transport,
        dest_url,
    ]
    return cmd


def parse_host_port(url: str, default_port: int = 8554) -> tuple[str, int]:
    parsed = urlparse(url if "://" in url else f"rtsp://{url}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or default_port
    return host, port


def gateway_reachable(url: str, timeout: float = 0.4) -> bool:
    host, port = parse_host_port(url)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def public_host_from_request(host_header: Optional[str], configured: str) -> str:
    if configured and configured.strip().lower() not in {"auto", ""}:
        return configured.strip()
    if not host_header:
        return "localhost"
    return host_header.split(":")[0] or "localhost"


def advertised_urls(
    cam_id: str,
    *,
    public_host: str,
    rtsp_port: int,
    webrtc_port: int,
    path_prefix: str,
    hls_via_proxy: bool,
) -> dict:
    path = rtsp_path(cam_id, path_prefix)
    hls = f"/live/{path}/index.m3u8" if hls_via_proxy else (
        f"http://{public_host}:8888/{path}/index.m3u8"
    )
    return {
        "rtsp_url": f"rtsp://{public_host}:{rtsp_port}/{path}",
        "webrtc_url": f"http://{public_host}:{webrtc_port}/{path}/whep",
        "hls_live_url": hls,
    }


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        proc.wait(timeout=3)


class RtspGateway:
    """Supervises one ffmpeg publisher per camera (worker 0 only)."""

    def __init__(
        self,
        settings: Settings,
        clock: FeedClock,
        get_cameras: Callable[[], List[Camera]],
    ):
        self._settings = settings
        self._clock = clock
        self._get_cameras = get_cameras
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._procs: Dict[str, subprocess.Popen] = {}
        self._started_at: Dict[str, float] = {}
        self._watch_thread: Optional[threading.Thread] = None
        self._workers: Dict[str, threading.Thread] = {}

    def start(self) -> None:
        if not self._settings.rtsp_enabled:
            logger.info("RTSP gateway disabled (RTSP_ENABLED=false)")
            return
        self._watch_thread = threading.Thread(
            target=self._watch, name="rtsp-watch", daemon=True
        )
        self._watch_thread.start()
        logger.info(
            "RTSP gateway starting; publish target %s",
            self._settings.rtsp_publish_url,
        )

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            procs = list(self._procs.items())
        for cam_id, proc in procs:
            _terminate(proc)
            logger.info("Stopped RTSP publisher for camera %s", cam_id)

    def is_publishing(self, cam_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(cam_id)
            started = self._started_at.get(cam_id, 0.0)
        if proc is None or proc.poll() is not None:
            return False
        # ffmpeg needs a moment to finish the RTSP announce.
        return (time.monotonic() - started) >= 1.0

    def _camera(self, cam_id: str) -> Optional[Camera]:
        for cam in self._get_cameras():
            if cam.id == cam_id:
                return cam
        return None

    def _watch(self) -> None:
        while not self._stop.is_set():
            for cam in self._get_cameras():
                if cam.id in self._workers:
                    continue
                if publish_path(cam) is None:
                    continue
                t = threading.Thread(
                    target=self._run_one, args=(cam.id,),
                    name=f"rtsp-{cam.id}", daemon=True,
                )
                self._workers[cam.id] = t
                t.start()
            self._stop.wait(3.0)

    def _run_one(self, cam_id: str) -> None:
        backoff = 2.0
        last_down_log = 0.0
        while not self._stop.is_set():
            cam = self._camera(cam_id)
            path = publish_path(cam) if cam else None
            if path is None:
                self._stop.wait(5.0)
                continue

            dest = join_url(
                self._settings.rtsp_publish_url,
                rtsp_path(cam_id, self._settings.rtsp_path_prefix),
            )
            if not gateway_reachable(self._settings.rtsp_publish_url):
                now = time.monotonic()
                if now - last_down_log > 30:
                    logger.warning(
                        "MediaMTX not reachable at %s — retrying camera %s",
                        self._settings.rtsp_publish_url, cam_id,
                    )
                    last_down_log = now
                self._stop.wait(3.0)
                continue

            offset = 0.0
            duration = cam.duration if cam else None
            if duration:
                offset = float(self._clock.state(duration).offset or 0.0)

            cmd = ffmpeg_publish_cmd(
                ffmpeg_bin=self._settings.ffmpeg_bin,
                video_path=str(path),
                dest_url=dest,
                offset=offset,
                transport=self._settings.rtsp_transport,
            )
            logger.info(
                "Publishing camera %s -> %s (offset %.1fs, %s)",
                cam_id, dest, offset, path.name,
            )
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError:
                logger.error("ffmpeg not found (%s)", self._settings.ffmpeg_bin)
                self._stop.wait(15.0)
                continue

            with self._lock:
                self._procs[cam_id] = proc
                self._started_at[cam_id] = time.monotonic()

            while proc.poll() is None and not self._stop.is_set():
                self._stop.wait(1.0)

            with self._lock:
                self._procs.pop(cam_id, None)
                self._started_at.pop(cam_id, None)

            if self._stop.is_set():
                _terminate(proc)
                return

            logger.warning(
                "ffmpeg publisher for camera %s exited (%s); restarting",
                cam_id, proc.returncode,
            )
            self._stop.wait(backoff)
            backoff = min(backoff * 1.5, 15.0)
