# CCTV Live-Feed Simulator

Serves pre-recorded CCTV videos as time-aligned live feeds via HLS.  
Drop video files into `videos/`, start the server — cameras appear on the dashboard
and videos are automatically converted and segmented in the background.

## Architecture

```
                  ┌─────────────────────────────────┐
  Users ──────►   │          nginx (:80)             │
  (200+)          │  ┌─────────┐  ┌───────────────┐  │
                  │  │ /hls/*  │  │ /api/* /stream │  │
                  │  │ sendfile│  │ proxy_pass ──► │──┼──► gunicorn + uvicorn (:8000)
                  │  │ (zero   │  │               │  │    4+ async workers
                  │  │  copy)  │  └───────────────┘  │    auto-prepare thread
                  │  └─────────┘                     │
                  └─────────────────────────────────┘
                         │
                   videos/hls/<slug>/*.ts   ← pre-segmented, never re-encoded at runtime
```

**Key design for 200+ concurrent users at original quality:**

- **nginx serves all HLS segments** (`.ts` files, `.m3u8` playlists) directly from disk
  via `sendfile()` — zero-copy, zero Python, zero CPU per viewer.
- **No runtime transcoding** — all conversion happens once at startup (background thread).
  Segments are lossless stream-copies of the source (`-c copy`), preserving original quality.
- **gunicorn** handles only lightweight API calls (camera state, dashboard HTML).
  With 4 workers it can sustain thousands of JSON responses/second.
- **HLS with hls.js** on the client ensures gap-free, buffer-ahead playback.

## Quick Start (bare metal)

```bash
# 1. Place video files in the videos/ directory
cp /path/to/*.mp4 videos/

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and edit config
cp .env.example .env

# 4. Start the server (auto-detects CPU count for workers)
scripts/run.sh

# On first start, videos are automatically:
#   H.264 MP4 → faststart remux (instant)
#   H.264 MKV/AVI → remux to MP4 (seconds)
#   HEVC/other → transcode to H.264 (minutes-hours)
#   All → HLS segments (seconds-minutes)
# Cameras go live on the dashboard as each finishes.
```

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
# nginx listens on port 80 (override with LISTEN_PORT in .env)
```

### Scaling for 200+ users

| Component | Role | Default | Tuning |
|-----------|------|---------|--------|
| nginx | Serve HLS segments (sendfile) | auto workers | Handles 10,000+ concurrent connections out of the box |
| gunicorn | API endpoints only | 4 workers | Set `WORKERS=2×CPU` in `.env`; each worker handles ~500 req/s |
| Thread pool | Progressive stream fallback | 128 threads | `READ_THREADS` in `.env` |

**Bandwidth math:** 12 cameras × 200 users × ~1 Mbps average = ~2.4 Gbps peak.  
A server with a 10 Gbps NIC handles this easily. Disk I/O is the bottleneck —
use SSDs for the `videos/hls/` directory.

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

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard |
| GET | `/camera/{id}` | Full-screen camera player |
| GET | `/api/cameras` | List all cameras |
| GET | `/api/cameras/{id}/state` | Camera state + timing |
| GET | `/api/prepare/status` | Background prep progress |
| GET | `/hls/{slug}/index.m3u8` | HLS playlist (nginx-served) |
| GET | `/stream/{id}` | Progressive MP4 fallback |
| GET | `/healthz` | Health check |

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
