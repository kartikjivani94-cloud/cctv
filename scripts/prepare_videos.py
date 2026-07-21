#!/usr/bin/env python3
"""One-time preparation of the videos folder into browser-playable MP4s.

The runtime server does NO transcoding. This script converts anything a browser
cannot play natively into H.264/MP4 (with the moov atom at the front for
instant seeking) and writes it to ``videos/processed/<slug>.mp4``:

  * H.264 already in MP4/MOV  -> left as-is (served directly), unless --remux-h264
  * H.264 in MKV/AVI/etc      -> fast stream-copy remux to faststart MP4
  * HEVC / other codecs       -> transcode to H.264 (hardware-accelerated when
                                 h264_videotoolbox is available, else libx264)

Re-running is safe: existing outputs are skipped unless --force is given.

Usage:
    python scripts/prepare_videos.py [--videos-dir ./videos] [--force]
                                     [--remux-h264] [--bitrate 6M]
                                     [--crf 23] [--preset veryfast] [--only N]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import (  # noqa: E402
    DEFAULT_VIDEO_EXTENSIONS,
    WEB_SAFE_CONTAINERS,
    WEB_SAFE_VIDEO_CODECS,
)
from app.library import slugify  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def probe(path: Path) -> tuple[str, str]:
    cmd = [
        FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name:format=format_name",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        return "", ""
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    codec = streams[0].get("codec_name", "") if streams else ""
    container = (data.get("format", {}).get("format_name", "") or "").split(",")[0]
    return codec, container


def moov_at_front(path: Path, scan: int = 4_000_000) -> bool:
    """True if the MP4 'moov' atom appears before 'mdat' near the file start.

    Files where moov is at the end (typical of NVR/DVR exports) force the
    browser to hunt through gigabytes before playback/seeking, causing the
    heavy buffering this remux fixes.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(scan)
    except OSError:
        return False
    pm = head.find(b"moov")
    pd = head.find(b"mdat")
    return pm != -1 and (pd == -1 or pm < pd)


def duration_of(path: Path) -> float | None:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except (ValueError, AttributeError):
        return None


def _output_is_valid(src: Path, out: Path) -> bool:
    """A remux/transcode output is only accepted if it is plausibly complete:
    it must exist, be a substantial fraction of the source size, and match the
    source duration within 1%. This prevents ever replacing/keeping a truncated
    or header-only stub (which previously caused data loss)."""
    try:
        if not out.exists() or out.stat().st_size < 4096:
            return False
    except OSError:
        return False
    src_dur = duration_of(src)
    out_dur = duration_of(out)
    if not out_dur:
        return False
    if src_dur and abs(out_dur - src_dur) > max(2.0, src_dur * 0.01):
        return False
    # Stream copies stay close in size; require at least half the source bytes.
    if out.stat().st_size < src.stat().st_size * 0.5:
        return False
    return True


def faststart_inplace(src: Path, force: bool) -> str:
    """Losslessly rewrite an MP4/MOV with moov at the front, replacing the
    original ONLY after validating the output. Returns 'skip'|'done'|'fail'.
    A temp file in the same directory keeps disk usage flat; the original is
    never touched unless the new file passes validation."""
    if not force and moov_at_front(src):
        return "skip"
    tmp = src.with_name(src.stem + ".faststart.tmp" + src.suffix)
    tmp.unlink(missing_ok=True)
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-map", "0", "-c", "copy",
        "-movflags", "+faststart", str(tmp),
    ]
    result = subprocess.run(cmd)
    if result.returncode == 0 and _output_is_valid(src, tmp):
        tmp.replace(src)  # atomic within same filesystem
        return "done"
    tmp.unlink(missing_ok=True)
    return "fail"


def hardware_encoder_available() -> bool:
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True
        )
    except FileNotFoundError:
        return False
    return "h264_videotoolbox" in out.stdout


