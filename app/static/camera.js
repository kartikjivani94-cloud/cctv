const camId = decodeURIComponent(location.pathname.split("/").pop());

const video = document.getElementById("video");
const overlay = document.getElementById("overlay");
const spinner = document.getElementById("spinner");
const overlayBig = document.getElementById("overlayBig");
const overlaySub = document.getElementById("overlaySub");
const osdName = document.getElementById("osdName");
const osdLoc = document.getElementById("osdLoc");
const osdTime = document.getElementById("osdTime");
const osdDate = document.getElementById("osdDate");
const ingestLine = document.getElementById("ingestLine");
const statusLine = document.getElementById("statusLine");

let feed = null;
let fetchedAtMs = 0;
let driftTolerance = 2.5;
let timezone = undefined;

function serverNowSec() {
  const elapsed = (Date.now() - fetchedAtMs) / 1000;
  return (feed ? feed.server_epoch : Date.now() / 1000) + elapsed;
}

// Position within the video for "right now", derived from the 12h slot offset.
function expectedOffset() {
  if (!feed || feed.slot_offset == null) return 0;
  const elapsed = (Date.now() - fetchedAtMs) / 1000;
  let slot = (feed.slot_offset + elapsed) % feed.slot_seconds;
  const dur = feed.duration || (isFinite(video.duration) ? video.duration : null);
  if (dur && dur > 0) {
    slot = feed.loop ? slot % dur : Math.min(slot, dur - 0.5);
  }
  return slot;
}

// Opaque state screen (connecting / offline / processing / error). Hides the
// video, which is fine because in these states there is nothing to show.
function showState(big, sub) {
  overlay.classList.remove("hidden", "subtle");
  overlay.classList.add("solid");
  spinner.style.display = "block";
  overlayBig.textContent = big || "";
  overlaySub.textContent = sub || "";
}

// Transient buffering indicator: a small corner spinner that NEVER covers the
// frame, so a momentary stall shows the last frame rather than a blank screen.
function showBuffering() {
  overlay.classList.remove("hidden", "solid");
  overlay.classList.add("subtle");
  spinner.style.display = "block";
  overlayBig.textContent = "";
  overlaySub.textContent = "";
}

function hideOverlay() { overlay.classList.add("hidden"); }

// Backwards-compatible shim used by the state branches below.
function showOverlay(big, sub) { showState(big, sub); }

function tickOsd() {
  const now = new Date(serverNowSec() * 1000);
  const opts = { hour12: false, timeZone: timezone };
  osdTime.textContent = now.toLocaleTimeString("en-GB", opts);
  osdDate.textContent = now.toLocaleDateString("en-GB", {
    weekday: "short", year: "numeric", month: "short", day: "2-digit", timeZone: timezone,
  });
}

let hls = null;
let liveHlsFailed = false;

function seekToLiveAndPlay() {
  try { video.currentTime = expectedOffset(); } catch (e) {}
  video.play().catch(() => showOverlay("PAUSED", "Tap anywhere to start the live feed.", false));
}

function playFromLiveEdge() {
  video.play().catch(() => showOverlay("PAUSED", "Tap anywhere to start the live feed.", false));
}

function startLiveHls(url, live) {
  if (hls) { hls.destroy(); hls = null; }
  hls = new Hls(live ? {
    liveSyncDurationCount: 2,
    liveMaxLatencyDurationCount: 6,
    liveDurationInfinity: true,
    lowLatencyMode: true,
    maxBufferLength: 8,
    maxMaxBufferLength: 20,
  } : {
    maxBufferLength: 60,
    maxMaxBufferLength: 600,
    backBufferLength: 60,
    startPosition: expectedOffset(),
    lowLatencyMode: false,
  });
  hls.loadSource(url);
  hls.attachMedia(video);
  hls.on(Hls.Events.MANIFEST_PARSED, live ? playFromLiveEdge : seekToLiveAndPlay);
  hls.on(Hls.Events.ERROR, (evt, data) => {
    if (!data.fatal) return;
    if (live) {
      liveHlsFailed = true;
      if (hls) { hls.destroy(); hls = null; }
      startLive();
      return;
    }
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
    else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
    else { hls.destroy(); hls = null; setTimeout(init, 2000); }
  });
}

function startLive() {
  // Preferred path: MediaMTX live HLS (real-time, no byte-range seeking).
  if (feed.hls_live_url && window.Hls && window.Hls.isSupported() && !liveHlsFailed) {
    startLiveHls(feed.hls_live_url, true);
    return;
  }
  // Native live HLS (Safari / iOS).
  if (feed.hls_live_url && video.canPlayType("application/vnd.apple.mpegurl") && !liveHlsFailed) {
    video.src = feed.hls_live_url;
    video.addEventListener("loadedmetadata", playFromLiveEdge, { once: true });
    return;
  }
  // VOD HLS (segmented files) for gap-free continuous playback when the
  // live gateway is down.
  if (feed.hls_url && window.Hls && window.Hls.isSupported()) {
    startLiveHls(feed.hls_url, false);
    return;
  }
  // Native HLS (Safari / iOS).
  if (feed.hls_url && video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = feed.hls_url;
    video.addEventListener("loadedmetadata", seekToLiveAndPlay, { once: true });
    return;
  }
  // Fallback: progressive MP4 range streaming.
  if (video.src.indexOf(feed.stream_url) === -1) {
    video.src = feed.stream_url;
    video.load();
  }
  if (video.readyState >= 1) seekToLiveAndPlay();
  else video.addEventListener("loadedmetadata", seekToLiveAndPlay, { once: true });
}

