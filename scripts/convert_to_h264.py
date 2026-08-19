#!/usr/bin/env python3
"""Convert every video in ``videos/`` to a browser-safe H.264 MP4.

This is a one-shot, offline converter. It does not start the server.

Output (same layout the server already looks for):

    videos/processed/<slug>.mp4

Rules:

  * Genuine H.264 in MP4/MOV/M4V  → lossless remux (+faststart)
  * H.264 in MKV/AVI/etc          → lossless remux into MP4
  * HEVC / other codecs           → transcode to H.264 (libx264)
  * Mislabeled DVR exports        → detect the real stream (hevc/h264/mpegts)
                                    and force that demuxer, then convert

Existing outputs are skipped unless ``--force`` is given. The original files
are never overwritten.

Usage:
    python scripts/convert_to_h264.py
    python scripts/convert_to_h264.py --videos-dir ./videos --force
    python scripts/convert_to_h264.py --only 3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import DEFAULT_VIDEO_EXTENSIONS, WEB_SAFE_VIDEO_CODECS  # noqa: E402
from app.library import slugify  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
FALLBACK_DEMUXERS = ("hevc", "h264", "mpegts", "mpeg4")
WEB_SAFE_EXTS = {"mp4", "m4v", "mov"}


def duration_of(path: Path, input_format: str = "") -> float | None:
    cmd = [FFPROBE, "-v", "error"]
    if input_format:
        cmd += ["-f", input_format]
    cmd += ["-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except (ValueError, AttributeError):
        return None


def decodes(path: Path, input_format: str = "") -> bool:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    if input_format:
        cmd += ["-f", input_format]
    cmd += ["-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-y", os.devnull]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def probe_codec(path: Path, input_format: str = "") -> tuple[str, str, float | None]:
    cmd = [FFPROBE, "-v", "error"]
    if input_format:
        cmd += ["-f", input_format]
    cmd += [
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name:format=format_name,duration",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        return "", "", None
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    codec = streams[0].get("codec_name", "") if streams else ""
    fmt = data.get("format", {}) or {}
    container = (fmt.get("format_name", "") or "").split(",")[0]
    dur = None
    if fmt.get("duration"):
        try:
            dur = float(fmt["duration"])
        except ValueError:
            dur = None
    return codec, container, dur


def inspect(path: Path) -> tuple[str, str, float | None, str]:
    """Return (codec, container, duration, forced_demuxer).

    ``forced_demuxer`` is empty when the container is honest; otherwise it is
    the raw demuxer that actually yields frames (e.g. ``hevc``).
    """
    codec, container, duration = probe_codec(path)
    if decodes(path):
        return codec, container, duration, ""

    for fmt in FALLBACK_DEMUXERS:
        if not decodes(path, fmt):
            continue
        rec_codec, rec_container, rec_dur = probe_codec(path, fmt)
        return rec_codec or fmt, rec_container or container, rec_dur or duration, fmt

    return codec, container, duration, ""


def output_is_valid(src: Path, out: Path, remux: bool, src_format: str = "") -> bool:
    try:
        if not out.exists() or out.stat().st_size < 4096:
            return False
    except OSError:
        return False
    out_dur = duration_of(out)
    if not out_dur:
        return False
    src_dur = duration_of(src, src_format)
    if src_dur:
        tol = max(2.0, src_dur * 0.02) if remux else max(60.0, src_dur * 0.10)
        if abs(out_dur - src_dur) > tol:
            return False
    if remux and out.stat().st_size < src.stat().st_size * 0.5:
        return False
    return True


def convert(src: Path, dst: Path, codec: str, src_format: str) -> bool:
    tmp = dst.with_suffix(".tmp.mp4")
    tmp.unlink(missing_ok=True)
    fmt_in = ["-f", src_format] if src_format else []
    # Stream-copy only when the *actual* codec is already H.264. A forced
    # demuxer is fine here (raw H.264 elementary stream → MP4). HEVC always
    # transcodes so browsers can play it.
    remux = codec in WEB_SAFE_VIDEO_CODECS

    if remux:
        cmd = (
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
            + fmt_in
            + ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-sn",
               "-c", "copy", "-movflags", "+faststart", str(tmp)]
        )
    else:
        # HEVC / other codecs, including DVR files whose container lies.
        cmd = (
            [FFMPEG, "-hide_banner", "-loglevel", "warning", "-y",
             "-fflags", "+discardcorrupt+genpts+igndts",
             "-err_detect", "ignore_err"]
            + fmt_in
            + ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-sn",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
               "-max_muxing_queue_size", "9999",
               "-movflags", "+faststart", str(tmp)]
        )

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode == 0 and output_is_valid(src, tmp, remux=remux, src_format=src_format):
        tmp.replace(dst)
        return True
    tmp.unlink(missing_ok=True)
    err = result.stderr.decode(errors="replace")[:400]
    if err:
        print(f"    ffmpeg: {err}")
    return False


def list_sources(videos_dir: Path) -> list[Path]:
    exts = {
        e if e.startswith(".") else f".{e}"
        for e in (x.strip().lower() for x in DEFAULT_VIDEO_EXTENSIONS.split(","))
    }
    return sorted(
        (
            p for p in videos_dir.iterdir()
            if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(".")
        ),
        key=lambda p: p.name.lower(),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--videos-dir", default="./videos")
    ap.add_argument("--force", action="store_true", help="reconvert even if output already exists")
    ap.add_argument("--only", type=int, default=None, help="convert only camera number N")
    args = ap.parse_args()

    videos_dir = Path(args.videos_dir)
    processed_dir = videos_dir / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)

    sources = list_sources(videos_dir)
    if not sources:
        print(f"No videos found in {videos_dir}")
        return 1

    print(f"Found {len(sources)} video(s) in {videos_dir}\n")
    done = skipped = failed = 0

    for number, src in enumerate(sources, start=1):
        if args.only and number != args.only:
            continue

        slug = slugify(src.stem)
        dst = processed_dir / f"{slug}.mp4"

        if dst.exists() and not args.force:
            print(f"[{number}] SKIP (already processed): {dst.name}")
            skipped += 1
            continue

        codec, container, duration, src_format = inspect(src)
        hours = (duration or 0) / 3600.0
        note = f" via -f {src_format}" if src_format else ""
        action = "REMUX" if codec in WEB_SAFE_VIDEO_CODECS else "TRANSCODE"
        print(
            f"[{number}] {action} {src.name}  ({codec}/{src.suffix.lstrip('.')}"
            f"{note}, {hours:.1f}h) -> processed/{dst.name}",
            flush=True,
        )

        if not codec and not src_format:
            print("    FAILED (no decodable video stream)")
            failed += 1
            continue

        if convert(src, dst, codec, src_format):
            out_dur = duration_of(dst) or 0
            print(f"    done  ({out_dur / 3600.0:.1f}h, {dst.stat().st_size / 1e6:.1f} MB)")
            done += 1
        else:
            print("    FAILED")
            failed += 1

    print(f"\nSummary: {done} converted, {skipped} skipped, {failed} failed.")
    print(f"Outputs: {processed_dir}/")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