def build_command(src: Path, dst_tmp: Path, action: str, args) -> list[str]:
    base = [FFMPEG, "-hide_banner", "-y", "-i", str(src), "-map", "0:v:0",
            "-map", "0:a?", "-sn"]
    if action == "remux":
        enc = ["-c", "copy"]
    else:  # transcode
        if not args.force_software and hardware_encoder_available():
            enc = ["-c:v", "h264_videotoolbox", "-b:v", args.bitrate,
                   "-tag:v", "avc1", "-pix_fmt", "yuv420p"]
        else:
            enc = ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
                   "-pix_fmt", "yuv420p"]
        enc += ["-c:a", "aac", "-b:a", "128k"]
    return base + enc + ["-movflags", "+faststart", str(dst_tmp)]


def is_web_safe(codec: str, container_ext: str) -> bool:
    return codec in WEB_SAFE_VIDEO_CODECS and container_ext in WEB_SAFE_CONTAINERS


def _servable_source(src: Path, processed_dir: Path) -> Path | None:
    """Return the browser-playable file to segment: the processed MP4 if it
    exists, else the source itself if it is already H.264 MP4/MOV."""
    slug = slugify(src.stem)
    processed = processed_dir / f"{slug}.mp4"
    if processed.exists():
        return processed
    codec, _ = probe(src)
    if is_web_safe(codec, src.suffix.lower().lstrip(".")):
        return src
    return None


def _run_hls(sources, args) -> int:
    """Segment each servable camera into a lossless (-c copy) HLS VOD playlist.

    HLS delivery lets the browser keep a multi-segment buffer ahead, giving
    gap-free continuous playback; segments start on keyframes so seeking to the
    live offset is clean. Quality is identical to the source (stream copy)."""
    videos_dir = Path(args.videos_dir)
    processed_dir = videos_dir / "processed"
    hls_root = videos_dir / "hls"
    hls_root.mkdir(parents=True, exist_ok=True)

    done = skipped = failed = 0
    for number, src in enumerate(sources, start=1):
        if args.only and number != args.only:
            continue
        slug = slugify(src.stem)
        playlist = hls_root / slug / "index.m3u8"
        if playlist.exists() and not args.force:
            print(f"[{number}] SKIP (HLS exists): {slug}")
            skipped += 1
            continue
        servable = _servable_source(src, processed_dir)
        if servable is None:
            print(f"[{number}] SKIP (needs transcode first; run without --hls): {src.name}")
            skipped += 1
            continue

        out_dir = hls_root / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("*.ts"):
            old.unlink()
        print(f"[{number}] HLS {src.name} -> hls/{slug}/ ...", flush=True)
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(servable), "-c", "copy",
            "-f", "hls", "-hls_time", str(args.hls_time),
            "-hls_playlist_type", "vod", "-hls_flags", "independent_segments",
            "-hls_segment_type", "mpegts",
            "-hls_list_size", "0",
            "-hls_segment_filename", str(out_dir / "seg_%05d.ts"),
            str(playlist),
        ]
        result = subprocess.run(cmd)
        if result.returncode == 0 and playlist.exists() and any(out_dir.glob("*.ts")):
            done += 1
            print(f"    done ({len(list(out_dir.glob('*.ts')))} segments)")
        else:
            failed += 1
            print(f"    FAILED ({result.returncode})")
    print(f"\nSummary: {done} segmented, {skipped} skipped, {failed} failed.")
    return 0 if failed == 0 else 2