// Keep the feed aligned to wall-clock WITHOUT re-seeking (which on sparse
// surveillance keyframes causes a buffer/seek death-spiral). Instead nudge the
// playback speed slightly to converge, and only hard-seek on a large desync.
const SOFT_DEADBAND = 1.0;   // s: within this, play at exactly 1x
const HARD_RESYNC = 30.0;    // s: only a real desync forces a seek

function syncPlayback() {
  if (!feed || feed.status !== "live") return;
  // Live RTSP/HLS is already the wall-clock edge; do not seek inside it.
  if (feed.hls_live_url && !liveHlsFailed) return;
  // Never fight the buffer: skip while paused, seeking, or starved of data.
  if (video.paused || video.seeking || video.readyState < 3) return;

  const drift = video.currentTime - expectedOffset(); // <0 => behind live

  if (Math.abs(drift) > HARD_RESYNC) {
    try { video.currentTime = expectedOffset(); } catch (e) {}
    video.playbackRate = 1.0;
    return;
  }
  if (drift < -SOFT_DEADBAND) {
    video.playbackRate = 1.05;        // behind -> gently catch up
  } else if (drift > SOFT_DEADBAND) {
    video.playbackRate = 0.95;        // ahead -> gently ease back
  } else {
    video.playbackRate = 1.0;
  }
}

function applyMeta() {
  osdName.textContent = feed.name || `Camera ${camId}`;
  osdLoc.textContent = feed.location || "";
  document.title = `${feed.name || "Camera " + camId} - CCTV Feed`;
  if (ingestLine) {
    ingestLine.textContent = feed.rtsp_url || "";
    ingestLine.title = feed.rtsp_url
      ? `AI ingest: ${feed.rtsp_url}`
      : "";
  }
}

async function fetchState() {
  const res = await fetch(`/api/cameras/${encodeURIComponent(camId)}/state`, { cache: "no-store" });
  return res.json();
}

async function init() {
  liveHlsFailed = false;
  try {
    feed = await fetchState();
    fetchedAtMs = Date.now();
  } catch (e) {
    showOverlay("CONNECTION LOST", "Retrying...", true);
    setTimeout(init, 5000);
    return;
  }

  if (feed.drift_tolerance) driftTolerance = feed.drift_tolerance;
  timezone = feed.timezone;
  applyMeta();

  if (feed.status === "processing") {
    video.removeAttribute("src"); video.load();
    showOverlay("PROCESSING", "This camera's video needs conversion. Run scripts/prepare_videos.py, then reload.", false);
    statusLine.textContent = "Processing required";
    return;
  }
  if (feed.status !== "live") {
    video.removeAttribute("src"); video.load();
    showOverlay("NO SIGNAL", feed.detail || "This camera is unavailable.", false);
    statusLine.textContent = "No signal";
    return;
  }

  statusLine.textContent = "LIVE";
  showState("CONNECTING", "");
  startLive();
}

// Periodically resync the timing baseline (correct long-run clock drift)
// without reloading the video, unless availability changed.
async function resync() {
  if (!feed) return;
  try {
    const fresh = await fetchState();
    if (fresh.status !== "live") { init(); return; }
    feed = fresh;
    fetchedAtMs = Date.now();
  } catch (e) { /* ignore, keep playing */ }
}

// Debounce the buffering indicator: brief (<600ms) stalls show nothing at all,
// so momentary hiccups never produce any visible change on screen.
let bufferingTimer = null;
function clearBuffering() {
  if (bufferingTimer) { clearTimeout(bufferingTimer); bufferingTimer = null; }
  if (feed && feed.status === "live") hideOverlay();
}
function armBuffering() {
  if (!feed || feed.status !== "live") return;
  if (bufferingTimer) return;
  bufferingTimer = setTimeout(() => { showBuffering(); bufferingTimer = null; }, 600);
}

video.addEventListener("playing", clearBuffering);
video.addEventListener("canplay", clearBuffering);
video.addEventListener("timeupdate", clearBuffering);
video.addEventListener("waiting", armBuffering);
video.addEventListener("stalled", armBuffering);
video.addEventListener("error", () => {
  // Only surface a hard error for the progressive fallback; hls.js recovers
  // its own media/network errors (handled in startLive) without blanking.
  if (feed && feed.status === "live" && !feed.hls_url) {
    showState("RECONNECTING", "Re-establishing feed...");
    setTimeout(init, 3000);
  }
});
video.addEventListener("ended", () => {
  if (feed && feed.status === "live") {
    try { video.currentTime = expectedOffset(); } catch (e) {}
    video.play().catch(() => {});
  }
});

document.body.addEventListener("click", () => {
  if (feed && feed.status === "live" && video.paused) video.play().catch(() => {});
});

setInterval(tickOsd, 250);
setInterval(syncPlayback, 3000);
setInterval(resync, 5 * 60 * 1000);
init();
