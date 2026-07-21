"""Wall-clock alignment for an always-on two-slot feed.

Each video holds ~12 hours of footage that begins at ``slot_start`` (default
21:00). The day is divided into two back-to-back 12h slots, so the same clip is
aligned to both the night slot (21:00->09:00) and the day slot (09:00->21:00).

The position inside the slot is ``(now - most_recent_21:00) mod 12h``:
  * 03:00 -> 6h into the clip  (== 03:00 footage)
  * 10:00 -> 1h into the clip  (== 22:00 footage, i.e. "10pm feed")
The feed is therefore always live; there is no off period. If a clip is shorter
than a slot it loops within the slot.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo


@dataclass
class FeedState:
    status: str                 # always "live" in the two-slot model
    offset: float               # seconds into the video for the initial seek
    slot_offset: float          # position within the 12h slot (pre video-loop)
    slot_seconds: float         # length of a slot (e.g. 43200)
    duration: Optional[float]   # clip duration if known
    loop: bool                  # loop the clip if slot_offset exceeds duration
    wall_time: str              # current reference-tz wall clock (ISO)
    server_epoch: float         # unix time for client clock sync

    def to_dict(self) -> dict:
        return asdict(self)


class FeedClock:
    def __init__(
        self,
        timezone: str,
        slot_start: time,
        slot_seconds: float,
        loop_within_video: bool,
    ):
        self._tz = ZoneInfo(timezone)
        self._slot_start = slot_start
        self._slot_seconds = slot_seconds
        self._loop = loop_within_video

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def _slot_offset(self, now: datetime) -> float:
        anchor = datetime.combine(now.date(), self._slot_start, tzinfo=self._tz)
        if now < anchor:
            anchor -= timedelta(days=1)
        elapsed = (now - anchor).total_seconds()
        return elapsed % self._slot_seconds

    def state(self, duration: Optional[float], now: Optional[datetime] = None) -> FeedState:
        now = now or self.now()
        slot_offset = self._slot_offset(now)

        offset = slot_offset
        if duration and duration > 0 and slot_offset >= duration:
            offset = slot_offset % duration if self._loop else max(0.0, duration - 1.0)

        return FeedState(
            status="live",
            offset=offset,
            slot_offset=slot_offset,
            slot_seconds=self._slot_seconds,
            duration=duration,
            loop=self._loop,
            wall_time=now.isoformat(),
            server_epoch=now.timestamp(),
        )
