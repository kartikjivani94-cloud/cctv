# CCTV Live-Feed Simulator

Serves pre-recorded CCTV videos as **time-aligned live RTSP feeds** (plus HLS/WebRTC for browsers).  
Drop video files into `videos/`, start the server — each file is published with ffmpeg `-re` so 1 second of video takes 1 second to stream.

## Architecture

```
  AI clients (OpenCV / GStreamer / DeepStream)
          │  rtsp://<host>:8554/stream/<id>
          ▼
  ┌───────────────────────────────────────────┐
  │              MediaMTX                      │
  │   RTSP :8554   WebRTC :8889   HLS :8888   │
  └────────────────▲──────────────────────────┘
                   │ ffmpeg -re -stream_loop -1 -c copy
                   │ (one publisher per camera)
                   │
            videos/*.mp4  (H.264 / H.265)

  Browsers ──► nginx :80
                 /           dashboard + API
                 /live/*     MediaMTX live HLS (real-time)
                 /hls/*      VOD segments (fallback)
```

**Why RTSP instead of HTTP MP4:** pulling a progressive MP4 over byte-ranges lets a client burst through 12 hours of frames in minutes, which breaks Kalman filters, ByteTrack, and latency benchmarks. `-re` enforces native framerate; MediaMTX then fans the same live path out as RTSP (AI ingest), WebRTC, and low-latency HLS (dashboards).

## Quick Start (bare metal)

```bash
# 1. Place video files in the videos/ directory
cp /path/to/*.mp4 videos/

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and edit config
cp .env.example .env

# 4. Convert videos to H.264 MP4 (writes videos/processed/<slug>.mp4)
python scripts/convert_to_h264.py

# 5. Start MediaMTX + the API (auto-detects CPU count for workers)
scripts/run.sh
```

On first start, videos are prepared in the background (remux / transcode / HLS fallback). Each servable camera is published to `rtsp://localhost:8554/stream/<id>`.

### AI / inference ingest

```python
import cv2
cap = cv2.VideoCapture("rtsp://<host>:8554/stream/1")
```

```bash
ffplay -rtsp_transport tcp rtsp://localhost:8554/stream/1
# catalog of every live endpoint:
curl -s http://localhost:8000/api/ingest
```

OpenCV, GStreamer (`rtspsrc`), FFmpeg, and NVIDIA DeepStream can all open those RTSP URLs. Browser dashboards use live HLS at `/live/stream/<id>/index.m3u8` (or WebRTC on port 8889).

## Supported Formats

Containers: `.mp4` `.m4v` `.mov` `.mkv` `.avi` `.webm` (case-insensitive).  
Codecs: H.264 and H.265 are stream-copied into RTSP (`-c copy`); anything else is transcoded to H.264 for the browser fallback. HEVC cameras stay HEVC on the RTSP path.

### Malformed DVR/NVR exports

Some recorders write a raw elementary stream into a container that declares the
wrong codec — for example an HEVC stream inside an MP4 whose sample description
claims `avc1`. Such a file probes as ordinary H.264/MP4 but **no decoder can read
it**, so metadata alone cannot be trusted.

On first scan every file is therefore decode-verified: if no frame can be decoded
through normal container parsing, each fallback demuxer (`hevc`, `h264`, `mpegts`,
`mpeg4`) is tried until one yields a frame. The working demuxer is recorded and
forced via `ffmpeg -f` for all later conversions, and the file is rebuilt into a
valid MP4 before segmenting. The true duration is taken from the repaired file,
since the original container's value is often wrong too.

Results are cached (keyed by path, size and mtime), so the verification cost is
paid once per file, not on every restart.

## Production Deployment (Docker)

```bash
# 1. Place videos
cp /path/to/*.mp4 videos/

# 2. Configure
cp .env.example .env
# Edit .env: set SHARE_USERNAME, SHARE_PASSWORD, WORKERS, etc.

# 3. Build and start
docker compose up -d --build

# The dashboard is at http://<server-ip>/
# RTSP ingest (OpenCV / DeepStream): rtsp://<server-ip>:8554/stream/<id>
# nginx listens on port 80 (override with LISTEN_PORT in .env)
```

### Scaling for 200+ users

| Component | Role | Default | Tuning |
|-----------|------|---------|--------|
| MediaMTX | Live RTSP / WebRTC / HLS fan-out | 1 container | One publisher process per camera (`-c copy`, cheap) |
| nginx | Dashboard, API proxy, live HLS proxy | auto workers | Handles 10,000+ concurrent connections |
| gunicorn | API endpoints only | 4 workers | Set `WORKERS=2×CPU` in `.env` |

**Bandwidth math:** 12 cameras × 200 users × ~1 Mbps average = ~2.4 Gbps peak.  
A server with a 10 Gbps NIC handles this easily. Disk I/O is the bottleneck —
use SSDs for the `videos/` directory.

## Configuration

All settings are in `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `VIDEOS_DIR` | `./videos` | Path to video files |
| `TIMEZONE` | `Asia/Kolkata` | Wall-clock timezone |
| `SLOT_START` | `21:00` | Video start time (maps to start of each 12h slot) |
| `SLOT_HOURS` | `12.0` | Slot duration |
| `WORKERS` | `2×CPU` | Gunicorn worker count |
| `READ_THREADS` | `128` | Thread pool for file I/O |
| `SHARE_USERNAME` | *(none)* | HTTP Basic Auth username |
| `SHARE_PASSWORD` | *(none)* | HTTP Basic Auth password |
| `LISTEN_PORT` | `80` | Docker: nginx listen port |
| `HLS_TIME` | `6` | HLS segment length (seconds) |
| `RTSP_ENABLED` | `true` | Publish cameras as live RTSP via MediaMTX |
| `RTSP_PUBLISH_URL` | `rtsp://127.0.0.1:8554` | Where ffmpeg publishes (use `rtsp://mediamtx:8554` in Docker) |
| `RTSP_PUBLIC_HOST` | `auto` | Host advertised in `rtsp_url` (`auto` = request Host) |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard |
| GET | `/camera/{id}` | Full-screen camera player |
| GET | `/api/cameras` | List all cameras |
| GET | `/api/cameras/{id}/state` | Camera state + timing + live URLs |
| GET | `/api/ingest` | RTSP / WebRTC / live-HLS catalog for AI clients |
| GET | `/api/prepare/status` | Background prep progress |
| GET | `/live/stream/{id}/index.m3u8` | Live HLS (real-time, from MediaMTX) |
| GET | `/hls/{slug}/index.m3u8` | VOD HLS playlist (fallback) |
| GET | `/stream/{id}` | Progressive MP4 fallback |
| GET | `/healthz` | Health check |

Live ingest URLs (also returned by `/api/ingest`):

| Protocol | URL |
|----------|-----|
| RTSP | `rtsp://<host>:8554/stream/<id>` |
| WebRTC (WHEP) | `http://<host>:8889/stream/<id>/whep` |
| HLS | `http://<host>/live/stream/<id>/index.m3u8` |

## Time Alignment

Videos are mapped to two 12-hour slots per day:

- **Slot A:** 21:00 → 09:00 (night)
- **Slot B:** 09:00 → 21:00 (day, same video loops)

At 3 AM, the feed shows the 3 AM portion of the video.  
At 10 AM, the feed shows the 10 PM portion (wraps into Slot B).

## Internet Sharing

**With Cloudflare Tunnel (free, no account needed):**
```bash
# Install cloudflared, then:
scripts/share.sh
```

**With a VPS:** Deploy with Docker, point your domain's DNS to the server IP.
nginx handles TLS termination if you add a cert (or put Cloudflare in front).
