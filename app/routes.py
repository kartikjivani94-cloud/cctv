"""HTTP routes: HTML pages, camera-state API, and the local range proxy."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .rtsp import advertised_urls, public_host_from_request, publish_path
from .streaming import guess_content_type, parse_range, stream_file

logger = logging.getLogger("cctv.routes")

STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter()


def _public_host(request: Request) -> str:
    return public_host_from_request(
        request.headers.get("host"),
        request.app.state.settings.rtsp_public_host,
    )


def _live_urls(cam_id: str, request: Request) -> dict:
    settings = request.app.state.settings
    return advertised_urls(
        cam_id,
        public_host=_public_host(request),
        rtsp_port=settings.rtsp_port,
        webrtc_port=settings.webrtc_port,
        path_prefix=settings.rtsp_path_prefix,
        hls_via_proxy=settings.hls_live_via_proxy,
    )


def _is_rtsp_live(cam_id: str, request: Request) -> bool:
    gw = getattr(request.app.state, "rtsp", None)
    if gw is not None and gw.is_publishing(cam_id):
        return True
    settings = request.app.state.settings
    if not getattr(settings, "rtsp_enabled", False):
        return False
    cam = request.app.state.cameras_by_id.get(cam_id)
    return cam is not None and publish_path(cam) is not None


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
    cameras = []
    for cam in request.app.state.cameras:
        pub = cam.public()
        if _is_rtsp_live(cam.id, request):
            pub["delivery"] = "rtsp"
            pub.update(_live_urls(cam.id, request))
        cameras.append(pub)
    return JSONResponse({"cameras": cameras})


@router.get("/api/ingest")
async def ingest_catalog(request: Request) -> JSONResponse:
    """RTSP/WebRTC/HLS endpoints for AI inference clients and dashboards."""
    cameras = []
    for cam in request.app.state.cameras:
        if not cam.servable and not _is_rtsp_live(cam.id, request):
            continue
        entry = {
            "id": cam.id,
            "number": cam.number,
            "name": cam.name,
            "location": cam.location,
            "codec": cam.video_codec,
            "live": _is_rtsp_live(cam.id, request),
            **_live_urls(cam.id, request),
        }
        cameras.append(entry)
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
    if _is_rtsp_live(cam.id, request):
        payload["delivery"] = "rtsp"
        payload.update(_live_urls(cam.id, request))
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


@router.api_route("/live/{path:path}", methods=["GET", "HEAD"])
async def live_hls_proxy(path: str, request: Request):
    """Proxy MediaMTX live HLS so the dashboard can play same-origin."""
    origin = request.app.state.settings.hls_live_origin.rstrip("/")
    url = f"{origin}/{path}"
    client = getattr(request.app.state, "http", None)
    if client is None:
        raise HTTPException(status_code=502, detail="Live gateway client not ready")
    headers = {}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    try:
        upstream = await client.request(request.method, url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live HLS proxy failed for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail="Live gateway unavailable") from exc
    out_headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-cache",
    }
    content_type = upstream.headers.get("content-type", "application/octet-stream")
    if "content-range" in upstream.headers:
        out_headers["Content-Range"] = upstream.headers["content-range"]
        out_headers["Accept-Ranges"] = "bytes"
    return Response(
        content=upstream.content if request.method != "HEAD" else b"",
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=content_type,
    )
