"""Application factory and lifecycle wiring for the CCTV live-feed simulator."""
from __future__ import annotations

import base64
import binascii
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .autoprepare import AutoPreparer
from .config import get_settings
from .library import scan_library
from .routes import router
from .rtsp import RtspGateway
from .timefeed import FeedClock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cctv")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Enlarge the default thread pool: each active viewer stream performs blocking
    # os.pread calls via asyncio.to_thread. A single user can open every camera at
    # once, so we need plenty of headroom to avoid starving reads (= buffering).
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=settings.read_threads))

    cameras = scan_library(settings)
    clock = FeedClock(
        timezone=settings.timezone,
        slot_start=settings.slot_start_time,
        slot_seconds=settings.slot_seconds,
        loop_within_video=settings.loop_within_video,
    )

    for cam in cameras:
        logger.info(
            "Camera %s '%s' [%s/%s] %.1fh -> %s",
            cam.id, cam.location, cam.container, cam.video_codec,
            (cam.duration or 0) / 3600.0,
            "HLS-ready" if cam.hls_ready else (
                "servable" if cam.servable else (
                    "needs prepare" if cam.needs_prepare else "error"
                )
            ),
        )

    app.state.settings = settings
    app.state.cameras = cameras
    app.state.cameras_by_id = {c.id: c for c in cameras}
    app.state.clock = clock

    # Only one gunicorn worker should run the background preparer to avoid
    # multiple ffmpeg processes fighting over the same files.
    import os
    is_worker_zero = os.environ.get("CCTV_WORKER_ID", "0") == "0"

    preparer = AutoPreparer(settings)
    if not settings.single_preparer or is_worker_zero:
        preparer.start(cameras, app)
    else:
        logger.info("AutoPrepare skipped on this worker (CCTV_WORKER_ID=%s)",
                     os.environ.get("CCTV_WORKER_ID"))
    app.state.preparer = preparer

    rtsp = RtspGateway(settings, clock, lambda: app.state.cameras)
    if not settings.single_preparer or is_worker_zero:
        rtsp.start()
    app.state.rtsp = rtsp
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0), follow_redirects=True)

    try:
        yield
    finally:
        rtsp.stop()
        await app.state.http.aclose()


def _add_basic_auth(app: FastAPI, username: str, password: str) -> None:
    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                user, _, pw = decoded.partition(":")
                if secrets.compare_digest(user, username) and secrets.compare_digest(pw, password):
                    return await call_next(request)
            except (binascii.Error, UnicodeDecodeError):
                pass
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="CCTV"'},
            content="Authentication required",
        )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="CCTV Live-Feed Simulator", version="3.0.0", lifespan=lifespan)

    if settings.auth_enabled:
        _add_basic_auth(app, settings.share_username, settings.share_password)
        logger.info("HTTP Basic Auth enabled for sharing.")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["*"],
    )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Serve HLS playlists/segments directly (StaticFiles handles Range + caching).
    hls_dir = Path(settings.videos_dir) / settings.hls_subdir
    hls_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/hls", StaticFiles(directory=str(hls_dir)), name="hls")

    app.include_router(router)
    return app


app = create_app()