def _run_inplace(sources, args) -> int:
    """Faststart-remux H.264 MP4/MOV files in place (no processed/ copies)."""
    print(f"In-place faststart remux over {len(sources)} candidate files.\n")
    done = skipped = failed = 0
    for number, src in enumerate(sources, start=1):
        if args.only and number != args.only:
            continue
        codec, _ = probe(src)
        container_ext = src.suffix.lower().lstrip(".")
        if not is_web_safe(codec, container_ext):
            print(f"[{number}] SKIP (not H.264 MP4/MOV; use normal mode): {src.name}")
            skipped += 1
            continue
        if not args.force and moov_at_front(src):
            print(f"[{number}] SKIP (already faststart): {src.name}")
            skipped += 1
            continue
        print(f"[{number}] FASTSTART {src.name} ...", flush=True)
        outcome = faststart_inplace(src, args.force)
        if outcome == "done":
            done += 1
            print(f"    done (moov moved to front)")
        elif outcome == "skip":
            skipped += 1
            print(f"    skip (already faststart)")
        else:
            failed += 1
            print(f"    FAILED")
    print(f"\nSummary: {done} remuxed, {skipped} skipped, {failed} failed.")
    return 0 if failed == 0 else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--videos-dir", default="./videos")
    ap.add_argument("--force", action="store_true", help="reprocess even if output exists")
    ap.add_argument("--remux-h264", action="store_true",
                    help="also faststart-remux H.264 MP4s (otherwise served as-is)")
    ap.add_argument("--bitrate", default="6M", help="target bitrate for hardware transcode")
    ap.add_argument("--crf", type=int, default=23, help="libx264 CRF (software transcode)")
    ap.add_argument("--preset", default="veryfast", help="libx264 preset")
    ap.add_argument("--force-software", action="store_true", help="force libx264 (no hardware)")
    ap.add_argument("--only", type=int, default=None, help="process only camera number N")
    ap.add_argument("--inplace", action="store_true",
                    help="faststart-remux H.264 MP4/MOV files in place (lossless, "
                         "no extra disk); fixes moov-at-end buffering")
    ap.add_argument("--hls", action="store_true",
                    help="segment servable cameras into lossless HLS playlists for "
                         "gap-free continuous playback")
    ap.add_argument("--hls-time", type=int, default=6, help="HLS segment length (s)")
    ap.add_argument("--skip-transcode", action="store_true",
                    help="skip HEVC/other transcodes (only do fast remuxes)")
    args = ap.parse_args()

    videos_dir = Path(args.videos_dir)
    processed = videos_dir / "processed"
    exts = {e if e.startswith(".") else f".{e}"
            for e in (x.strip().lower() for x in DEFAULT_VIDEO_EXTENSIONS.split(","))}

    sources = sorted(
        (p for p in videos_dir.iterdir()
         if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )
    if not sources:
        print(f"No videos found in {videos_dir}")
        return 1

    if args.hls:
        return _run_hls(sources, args)

    if args.inplace:
        return _run_inplace(sources, args)

    processed.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(sources)} videos. Hardware encoder: "
          f"{'yes' if hardware_encoder_available() and not args.force_software else 'no (libx264)'}\n")

    processed_count = skipped = failed = 0
    for number, src in enumerate(sources, start=1):
        if args.only and number != args.only:
            continue
        slug = slugify(src.stem)
        codec, container = probe(src)
        container_ext = src.suffix.lower().lstrip(".")
        dst = processed / f"{slug}.mp4"

        web_safe = is_web_safe(codec, container_ext)
        if web_safe and not args.remux_h264:
            print(f"[{number}] SKIP (already web-safe h264/{container_ext}): {src.name}")
            skipped += 1
            continue
        if dst.exists() and not args.force:
            print(f"[{number}] SKIP (exists): {dst.name}")
            skipped += 1
            continue

        action = "remux" if codec in WEB_SAFE_VIDEO_CODECS else "transcode"
        if action == "transcode" and args.skip_transcode:
            print(f"[{number}] SKIP (transcode skipped): {src.name}")
            skipped += 1
            continue
        print(f"[{number}] {action.upper()} {src.name}  ({codec}/{container_ext}) -> {dst.name}")
        tmp = dst.with_suffix(".tmp.mp4")
        tmp.unlink(missing_ok=True)
        cmd = build_command(src, tmp, action, args)
        result = subprocess.run(cmd)
        # For remux (stream copy) validate against the source; transcodes only
        # need a valid duration match (size differs by design).
        valid = tmp.exists() and duration_of(tmp) is not None
        if action == "remux":
            valid = _output_is_valid(src, tmp)
        else:
            sd, od = duration_of(src), duration_of(tmp)
            valid = tmp.exists() and od is not None and (
                not sd or abs(od - sd) <= max(2.0, sd * 0.02)
            )
        if result.returncode == 0 and valid:
            tmp.replace(dst)
            processed_count += 1
            print(f"    done -> {dst}")
        else:
            failed += 1
            tmp.unlink(missing_ok=True)
            print(f"    FAILED ({result.returncode}, output validation="
                  f"{'ok' if valid else 'BAD'})")

    print(f"\nSummary: {processed_count} processed, {skipped} skipped, {failed} failed.")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
