"""Automatic background preparation of video files on server startup.

When the server starts it launches a background thread that walks every
camera that is not yet HLS-ready and converts it:

  1. H.264 MP4/MOV without faststart → lossless in-place moov remux.
  2. H.264 in MKV/AVI/etc           → lossless remux  → videos/processed/<slug>.mp4
  3. HEVC / other codecs            → H.264 transcode → videos/processed/<slug>.mp4
  4. All servable cameras           → HLS segments    → videos/hls/<slug>/

Each step is idempotent; restarting the server skips already-done work.
The app.state.cameras list is patched live as each camera becomes ready
so viewers can start using a camera the moment its HLS playlist appears.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from fastapi import FastAPI
    from .config import Settings
    from .library import Camera

logger = logging.getLogger("cctv.autoprepare")

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


# ---------------------------------------------------------------------------
# Internal helpers (duplicated from prepare_videos.py to avoid a scripts/
# import from inside the app package)
# ---------------------------------------------------------------------------

def _moov_at_front(path: Path, scan: int = 4_000_000) -> bool:
    try:
        head = path.read_bytes()[:scan]
    except OSError:
        return False
    pm, pd = head.find(b"moov"), head.find(b"mdat")
    return pm != -1 and (pd == -1 or pm < pd)


def _duration_of(path: Path) -> Optional[float]:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except (ValueError, AttributeError):
        return None


def _validate(src: Path, out: Path, remux: bool) -> bool:
    try:
        if not out.exists() or out.stat().st_size < 4096:
            return False
    except OSError:
        return False
    out_dur = _duration_of(out)
    if not out_dur:
        return False
    src_dur = _duration_of(src)
    if src_dur:
        # For remux the duration must match tightly (stream copy, no frame drops).
        # For transcode, corrupted sources may drop many frames so allow a wider
        # tolerance (within 10% or 60s, whichever is larger).
        tol = max(2.0, src_dur * 0.02) if remux else max(60.0, src_dur * 0.10)
        if abs(out_dur - src_dur) > tol:
            return False
    # Remux output must be at least half the source size (stream copy).
    # Transcode output is always smaller (re-encoded), so skip size check.
    if remux and out.stat().st_size < src.stat().st_size * 0.5:
        return False
    return True


def _hw_encoder() -> bool:
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                           capture_output=True, text=True)
        return "h264_videotoolbox" in r.stdout
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Status dataclass
# ---------------------------------------------------------------------------

@dataclass
class PrepStatus:
    camera_id: str
    slug: str
    # waiting | remuxing | transcoding | hls | done | skip | error
    state: str
    message: str = ""


# ---------------------------------------------------------------------------
# AutoPreparer
# ---------------------------------------------------------------------------

class AutoPreparer:
    """Spawns a single background thread on start(); updates app state live."""

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings
        self._statuses: Dict[str, PrepStatus] = {}
        self._lock = threading.Lock()
        self._done = False

    # ---- public API -------------------------------------------------------

    def start(self, cameras: "List[Camera]", app: "FastAPI") -> None:
        from .library import scan_library  # avoid circular at module level

        needs_work = []
        for cam in cameras:
            if cam.hls_ready:
                self._put(cam.id, PrepStatus(cam.id, cam.slug, "skip", "Already HLS-ready"))
            else:
                self._put(cam.id, PrepStatus(cam.id, cam.slug, "waiting", "Queued"))
                needs_work.append(cam)

        if not needs_work:
            self._done = True
            logger.info("AutoPrepare: all cameras already HLS-ready")
            return

        logger.info("AutoPrepare: %d camera(s) need preparation — starting background thread", len(needs_work))
        t = threading.Thread(
            target=self._run, args=(needs_work, app), daemon=True, name="autoprepare"
        )
        t.start()

    def statuses(self) -> List[dict]:
        with self._lock:
            return [v.__dict__.copy() for v in self._statuses.values()]

    @property
    def done(self) -> bool:
        return self._done

    # ---- private -----------------------------------------------------------

    def _put(self, cam_id: str, status: PrepStatus) -> None:
        with self._lock:
            self._statuses[cam_id] = status

    def _set(self, cam_id: str, state: str, message: str = "") -> None:
        with self._lock:
            s = self._statuses.get(cam_id)
            if s:
                s.state = state
                s.message = message

    def _run(self, needs_work: "List[Camera]", app: "FastAPI") -> None:
        from .library import scan_library

        settings = self._settings
        videos_dir = Path(settings.videos_dir)
        processed_dir = videos_dir / settings.processed_subdir
        hls_root = videos_dir / settings.hls_subdir
        processed_dir.mkdir(parents=True, exist_ok=True)
        hls_root.mkdir(parents=True, exist_ok=True)

        hw = _hw_encoder()

        for cam in needs_work:
            try:
                self._process(cam, processed_dir, hls_root, settings, hw)
            except Exception as exc:
                logger.exception("AutoPrepare: camera %s unhandled error", cam.id)
                self._set(cam.id, "error", str(exc))

            # Patch just this camera in app.state so it becomes live immediately.
            try:
                fresh_cameras = scan_library(settings)
                fresh_map = {c.id: c for c in fresh_cameras}
                # Replace only the camera we just finished (preserve ordering).
                updated = [fresh_map.get(c.id, c) for c in app.state.cameras]
                app.state.cameras = updated
                app.state.cameras_by_id = {c.id: c for c in updated}
            except Exception as exc:
                logger.warning("AutoPrepare: library re-scan after camera %s failed: %s", cam.id, exc)

        self._done = True
        logger.info("AutoPrepare: finished all cameras")

    def _process(self, cam: "Camera", processed_dir: Path, hls_root: Path,
                 settings: "Settings", hw: bool) -> None:
        cam_id = cam.id
        src = cam.source_path
        slug = cam.slug

        # ---- Step 1: produce a servable (H.264 MP4) file ----
        servable = self._ensure_servable(cam, processed_dir, hw)
        if servable is None:
            return  # error already recorded

        # ---- Step 2: HLS segmentation ----
        playlist = hls_root / slug / "index.m3u8"
        if playlist.exists():
            self._set(cam_id, "done", "")
            return

        self._set(cam_id, "hls", "Generating HLS segments…")
        out_dir = hls_root / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("*.ts"):
            old.unlink()

        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(servable), "-c", "copy",
            "-f", "hls", "-hls_time", str(settings.hls_time),
            "-hls_playlist_type", "vod", "-hls_flags", "independent_segments",
            "-hls_segment_type", "mpegts", "-hls_list_size", "0",
            "-hls_segment_filename", str(out_dir / "seg_%05d.ts"),
            str(playlist),
        ]
        r = subprocess.run(cmd, capture_output=True)
        seg_count = len(list(out_dir.glob("*.ts")))
        if r.returncode == 0 and playlist.exists() and seg_count > 0:
            self._set(cam_id, "done", f"{seg_count} segments")
            logger.info("AutoPrepare: camera %s HLS ready (%d segments)", cam_id, seg_count)
        else:
            self._set(cam_id, "error", "HLS segmentation failed")
            logger.error("AutoPrepare: camera %s HLS failed: %s", cam_id,
                         r.stderr.decode(errors="replace")[:300])

    def _ensure_servable(self, cam: "Camera", processed_dir: Path, hw: bool) -> Optional[Path]:
        """Return a web-safe H.264 MP4 path, converting if necessary."""
        src = cam.source_path
        slug = cam.slug
        container_ext = src.suffix.lower().lstrip(".")

        # Already processed?
        processed = processed_dir / f"{slug}.mp4"
        if processed.exists():
            return processed

        # Source is already web-safe H.264 MP4/MOV?
        if cam.video_codec == "h264" and container_ext in {"mp4", "m4v", "mov"}:
            if not _moov_at_front(src):
                self._set(cam.id, "remuxing", "Fixing moov atom position…")
                ok = self._faststart_inplace(cam.id, src)
                if not ok:
                    return None
            return src

        # H.264 in non-web container → lossless remux to MP4.
        if cam.video_codec == "h264":
            self._set(cam.id, "remuxing", "Remuxing to MP4…")
            return self._convert(cam.id, src, processed, remux=True)

        # Other codec (HEVC, etc.) → transcode to H.264.
        if cam.needs_prepare:
            self._set(cam.id, "transcoding", "Transcoding to H.264 MP4…")
            return self._convert(cam.id, src, processed, remux=False, hw=hw)

        # Servable via progressive (shouldn't reach here, but safe fallback).
        return cam.serve_path

    def _faststart_inplace(self, cam_id: str, src: Path) -> bool:
        tmp = src.with_name(src.stem + "._fstmp" + src.suffix)
        tmp.unlink(missing_ok=True)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(src), "-map", "0", "-c", "copy",
               "-movflags", "+faststart", str(tmp)]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and _validate(src, tmp, remux=True):
            tmp.replace(src)
            logger.info("AutoPrepare: camera %s faststart remux done", cam_id)
            return True
        tmp.unlink(missing_ok=True)
        self._set(cam_id, "error", "In-place faststart remux failed")
        return False

    def _convert(self, cam_id: str, src: Path, dst: Path,
                 remux: bool, hw: bool = False) -> Optional[Path]:
        tmp = dst.with_suffix(".tmp.mp4")
        tmp.unlink(missing_ok=True)

        if remux:
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                   "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-sn",
                   "-c", "copy", "-movflags", "+faststart", str(tmp)]
        else:
            # Always use libx264 (software) for HEVC transcoding: NVR/DVR HEVC
            # streams often have broken reference frames that h264_videotoolbox
            # rejects. libx264 is more tolerant and avoids hardware-session
            # contention across gunicorn worker processes.
            enc = ["-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "23", "-pix_fmt", "yuv420p"]
            # Input flags (BEFORE -i): discard corrupt packets, ignore DTS gaps,
            # tolerate HEVC bitstream errors common in NVR/DVR exports.
            cmd = ([FFMPEG, "-hide_banner", "-loglevel", "warning", "-y",
                    "-fflags", "+discardcorrupt+genpts+igndts",
                    "-err_detect", "ignore_err",
                    "-i", str(src),
                    "-map", "0:v:0", "-map", "0:a?", "-sn",
                    "-max_muxing_queue_size", "9999"]
                   + enc + ["-c:a", "aac", "-b:a", "128k",
                             "-movflags", "+faststart", str(tmp)])

        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and _validate(src, tmp, remux=remux):
            tmp.replace(dst)
            logger.info("AutoPrepare: camera %s -> %s", cam_id, dst.name)
            return dst

        tmp.unlink(missing_ok=True)
        err = r.stderr.decode(errors="replace")[:300]
        self._set(cam_id, "error", f"Conversion failed: {err}")
        logger.error("AutoPrepare: camera %s conversion failed: %s", cam_id, err)
        return None
