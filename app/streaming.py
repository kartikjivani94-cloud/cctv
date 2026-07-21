"""Efficient local-file range streaming.

Reads are done with ``os.pread`` on a per-request file descriptor via a thread
so the event loop is never blocked. Because reads go straight through the OS
page cache, many concurrent viewers of the same camera - or one viewer opening
every camera at once - are served hot bytes from RAM at near-zero CPU, with no
transcoding.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator, Optional, Tuple


def guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
    }.get(suffix, "video/mp4")


def parse_range(range_header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """Parse a single ``bytes=`` range into inclusive (start, end); None if invalid."""
    if not range_header:
        return (0, size - 1)
    if not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes="):].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "":
            length = int(end_s)
            if length <= 0:
                return None
            return (max(0, size - length), size - 1)
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    end = min(end, size - 1)
    if start > end or start >= size:
        return None
    return (start, end)


async def stream_file(path: Path, start: int, end: int, chunk_size: int) -> AsyncIterator[bytes]:
    fd = await asyncio.to_thread(os.open, str(path), os.O_RDONLY)
    try:
        pos = start
        remaining = end - start + 1
        while remaining > 0:
            n = min(chunk_size, remaining)
            data = await asyncio.to_thread(os.pread, fd, n, pos)
            if not data:
                break
            pos += len(data)
            remaining -= len(data)
            yield data
    finally:
        await asyncio.to_thread(os.close, fd)
