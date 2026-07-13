#!/usr/bin/env python3
"""Visualize raw vs smoothed UMI world EEF trajectories.

This viewer is intentionally focused on the transformed/world EEF view. It
loads matching raw/smooth pairs from ``actions.eef_pose_*`` first and falls
back to ``observation.state.eef_pose_*`` when needed, then overlays both
trajectories in one canvas. Frames where smoothed and raw positions differ are
drawn with a stronger highlight color.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SOURCE_PAIRS = (
    ("actions", Path("actions.eef_pose_raw/data.csv"), Path("actions.eef_pose_smooth/data.csv")),
    (
        "observation",
        Path("observation.state.eef_pose_raw/data.csv"),
        Path("observation.state.eef_pose_smooth/data.csv"),
    ),
)
SIDES = ("left", "right")


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Smooth Trajectory Comparison</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #fff;
      --line: #d8dde6;
      --text: #172033;
      --muted: #647084;
      --accent: #0f766e;
      --raw-left: #2563eb;
      --raw-right: #dc2626;
      --smooth-left: #06b6d4;
      --smooth-right: #f59e0b;
      --diff-left: #7c3aed;
      --diff-right: #16a34a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 13px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 {
      margin: 0;
      font-size: 17px;
      font-weight: 650;
      letter-spacing: 0;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px;
      color: var(--muted);
      font-size: 12px;
    }
    .dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      margin-right: 5px;
      vertical-align: -1px;
    }
    main {
      display: grid;
      grid-template-columns: minmax(280px, 340px) minmax(0, 1fr);
      gap: 14px;
      padding: 14px;
    }
    aside, .viewer {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    aside {
      padding: 14px;
      height: calc(100vh - 86px);
      overflow: auto;
    }
    .field { margin-bottom: 13px; }
    label {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 5px;
    }
    select, input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 8px 9px;
      font: inherit;
    }
    button {
      border: 1px solid var(--accent);
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      padding: 8px 11px;
      font: inherit;
      cursor: pointer;
    }
    button.secondary {
      border-color: var(--line);
      background: #fff;
      color: var(--text);
    }
    .meta {
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .meta strong { color: var(--text); font-weight: 620; }
    .viewer-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
    }
    .viewer-title strong { color: var(--text); }
    canvas {
      display: block;
      width: 100%;
      height: min(74vh, 780px);
      min-height: 520px;
      background: #fbfcfe;
      cursor: grab;
      touch-action: none;
      user-select: none;
    }
    canvas.is-dragging { cursor: grabbing; }
    .controls {
      display: grid;
      grid-template-columns: 80px minmax(0, 1fr) 90px;
      gap: 10px;
      align-items: center;
      padding: 10px 12px 12px;
      border-top: 1px solid var(--line);
    }
    input[type="range"] { width: 100%; }
    .status {
      padding: 10px 12px 12px;
      color: var(--muted);
      border-top: 1px solid var(--line);
      overflow-wrap: anywhere;
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      aside { height: auto; }
      canvas { height: 540px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>World EEF Raw vs Smooth</h1>
    <div class="legend">
      <span><span class="dot" style="background:var(--raw-left)"></span>raw left</span>
      <span><span class="dot" style="background:var(--raw-right)"></span>raw right</span>
      <span><span class="dot" style="background:var(--smooth-left)"></span>smooth left</span>
      <span><span class="dot" style="background:var(--smooth-right)"></span>smooth right</span>
      <span><span class="dot" style="background:var(--diff-left)"></span>changed left</span>
      <span><span class="dot" style="background:var(--diff-right)"></span>changed right</span>
    </div>
  </header>
  <main>
    <aside>
      <div class="field">
        <label>Episode</label>
        <select id="episodeSelect"></select>
      </div>
      <div class="field">
        <label>Difference threshold, meters</label>
        <input id="diffThreshold" type="number" step="0.0001" min="0" value="0.0005">
      </div>
      <div class="field" style="display:flex; gap:8px;">
        <button id="reloadEpisode">Reload</button>
        <button id="resetView" class="secondary">Reset view</button>
      </div>
      <div class="field meta">
        <div>Source: <strong id="sourcePath">-</strong></div>
        <div>Episode: <strong id="episodeName">-</strong></div>
        <div>Frames: <strong id="frameCount">0</strong></div>
        <div>Frame: <strong id="frameIndex">0</strong></div>
        <div>Time: <strong id="timestamp">-</strong></div>
        <div>Smooth file: <strong id="smoothStatus">-</strong></div>
        <div>Changed frames: <strong id="changedFrames">0</strong></div>
      </div>
    </aside>
    <section class="viewer">
      <div class="viewer-title">
        <strong>Transformed / world EEF comparison</strong>
        <span id="stamp">-</span>
      </div>
      <canvas id="canvas"></canvas>
      <div class="controls">
        <button id="play">Play</button>
        <input id="progress" type="range" min="0" max="0" value="0">
        <span id="counter">0 / 0</span>
      </div>
      <div id="status" class="status">Loading episodes...</div>
    </section>
  </main>
<script>
const sides = ["left", "right"];
const colors = {
  raw: {left: "#2563eb", right: "#dc2626"},
  smooth: {left: "#06b6d4", right: "#f59e0b"},
  diff: {left: "#7c3aed", right: "#16a34a"},
  axis: ["#ef4444", "#22c55e", "#3b82f6"],
};

function defaultView() {
  return {panX: 0, panY: 0, yaw: -42 * Math.PI / 180, pitch: -28 * Math.PI / 180, zoom: 1, drag: null};
}

const state = {
  episodes: [],
  rawFrames: [],
  smoothFrames: [],
  diff: {left: [], right: []},
  index: 0,
  playing: false,
  playStartedAt: 0,
  playStartTimestamp: 0,
  view: defaultView(),
};

const els = {
  episodeSelect: document.getElementById("episodeSelect"),
  diffThreshold: document.getElementById("diffThreshold"),
  reloadEpisode: document.getElementById("reloadEpisode"),
  resetView: document.getElementById("resetView"),
  sourcePath: document.getElementById("sourcePath"),
  episodeName: document.getElementById("episodeName"),
  frameCount: document.getElementById("frameCount"),
  frameIndex: document.getElementById("frameIndex"),
  timestamp: document.getElementById("timestamp"),
  smoothStatus: document.getElementById("smoothStatus"),
  changedFrames: document.getElementById("changedFrames"),
  canvas: document.getElementById("canvas"),
  play: document.getElementById("play"),
  progress: document.getElementById("progress"),
  counter: document.getElementById("counter"),
  stamp: document.getElementById("stamp"),
  status: document.getElementById("status"),
};

function setStatus(text) {
  els.status.textContent = text;
}

function add(a, b) {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

function dot(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

function cross(a, b) {
  return [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
}

function normalize(v) {
  const n = Math.hypot(v[0], v[1], v[2]);
  if (n < 1e-9) return [1, 0, 0];
  return [v[0] / n, v[1] / n, v[2] / n];
}

function rotation6dToMatrix(values) {
  const c1 = normalize(values.slice(0, 3));
  const rawC2 = values.slice(3, 6);
  const d = dot(c1, rawC2);
  const c2 = normalize([rawC2[0] - d * c1[0], rawC2[1] - d * c1[1], rawC2[2] - d * c1[2]]);
  const c3 = cross(c1, c2);
  return [
    [c1[0], c2[0], c3[0]],
    [c1[1], c2[1], c3[1]],
    [c1[2], c2[2], c3[2]],
  ];
}

function resizeCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.floor(rect.width * dpr));
  const height = Math.max(1, Math.floor(rect.height * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {ctx, width: rect.width, height: rect.height};
}

function rotateForView(p) {
  const view = state.view;
  const cy = Math.cos(view.yaw);
  const sy = Math.sin(view.yaw);
  const cp = Math.cos(view.pitch);
  const sp = Math.sin(view.pitch);
  const x1 = cy * p[0] - sy * p[1];
  const y1 = sy * p[0] + cy * p[1];
  const z1 = p[2];
  return [x1, cp * y1 - sp * z1, sp * y1 + cp * z1];
}

function sceneFor() {
  const points = [[0, 0, 0]];
  for (const frames of [state.rawFrames, state.smoothFrames]) {
    for (const frame of frames) {
      for (const side of sides) points.push(frame[side].pos);
    }
  }
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (const p of points) {
    for (let i = 0; i < 3; i++) {
      min[i] = Math.min(min[i], p[i]);
      max[i] = Math.max(max[i], p[i]);
    }
  }
  const center = min.map((v, i) => (v + max[i]) / 2);
  const radius = Math.max(...max.map((v, i) => Math.abs(v - min[i])), 0.05);
  return {center, radius};
}

function makeProject(scene, width, height) {
  const scale = 0.78 * Math.min(width, height) / scene.radius * state.view.zoom;
  return p => {
    const shifted = [p[0] - scene.center[0], p[1] - scene.center[1], p[2] - scene.center[2]];
    const v = rotateForView(shifted);
    return [width / 2 + v[0] * scale + state.view.panX, height / 2 - v[1] * scale + state.view.panY];
  };
}

function drawLine(ctx, project, a, b, color, width = 1, alpha = 1) {
  const pa = project(a);
  const pb = project(b);
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  ctx.moveTo(pa[0], pa[1]);
  ctx.lineTo(pb[0], pb[1]);
  ctx.stroke();
  ctx.restore();
}

function drawPoint(ctx, project, p, color, radius = 4) {
  const s = project(p);
  ctx.save();
  ctx.fillStyle = color;
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  ctx.arc(s[0], s[1], radius, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

function drawPolyline(ctx, project, frames, side, color, stop, alpha, width) {
  if (frames.length < 2) return;
  const end = Math.min(stop, frames.length - 1);
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  const first = project(frames[0][side].pos);
  ctx.moveTo(first[0], first[1]);
  for (let i = 1; i <= end; i++) {
    const p = project(frames[i][side].pos);
    ctx.lineTo(p[0], p[1]);
  }
  ctx.stroke();
  ctx.restore();
}

function drawChangedSegments(ctx, project, side, stop) {
  const frames = state.smoothFrames.length ? state.smoothFrames : state.rawFrames;
  if (frames.length < 2) return;
  ctx.save();
  ctx.strokeStyle = colors.diff[side];
  ctx.lineWidth = 4.2;
  ctx.globalAlpha = 0.95;
  for (let i = 1; i <= Math.min(stop, frames.length - 1); i++) {
    if (!state.diff[side][i] && !state.diff[side][i - 1]) continue;
    const a = project(frames[i - 1][side].pos);
    const b = project(frames[i][side].pos);
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.stroke();
  }
  ctx.restore();
}

function drawTriad(ctx, project, pose, scene) {
  const rot = rotation6dToMatrix(pose.rot6);
  const length = Math.max(0.018, scene.radius * 0.11);
  for (let col = 0; col < 3; col++) {
    const axis = [rot[0][col], rot[1][col], rot[2][col]];
    const end = add(pose.pos, axis.map(v => v * length));
    drawLine(ctx, project, pose.pos, end, colors.axis[col], 2.2, 0.95);
  }
}

function drawGrid(ctx, project, scene) {
  const span = scene.radius * 0.75;
  const center = scene.center;
  const z = 0;
  for (let i = -3; i <= 3; i++) {
    const t = i / 3 * span;
    drawLine(ctx, project, [center[0] - span, center[1] + t, z], [center[0] + span, center[1] + t, z], "#d8dde6", 1, 0.55);
    drawLine(ctx, project, [center[0] + t, center[1] - span, z], [center[0] + t, center[1] + span, z], "#d8dde6", 1, 0.55);
  }
  drawLine(ctx, project, [0, 0, 0], [span * 0.45, 0, 0], colors.axis[0], 1.5, 0.75);
  drawLine(ctx, project, [0, 0, 0], [0, span * 0.45, 0], colors.axis[1], 1.5, 0.75);
  drawLine(ctx, project, [0, 0, 0], [0, 0, span * 0.45], colors.axis[2], 1.5, 0.75);
}

function render() {
  const {ctx, width, height} = resizeCanvas(els.canvas);
  ctx.clearRect(0, 0, width, height);
  if (!state.rawFrames.length) return;
  const scene = sceneFor();
  const project = makeProject(scene, width, height);
  drawGrid(ctx, project, scene);
  const stop = state.index;
  for (const side of sides) {
    drawPolyline(ctx, project, state.rawFrames, side, colors.raw[side], state.rawFrames.length - 1, 0.16, 1);
    drawPolyline(ctx, project, state.rawFrames, side, colors.raw[side], stop, 0.55, 1.8);
  }
  if (state.smoothFrames.length) {
    for (const side of sides) {
      drawPolyline(ctx, project, state.smoothFrames, side, colors.smooth[side], state.smoothFrames.length - 1, 0.18, 1.2);
      drawPolyline(ctx, project, state.smoothFrames, side, colors.smooth[side], stop, 0.8, 2.2);
      drawChangedSegments(ctx, project, side, stop);
    }
  }
  const raw = state.rawFrames[state.index];
  const smooth = state.smoothFrames[state.index];
  for (const side of sides) {
    drawPoint(ctx, project, raw[side].pos, colors.raw[side], 5);
    drawTriad(ctx, project, raw[side], scene);
    if (smooth) {
      drawLine(ctx, project, raw[side].pos, smooth[side].pos, colors.diff[side], 1.5, state.diff[side][state.index] ? 0.85 : 0.22);
      drawPoint(ctx, project, smooth[side].pos, state.diff[side][state.index] ? colors.diff[side] : colors.smooth[side], 6);
      drawTriad(ctx, project, smooth[side], scene);
    }
  }
}

function computeDiff() {
  const threshold = Math.max(0, Number(els.diffThreshold.value || 0));
  state.diff = {left: [], right: []};
  let changed = 0;
  for (let i = 0; i < state.rawFrames.length; i++) {
    let any = false;
    for (const side of sides) {
      const raw = state.rawFrames[i]?.[side]?.pos;
      const smooth = state.smoothFrames[i]?.[side]?.pos;
      const hit = Boolean(raw && smooth && Math.hypot(raw[0] - smooth[0], raw[1] - smooth[1], raw[2] - smooth[2]) > threshold);
      state.diff[side][i] = hit;
      any = any || hit;
    }
    if (any) changed++;
  }
  els.changedFrames.textContent = String(changed);
}

function syncProgress() {
  const max = Math.max(0, state.rawFrames.length - 1);
  els.progress.max = String(max);
  els.progress.value = String(state.index);
  const label = `${state.index + (max > 0 ? 1 : 0)} / ${state.rawFrames.length}`;
  els.counter.textContent = label;
  els.frameIndex.textContent = state.rawFrames.length ? String(state.index) : "0";
  const timestamp = state.rawFrames[state.index]?.timestamp;
  const stamp = timestamp == null ? "-" : `${timestamp} ms`;
  els.timestamp.textContent = stamp;
  els.stamp.textContent = stamp;
}

function pause() {
  state.playing = false;
  els.play.textContent = "Play";
}

function findFrameByTimestamp(timestamp) {
  const frames = state.rawFrames;
  let lo = 0;
  let hi = frames.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (frames[mid].timestamp <= timestamp) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

function tick(now) {
  if (!state.playing) return;
  const elapsed = now - state.playStartedAt;
  const nextTimestamp = state.playStartTimestamp + elapsed;
  state.index = findFrameByTimestamp(nextTimestamp);
  if (state.index >= state.rawFrames.length - 1) pause();
  syncProgress();
  render();
  if (state.playing) requestAnimationFrame(tick);
}

function play() {
  if (!state.rawFrames.length) return;
  if (state.index >= state.rawFrames.length - 1) state.index = 0;
  state.playing = true;
  state.playStartedAt = performance.now();
  state.playStartTimestamp = state.rawFrames[state.index].timestamp;
  els.play.textContent = "Pause";
  requestAnimationFrame(tick);
}

function setIndex(value) {
  pause();
  state.index = Math.max(0, Math.min(Number(value), state.rawFrames.length - 1));
  syncProgress();
  render();
}

async function loadEpisodes() {
  const res = await fetch("/api/episodes");
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  state.episodes = data.episodes || [];
  els.sourcePath.textContent = data.source_root || "-";
  els.episodeSelect.innerHTML = "";
  for (const ep of state.episodes) {
    const opt = document.createElement("option");
    opt.value = ep.path;
    opt.textContent = `${ep.path}${ep.smooth ? "" : " (no smooth)"}`;
    els.episodeSelect.appendChild(opt);
  }
  if (state.episodes.length) {
    await loadEpisode(state.episodes[0].path);
  } else {
    setStatus("No episodes with eef_pose_raw were found.");
  }
}

async function loadEpisode(path) {
  pause();
  setStatus(`Loading ${path}...`);
  const res = await fetch(`/api/episode?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  state.rawFrames = data.raw_frames || [];
  state.smoothFrames = data.smooth_frames || [];
  state.index = 0;
  els.episodeName.textContent = data.path || path;
  els.frameCount.textContent = String(state.rawFrames.length);
  els.smoothStatus.textContent = data.smooth_available ? "available" : "missing";
  computeDiff();
  syncProgress();
  render();
  setStatus(data.smooth_available ? "Loaded raw and smoothed trajectories." : "Loaded raw trajectory; smoothed output is missing.");
}

function setupCanvasInteraction() {
  const canvas = els.canvas;
  canvas.addEventListener("contextmenu", event => event.preventDefault());
  canvas.addEventListener("pointerdown", event => {
    if (event.button !== 0 && event.button !== 1) return;
    event.preventDefault();
    pause();
    canvas.classList.add("is-dragging");
    canvas.setPointerCapture(event.pointerId);
    state.view.drag = {
      mode: event.button === 1 ? "rotate" : "pan",
      x: event.clientX,
      y: event.clientY,
    };
  });
  canvas.addEventListener("pointermove", event => {
    const drag = state.view.drag;
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    drag.x = event.clientX;
    drag.y = event.clientY;
    if (drag.mode === "pan") {
      state.view.panX += dx;
      state.view.panY += dy;
    } else {
      state.view.yaw += dx * 0.01;
      state.view.pitch += dy * 0.01;
    }
    render();
  });
  const stopDrag = event => {
    if (!state.view.drag) return;
    state.view.drag = null;
    canvas.classList.remove("is-dragging");
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  };
  canvas.addEventListener("pointerup", stopDrag);
  canvas.addEventListener("pointercancel", stopDrag);
  canvas.addEventListener("pointerleave", event => {
    if (event.buttons === 0) {
      state.view.drag = null;
      canvas.classList.remove("is-dragging");
    }
  });
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    const factor = Math.exp(-event.deltaY * 0.001);
    state.view.zoom = Math.max(0.15, Math.min(12, state.view.zoom * factor));
    render();
  }, {passive: false});
}

els.episodeSelect.addEventListener("change", event => loadEpisode(event.target.value).catch(err => setStatus(err.message)));
els.reloadEpisode.addEventListener("click", () => loadEpisode(els.episodeSelect.value).catch(err => setStatus(err.message)));
els.resetView.addEventListener("click", () => { state.view = defaultView(); render(); });
els.diffThreshold.addEventListener("change", () => { computeDiff(); render(); });
els.play.addEventListener("click", () => state.playing ? pause() : play());
els.progress.addEventListener("input", event => setIndex(event.target.value));
window.addEventListener("resize", render);
setupCanvasInteraction();
loadEpisodes().catch(err => setStatus(err.message));
</script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a raw-vs-smoothed world EEF trajectory visualizer.")
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path("/home/ubuntu/data/curated_samples/success"),
        help="Dataset root or a single episode directory.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    return parser.parse_args()


def is_episode_dir(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("episode_")


def episode_dirs(source: Path) -> list[Path]:
    source = source.resolve()
    if is_episode_dir(source):
        return [source]
    return sorted(path for path in source.rglob("episode_*") if is_episode_dir(path))


def source_root_for(source: Path) -> Path:
    source = source.resolve()
    return source.parent if is_episode_dir(source) else source


def eef_pose_paths(episode_dir: Path) -> tuple[str, Path, Path] | None:
    for name, raw_rel, smooth_rel in SOURCE_PAIRS:
        raw_path = episode_dir / raw_rel
        if raw_path.exists():
            return name, raw_path, episode_dir / smooth_rel
    return None


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def float_value(row: dict[str, str], key: str, path: Path, index: int) -> float:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid float in {path}, row {index}, column {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"Non-finite float in {path}, row {index}, column {key}")
    return value


def columns_for(fieldnames: list[str], side: str) -> tuple[list[str], list[str]]:
    candidates = [
        ([f"{side}_tcp.{axis}" for axis in ("x", "y", "z")], [f"{side}_tcp.r{i}" for i in range(1, 7)]),
        ([f"{side}_{axis}" for axis in ("x", "y", "z")], [f"{side}_r{i}" for i in range(1, 7)]),
    ]
    for xyz, rot in candidates:
        if all(col in fieldnames for col in xyz):
            if not all(col in fieldnames for col in rot):
                rot = []
            return xyz, rot
    raise ValueError(f"Missing {side} position columns. Supported prefixes: {side}_tcp.* or {side}_*")


def csv_to_frames(path: Path) -> list[dict[str, object]]:
    fieldnames, rows = read_csv(path)
    specs = {side: columns_for(fieldnames, side) for side in SIDES}
    frames: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        frame: dict[str, object] = {"timestamp": float_value(row, "timestamp_ms", path, index)}
        for side in SIDES:
            xyz, rot = specs[side]
            pose = {
                "pos": [float_value(row, col, path, index) for col in xyz],
                "rot6": [float_value(row, col, path, index) for col in rot] if rot else [1, 0, 0, 0, 1, 0],
            }
            frame[side] = pose
        frames.append(frame)
    return frames


def choose_port(host: str, preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, preferred))
        except OSError:
            sock.bind((host, 0))
        return int(sock.getsockname()[1])


def make_handler(source: Path):
    root = source_root_for(source)
    episodes = episode_dirs(source)
    valid = {str(path.relative_to(root)): path for path in episodes if eef_pose_paths(path) is not None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                return

        def send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
            self.send_bytes(text.encode("utf-8"), content_type, status)

        def send_json(self, payload: object, status: int = 200) -> None:
            self.send_bytes(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self.send_text(HTML, content_type="text/html; charset=utf-8")
                return
            if parsed.path == "/api/episodes":
                rows = []
                for rel, path in sorted(valid.items()):
                    paths = eef_pose_paths(path)
                    rows.append({"path": rel, "smooth": bool(paths and paths[2].exists())})
                self.send_json({"source_root": str(root), "episodes": rows})
                return
            if parsed.path == "/api/episode":
                query = parse_qs(parsed.query)
                rel = query.get("path", [""])[0]
                episode_dir = valid.get(rel)
                if episode_dir is None:
                    self.send_text(f"Unknown episode: {rel}", status=404)
                    return
                paths = eef_pose_paths(episode_dir)
                if paths is None:
                    self.send_text(f"Missing eef_pose_raw for {rel}", status=404)
                    return
                source_name, raw_path, smooth_path = paths
                try:
                    raw_frames = csv_to_frames(raw_path)
                    smooth_frames = csv_to_frames(smooth_path) if smooth_path.exists() else []
                    if smooth_frames and len(smooth_frames) != len(raw_frames):
                        self.send_text(
                            f"Frame count mismatch for {rel}: raw={len(raw_frames)}, smooth={len(smooth_frames)}",
                            status=500,
                        )
                        return
                except Exception as exc:
                    self.send_text(str(exc), status=500)
                    return
                self.send_json(
                    {
                        "path": rel,
                        "source_kind": source_name,
                        "raw_source": str(raw_path),
                        "smooth_source": str(smooth_path) if smooth_path.exists() else "",
                        "smooth_available": bool(smooth_frames),
                        "raw_frames": raw_frames,
                        "smooth_frames": smooth_frames,
                    }
                )
                return
            self.send_text("Not found", status=404)

    return Handler


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    port = choose_port(args.host, args.port)
    server = ThreadingHTTPServer((args.host, port), make_handler(source))
    url = f"http://{args.host}:{port}/"
    print(f"Serving smooth trajectory visualizer at {url}")
    print(f"Source: {source}")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
