"""Discover local video files and expose them as numbered cameras.

Each file in the videos folder becomes one camera (numbered by sorted
filename). ffprobe results are cached on disk keyed by path+size+mtime so we
don't re-probe hundreds of GB on every boot. A camera is directly servable if
its video track is H.264 in an MP4/MOV container, or if a processed MP4 already
exists (produced by scripts/prepare_videos.py); otherwise it needs preparation.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import (
    WEB_SAFE_CONTAINERS,
    WEB_SAFE_VIDEO_CODECS,
    Settings,
)

logger = logging.getLogger("cctv.library")

# Noise tokens commonly found in NVR/DVR export filenames.
_NOISE_PATTERNS = [
    r"\d{4}-\d{2}-\d{2}", r"\d{2}-\d{2}-\d{2}",
    r"\bNVR\b", r"\bMyExport_?", r"\bRLVD\b", r"\bch\d+\b",
    r"CSITMS-?\d*", r"PTZ\d*", r"BS-?\d+", r"B\d+-",
    r"\d{2}h\d{2}min\d{2}s\d+ms",
    r"\d{1,2}[A-Za-z]{3}\d{4}",
    r"AR\d+[A-Z]\d+",
    r"\b\d{4,}\b",
]


@dataclass
class Camera:
    id: str
    number: int
    name: str
    location: str
    slug: str
    source_path: Path
    serve_path: Optional[Path]
    duration: Optional[float]
    video_codec: str = ""
    container: str = ""
    size: int = 0
    servable: bool = False
    needs_prepare: bool = False
    error: str = ""
    hls_ready: bool = False
    hls_url: Optional[str] = None

    def public(self) -> dict:
        status = "live" if self.servable else ("processing" if self.needs_prepare else "error")
        return {
            "id": self.id,
            "number": self.number,
            "name": self.name,
            "location": self.location,
            "duration": self.duration,
            "codec": self.video_codec,
            "container": self.container,
            "status": status,
            "delivery": "hls" if self.hls_ready else "progressive",
            "detail": self.error,
        }


@dataclass
class _Probe:
    video_codec: str
    container: str
    duration: Optional[float]
    ok: bool = True
    error: str = ""


class _ProbeCache:
    def __init__(self, path: Path, ffprobe_bin: str):
        self._path = path
        self._ffprobe = ffprobe_bin
        self._data: Dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except Exception:  # noqa: BLE001
                self._data = {}
        self._dirty = False

    def _key(self, file: Path) -> str:
        st = file.stat()
        return f"{file}:{st.st_size}:{st.st_mtime_ns}"

    def probe(self, file: Path) -> _Probe:
        key = self._key(file)
        cached = self._data.get(key)
        if cached is not None:
            return _Probe(**cached)
        result = _run_ffprobe(self._ffprobe, file)
        self._data[key] = result.__dict__
        self._dirty = True
        return result

    def flush(self) -> None:
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))
        self._dirty = False


def _run_ffprobe(ffprobe_bin: str, file: Path) -> _Probe:
    cmd = [
        ffprobe_bin, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name:format=format_name,duration",
        "-of", "json", str(file),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return _Probe("", "", None, ok=False, error="ffprobe not found")
    except subprocess.TimeoutExpired:
        return _Probe("", "", None, ok=False, error="ffprobe timeout")
    if out.returncode != 0:
        return _Probe("", "", None, ok=False, error=out.stderr.strip()[:200] or "probe failed")
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return _Probe("", "", None, ok=False, error="bad ffprobe output")
    streams = data.get("streams") or []
    codec = streams[0].get("codec_name", "") if streams else ""
    fmt = data.get("format", {}) or {}
    container = (fmt.get("format_name", "") or "").split(",")[0]
    duration = None
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except ValueError:
            duration = None
    return _Probe(codec, container, duration)


def slugify(stem: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return slug or "camera"


def clean_location(stem: str) -> str:
    text = stem.replace("_", " ")
    for pat in _NOISE_PATTERNS:
        text = re.sub(pat, " ", text)
    text = re.sub(r"\s*-\s*-\s*", " ", text)      # collapse "- -" remnants
    text = re.sub(r"(^|\s)-(\s|$)", " ", text)     # drop orphan dashes
    text = re.sub(r"\s+", " ", text).strip(" -._")
    return text or stem.strip()


def _is_web_safe(container_from_ext: str, probe: _Probe) -> bool:
    return (
        probe.ok
        and probe.video_codec in WEB_SAFE_VIDEO_CODECS
        and container_from_ext in WEB_SAFE_CONTAINERS
    )


def scan_library(settings: Settings) -> List[Camera]:
    videos_dir = Path(settings.videos_dir)
    processed_dir = videos_dir / settings.processed_subdir
    hls_dir = videos_dir / settings.hls_subdir
    exts = set(settings.extensions)
    cache = _ProbeCache(Path(settings.library_cache_file), settings.ffprobe_bin)

    sources = sorted(
        (
            p for p in videos_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() in exts
            and not p.name.startswith(".")
        ),
        key=lambda p: p.name.lower(),
    )

    cameras: List[Camera] = []
    for number, src in enumerate(sources, start=1):
        stem = src.stem
        slug = slugify(stem)
        probe = cache.probe(src)
        container_ext = src.suffix.lower().lstrip(".")

        processed = processed_dir / f"{slug}.mp4"
        serve_path: Optional[Path] = None
        servable = False
        needs_prepare = False
        error = probe.error

        if processed.exists():
            serve_path = processed
            servable = True
        elif _is_web_safe(container_ext, probe):
            serve_path = src
            servable = True
        else:
            needs_prepare = probe.ok  # a valid but non-web-safe file can be prepared

        # Prefer HLS delivery (segmented, gap-free) when a playlist exists.
        hls_playlist = hls_dir / slug / "index.m3u8"
        hls_ready = hls_playlist.exists()
        hls_url = f"/hls/{slug}/index.m3u8" if hls_ready else None
        if hls_ready:
            servable = True
            needs_prepare = False

        cameras.append(
            Camera(
                id=str(number),
                number=number,
                name=f"Camera {number}",
                location=clean_location(stem),
                slug=slug,
                source_path=src,
                serve_path=serve_path,
                duration=probe.duration,
                video_codec=probe.video_codec,
                container=container_ext,
                size=src.stat().st_size,
                servable=servable,
                needs_prepare=needs_prepare,
                error=error,
                hls_ready=hls_ready,
                hls_url=hls_url,
            )
        )

    cache.flush()
    logger.info(
        "Library: %d cameras (%d servable, %d HLS, %d need prepare)",
        len(cameras),
        sum(c.servable for c in cameras),
        sum(c.hls_ready for c in cameras),
        sum(c.needs_prepare for c in cameras),
    )
    return cameras
