"""End-to-end smoke test for the local-video model (no ffmpeg/network needed)."""
import os
import tempfile
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.library import Camera, _is_web_safe, _Probe, clean_location, slugify
from app.routes import router
from app.streaming import parse_range
from app.timefeed import FeedClock

BLOB = os.urandom(3 * 1024 * 1024 + 777)
IST = ZoneInfo("Asia/Kolkata")


class DummySettings:
    timezone = "Asia/Kolkata"
    drift_tolerance_seconds = 2.5
    stream_chunk_size = 256 * 1024


def build_app(video_path: Path):
    app = FastAPI()
    app.include_router(router)
    cam = Camera(
        id="1", number=1, name="Camera 1", location="Test Cam", slug="cam-1",
        source_path=video_path, serve_path=video_path,
        duration=43200.0, video_codec="h264", container="mp4",
        size=video_path.stat().st_size, servable=True,
    )
    app.state.settings = DummySettings()
    app.state.cameras = [cam]
    app.state.cameras_by_id = {"1": cam}
    app.state.clock = FeedClock("Asia/Kolkata", time(21, 0), 43200.0, True)
    return app


def test_time_alignment():
    clock = FeedClock("Asia/Kolkata", time(21, 0), 43200.0, True)
    cases = {
        "2026-07-13T21:00": 0,          # slot start -> video start
        "2026-07-14T03:00": 6 * 3600,   # 3am -> 6h in (== 3am footage)
        "2026-07-14T10:00": 3600,       # 10am -> 1h in (== "10pm" footage)
        "2026-07-14T09:00": 0,          # 9am -> new day slot, back to start
    }
    for iso, expected in cases.items():
        now = datetime.fromisoformat(iso).replace(tzinfo=IST)
        st = clock.state(43200.0, now=now)
        assert st.status == "live"
        assert abs(st.slot_offset - expected) < 1, (iso, st.slot_offset, expected)
    # Short clip loops within the slot.
    now = datetime.fromisoformat("2026-07-14T05:00").replace(tzinfo=IST)  # 8h into slot
    st = clock.state(3600.0, now=now)  # 1h clip
    assert abs(st.offset - 0.0) < 1, st.offset  # 8h % 1h == 0
    print("TIME_OK (2-slot alignment + loop verified)")


def test_helpers():
    assert slugify("AR01Y4848 Tri Mandir NVR_ch12_20260613205931") \
        == "ar01y4848-tri-mandir-nvr-ch12-20260613205931"
    loc = clean_location("MyExport_CN Vidhyalaya P2 RLVD_13Jun2026_205959_14Jun2026_090000")
    assert "Vidhyalaya" in loc and "RLVD" not in loc and "205959" not in loc, loc
    print("HELPERS_OK (slugify + location cleaning)")


def test_web_safe_requires_real_decodability():
    """A container that lies about its codec must not be served directly.

    Some DVR exports wrap an HEVC elementary stream in an MP4 whose sample
    description claims 'avc1'. It probes as playable h264/mp4 but no decoder
    can read it, so it has to be repaired instead of streamed as-is.
    """
    genuine = _Probe("h264", "mov,mp4", 60.0)
    assert _is_web_safe("mp4", genuine)

    # Undecodable despite advertising a browser-friendly codec.
    lying = _Probe("h264", "mov,mp4", 60.0, decodable=False)
    assert not _is_web_safe("mp4", lying)

    # Readable only via a forced demuxer, so the container itself is invalid.
    recovered = _Probe("h264", "mov,mp4", 60.0, input_format="h264")
    assert not _is_web_safe("mp4", recovered)
    print("DECODABILITY_OK (mislabeled containers rejected)")


def test_range_parsing():
    assert parse_range(None, 1000) == (0, 999)
    assert parse_range("bytes=0-99", 1000) == (0, 99)
    assert parse_range("bytes=500-", 1000) == (500, 999)
    assert parse_range("bytes=-100", 1000) == (900, 999)
    assert parse_range("bytes=2000-3000", 1000) is None
    assert parse_range("junk", 1000) is None
    print("RANGE_OK")


def test_routes():
    with tempfile.TemporaryDirectory() as tmp:
        vid = Path(tmp) / "cam.mp4"
        vid.write_bytes(BLOB)
        client = TestClient(build_app(vid))

        r = client.get("/api/cameras")
        assert r.status_code == 200 and r.json()["cameras"][0]["status"] == "live", r.text

        r = client.get("/api/cameras/1/state")
        js = r.json()
        assert js["status"] == "live" and js["stream_url"] == "/stream/1"
        assert "slot_offset" in js and js["slot_seconds"] == 43200.0

        r = client.get("/stream/1")
        assert r.status_code == 200 and r.content == BLOB
        assert r.headers["accept-ranges"] == "bytes"

        r = client.get("/stream/1", headers={"Range": "bytes=1048576-2097151"})
        assert r.status_code == 206
        assert r.headers["content-range"] == f"bytes 1048576-2097151/{len(BLOB)}"
        assert r.content == BLOB[1048576:2097152]

        r = client.get("/stream/1", headers={"Range": "bytes=-100"})
        assert r.status_code == 206 and r.content == BLOB[-100:]

        r = client.get("/stream/1", headers={"Range": f"bytes={len(BLOB)+10}-"})
        assert r.status_code == 416

        r = client.head("/stream/1", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and r.headers["content-length"] == "100"

        assert client.get("/stream/999").status_code == 404
        print("ROUTES_OK (200/206/416/HEAD/404 verified)")


if __name__ == "__main__":
    test_time_alignment()
    test_helpers()
    test_web_safe_requires_real_decodability()
    test_range_parsing()
    test_routes()
    print("ALL_OK")
