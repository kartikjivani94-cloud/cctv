"""Centralized configuration for the CCTV live-feed simulator.

Videos are read from a local folder. Each video file becomes one camera,
numbered by sorted filename. Everything is environment-driven so the same code
can run locally or be exposed to the internet without edits.
"""
from __future__ import annotations

from datetime import time
from functools import lru_cache
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Containers/codecs a browser can play directly (video track must be H.264).
WEB_SAFE_VIDEO_CODECS = {"h264"}
WEB_SAFE_CONTAINERS = {"mp4", "m4v", "mov"}
DEFAULT_VIDEO_EXTENSIONS = ".mp4,.m4v,.mov,.mkv,.avi,.webm"


def _parse_hhmm(value: str) -> time:
    hour, minute = (int(part) for part in value.strip().split(":"))
    return time(hour=hour, minute=minute)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Video source --------------------------------------------------------
    videos_dir: str = "./videos"
    processed_subdir: str = "processed"  # where prepare_videos.py writes MP4s
    hls_subdir: str = "hls"              # where HLS playlists/segments live
    hls_time: int = 6                    # target HLS segment length (seconds)
    video_extensions: str = DEFAULT_VIDEO_EXTENSIONS
    library_cache_file: str = "./cache/library.json"
    ffprobe_bin: str = "ffprobe"
    ffmpeg_bin: str = "ffmpeg"

    # --- Feed timing (two 12h slots per day) ---------------------------------
    timezone: str = "Asia/Kolkata"
    slot_start: str = "21:00"  # wall-clock time that maps to each video's start
    slot_hours: float = 12.0   # length of one slot; the day has 2 back-to-back slots
    loop_within_video: bool = True   # if a clip is shorter than a slot, loop it
    drift_tolerance_seconds: float = 5.0  # forgiving -> fewer re-seeks -> less rebuffering

    # --- Streaming -----------------------------------------------------------
    stream_chunk_size: int = 2 * 1024 * 1024  # 2 MiB read unit for range responses
    read_threads: int = 128  # thread-pool for blocking I/O (file reads, ffprobe)

    # --- Server / sharing ----------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4  # gunicorn worker count (2×CPU is a good baseline for I/O bound)
    cors_allow_origins: str = "*"
    # Optional HTTP Basic Auth (recommended when exposing over the internet).
    share_username: Optional[str] = None
    share_password: Optional[str] = None
    # When True, only worker 0 runs the AutoPreparer background thread.
    # Multiple workers would fight over the same ffmpeg processes.
    single_preparer: bool = True

    @field_validator("stream_chunk_size")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @property
    def slot_start_time(self) -> time:
        return _parse_hhmm(self.slot_start)

    @property
    def slot_seconds(self) -> float:
        return self.slot_hours * 3600.0

    @property
    def extensions(self) -> List[str]:
        return [
            e if e.startswith(".") else f".{e}"
            for e in (x.strip().lower() for x in self.video_extensions.split(","))
            if e
        ]

    @property
    def cors_origins(self) -> List[str]:
        if self.cors_allow_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.share_username and self.share_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
