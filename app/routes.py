"""HTTP routes: HTML pages, camera-state API, and the local range proxy."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .streaming import guess_content_type, parse_range, stream_file

logger = logging.getLogger("cctv.routes")

STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter()


@router.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@router.get("/camera/{cam_id}")
async def camera_page(cam_id: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "camera.html")


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/api/prepare/status")
async def prepare_status(request: Request) -> JSONResponse:
    """Returns per-camera preparation progress from the background AutoPreparer."""
    preparer = getattr(request.app.state, "preparer", None)
    if preparer is None:
        return JSONResponse({"done": True, "cameras": []})
    return JSONResponse({"done": preparer.done, "cameras": preparer.statuses()})


@router.get("/api/cameras")
async def list_cameras(request: Request) -> JSONResponse:
    cameras = [cam.public() for cam in request.app.state.cameras]
    return JSONResponse({"cameras": cameras})


@router.get("/api/cameras/{cam_id}/state")
async def camera_state(cam_id: str, request: Request) -> JSONResponse:
    state = request.app.state
    cam = state.cameras_by_id.get(cam_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Unknown camera")

    base = cam.public()
    if not cam.servable:
        return JSONResponse(base)

    feed = state.clock.state(cam.duration)
    payload = {
        **base,
        "stream_url": f"/stream/{cam.id}",
        "hls_url": cam.hls_url,
        "timezone": state.settings.timezone,
        "drift_tolerance": state.settings.drift_tolerance_seconds,
        **feed.to_dict(),
    }
    return JSONResponse(payload)


@router.api_route("/stream/{cam_id}", methods=["GET", "HEAD"])
async def stream(cam_id: str, request: Request):
    state = request.app.state
    cam = state.cameras_by_id.get(cam_id)
    if cam is None or not cam.servable or cam.serve_path is None:
        raise HTTPException(status_code=404, detail="Unknown or unavailable camera")

    path = cam.serve_path
    try:
        st = path.stat()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Video file missing")
    size = st.st_size

    # The bytes for any given range of a static file never change, so let the
    # browser cache ranges. This avoids re-downloading already-buffered data
    # when the player re-seeks (drift correction), cutting re-buffering.
    etag = f'"{st.st_mtime_ns:x}-{size:x}"'
    parsed = parse_range(request.headers.get("range"), size)
    if parsed is None:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    start, end = parsed
    has_range = request.headers.get("range") is not None
    length = end - start + 1

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": guess_content_type(path),
        "Cache-Control": "public, max-age=86400",
        "ETag": etag,
    }
    status_code = 200
    if has_range:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        status_code = 206

    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers)

    return StreamingResponse(
        stream_file(path, start, end, state.settings.stream_chunk_size),
        status_code=status_code,
        headers=headers,
    )
