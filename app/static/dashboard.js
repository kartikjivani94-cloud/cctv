const grid = document.getElementById("grid");
const clockEl = document.getElementById("clock");

// Preparation state keyed by camera id ("waiting"|"remuxing"|"transcoding"|"hls"|"done"|"skip"|"error")
let prepMap = {};
let prepDone = true;

const PREP_LABEL = {
  waiting: "Queued",
  remuxing: "Remuxing…",
  transcoding: "Transcoding…",
  hls: "Building HLS…",
  done: "Live",
  skip: "Live",
  error: "Error",
};
const PREP_IS_WORKING = new Set(["waiting", "remuxing", "transcoding", "hls"]);

function badge(cam) {
  const prep = prepMap[cam.id];
  if (prep && PREP_IS_WORKING.has(prep.state)) {
    const label = PREP_LABEL[prep.state] || prep.state;
    const msg = prep.message ? ` — ${prep.message}` : "";
    return `<span class="badge off"><span class="ind"></span>${label}${msg}</span>`;
  }
  if (cam.status === "live") {
    return `<span class="badge live"><span class="ind"></span>Live</span>`;
  }
  if (prep && prep.state === "error") {
    return `<span class="badge unavailable"><span class="ind"></span>Error</span>`;
  }
  return `<span class="badge unavailable"><span class="ind"></span>No Signal</span>`;
}

function isClickable(cam) {
  if (cam.status === "live") return true;
  const prep = prepMap[cam.id];
  return prep && prep.state === "done";
}

function tileTitle(cam) {
  const prep = prepMap[cam.id];
  if (prep && PREP_IS_WORKING.has(prep.state)) {
    return `Processing in background — will become live automatically`;
  }
  if (prep && prep.state === "error") {
    return prep.message || "Preparation failed";
  }
  return cam.detail || "";
}

function tile(cam) {
  const inner = `
    <div class="thumb">
      ${badge(cam)}
      <div class="cam-no">${cam.number}</div>
    </div>
    <div class="meta">
      <div class="name">${cam.name}</div>
      <div class="loc">${cam.location || ""}</div>
    </div>`;

  if (isClickable(cam)) {
    return `<a class="tile" href="/camera/${encodeURIComponent(cam.id)}">${inner}</a>`;
  }
  const title = tileTitle(cam).replace(/"/g, "'");
  return `<div class="tile" title="${title}">${inner}</div>`;
}

async function refreshCameras() {
  try {
    const res = await fetch("/api/cameras", { cache: "no-store" });
    const data = await res.json();
    if (!data.cameras.length) {
      grid.innerHTML = `<div class="loc">No videos found in the videos folder.</div>`;
      return;
    }
    grid.innerHTML = data.cameras.map(tile).join("");
  } catch (e) {
    grid.innerHTML = `<div class="loc">Unable to load cameras.</div>`;
  }
}

async function pollPrepare() {
  try {
    const res = await fetch("/api/prepare/status", { cache: "no-store" });
    const data = await res.json();
    prepDone = data.done;
    prepMap = {};
    for (const s of (data.cameras || [])) {
      prepMap[s.camera_id] = s;
    }
  } catch (_) {}
}

function tickClock() {
  clockEl.textContent = new Date().toLocaleTimeString("en-GB");
}

async function tick() {
  await pollPrepare();
  await refreshCameras();
}

setInterval(tickClock, 1000);
tickClock();

// Fast polling while cameras are being prepared; slow down once all done.
tick();
const fastTimer = setInterval(async () => {
  await tick();
  if (prepDone) {
    clearInterval(fastTimer);
    setInterval(tick, 30000);  // slow refresh once everything is ready
  }
}, 4000);
