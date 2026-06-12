import argparse
import csv
import hashlib
import json
import shutil
import socket
import subprocess
import threading
import tempfile
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from .ee_transform import CONFIG_PATH, EEF_POSE_DIR, load_config, transform_row
except ImportError:
    from ee_transform import CONFIG_PATH, EEF_POSE_DIR, load_config, transform_row


VIDEO_STREAMS = {
    "left": "observation.image.left_wrist_view",
    "right": "observation.image.right_wrist_view",
}
VIDEO_TRANSCODE_LOCK = threading.Lock()
ACTION_EEF_POSE_DIR = "actions.eef_pose"
EXPORT_TRANSFORM_VERSION = "v1"
EXPORT_TRANSFORM_TAG = "world_eef_raw"
EXPORT_PHASES_PER_EPISODE = 5


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EEF Trajectory Visualizer</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d8dde6;
      --text: #172033;
      --muted: #647084;
      --accent: #0f766e;
      --left: #2563eb;
      --right: #dc2626;
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
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .header-left {
      display: flex;
      align-items: center;
      gap: 16px;
      min-width: 0;
    }
    h1 {
      font-size: 18px;
      margin: 0;
      font-weight: 650;
      letter-spacing: 0;
      white-space: nowrap;
    }
    .mode-tabs {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f8fafc;
    }
    .tab-button {
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: var(--muted);
      padding: 7px 11px;
      font-weight: 620;
    }
    .tab-button.is-active {
      background: var(--panel);
      color: var(--accent);
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.12);
    }
    main {
      display: grid;
      grid-template-columns: minmax(260px, 330px) minmax(0, 1fr);
      gap: 14px;
      padding: 14px;
    }
    aside, .viewer, .control-strip {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    aside {
      padding: 14px;
      height: calc(100vh - 85px);
      overflow: auto;
    }
    .field { margin-bottom: 12px; }
    label {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 5px;
    }
    select, input[type="number"], input[type="text"] {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 8px 9px;
      font: inherit;
    }
    input[type="file"] { width: 100%; }
    .folder-input {
      position: absolute;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }
    .folder-picker {
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr);
      align-items: center;
      gap: 10px;
      width: 100%;
      min-height: 74px;
      padding: 12px;
      border: 1px dashed #9aa7b8;
      border-radius: 8px;
      background: #f8fafc;
      color: var(--text);
      cursor: pointer;
      text-align: left;
    }
    .folder-picker:hover {
      border-color: var(--accent);
      background: #f1f7f6;
    }
    .folder-picker-icon {
      display: grid;
      place-items: center;
      width: 42px;
      height: 42px;
      border-radius: 8px;
      background: #e7f1ef;
      color: var(--accent);
      font-size: 22px;
      font-weight: 650;
    }
    .folder-picker-title {
      display: block;
      font-size: 14px;
      font-weight: 650;
      color: var(--text);
    }
    .folder-picker-subtitle {
      display: block;
      margin-top: 3px;
      font-size: 12px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    button {
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 6px;
      color: var(--text);
      padding: 8px 11px;
      font: inherit;
      cursor: pointer;
    }
    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }
    button:disabled {
      opacity: 0.55;
      cursor: default;
    }
    .meta {
      display: grid;
      gap: 3px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .meta strong { color: var(--text); font-weight: 620; }
    .rot-row {
      display: grid;
      grid-template-columns: 72px 1fr;
      gap: 8px;
      margin-bottom: 8px;
    }
    .offset-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      background: #f0f3f7;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px;
      font-size: 12px;
      max-height: 180px;
      overflow: auto;
    }
    .workspace {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .transformed-only { display: none !important; }
    body[data-mode="transformed"] .original-only { display: none !important; }
    body[data-mode="transformed"] .transformed-only { display: block !important; }
    body[data-mode="transformed"] .workspace { grid-template-columns: minmax(0, 1fr); }
    body[data-mode="transformed"] aside { height: calc(100vh - 85px); }
    .video-row {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      margin-top: 14px;
    }
    .video-panel {
      min-width: 0;
      overflow: hidden;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .video-panel video {
      display: block;
      width: 100%;
      aspect-ratio: 16 / 9;
      max-height: 300px;
      background: #111827;
      object-fit: contain;
      transform: rotate(180deg);
    }
    .viewer {
      min-width: 0;
      overflow: hidden;
    }
    .viewer-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
    }
    .viewer-title strong {
      color: var(--text);
      font-size: 14px;
    }
    canvas {
      display: block;
      width: 100%;
      height: min(62vh, 680px);
      min-height: 390px;
      background: #fbfcfe;
      cursor: grab;
      touch-action: none;
      user-select: none;
    }
    canvas.is-dragging { cursor: grabbing; }
    .pose-tooltip {
      position: fixed;
      z-index: 20;
      min-width: 218px;
      max-width: 280px;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.97);
      box-shadow: 0 10px 24px rgba(15, 23, 42, 0.16);
      color: var(--text);
      font-size: 12px;
      pointer-events: none;
      opacity: 0;
      transform: translate3d(0, 0, 0);
      transition: opacity 80ms ease;
    }
    .pose-tooltip.is-visible { opacity: 1; }
    .pose-tooltip-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 6px;
      font-weight: 650;
    }
    .pose-tooltip-title span:last-child {
      color: var(--muted);
      font-weight: 500;
    }
    .pose-tooltip-row {
      display: grid;
      grid-template-columns: 42px minmax(0, 1fr);
      gap: 8px;
      align-items: baseline;
      margin-top: 3px;
    }
    .pose-tooltip-row span:first-child { color: var(--muted); }
    .pose-tooltip-row span:last-child {
      font-variant-numeric: tabular-nums;
      overflow-wrap: anywhere;
    }
    .panel-controls {
      display: grid;
      grid-template-columns: 86px minmax(0, 1fr) 74px;
      gap: 10px;
      align-items: center;
      padding: 10px 12px 12px;
      border-top: 1px solid var(--line);
    }
    input[type="range"] { width: 100%; }
    .legend {
      display: flex;
      align-items: center;
      gap: 12px;
      color: var(--muted);
      font-size: 12px;
    }
    .dot {
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      margin-right: 5px;
      vertical-align: -1px;
    }
    .status {
      padding: 12px;
      color: var(--muted);
    }
    .path-value {
      display: inline-block;
      max-width: 100%;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .export-progress {
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .export-progress progress {
      width: 100%;
      height: 10px;
      accent-color: var(--accent);
    }
    .export-progress span {
      overflow-wrap: anywhere;
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      aside { height: auto; }
      .workspace { grid-template-columns: 1fr; }
      .video-row { grid-template-columns: 1fr; }
      canvas { height: 430px; }
    }
  </style>
</head>
<body data-mode="original">
  <header>
    <div class="header-left">
      <h1>EEF Trajectory Visualizer</h1>
      <nav class="mode-tabs" aria-label="Visualizer mode">
        <button id="originalTab" class="tab-button is-active" type="button" data-mode="original">original</button>
        <button id="transformedTab" class="tab-button" type="button" data-mode="transformed">transformed</button>
      </nav>
    </div>
    <div class="legend">
      <span><span class="dot" style="background: var(--left)"></span>left</span>
      <span><span class="dot" style="background: var(--right)"></span>right</span>
      <span style="color:#ef4444">X</span>
      <span style="color:#22c55e">Y</span>
      <span style="color:#3b82f6">Z</span>
    </div>
  </header>
  <main>
    <aside>
      <div class="field">
        <label>Open class folder</label>
        <button id="folderPicker" class="folder-picker" type="button">
          <span class="folder-picker-icon">+</span>
          <span>
            <span class="folder-picker-title">Choose local class folder</span>
            <span id="folderPickerSubtitle" class="folder-picker-subtitle">Select a folder containing episode_* directories</span>
          </span>
        </button>
        <input id="folderInput" class="folder-input" type="file" webkitdirectory directory multiple>
      </div>
      <div class="field">
        <label>Server class or episode path</label>
        <div style="display:grid; grid-template-columns:minmax(0,1fr) 72px 76px; gap:8px;">
          <input id="serverPath" type="text" placeholder="/path/to/class or /path/to/class/episode_0001">
          <button id="serverOpen">Open</button>
          <button id="serverBrowse">Browse</button>
        </div>
      </div>
      <div class="field">
        <label>Episode</label>
        <select id="episodeSelect"></select>
      </div>
      <div class="field meta">
        <div>Class: <strong id="className">-</strong></div>
        <div>Source path: <strong id="sourcePath" class="path-value">-</strong></div>
        <div>Episode: <strong id="episodeName">-</strong></div>
        <div>Frames: <strong id="frameCount">0</strong></div>
        <div>Frame: <strong id="frameIndex">0</strong></div>
      </div>
      <div class="field meta">
        <div><strong>World orientation mapping</strong></div>
        <div>World X = original EEF Z</div>
        <div>World Y = -original EEF Y</div>
        <div>World Z = original EEF X</div>
        <div>TCP with tracker zero rotation = world EEF Ry(+90 deg)</div>
      </div>
      <div class="field transformed-only">
        <label>World offset, meters</label>
        <div class="offset-grid">
          <input id="transformedOffsetX" type="number" step="0.001" placeholder="x">
          <input id="transformedOffsetY" type="number" step="0.001" placeholder="y">
          <input id="transformedOffsetZ" type="number" step="0.001" placeholder="z">
        </div>
      </div>
      <div class="field transformed-only" style="display:flex; gap:8px;">
        <button id="resetTransformedOffset">Reset offset</button>
      </div>
      <div class="field original-only">
        <label>Export folder</label>
        <div style="display:grid; grid-template-columns:minmax(0,1fr) 82px; gap:8px;">
          <input id="exportDir" type="text" value="outputs" placeholder="outputs">
          <button id="chooseExportDir">Choose</button>
        </div>
      </div>
      <div class="field original-only" style="display:flex; gap:8px;">
        <button id="exportEpisode">Export episode</button>
        <button id="exportClass">Export class</button>
      </div>
      <div class="field export-progress original-only">
        <progress id="exportProgress" max="1" value="0"></progress>
        <span id="exportProgressText">Export idle.</span>
      </div>
      <div class="field original-only">
        <label style="display:flex; align-items:center; gap:7px; color:var(--text); font-size:13px;">
          <input id="partialExport" type="checkbox" style="width:auto;">
          Partial episode export
        </label>
        <div style="display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:8px;">
          <input id="partialStartFrame" type="number" min="0" max="0" step="1" value="0" placeholder="start frame">
          <input id="partialEndFrame" type="number" min="0" max="0" step="1" value="0" placeholder="end frame">
        </div>
      </div>
      <div class="field original-only">
        <label>Rotation sequence</label>
        <div id="rotationEditor"></div>
      </div>
      <div class="field original-only">
        <label>Position offset, meters</label>
        <div class="offset-grid">
          <input id="offsetX" type="number" step="0.001" placeholder="x">
          <input id="offsetY" type="number" step="0.001" placeholder="y">
          <input id="offsetZ" type="number" step="0.001" placeholder="z">
        </div>
      </div>
      <div class="field original-only">
        <label>Right final offset, meters</label>
        <div class="offset-grid">
          <input id="rightOffsetX" type="number" step="0.001" placeholder="x">
          <input id="rightOffsetY" type="number" step="0.001" placeholder="y">
          <input id="rightOffsetZ" type="number" step="0.001" placeholder="z">
        </div>
      </div>
      <div class="field original-only" style="display:flex; gap:8px;">
        <button class="primary" id="applyConfig">Apply</button>
        <button id="resetConfig">Reset</button>
      </div>
      <div class="field original-only">
        <label>Active config</label>
        <pre id="configJson"></pre>
      </div>
    </aside>
    <section>
      <div class="workspace">
        <div class="viewer original-only">
          <div class="viewer-title">
            <strong>Original trajectory</strong>
            <span id="rawStamp">-</span>
          </div>
          <canvas id="rawCanvas"></canvas>
          <div class="panel-controls">
            <button id="rawPlay">Play</button>
            <input id="rawProgress" type="range" min="0" max="0" value="0">
            <span id="rawCounter">0 / 0</span>
          </div>
        </div>
        <div class="viewer">
          <div class="viewer-title">
            <strong>World EEF pose</strong>
            <span id="transformedStamp">-</span>
          </div>
          <canvas id="transformedCanvas"></canvas>
          <div class="panel-controls">
            <button id="transformedPlay">Play</button>
            <input id="transformedProgress" type="range" min="0" max="0" value="0">
            <span id="transformedCounter">0 / 0</span>
          </div>
        </div>
      </div>
      <div class="video-row">
        <div class="video-panel">
          <div class="viewer-title">
            <strong>Left wrist view</strong>
            <span id="leftVideoStamp">-</span>
          </div>
          <video id="leftVideo" muted playsinline preload="metadata"></video>
        </div>
        <div class="video-panel">
          <div class="viewer-title">
            <strong>Right wrist view</strong>
            <span id="rightVideoStamp">-</span>
          </div>
          <video id="rightVideo" muted playsinline preload="metadata"></video>
        </div>
      </div>
      <div id="status" class="status">Load a class folder or start the server with a class path.</div>
    </section>
  </main>
  <div id="poseTooltip" class="pose-tooltip" aria-hidden="true"></div>
<script>
const EEF_POSE_DIR = "observation.state.eef_pose";
const ACTION_EEF_POSE_DIR = "actions.eef_pose";
const EXPORT_TRANSFORM_VERSION = "v1";
const EXPORT_TRANSFORM_TAG = "world_eef_raw";
const VIDEO_STREAMS = {
  left: "observation.image.left_wrist_view",
  right: "observation.image.right_wrist_view",
};
const sides = ["left", "right"];
const sideColors = {left: "#2563eb", right: "#dc2626"};
const eefColors = {left: "#06b6d4", right: "#f59e0b"};
const axisColors = ["#ef4444", "#22c55e", "#3b82f6"];

function defaultView() {
  return {panX: 0, panY: 0, yaw: -42 * Math.PI / 180, pitch: -28 * Math.PI / 180, zoom: 1, drag: null};
}

const state = {
  mode: "original",
  source: "server",
  className: "-",
  classPath: "",
  episodes: [],
  folderFiles: new Map(),
  folderAllFiles: new Map(),
  folderVideoFiles: new Map(),
  exportDirectoryHandle: null,
  exporting: false,
  videoObjectUrls: [],
  videos: {
    left: {timestamps: [], available: false},
    right: {timestamps: [], available: false},
  },
  rawFrames: [],
  rawEefFrames: [],
  transformedFrames: [],
  transformedWorldOffset: {x: 0, y: 0, z: 0},
  config: null,
  defaultConfig: null,
  index: 0,
  playing: false,
  playStartedAt: 0,
  playStartTimestamp: 0,
  playStartIndex: 0,
  views: {
    raw: defaultView(),
    transformed: defaultView(),
  },
  hoverTargets: {
    raw: [],
    transformed: [],
  },
};

const els = {
  originalTab: document.getElementById("originalTab"),
  transformedTab: document.getElementById("transformedTab"),
  folderInput: document.getElementById("folderInput"),
  folderPicker: document.getElementById("folderPicker"),
  folderPickerSubtitle: document.getElementById("folderPickerSubtitle"),
  serverPath: document.getElementById("serverPath"),
  serverOpen: document.getElementById("serverOpen"),
  serverBrowse: document.getElementById("serverBrowse"),
  episodeSelect: document.getElementById("episodeSelect"),
  className: document.getElementById("className"),
  episodeName: document.getElementById("episodeName"),
  sourcePath: document.getElementById("sourcePath"),
  frameCount: document.getElementById("frameCount"),
  frameIndex: document.getElementById("frameIndex"),
  exportDir: document.getElementById("exportDir"),
  chooseExportDir: document.getElementById("chooseExportDir"),
  exportEpisode: document.getElementById("exportEpisode"),
  exportClass: document.getElementById("exportClass"),
  exportProgress: document.getElementById("exportProgress"),
  exportProgressText: document.getElementById("exportProgressText"),
  partialExport: document.getElementById("partialExport"),
  partialStartFrame: document.getElementById("partialStartFrame"),
  partialEndFrame: document.getElementById("partialEndFrame"),
  rotationEditor: document.getElementById("rotationEditor"),
  offsetX: document.getElementById("offsetX"),
  offsetY: document.getElementById("offsetY"),
  offsetZ: document.getElementById("offsetZ"),
  rightOffsetX: document.getElementById("rightOffsetX"),
  rightOffsetY: document.getElementById("rightOffsetY"),
  rightOffsetZ: document.getElementById("rightOffsetZ"),
  transformedOffsetX: document.getElementById("transformedOffsetX"),
  transformedOffsetY: document.getElementById("transformedOffsetY"),
  transformedOffsetZ: document.getElementById("transformedOffsetZ"),
  resetTransformedOffset: document.getElementById("resetTransformedOffset"),
  applyConfig: document.getElementById("applyConfig"),
  resetConfig: document.getElementById("resetConfig"),
  configJson: document.getElementById("configJson"),
  rawCanvas: document.getElementById("rawCanvas"),
  transformedCanvas: document.getElementById("transformedCanvas"),
  leftVideo: document.getElementById("leftVideo"),
  rightVideo: document.getElementById("rightVideo"),
  leftVideoStamp: document.getElementById("leftVideoStamp"),
  rightVideoStamp: document.getElementById("rightVideoStamp"),
  rawPlay: document.getElementById("rawPlay"),
  transformedPlay: document.getElementById("transformedPlay"),
  rawProgress: document.getElementById("rawProgress"),
  transformedProgress: document.getElementById("transformedProgress"),
  rawCounter: document.getElementById("rawCounter"),
  transformedCounter: document.getElementById("transformedCounter"),
  rawStamp: document.getElementById("rawStamp"),
  transformedStamp: document.getElementById("transformedStamp"),
  poseTooltip: document.getElementById("poseTooltip"),
  status: document.getElementById("status"),
};

function setStatus(text) {
  els.status.textContent = text;
}

function isTransformedMode() {
  return state.mode === "transformed";
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function yieldToBrowser() {
  await sleep(0);
}

function sourcePathLabel() {
  if (state.source === "server") return state.classPath || "-";
  if (!state.classPath) return "-";
  if (state.classPath.startsWith("/") || /^[A-Za-z]:[\\/]/.test(state.classPath)) return state.classPath;
  return `${state.classPath} (browser-relative)`;
}

function pathParts(path) {
  return String(path || "").split(/[\\/]+/).filter(Boolean);
}

function isEpisodeName(name) {
  return /^episode_\d+/.test(String(name || ""));
}

function classNameForPath(path) {
  const parts = pathParts(path);
  if (!parts.length) return "";
  const last = parts[parts.length - 1];
  if (isEpisodeName(last) && parts.length >= 2) return parts[parts.length - 2];
  return last;
}

function syncSourceMeta() {
  els.className.textContent = state.className || "-";
  els.className.title = state.className || "";
  els.sourcePath.textContent = sourcePathLabel();
  els.sourcePath.title = sourcePathLabel();
}

function setExportButtonsDisabled(disabled) {
  els.exportEpisode.disabled = disabled;
  els.exportClass.disabled = disabled;
  els.chooseExportDir.disabled = disabled;
  els.folderPicker.disabled = disabled;
  els.serverBrowse.disabled = disabled;
  els.serverOpen.disabled = disabled;
}

function setExportProgress(value, max, text) {
  const safeMax = Math.max(1, Number(max) || 1);
  const safeValue = Math.max(0, Math.min(Number(value) || 0, safeMax));
  els.exportProgress.max = String(safeMax);
  els.exportProgress.value = String(safeValue);
  els.exportProgress.setAttribute("aria-valuemax", String(safeMax));
  els.exportProgress.setAttribute("aria-valuenow", String(safeValue));
  els.exportProgressText.textContent = text;
}

function beginExport(scope) {
  if (state.exporting) {
    setStatus("An export is already running.");
    return false;
  }
  state.exporting = true;
  setExportButtonsDisabled(true);
  setExportProgress(0, 1, `Preparing ${scope} export...`);
  return true;
}

function endExport() {
  state.exporting = false;
  setExportButtonsDisabled(false);
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function degToRad(value) {
  return value * Math.PI / 180;
}

function identity3() {
  return [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
}

function rotationMatrix(axis, degrees) {
  const a = degToRad(Number(degrees) || 0);
  const c = Math.cos(a);
  const s = Math.sin(a);
  if (axis === "x") return [[1, 0, 0], [0, c, -s], [0, s, c]];
  if (axis === "y") return [[c, 0, s], [0, 1, 0], [-s, 0, c]];
  if (axis === "z") return [[c, -s, 0], [s, c, 0], [0, 0, 1]];
  return identity3();
}

function matMul(a, b) {
  const out = identity3();
  for (let r = 0; r < 3; r++) {
    for (let c = 0; c < 3; c++) {
      out[r][c] = a[r][0] * b[0][c] + a[r][1] * b[1][c] + a[r][2] * b[2][c];
    }
  }
  return out;
}

function matTranspose(m) {
  return [
    [m[0][0], m[1][0], m[2][0]],
    [m[0][1], m[1][1], m[2][1]],
    [m[0][2], m[1][2], m[2][2]],
  ];
}

function matVec(m, v) {
  return [
    m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
    m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
    m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
  ];
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

function matrixToRotation6d(m) {
  return [m[0][0], m[1][0], m[2][0], m[0][1], m[1][1], m[2][1]];
}

function matrixToRpyDegrees(m) {
  const sy = Math.hypot(m[0][0], m[1][0]);
  let roll;
  let pitch;
  let yaw;
  if (sy > 1e-6) {
    roll = Math.atan2(m[2][1], m[2][2]);
    pitch = Math.atan2(-m[2][0], sy);
    yaw = Math.atan2(m[1][0], m[0][0]);
  } else {
    roll = Math.atan2(-m[1][2], m[1][1]);
    pitch = Math.atan2(-m[2][0], sy);
    yaw = 0;
  }
  const k = 180 / Math.PI;
  return [roll * k, pitch * k, yaw * k];
}

function poseRpyDegrees(pose) {
  return matrixToRpyDegrees(rotation6dToMatrix(pose.rot6));
}

function buildAlignMatrix(config) {
  return buildRotationSequence(config.rotation_sequence || []);
}

function buildRotationSequence(sequence) {
  let m = identity3();
  for (const step of sequence || []) {
    m = matMul(m, rotationMatrix(String(step.axis || "x").toLowerCase(), Number(step.degrees) || 0));
  }
  return m;
}

function vectorFromConfig(value, fallback = {x: 0, y: 0, z: 0}) {
  const source = value || fallback;
  return [Number(source.x || 0), Number(source.y || 0), Number(source.z || 0)];
}

function localEefVector(config) {
  return vectorFromConfig(config?.local_ee_projection?.tracker_based_ee_local_position);
}

function trackerAxesInEefMatrix(config) {
  const axes = config?.local_ee_projection?.tracker_axes_in_eef_frame || {
    x: [1, 0, 0],
    y: [0, 1, 0],
    z: [0, 0, 1],
  };
  return [
    [Number(axes.x[0] || 0), Number(axes.y[0] || 0), Number(axes.z[0] || 0)],
    [Number(axes.x[1] || 0), Number(axes.y[1] || 0), Number(axes.z[1] || 0)],
    [Number(axes.x[2] || 0), Number(axes.y[2] || 0), Number(axes.z[2] || 0)],
  ];
}

function worldAxesInTransformedMatrix(config) {
  const axes = config?.world_projection?.world_axes_in_transformed_frame || {
    x: [0, 0, 1],
    y: [0, -1, 0],
    z: [1, 0, 0],
  };
  return [
    [Number(axes.x[0] || 0), Number(axes.y[0] || 0), Number(axes.z[0] || 0)],
    [Number(axes.x[1] || 0), Number(axes.y[1] || 0), Number(axes.z[1] || 0)],
    [Number(axes.x[2] || 0), Number(axes.y[2] || 0), Number(axes.z[2] || 0)],
  ];
}

function worldPositionOffset(config) {
  return vectorFromConfig(config?.world_projection?.world_position_offset_m);
}

function zeroRpyPoseInTransformedMatrix(config) {
  const sequence = config?.world_projection?.zero_rpy_pose_in_transformed_frame?.rotation_sequence || [
    {axis: "y", degrees: -90},
  ];
  return buildRotationSequence(sequence);
}

function zeroWorldEefPoseMatrix(config) {
  const sequence = config?.world_projection?.zero_world_eef_pose?.rotation_sequence || [
    {axis: "y", degrees: 90},
  ];
  return buildRotationSequence(sequence);
}

function transformFrame(frame, config) {
  const coordinateRotation = matTranspose(buildAlignMatrix(config));
  const baseOffset = vectorFromConfig(config.position_offset_m);
  const rightOffset = vectorFromConfig(config.right_position_offset_m);
  const localEef = localEefVector(config);
  const eefFromTracker = matTranspose(trackerAxesInEefMatrix(config));
  const transformedFromWorld = worldAxesInTransformedMatrix(config);
  const worldFromTransformed = matTranspose(transformedFromWorld);
  const worldOffset = worldPositionOffset(config);
  const out = {timestamp: frame.timestamp, left: null, right: null};
  for (const side of sides) {
    const pose = frame[side];
    const trackerRotationOriginal = rotation6dToMatrix(pose.rot6);
    const trackerPositionTransformed = add(
      add(matVec(coordinateRotation, pose.pos), baseOffset),
      side === "right" ? rightOffset : [0, 0, 0],
    );
    const trackerRotationTransformed = matMul(coordinateRotation, trackerRotationOriginal);
    const eefPositionTransformed = add(
      trackerPositionTransformed,
      matVec(trackerRotationTransformed, localEef),
    );
    const eefRotationOriginal = matMul(trackerRotationOriginal, eefFromTracker);
    const deltaOriginalEef = matMul(matTranspose(eefFromTracker), eefRotationOriginal);
    const deltaWorld = matMul(matMul(worldFromTransformed, deltaOriginalEef), transformedFromWorld);
    const eefRotationWorld = matMul(deltaWorld, zeroWorldEefPoseMatrix(config));
    out[side] = {
      pos: add(matVec(worldFromTransformed, eefPositionTransformed), worldOffset),
      rot6: matrixToRotation6d(eefRotationWorld),
    };
  }
  return out;
}

function syncConfigEditor() {
  const sequence = state.config.rotation_sequence || [];
  els.rotationEditor.innerHTML = "";
  for (let i = 0; i < Math.max(3, sequence.length); i++) {
    const step = sequence[i] || {axis: "x", degrees: 0};
    const row = document.createElement("div");
    row.className = "rot-row";
    row.innerHTML = `
      <select class="rot-axis">
        <option value="x">x</option>
        <option value="y">y</option>
        <option value="z">z</option>
      </select>
      <input class="rot-degrees" type="number" step="0.1" value="${Number(step.degrees || 0)}">
    `;
    row.querySelector(".rot-axis").value = String(step.axis || "x").toLowerCase();
    els.rotationEditor.appendChild(row);
  }
  const offset = state.config.position_offset_m || {x: 0, y: 0, z: 0};
  els.offsetX.value = Number(offset.x || 0);
  els.offsetY.value = Number(offset.y || 0);
  els.offsetZ.value = Number(offset.z || 0);
  const rightOffset = state.config.right_position_offset_m || {x: 0, y: 0, z: 0};
  els.rightOffsetX.value = Number(rightOffset.x || 0);
  els.rightOffsetY.value = Number(rightOffset.y || 0);
  els.rightOffsetZ.value = Number(rightOffset.z || 0);
  updateConfigJson();
}

function readConfigEditor() {
  const rows = Array.from(els.rotationEditor.querySelectorAll(".rot-row"));
  state.config.rotation_sequence = rows.map(row => ({
    axis: row.querySelector(".rot-axis").value,
    degrees: Number(row.querySelector(".rot-degrees").value || 0),
  }));
  state.config.position_offset_m = {
    x: Number(els.offsetX.value || 0),
    y: Number(els.offsetY.value || 0),
    z: Number(els.offsetZ.value || 0),
  };
  state.config.right_position_offset_m = {
    x: Number(els.rightOffsetX.value || 0),
    y: Number(els.rightOffsetY.value || 0),
    z: Number(els.rightOffsetZ.value || 0),
  };
  updateConfigJson();
}

function updateConfigJson() {
  els.configJson.textContent = JSON.stringify(state.config, null, 2);
}

function readTransformedOffsetEditor() {
  state.transformedWorldOffset = {
    x: Number(els.transformedOffsetX.value || 0),
    y: Number(els.transformedOffsetY.value || 0),
    z: Number(els.transformedOffsetZ.value || 0),
  };
}

function syncTransformedOffsetEditor() {
  els.transformedOffsetX.value = Number(state.transformedWorldOffset.x || 0);
  els.transformedOffsetY.value = Number(state.transformedWorldOffset.y || 0);
  els.transformedOffsetZ.value = Number(state.transformedWorldOffset.z || 0);
}

function addWorldOffsetToFrame(frame, offset) {
  const delta = [Number(offset.x || 0), Number(offset.y || 0), Number(offset.z || 0)];
  return {
    timestamp: frame.timestamp,
    left: {pos: add(frame.left.pos, delta), rot6: frame.left.rot6.slice()},
    right: {pos: add(frame.right.pos, delta), rot6: frame.right.rot6.slice()},
  };
}

function computeEefPoseFrames(frames, config) {
  const localEef = localEefVector(config);
  const eefFromTracker = matTranspose(trackerAxesInEefMatrix(config));
  return frames.map(frame => {
    const out = {timestamp: frame.timestamp, left: null, right: null};
    for (const side of sides) {
      const trackerRot = rotation6dToMatrix(frame[side].rot6);
      const eefRot = matMul(trackerRot, eefFromTracker);
      out[side] = {
        pos: add(frame[side].pos, matVec(trackerRot, localEef)),
        rot6: matrixToRotation6d(eefRot),
      };
    }
    return out;
  });
}

function recomputeDisplayFrames() {
  if (!state.rawFrames.length || !state.config) return;
  if (!isTransformedMode()) readConfigEditor();
  state.rawEefFrames = computeEefPoseFrames(state.rawFrames, state.config);
  if (isTransformedMode()) {
    readTransformedOffsetEditor();
    state.transformedFrames = state.rawFrames.map(frame => addWorldOffsetToFrame(frame, state.transformedWorldOffset));
  } else {
    state.transformedFrames = state.rawFrames.map(frame => transformFrame(frame, state.config));
  }
  state.index = Math.min(state.index, state.rawFrames.length - 1);
  syncProgress();
  render();
}

function recomputeTransformed() {
  recomputeDisplayFrames();
}

function parseCsv(text) {
  const lines = text.trim().split(/\r?\n/).filter(Boolean);
  if (lines.length <= 1) return [];
  const header = lines[0].split(",");
  const rows = [];
  for (let i = 1; i < lines.length; i++) {
    const cells = lines[i].split(",");
    const row = {};
    for (let c = 0; c < header.length; c++) row[header[c]] = cells[c];
    rows.push(row);
  }
  return rows;
}

function parseTimestampCsv(text) {
  return parseCsv(text)
    .map(row => Number(row.timestamp_ms))
    .filter(value => Number.isFinite(value));
}

function rowsToFrames(rows) {
  return rows.map(row => ({
    timestamp: Number(row.timestamp_ms),
    left: {
      pos: [Number(row.left_x), Number(row.left_y), Number(row.left_z)],
      rot6: [1, 2, 3, 4, 5, 6].map(i => Number(row[`left_r${i}`])),
    },
    right: {
      pos: [Number(row.right_x), Number(row.right_y), Number(row.right_z)],
      rot6: [1, 2, 3, 4, 5, 6].map(i => Number(row[`right_r${i}`])),
    },
  }));
}

function frameToRow(row, frame) {
  const out = {...row};
  for (const side of sides) {
    out[`${side}_x`] = Number(frame[side].pos[0]).toFixed(9);
    out[`${side}_y`] = Number(frame[side].pos[1]).toFixed(9);
    out[`${side}_z`] = Number(frame[side].pos[2]).toFixed(9);
    for (let i = 1; i <= 6; i++) {
      out[`${side}_r${i}`] = Number(frame[side].rot6[i - 1]).toFixed(9);
    }
  }
  return out;
}

function csvEscape(value) {
  const text = value == null ? "" : String(value);
  if (!/[",\r\n]/.test(text)) return text;
  return `"${text.replace(/"/g, '""')}"`;
}

function transformCsvText(text) {
  const lines = text.trim().split(/\r?\n/).filter(Boolean);
  if (lines.length <= 1) return text;
  const header = lines[0].split(",");
  const rows = parseCsv(text);
  const transformedRows = rows.map(row => frameToRow(row, transformFrame(rowsToFrames([row])[0], state.config)));
  return [
    header.join(","),
    ...transformedRows.map(row => header.map(key => csvEscape(row[key])).join(",")),
  ].join("\n") + "\n";
}

function csvRowsToText(header, rows) {
  return [
    header.join(","),
    ...rows.map(row => header.map(key => csvEscape(row[key])).join(",")),
  ].join("\n") + "\n";
}

function cropTimestampedCsvText(text, partial) {
  if (!partial?.enabled) return text;
  const lines = text.trim().split(/\r?\n/).filter(Boolean);
  if (lines.length <= 1) return text;
  const header = lines[0].split(",");
  if (!header.includes("timestamp_ms")) return text;

  const rows = parseCsv(text);
  const keptRows = [];
  for (const row of rows) {
    const timestamp = Number(row.timestamp_ms);
    if (!Number.isFinite(timestamp)) continue;
    if (timestamp < partial.start_timestamp_ms || timestamp > partial.end_timestamp_ms) continue;
    keptRows.push({
      ...row,
      timestamp_ms: formatTimestampMs(timestamp - partial.start_timestamp_ms),
    });
  }
  return csvRowsToText(header, keptRows);
}

function formatTimestampMs(value) {
  const rounded = Math.round(value);
  if (Math.abs(value - rounded) < 1e-6) return String(rounded);
  return Number(value).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
}

async function updateMetadataText(text, partial = {enabled: false}) {
  try {
    const metadata = JSON.parse(text);
    if (metadata && typeof metadata === "object" && !Array.isArray(metadata)) {
      metadata.umi_transform_version = EXPORT_TRANSFORM_VERSION;
      metadata.umi_transform_tag = EXPORT_TRANSFORM_TAG;
      metadata.umi_transform_cropped = Boolean(partial?.enabled);
      if (partial?.enabled) {
        metadata.umi_transform_crop = {
          enabled: true,
          source: "browser_local_partial_export",
          start_frame: partial.start_frame,
          end_frame: partial.end_frame,
          frame_count: partial.end_frame - partial.start_frame + 1,
          start_timestamp_ms: partial.start_timestamp_ms,
          end_timestamp_ms: partial.end_timestamp_ms,
          timestamp_offset_ms: partial.start_timestamp_ms,
          video_policy: "video.mp4 and video timestamps.csv copied unchanged in browser-local export",
        };
      }
      return JSON.stringify(metadata, null, 2) + "\n";
    }
  } catch {
    // Keep invalid JSON unchanged.
  }
  return text;
}

async function sha256Hex(blob) {
  const buffer = await blob.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest)).map(byte => byte.toString(16).padStart(2, "0")).join("");
}

async function getChildDirectory(parent, name) {
  return await parent.getDirectoryHandle(name, {create: true});
}

async function getNestedDirectory(parent, parts) {
  let current = parent;
  for (const part of parts) {
    if (!part) continue;
    current = await getChildDirectory(current, part);
  }
  return current;
}

async function writeFile(directory, filename, data) {
  const handle = await directory.getFileHandle(filename, {create: true});
  const writable = await handle.createWritable();
  await writable.write(data);
  await writable.close();
}

async function writeRelativeFile(root, relativePath, data) {
  const parts = relativePath.split("/").filter(Boolean);
  const filename = parts.pop();
  const directory = await getNestedDirectory(root, parts);
  await writeFile(directory, filename, data);
}

async function transformedLocalFilePayload(innerPath, file, partial = {enabled: false}) {
  if (innerPath === `${EEF_POSE_DIR}/data.csv` || innerPath === `${ACTION_EEF_POSE_DIR}/data.csv`) {
    const transformed = transformCsvText(await file.text());
    return new Blob([cropTimestampedCsvText(transformed, partial)], {type: "text/csv;charset=utf-8"});
  }
  if (partial?.enabled && innerPath.endsWith("/data.csv")) {
    return new Blob([cropTimestampedCsvText(await file.text(), partial)], {type: "text/csv;charset=utf-8"});
  }
  if (innerPath === "metadata.json") {
    return new Blob([await updateMetadataText(await file.text(), partial)], {type: "application/json;charset=utf-8"});
  }
  return file;
}

function updatePartialFrameControls(resetValues = false) {
  const max = Math.max(0, state.rawFrames.length - 1);
  for (const input of [els.partialStartFrame, els.partialEndFrame]) {
    input.min = "0";
    input.max = String(max);
  }
  if (resetValues) {
    els.partialStartFrame.value = "0";
    els.partialEndFrame.value = String(max);
    return;
  }
  const start = Math.max(0, Math.min(Number(els.partialStartFrame.value || 0), max));
  const end = Math.max(start, Math.min(Number(els.partialEndFrame.value || max), max));
  els.partialStartFrame.value = String(start);
  els.partialEndFrame.value = String(end);
}

function partialExportPayload(scope) {
  if (scope !== "episode" || !els.partialExport.checked) return {enabled: false};
  if (!state.rawFrames.length) throw new Error("No frames are loaded for partial export.");
  const max = state.rawFrames.length - 1;
  const startFrame = Number(els.partialStartFrame.value);
  const endFrame = Number(els.partialEndFrame.value);
  if (!Number.isInteger(startFrame) || !Number.isInteger(endFrame)) {
    throw new Error("Partial export frame range must use integer frame numbers.");
  }
  if (startFrame < 0 || endFrame < 0 || startFrame > max || endFrame > max || startFrame > endFrame) {
    throw new Error(`Partial export frame range must be between 0 and ${max}, with start <= end.`);
  }
  return {enabled: true, start_frame: startFrame, end_frame: endFrame};
}

async function exportLocalEpisode(episode, destinationClassDir, progress, partial = {enabled: false}) {
  const prefix = `${episode}/`;
  const entries = Array.from(state.folderAllFiles.entries())
    .filter(([key]) => key.startsWith(prefix))
    .map(([key, file]) => [key.slice(prefix.length), file]);
  if (!entries.length) throw new Error(`No files found for ${episode}`);

  const episodeDir = await getNestedDirectory(destinationClassDir, [episode]);
  const written = [];
  for (const [innerPath, file] of entries) {
    if (innerPath === "checksums.sha256") continue;
    const payload = await transformedLocalFilePayload(innerPath, file, partial);
    await writeRelativeFile(episodeDir, innerPath, payload);
    written.push({path: innerPath, payload});
    if (progress) await progress(episode, innerPath);
  }

  const checksumEntries = [];
  for (const item of written.sort((a, b) => a.path.localeCompare(b.path))) {
    checksumEntries.push(`${await sha256Hex(item.payload)}  ${item.path}`);
  }
  await writeRelativeFile(episodeDir, "checksums.sha256", checksumEntries.join("\n") + (checksumEntries.length ? "\n" : ""));
  if (progress) await progress(episode, "checksums.sha256");
  return {episode, file_count: written.length + 1};
}

function localEpisodesHaveWristVideos(episodes) {
  for (const episode of episodes) {
    for (const stream of Object.values(VIDEO_STREAMS)) {
      if (state.folderVideoFiles.has(`${episode}:${stream}:video`)) return true;
    }
  }
  return false;
}

function localExportEntries(selectedEpisodes) {
  const selected = new Set(selectedEpisodes);
  return Array.from(state.folderAllFiles.entries())
    .filter(([key]) => selected.has(key.split("/", 1)[0]))
    .sort((a, b) => a[0].localeCompare(b[0]));
}

async function startFolderUploadExport(scope, selectedEpisodes, partial, outputDir) {
  const res = await fetch("/api/upload_export/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      scope,
      class_name: state.className || "local",
      selected_episodes: selectedEpisodes,
      output_dir: outputDir,
      config: readExportConfig(),
      partial,
    }),
  });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

async function uploadFolderFile(uploadId, relativePath, file) {
  const res = await fetch(
    `/api/upload_export/file?id=${encodeURIComponent(uploadId)}&path=${encodeURIComponent(relativePath)}`,
    {
      method: "POST",
      headers: {"Content-Type": file.type || "application/octet-stream"},
      body: file,
    },
  );
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

async function finishFolderUploadExport(uploadId) {
  const res = await fetch("/api/upload_export/finish", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: uploadId}),
  });
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}

async function pollExportJob(jobId, offset = 0, totalOffset = 0) {
  while (true) {
    const res = await fetch(`/api/export_progress?id=${encodeURIComponent(jobId)}`);
    if (!res.ok) throw new Error(await res.text());
    const job = await res.json();
    setExportProgress(offset + (job.value || 0), totalOffset + (job.max || 1), job.message || "Exporting...");
    if (job.status === "done" || job.status === "error") return job;
    await sleep(350);
  }
}

async function exportLocalData(scope) {
  let partial;
  try {
    partial = partialExportPayload(scope);
  } catch (error) {
    setExportProgress(0, 1, error.message);
    setStatus(error.message);
    return;
  }
  if (partial.enabled) {
    partial = {
      ...partial,
      start_timestamp_ms: Number(state.rawFrames[partial.start_frame]?.timestamp),
      end_timestamp_ms: Number(state.rawFrames[partial.end_frame]?.timestamp),
    };
    if (!Number.isFinite(partial.start_timestamp_ms) || !Number.isFinite(partial.end_timestamp_ms)) {
      setExportProgress(0, 1, "Partial export timestamps are invalid.");
      setStatus("Partial export timestamps are invalid.");
      return;
    }
  }
  const episode = els.episodeSelect.value;
  const selectedEpisodes = scope === "episode" ? [episode] : Array.from(state.episodes);
  if (!selectedEpisodes.length || !selectedEpisodes[0]) {
    setExportProgress(0, 1, "No episode selected.");
    setStatus("Select an episode before exporting.");
    return;
  }

  const outputDir = els.exportDir.value.trim() || "outputs";
  const entries = localExportEntries(selectedEpisodes);
  if (!entries.length) {
    setExportProgress(0, 1, "No local files found for export.");
    setStatus("No local files found for export.");
    return;
  }

  let uploadedFiles = 0;
  const exportPhaseCount = Math.max(1, selectedEpisodes.length * 5);
  const totalSteps = entries.length + exportPhaseCount;
  setExportProgress(0, totalSteps, "Preparing upload to local server...");
  setStatus("Uploading browser-selected files to the Python server for ffmpeg-capable export...");

  try {
    const upload = await startFolderUploadExport(scope, selectedEpisodes, partial, outputDir);
    for (const [relativePath, file] of entries) {
      await uploadFolderFile(upload.upload_id, relativePath, file);
      uploadedFiles += 1;
      setExportProgress(uploadedFiles, totalSteps, `Uploaded ${relativePath}`);
      await yieldToBrowser();
    }

    setExportProgress(uploadedFiles, totalSteps, "Starting server-side export...");
    const started = await finishFolderUploadExport(upload.upload_id);
    if (!started.job_id) throw new Error("Server did not return an export job id.");
    const job = await pollExportJob(started.job_id, uploadedFiles, uploadedFiles);
    if (job.status === "error") throw new Error(job.error || "Export failed.");
    const result = job.result || {};
    setExportProgress(totalSteps, totalSteps, `Exported ${result.exported_count || 0} episode(s).`);
    setStatus(`Exported ${result.exported_count || 0} episode(s) to ${result.output_path || outputDir}`);
  } catch (error) {
    setExportProgress(uploadedFiles, totalSteps, `Export failed: ${error.message}`);
    setStatus(`Export failed: ${error.message}`);
  }
}

function videoElement(side) {
  return side === "left" ? els.leftVideo : els.rightVideo;
}

function videoStampElement(side) {
  return side === "left" ? els.leftVideoStamp : els.rightVideoStamp;
}

function cleanupVideoObjectUrls() {
  for (const url of state.videoObjectUrls) URL.revokeObjectURL(url);
  state.videoObjectUrls = [];
}

function resetVideos() {
  cleanupVideoObjectUrls();
  for (const side of sides) {
    const video = videoElement(side);
    video.pause();
    video.removeAttribute("src");
    video.load();
    state.videos[side] = {timestamps: [], available: false, source: ""};
    videoStampElement(side).textContent = "-";
  }
}

function setVideoSource(side, url, timestamps, source = "server") {
  const video = videoElement(side);
  video.pause();
  video.src = url;
  video.load();
  state.videos[side] = {
    timestamps: timestamps || [],
    available: Boolean(url && timestamps && timestamps.length),
    source,
  };
}

function handleVideoError(side) {
  const info = state.videos[side];
  videoStampElement(side).textContent = "unplayable";
  if (info.source === "folder") {
    setStatus(
      "Local folder videos use the original file codec. If the browser cannot play them, start this visualizer with a class path so the server can transcode left_wrist_view/right_wrist_view to H.264.",
    );
  } else if (info.available) {
    setStatus(`Failed to load the ${side}_wrist_view browser-compatible video.`);
  }
}

function findNearestTimestamp(timestamps, timestamp) {
  if (!timestamps.length || timestamp == null) return null;
  let lo = 0;
  let hi = timestamps.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (timestamps[mid] <= timestamp) lo = mid;
    else hi = mid - 1;
  }
  if (lo < timestamps.length - 1) {
    const a = timestamps[lo];
    const b = timestamps[lo + 1];
    if (Math.abs(b - timestamp) < Math.abs(timestamp - a)) return b;
  }
  return timestamps[lo];
}

function videoTimeForTimestamp(side, timestamp) {
  const info = state.videos[side];
  if (!info.available || !info.timestamps.length || timestamp == null) return null;
  const first = info.timestamps[0];
  const last = info.timestamps[info.timestamps.length - 1];
  const clamped = Math.max(first, Math.min(timestamp, last));
  return Math.max(0, (clamped - first) / 1000);
}

function syncVideos(force = false) {
  const timestamp = state.rawFrames[state.index]?.timestamp;
  for (const side of sides) {
    const video = videoElement(side);
    const info = state.videos[side];
    if (!info.available) {
      videoStampElement(side).textContent = "missing";
      continue;
    }
    const target = videoTimeForTimestamp(side, timestamp);
    if (target == null) continue;
    if (force || Math.abs(video.currentTime - target) > 0.08) {
      try {
        video.currentTime = target;
      } catch {
        // Metadata may not be ready yet; the next render/tick will retry.
      }
    }
    const matched = findNearestTimestamp(info.timestamps, timestamp);
    videoStampElement(side).textContent = matched == null ? "-" : `${matched} ms`;
  }
}

function playVideos() {
  syncVideos(true);
  for (const side of sides) {
    const video = videoElement(side);
    if (!state.videos[side].available) continue;
    const promise = video.play();
    if (promise && typeof promise.catch === "function") promise.catch(() => {});
  }
}

function pauseVideos() {
  for (const side of sides) videoElement(side).pause();
}

function episodeSort(a, b) {
  const ai = Number((a.match(/^episode_(\d+)/) || [])[1]);
  const bi = Number((b.match(/^episode_(\d+)/) || [])[1]);
  if (Number.isFinite(ai) && Number.isFinite(bi) && ai !== bi) return ai - bi;
  return a.localeCompare(b);
}

function populateEpisodes(selectedEpisode = "") {
  els.episodeSelect.innerHTML = "";
  state.episodes = Array.from(state.episodes).sort(episodeSort);
  for (const episode of state.episodes) {
    const option = document.createElement("option");
    option.value = episode;
    option.textContent = episode;
    els.episodeSelect.appendChild(option);
  }
  syncSourceMeta();
  if (state.episodes.length) {
    const episode = state.episodes.includes(selectedEpisode) ? selectedEpisode : state.episodes[0];
    els.episodeSelect.value = episode;
    loadEpisode(episode);
  } else {
    setStatus("No episode folders with observation.state.eef_pose/data.csv were found.");
  }
}

async function loadServerEpisodes(path = "") {
  const query = path.trim() ? `?path=${encodeURIComponent(path.trim())}` : "";
  const res = await fetch(`/api/episodes${query}`);
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  state.className = data.class_name || "-";
  state.classPath = data.class_path || "";
  state.episodes = data.episodes || [];
  state.source = "server";
  state.exportDirectoryHandle = null;
  els.serverPath.value = state.classPath;
  populateEpisodes(data.selected_episode || "");
}

async function openServerPath() {
  const path = els.serverPath.value.trim();
  if (!path) {
    setStatus("Enter a server-side class path or episode path.");
    return;
  }
  try {
    await loadServerEpisodes(path);
  } catch (error) {
    setStatus(error.message);
  }
}

async function browseServerPath() {
  els.serverBrowse.disabled = true;
  setStatus("Opening source folder picker...");
  try {
    const res = await fetch("/api/select_source_dir", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({current: els.serverPath.value.trim() || "."}),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    if (data.path) {
      els.serverPath.value = data.path;
      setStatus(`Selected source path: ${data.path}`);
      await loadServerEpisodes(data.path);
    } else {
      setStatus("Source folder selection was cancelled.");
    }
  } catch (error) {
    setStatus(`Source folder picker failed: ${error.message}`);
  } finally {
    els.serverBrowse.disabled = false;
  }
}

async function exportData(scope) {
  if (!beginExport(scope)) return;
  try {
  if (state.source === "folder") {
    await exportLocalData(scope);
    return;
  }
  if (state.source !== "server" || !state.classPath) {
    setExportProgress(0, 1, "Server path is required.");
    setStatus("Export needs a server-side class path. Put the real class or episode path in Server class or episode path, click Open, then export.");
    return;
  }
  const episode = els.episodeSelect.value;
  if (scope === "episode" && !episode) {
    setExportProgress(0, 1, "No episode selected.");
    setStatus("Select an episode before exporting.");
    return;
  }
  const outputDir = els.exportDir.value.trim() || "outputs";
  let partial;
  try {
    partial = partialExportPayload(scope);
  } catch (error) {
    setExportProgress(0, 1, error.message);
    setStatus(error.message);
    return;
  }
  setExportProgress(0, 1, `Starting ${scope} export...`);
  setStatus(`Exporting ${scope}...`);
  try {
    const res = await fetch("/api/export", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        async: true,
        scope,
        episode,
        output_dir: outputDir,
        config: readExportConfig(),
        partial,
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    if (data.job_id) {
      const job = await pollExportJob(data.job_id);
      if (job.status === "error") throw new Error(job.error || "Export failed.");
      const result = job.result || {};
      setExportProgress(job.value || job.max || 1, job.max || 1, `Exported ${result.exported_count || 0} episode(s).`);
      setStatus(`Exported ${result.exported_count || 0} episode(s) to ${result.output_path || outputDir}`);
    } else {
      setExportProgress(1, 1, `Exported ${data.exported_count} episode(s).`);
      setStatus(`Exported ${data.exported_count} episode(s) to ${data.output_path}`);
    }
  } catch (error) {
    setExportProgress(0, 1, `Export failed: ${error.message}`);
    setStatus(error.message);
  } finally {
    endExport();
  }
  } finally {
    if (state.exporting) endExport();
  }
}

function readExportConfig() {
  readConfigEditor();
  return clone(state.config);
}

async function chooseExportDirectory() {
  els.chooseExportDir.disabled = true;
  setStatus("Opening output folder picker...");
  try {
    const res = await fetch("/api/select_output_dir", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({current: els.exportDir.value.trim() || "outputs"}),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    if (data.path) {
      state.exportDirectoryHandle = null;
      els.exportDir.value = data.path;
      setStatus(`Selected export folder: ${data.path}`);
    } else {
      setStatus("Output folder selection was cancelled.");
    }
  } catch (error) {
    setStatus(`Output folder picker failed: ${error.message}`);
  } finally {
    els.chooseExportDir.disabled = false;
  }
}

async function loadServerEpisode(episode) {
  const res = await fetch(`/api/episode_csv?episode=${encodeURIComponent(episode)}`);
  if (!res.ok) throw new Error(await res.text());
  return await res.text();
}

async function loadServerAssets(episode) {
  const res = await fetch(`/api/episode_assets?episode=${encodeURIComponent(episode)}`);
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  cleanupVideoObjectUrls();
  for (const side of sides) {
    const asset = data.videos?.[side];
    if (asset?.url && asset?.timestamps?.length) {
      setVideoSource(side, asset.url, asset.timestamps.map(Number), "server");
    } else {
      state.videos[side] = {timestamps: [], available: false, source: "server"};
      videoElement(side).removeAttribute("src");
      videoElement(side).load();
    }
  }
}

async function loadFolderEpisode(episode) {
  const file = state.folderFiles.get(episode);
  if (!file) throw new Error(`Missing local file for ${episode}`);
  return await file.text();
}

async function loadFolderAssets(episode) {
  cleanupVideoObjectUrls();
  for (const side of sides) {
    const stream = VIDEO_STREAMS[side];
    const videoFile = state.folderVideoFiles.get(`${episode}:${stream}:video`);
    const timestampFile = state.folderVideoFiles.get(`${episode}:${stream}:timestamps`);
    if (!videoFile || !timestampFile) {
      state.videos[side] = {timestamps: [], available: false, source: "folder"};
      videoElement(side).removeAttribute("src");
      videoElement(side).load();
      continue;
    }
    const url = URL.createObjectURL(videoFile);
    state.videoObjectUrls.push(url);
    const timestamps = parseTimestampCsv(await timestampFile.text());
    setVideoSource(side, url, timestamps, "folder");
  }
}

async function loadEpisode(episode) {
  pause();
  setStatus(`Loading ${episode}...`);
  resetVideos();
  const text = state.source === "folder" ? await loadFolderEpisode(episode) : await loadServerEpisode(episode);
  if (state.source === "folder") await loadFolderAssets(episode);
  else await loadServerAssets(episode);
  state.rawFrames = rowsToFrames(parseCsv(text));
  state.rawEefFrames = computeEefPoseFrames(state.rawFrames, state.config);
  state.transformedFrames = isTransformedMode()
    ? state.rawFrames.map(frame => addWorldOffsetToFrame(frame, state.transformedWorldOffset))
    : state.rawFrames.map(frame => transformFrame(frame, state.config));
  state.index = 0;
  els.episodeName.textContent = episode;
  els.frameCount.textContent = String(state.rawFrames.length);
  updatePartialFrameControls(true);
  syncProgress();
  render();
  syncVideos(true);
  setStatus(`Loaded ${state.className} / ${episode}.`);
}

function handleFolder(files) {
  const matches = new Map();
  const allMatches = new Map();
  const videoMatches = new Map();
  let browserClassPath = "";
  for (const file of files) {
    const rel = file.webkitRelativePath || file.name;
    const parts = rel.split("/");
    const episodeIndex = parts.findIndex(part => /^episode_\d+/.test(part));
    if (episodeIndex < 0) continue;
    const episode = parts[episodeIndex];
    const innerPath = parts.slice(episodeIndex + 1).join("/");
    const classParts = episodeIndex > 0 ? parts.slice(0, episodeIndex) : [parts[0]];
    if (!browserClassPath) browserClassPath = classParts.join("/");
    if (innerPath) allMatches.set(`${episode}/${innerPath}`, file);
    if (innerPath === `${EEF_POSE_DIR}/data.csv`) {
      matches.set(episode, file);
      continue;
    }
    for (const stream of Object.values(VIDEO_STREAMS)) {
      if (innerPath === `${stream}/video.mp4`) videoMatches.set(`${episode}:${stream}:video`, file);
      if (innerPath === `${stream}/timestamps.csv`) videoMatches.set(`${episode}:${stream}:timestamps`, file);
    }
  }
  const typedServerPath = els.serverPath.value.trim();
  const classPath = typedServerPath || browserClassPath;
  const className = classNameForPath(classPath) || "local";
  state.source = "folder";
  state.className = className;
  state.classPath = classPath || state.className;
  state.folderFiles = matches;
  state.folderAllFiles = allMatches;
  state.folderVideoFiles = videoMatches;
  state.episodes = Array.from(matches.keys()).sort(episodeSort);
  els.serverPath.value = state.classPath;
  els.folderPickerSubtitle.textContent = `${state.classPath} - ${state.episodes.length} episode(s)`;
  populateEpisodes();
  setStatus(
    typedServerPath
      ? "Loaded browser folder selection using the server path field for class naming."
      : "Loaded browser folder selection. Browser security exposes only a relative folder path; use Browse/Open for an absolute server path.",
  );
}

function syncProgress() {
  const max = Math.max(0, state.rawFrames.length - 1);
  for (const progress of [els.rawProgress, els.transformedProgress]) {
    progress.max = String(max);
    progress.value = String(state.index);
  }
  const label = `${state.index + (max > 0 ? 1 : 0)} / ${state.rawFrames.length}`;
  els.rawCounter.textContent = label;
  els.transformedCounter.textContent = label;
  els.frameIndex.textContent = state.rawFrames.length ? String(state.index) : "0";
  const timestamp = state.rawFrames[state.index]?.timestamp;
  const stamp = timestamp == null ? "-" : `${timestamp} ms`;
  els.rawStamp.textContent = stamp;
  els.transformedStamp.textContent = stamp;
  syncVideos(false);
}

function pause() {
  state.playing = false;
  els.rawPlay.textContent = "Play";
  els.transformedPlay.textContent = "Play";
  pauseVideos();
}

function play() {
  if (!state.rawFrames.length) return;
  if (state.index >= state.rawFrames.length - 1) state.index = 0;
  state.playing = true;
  state.playStartedAt = performance.now();
  state.playStartIndex = state.index;
  state.playStartTimestamp = state.rawFrames[state.index].timestamp;
  els.rawPlay.textContent = "Pause";
  els.transformedPlay.textContent = "Pause";
  playVideos();
  requestAnimationFrame(tick);
}

function togglePlay() {
  if (state.playing) pause();
  else play();
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

function setIndex(index) {
  pause();
  state.index = Math.max(0, Math.min(Number(index), state.rawFrames.length - 1));
  syncProgress();
  render();
  syncVideos(true);
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

function rotateForView(p, view) {
  const cy = Math.cos(view.yaw);
  const sy = Math.sin(view.yaw);
  const cp = Math.cos(view.pitch);
  const sp = Math.sin(view.pitch);
  const x1 = cy * p[0] - sy * p[1];
  const y1 = sy * p[0] + cy * p[1];
  const z1 = p[2];
  return [x1, cp * y1 - sp * z1, sp * y1 + cp * z1];
}

function displayPoint(p, viewKey) {
  if (viewKey === "raw" && state.config) {
    return matVec(matTranspose(buildAlignMatrix(state.config)), p);
  }
  return p;
}

function sceneFor(frames, companionFrames = [], viewKey = "") {
  const points = [[0, 0, 0]];
  for (const frame of frames) {
    points.push(frame.left.pos, frame.right.pos);
  }
  for (const frame of companionFrames) {
    points.push(frame.left.pos, frame.right.pos);
  }
  const displayPoints = points.map(p => displayPoint(p, viewKey));
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (const p of displayPoints) {
    for (let i = 0; i < 3; i++) {
      min[i] = Math.min(min[i], p[i]);
      max[i] = Math.max(max[i], p[i]);
    }
  }
  const center = min.map((v, i) => (v + max[i]) / 2);
  const radius = Math.max(...max.map((v, i) => Math.abs(v - min[i])), 0.05);
  return {center, radius};
}

function makeProject(scene, width, height, viewKey) {
  const view = state.views[viewKey];
  const scale = 0.78 * Math.min(width, height) / scene.radius * view.zoom;
  return p => {
    const display = displayPoint(p, viewKey);
    const shifted = [display[0] - scene.center[0], display[1] - scene.center[1], display[2] - scene.center[2]];
    const v = rotateForView(shifted, view);
    return [width / 2 + v[0] * scale + view.panX, height / 2 - v[1] * scale + view.panY];
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

function drawPolyline(ctx, project, frames, side, color, endIndex, alpha, width) {
  if (frames.length < 2) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.globalAlpha = alpha;
  ctx.lineWidth = width;
  ctx.beginPath();
  const first = project(frames[0][side].pos);
  ctx.moveTo(first[0], first[1]);
  const stop = Math.min(endIndex, frames.length - 1);
  for (let i = 1; i <= stop; i++) {
    const p = project(frames[i][side].pos);
    ctx.lineTo(p[0], p[1]);
  }
  ctx.stroke();
  ctx.restore();
}

function drawPoint(ctx, project, p, color, radius = 4) {
  const s = project(p);
  ctx.save();
  ctx.fillStyle = color;
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.arc(s[0], s[1], radius, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

function drawTriad(ctx, project, pose, scene) {
  const rot = rotation6dToMatrix(pose.rot6);
  const length = Math.max(0.018, scene.radius * 0.13);
  for (let col = 0; col < 3; col++) {
    const axis = [rot[0][col], rot[1][col], rot[2][col]];
    const end = [
      pose.pos[0] + axis[0] * length,
      pose.pos[1] + axis[1] * length,
      pose.pos[2] + axis[2] * length,
    ];
    drawLine(ctx, project, pose.pos, end, axisColors[col], 2.4, 0.95);
  }
}

function drawGrid(ctx, project, scene) {
  const span = scene.radius * 0.7;
  const center = scene.center;
  const z = 0;
  ctx.save();
  for (let i = -3; i <= 3; i++) {
    const t = i / 3 * span;
    drawLine(ctx, project, [center[0] - span, center[1] + t, z], [center[0] + span, center[1] + t, z], "#d8dde6", 1, 0.55);
    drawLine(ctx, project, [center[0] + t, center[1] - span, z], [center[0] + t, center[1] + span, z], "#d8dde6", 1, 0.55);
  }
  drawLine(ctx, project, [0, 0, 0], [span * 0.45, 0, 0], axisColors[0], 1.5, 0.7);
  drawLine(ctx, project, [0, 0, 0], [0, span * 0.45, 0], axisColors[1], 1.5, 0.7);
  drawLine(ctx, project, [0, 0, 0], [0, 0, span * 0.45], axisColors[2], 1.5, 0.7);
  ctx.restore();
}

function formatPoseNumber(value, digits = 4) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return n.toFixed(digits);
}

function formatPoseVector(values, digits = 4) {
  return values.map(value => formatPoseNumber(value, digits)).join(", ");
}

function timestampLabel(timestamp) {
  const n = Number(timestamp);
  if (!Number.isFinite(n)) return "-";
  return `${formatPoseNumber(n, 3)} ms`;
}

function addHoverTarget(targets, project, pose, side, kind, color, frameIndex, timestamp) {
  const screen = project(pose.pos);
  targets.push({
    x: screen[0],
    y: screen[1],
    radius: 13,
    side,
    kind,
    color,
    frameIndex,
    timestamp,
    pos: pose.pos,
    rpy: poseRpyDegrees(pose),
  });
}

function nearestHoverTarget(canvas, viewKey, event) {
  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  let best = null;
  let bestDist = Infinity;
  for (const target of state.hoverTargets[viewKey] || []) {
    const dist = Math.hypot(x - target.x, y - target.y);
    if (dist <= target.radius && dist < bestDist) {
      best = target;
      bestDist = dist;
    }
  }
  return best;
}

function showPoseTooltip(target, event) {
  const label = `${target.side} ${target.kind}`;
  els.poseTooltip.innerHTML = `
    <div class="pose-tooltip-title">
      <span><span class="dot" style="background:${target.color}"></span>${label}</span>
      <span>frame ${target.frameIndex}</span>
    </div>
    <div class="pose-tooltip-row"><span>time</span><span>${timestampLabel(target.timestamp)}</span></div>
    <div class="pose-tooltip-row"><span>pos</span><span>${formatPoseVector(target.pos)} m</span></div>
    <div class="pose-tooltip-row"><span>rpy</span><span>${formatPoseVector(target.rpy, 2)} deg</span></div>
  `;
  els.poseTooltip.classList.add("is-visible");
  els.poseTooltip.setAttribute("aria-hidden", "false");
  const margin = 12;
  const gap = 14;
  const rect = els.poseTooltip.getBoundingClientRect();
  let left = event.clientX + gap;
  let top = event.clientY + gap;
  if (left + rect.width + margin > window.innerWidth) left = event.clientX - rect.width - gap;
  if (top + rect.height + margin > window.innerHeight) top = event.clientY - rect.height - gap;
  left = Math.max(margin, Math.min(left, window.innerWidth - rect.width - margin));
  top = Math.max(margin, Math.min(top, window.innerHeight - rect.height - margin));
  els.poseTooltip.style.left = `${left}px`;
  els.poseTooltip.style.top = `${top}px`;
}

function hidePoseTooltip() {
  els.poseTooltip.classList.remove("is-visible");
  els.poseTooltip.setAttribute("aria-hidden", "true");
}

function updatePoseTooltip(canvas, viewKey, event) {
  const view = state.views[viewKey];
  if (view.drag) {
    hidePoseTooltip();
    return;
  }
  const target = nearestHoverTarget(canvas, viewKey, event);
  if (target) {
    showPoseTooltip(target, event);
  } else {
    hidePoseTooltip();
  }
}

function drawViewer(canvas, frames, viewKey, companionFrames = []) {
  const {ctx, width, height} = resizeCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  state.hoverTargets[viewKey] = [];
  if (!frames.length) return;
  const scene = sceneFor(frames, companionFrames, viewKey);
  const project = makeProject(scene, width, height, viewKey);
  drawGrid(ctx, project, scene);
  for (const side of sides) {
    drawPolyline(ctx, project, frames, side, sideColors[side], frames.length - 1, 0.18, 1);
    drawPolyline(ctx, project, frames, side, sideColors[side], state.index, 0.72, 2);
    drawPoint(ctx, project, frames[0][side].pos, sideColors[side], 5);
    if (companionFrames.length) {
      drawPolyline(ctx, project, companionFrames, side, eefColors[side], companionFrames.length - 1, 0.14, 1);
      drawPolyline(ctx, project, companionFrames, side, eefColors[side], state.index, 0.78, 2);
      drawPoint(ctx, project, companionFrames[0][side].pos, eefColors[side], 4);
    }
  }
  const current = frames[state.index];
  for (const side of sides) {
    drawPoint(ctx, project, current[side].pos, sideColors[side], 6);
    drawTriad(ctx, project, current[side], scene);
    const poseKind = viewKey === "raw" ? "tracker" : "eef";
    addHoverTarget(state.hoverTargets[viewKey], project, current[side], side, poseKind, sideColors[side], state.index, current.timestamp);
  }
  if (companionFrames.length) {
    const currentCompanion = companionFrames[state.index];
    for (const side of sides) {
      drawLine(ctx, project, current[side].pos, currentCompanion[side].pos, eefColors[side], 1.3, 0.45);
      drawPoint(ctx, project, currentCompanion[side].pos, eefColors[side], 6);
      drawTriad(ctx, project, currentCompanion[side], scene);
      addHoverTarget(state.hoverTargets[viewKey], project, currentCompanion[side], side, "companion", eefColors[side], state.index, currentCompanion.timestamp);
    }
  }
}

function setupCanvasInteraction(canvas, viewKey) {
  canvas.addEventListener("contextmenu", event => event.preventDefault());
  canvas.addEventListener("pointerdown", event => {
    if (event.button !== 0 && event.button !== 1) return;
    event.preventDefault();
    pause();
    canvas.classList.add("is-dragging");
    canvas.setPointerCapture(event.pointerId);
    state.views[viewKey].drag = {
      mode: event.button === 1 ? "rotate" : "pan",
      x: event.clientX,
      y: event.clientY,
    };
  });
  canvas.addEventListener("pointermove", event => {
    const view = state.views[viewKey];
    if (!view.drag) {
      updatePoseTooltip(canvas, viewKey, event);
      return;
    }
    hidePoseTooltip();
    const dx = event.clientX - view.drag.x;
    const dy = event.clientY - view.drag.y;
    view.drag.x = event.clientX;
    view.drag.y = event.clientY;
    if (view.drag.mode === "pan") {
      view.panX += dx;
      view.panY += dy;
    } else {
      view.yaw += dx * 0.01;
      view.pitch += dy * 0.01;
    }
    render();
  });
  const stopDrag = event => {
    const view = state.views[viewKey];
    if (!view.drag) return;
    view.drag = null;
    canvas.classList.remove("is-dragging");
    if (canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
  };
  canvas.addEventListener("pointerup", stopDrag);
  canvas.addEventListener("pointercancel", stopDrag);
  canvas.addEventListener("pointerleave", event => {
    hidePoseTooltip();
    if (event.buttons === 0) {
      state.views[viewKey].drag = null;
      canvas.classList.remove("is-dragging");
    }
  });
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    hidePoseTooltip();
    const view = state.views[viewKey];
    const factor = Math.exp(-event.deltaY * 0.001);
    view.zoom = Math.max(0.15, Math.min(12, view.zoom * factor));
    render();
  }, {passive: false});
  canvas.addEventListener("dblclick", () => {
    hidePoseTooltip();
    state.views[viewKey] = defaultView();
    render();
  });
}

function render() {
  hidePoseTooltip();
  syncProgress();
  if (!isTransformedMode()) drawViewer(els.rawCanvas, state.rawFrames, "raw", state.rawEefFrames);
  drawViewer(els.transformedCanvas, state.transformedFrames, "transformed");
}

function setMode(mode) {
  if (!["original", "transformed"].includes(mode) || state.mode === mode) return;
  pause();
  state.mode = mode;
  document.body.dataset.mode = mode;
  els.originalTab.classList.toggle("is-active", mode === "original");
  els.transformedTab.classList.toggle("is-active", mode === "transformed");
  els.originalTab.setAttribute("aria-selected", mode === "original" ? "true" : "false");
  els.transformedTab.setAttribute("aria-selected", mode === "transformed" ? "true" : "false");
  recomputeDisplayFrames();
  requestAnimationFrame(render);
}

async function init() {
  state.defaultConfig = await (await fetch("/api/config")).json();
  state.config = clone(state.defaultConfig);
  syncConfigEditor();
  syncTransformedOffsetEditor();
  const configInputs = [
    els.rotationEditor,
    els.offsetX,
    els.offsetY,
    els.offsetZ,
    els.rightOffsetX,
    els.rightOffsetY,
    els.rightOffsetZ,
  ];
  for (const input of configInputs) input.addEventListener("input", recomputeTransformed);
  for (const input of [els.transformedOffsetX, els.transformedOffsetY, els.transformedOffsetZ]) {
    input.addEventListener("input", recomputeDisplayFrames);
  }
  els.resetTransformedOffset.addEventListener("click", () => {
    state.transformedWorldOffset = {x: 0, y: 0, z: 0};
    syncTransformedOffsetEditor();
    recomputeDisplayFrames();
  });
  els.originalTab.addEventListener("click", () => setMode("original"));
  els.transformedTab.addEventListener("click", () => setMode("transformed"));
  els.applyConfig.addEventListener("click", recomputeTransformed);
  els.resetConfig.addEventListener("click", () => {
    state.config = clone(state.defaultConfig);
    syncConfigEditor();
    recomputeTransformed();
  });
  els.episodeSelect.addEventListener("change", event => loadEpisode(event.target.value));
  els.folderPicker.addEventListener("click", () => els.folderInput.click());
  els.folderInput.addEventListener("change", event => handleFolder(Array.from(event.target.files || [])));
  els.serverOpen.addEventListener("click", openServerPath);
  els.serverBrowse.addEventListener("click", browseServerPath);
  els.serverPath.addEventListener("keydown", event => {
    if (event.key === "Enter") openServerPath();
  });
  els.chooseExportDir.addEventListener("click", chooseExportDirectory);
  els.exportEpisode.addEventListener("click", () => exportData("episode"));
  els.exportClass.addEventListener("click", () => exportData("class"));
  els.partialStartFrame.addEventListener("input", () => updatePartialFrameControls(false));
  els.partialEndFrame.addEventListener("input", () => updatePartialFrameControls(false));
  els.rawPlay.addEventListener("click", togglePlay);
  els.transformedPlay.addEventListener("click", togglePlay);
  els.rawProgress.addEventListener("input", event => setIndex(event.target.value));
  els.transformedProgress.addEventListener("input", event => setIndex(event.target.value));
  for (const side of sides) {
    videoElement(side).addEventListener("error", () => handleVideoError(side));
  }
  window.addEventListener("resize", render);
  setupCanvasInteraction(els.rawCanvas, "raw");
  setupCanvasInteraction(els.transformedCanvas, "transformed");
  await loadServerEpisodes();
}

init().catch(error => setStatus(error.message));
</script>
</body>
</html>
"""


def parse_args():
    parser = argparse.ArgumentParser(description="Open a local web visualizer for eef_pose trajectories.")
    parser.add_argument(
        "class_path",
        nargs="?",
        type=Path,
        help="Optional class directory, for example standard/battery. The web UI can also open a folder.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="Transform config JSON. Defaults to standard/ee_trajectory_config.json.",
    )
    parser.add_argument("--no-open", action="store_true", help="Start the server without opening a browser.")
    return parser.parse_args()


def is_episode_dir(path):
    return path.is_dir() and path.name.startswith("episode_")


def episode_sort_key(path):
    if path.name.startswith("episode_"):
        suffix = path.name.removeprefix("episode_")
        if suffix.isdigit():
            return (0, int(suffix), path.name)
    return (1, 0, path.name)


def normalize_class_selection(path):
    if path is None:
        return None, ""
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(
            f"Path does not exist: {resolved}\n"
            "Expected an episode_XXX directory, or a class directory containing episode_XXX subdirectories."
        )
    if is_episode_dir(resolved):
        return resolved.parent, resolved.name
    if resolved.is_dir():
        has_episode_children = any(is_episode_dir(child) for child in resolved.iterdir())
        if has_episode_children:
            return resolved, ""
    raise ValueError(
        f"Invalid input path: {resolved}\n"
        "Expected one of:\n"
        "  - an episode_XXX directory (e.g. .../class_name/episode_0001)\n"
        "  - a class directory containing at least one episode_XXX subdirectory "
        "(e.g. .../class_name)"
    )


def episode_dirs(class_path):
    if class_path is None or not class_path.exists():
        return []
    if is_episode_dir(class_path):
        return [class_path]
    return sorted(
        (path for path in class_path.iterdir() if is_episode_dir(path)),
        key=episode_sort_key,
    )


def class_name_for_path(class_path):
    if class_path is None:
        return "-"
    if is_episode_dir(class_path):
        return class_path.parent.name
    return class_path.name


def csv_rows(csv_path):
    with csv_path.open("r", newline="") as f:
        return list(csv.DictReader(f))


def timestamp_values(timestamp_path):
    with timestamp_path.open("r", newline="") as f:
        return [int(float(row["timestamp_ms"])) for row in csv.DictReader(f)]


def write_csv_rows(csv_path, fieldnames, rows):
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_timestamp_ms(value, path):
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid timestamp_ms in {path}: {value!r}") from exc


def format_timestamp_ms(value):
    rounded = round(value)
    if abs(value - rounded) < 1e-6:
        return str(int(rounded))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def json_timestamp_ms(value):
    rounded = round(value)
    if abs(value - rounded) < 1e-6:
        return int(rounded)
    return float(f"{value:.6f}")


def parse_frame_index(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer frame index.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError(f"{name} must be an integer frame index.")


def transform_pose_csv(csv_path, config):
    if not csv_path.exists():
        return False
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [transform_row(row, config=config) for row in reader]
    write_csv_rows(csv_path, fieldnames, rows)
    return True


def crop_timestamped_csv(csv_path, start_timestamp_ms, end_timestamp_ms, timestamp_offset_ms):
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "timestamp_ms" not in fieldnames:
        raise ValueError(f"Cannot crop {csv_path}: missing timestamp_ms column.")

    kept_rows = []
    for row in rows:
        timestamp_ms = parse_timestamp_ms(row.get("timestamp_ms"), csv_path)
        if start_timestamp_ms <= timestamp_ms <= end_timestamp_ms:
            out = dict(row)
            out["timestamp_ms"] = format_timestamp_ms(timestamp_ms - timestamp_offset_ms)
            kept_rows.append(out)
    write_csv_rows(csv_path, fieldnames, kept_rows)
    return {"path": str(csv_path), "input_rows": len(rows), "output_rows": len(kept_rows)}


def estimate_frame_delta_ms(timestamps):
    deltas = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
    if not deltas:
        return 33.333333
    deltas = sorted(deltas)
    return deltas[len(deltas) // 2]


def wrist_video_paths(episode_dir):
    return [
        episode_dir / stream_dir / "video.mp4"
        for stream_dir in VIDEO_STREAMS.values()
        if (episode_dir / stream_dir / "video.mp4").exists()
    ]


def is_wrist_video_path(video_path):
    return video_path.parent.name in set(VIDEO_STREAMS.values())


def run_video_transcode(video_path, temp_path, clip_start_sec=None, clip_end_sec=None, flip=False):
    command = ["ffmpeg", "-y", "-loglevel", "error"]
    if clip_start_sec is not None:
        command.extend(["-ss", f"{clip_start_sec:.6f}"])
    if clip_end_sec is not None:
        command.extend(["-to", f"{clip_end_sec:.6f}"])
    command.extend(["-i", str(video_path), "-an"])
    if flip:
        command.extend(["-vf", "hflip,vflip"])
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temp_path),
        ]
    )
    subprocess.run(command, check=True)
    temp_path.replace(video_path)


def flip_video_in_place(video_path):
    temp_path = video_path.with_name(f"{video_path.stem}.flip.tmp{video_path.suffix}")
    if temp_path.exists():
        temp_path.unlink()
    try:
        run_video_transcode(video_path, temp_path, flip=True)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return str(video_path)


def flip_wrist_videos(episode_dir):
    flipped = []
    for video_path in wrist_video_paths(episode_dir):
        flipped.append(flip_video_in_place(video_path))
    return flipped


def crop_video_with_timestamps(video_path, start_timestamp_ms, end_timestamp_ms, timestamp_offset_ms, flip=False):
    timestamp_path = video_path.parent / "timestamps.csv"
    if not timestamp_path.exists():
        raise ValueError(f"Cannot crop {video_path}: missing timestamps.csv.")
    with timestamp_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "timestamp_ms" not in fieldnames:
        raise ValueError(f"Cannot crop {timestamp_path}: missing timestamp_ms column.")
    timestamps = [parse_timestamp_ms(row.get("timestamp_ms"), timestamp_path) for row in rows]
    kept_indices = [
        index
        for index, timestamp_ms in enumerate(timestamps)
        if start_timestamp_ms <= timestamp_ms <= end_timestamp_ms
    ]
    if not kept_indices:
        raise ValueError(f"Cannot crop {video_path}: no video timestamps overlap the requested frame range.")

    first_video_timestamp = timestamps[0]
    first_index = kept_indices[0]
    last_index = kept_indices[-1]
    clip_start_sec = max(0.0, (start_timestamp_ms - first_video_timestamp) / 1000.0)
    if last_index + 1 < len(timestamps):
        clip_end_timestamp = timestamps[last_index + 1]
    else:
        clip_end_timestamp = timestamps[last_index] + estimate_frame_delta_ms(timestamps)
    clip_end_sec = max(clip_start_sec + 0.001, (clip_end_timestamp - first_video_timestamp) / 1000.0)

    output_rows = []
    for index in kept_indices:
        out = dict(rows[index])
        out["timestamp_ms"] = format_timestamp_ms(timestamps[index] - timestamp_offset_ms)
        output_rows.append(out)
    write_csv_rows(timestamp_path, fieldnames, output_rows)

    temp_path = video_path.with_name(f"{video_path.stem}.crop.tmp{video_path.suffix}")
    if temp_path.exists():
        temp_path.unlink()
    try:
        run_video_transcode(video_path, temp_path, clip_start_sec=clip_start_sec, clip_end_sec=clip_end_sec, flip=flip)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return {
        "path": str(video_path),
        "input_frames": len(rows),
        "output_frames": len(output_rows),
        "clip_start_sec": clip_start_sec,
        "clip_end_sec": clip_end_sec,
        "flipped": flip,
    }


def crop_exported_episode(episode_dir, crop_request):
    primary_csv = episode_dir / EEF_POSE_DIR / "data.csv"
    if not primary_csv.exists():
        raise ValueError(f"Cannot crop episode: missing {EEF_POSE_DIR}/data.csv.")
    primary_rows = csv_rows(primary_csv)
    if not primary_rows:
        raise ValueError(f"Cannot crop episode: {EEF_POSE_DIR}/data.csv has no rows.")

    start_frame = parse_frame_index(crop_request.get("start_frame", 0), "start_frame")
    end_frame = parse_frame_index(crop_request.get("end_frame", len(primary_rows) - 1), "end_frame")
    max_frame = len(primary_rows) - 1
    if start_frame < 0 or end_frame < 0 or start_frame > max_frame or end_frame > max_frame or start_frame > end_frame:
        raise ValueError(f"Crop frame range must be between 0 and {max_frame}, with start <= end.")

    start_timestamp_ms = parse_timestamp_ms(primary_rows[start_frame].get("timestamp_ms"), primary_csv)
    end_timestamp_ms = parse_timestamp_ms(primary_rows[end_frame].get("timestamp_ms"), primary_csv)
    if start_timestamp_ms > end_timestamp_ms:
        raise ValueError("Crop frame timestamps are not monotonic: start timestamp is after end timestamp.")

    csv_results = []
    for data_csv in sorted(episode_dir.rglob("data.csv")):
        csv_results.append(crop_timestamped_csv(data_csv, start_timestamp_ms, end_timestamp_ms, start_timestamp_ms))

    video_results = []
    for video_path in sorted(episode_dir.rglob("video.mp4")):
        video_results.append(
            crop_video_with_timestamps(
                video_path,
                start_timestamp_ms,
                end_timestamp_ms,
                start_timestamp_ms,
                flip=is_wrist_video_path(video_path),
            )
        )

    return {
        "enabled": True,
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frame_count": end_frame - start_frame + 1,
        "start_timestamp_ms": json_timestamp_ms(start_timestamp_ms),
        "end_timestamp_ms": json_timestamp_ms(end_timestamp_ms),
        "timestamp_offset_ms": json_timestamp_ms(start_timestamp_ms),
        "csv_files": len(csv_results),
        "video_files": len(video_results),
        "flipped_video_files": sum(1 for result in video_results if result.get("flipped")),
    }


def update_export_metadata(episode_dir, crop_info=None):
    metadata_path = episode_dir / "metadata.json"
    if not metadata_path.exists():
        return False
    with metadata_path.open("r", encoding="utf-8") as f:
        metadata = json.load(f)
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["umi_transform_version"] = EXPORT_TRANSFORM_VERSION
    metadata["umi_transform_tag"] = EXPORT_TRANSFORM_TAG
    metadata["umi_transform_cropped"] = bool(crop_info)
    if crop_info:
        metadata["umi_transform_crop"] = crop_info
    with metadata_path.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return True


def recompute_checksum_manifest(episode_dir):
    manifest_path = episode_dir / "checksums.sha256"
    if not manifest_path.exists():
        return False
    entries = []
    for path in sorted(episode_dir.rglob("*")):
        if not path.is_file() or path == manifest_path:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append(f"{digest.hexdigest()}  {path.relative_to(episode_dir).as_posix()}")
    manifest_path.write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")
    return True


def resolve_output_root(output_dir):
    raw = (output_dir or "outputs").strip() or "outputs"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def choose_directory(initial_dir):
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError(f"tkinter directory picker is unavailable: {exc}") from exc

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            title="Select export folder",
            initialdir=str(resolve_output_root(initial_dir)),
            mustexist=True,
        )
    finally:
        root.destroy()
    return selected


def export_episode_dir(source_episode_dir, destination_episode_dir, config, crop_request=None, progress=None):
    def notify(message, advance=False):
        if progress is not None:
            progress(message, advance=advance)

    source_episode_dir = Path(source_episode_dir).resolve()
    destination_episode_dir = Path(destination_episode_dir).resolve()
    if source_episode_dir == destination_episode_dir:
        raise ValueError("Export destination cannot be the source episode directory.")
    if destination_episode_dir.exists():
        notify(f"Removing existing {destination_episode_dir.name}...")
        shutil.rmtree(destination_episode_dir)
    destination_episode_dir.parent.mkdir(parents=True, exist_ok=True)
    notify(f"Copying {source_episode_dir.name}...")
    shutil.copytree(source_episode_dir, destination_episode_dir)
    notify("Copied source files.", advance=True)

    crop_info = None
    if crop_request and crop_request.get("enabled"):
        notify("Cropping CSV/video files...")
        crop_info = crop_exported_episode(destination_episode_dir, crop_request)
        flipped_videos = []
        notify("Cropped export window.", advance=True)
    else:
        notify("Flipping wrist-view videos...")
        flipped_videos = [
            str(Path(path).relative_to(destination_episode_dir))
            for path in flip_wrist_videos(destination_episode_dir)
        ]
        notify(f"Flipped {len(flipped_videos)} wrist-view video(s).", advance=True)

    notify("Transforming pose CSV files...")
    transformed_files = []
    for rel_dir in (EEF_POSE_DIR, ACTION_EEF_POSE_DIR):
        csv_path = destination_episode_dir / rel_dir / "data.csv"
        if transform_pose_csv(csv_path, config):
            transformed_files.append(str(csv_path.relative_to(destination_episode_dir)))
    notify(f"Transformed {len(transformed_files)} pose CSV file(s).", advance=True)

    notify("Updating metadata...")
    metadata_updated = update_export_metadata(destination_episode_dir, crop_info)
    notify("Updated metadata.", advance=True)
    notify("Recomputing checksums...")
    checksum_updated = recompute_checksum_manifest(destination_episode_dir)
    notify("Recomputed checksums.", advance=True)
    return {
        "source": str(source_episode_dir),
        "destination": str(destination_episode_dir),
        "transformed_files": transformed_files,
        "crop": crop_info,
        "flipped_videos": flipped_videos,
        "metadata_updated": metadata_updated,
        "checksum_updated": checksum_updated,
    }


def export_selected_episodes(selected, destination_class_dir, class_name, config, crop_request=None, progress=None):
    total = max(1, len(selected) * EXPORT_PHASES_PER_EPISODE)
    completed = 0

    def report(value, message):
        if progress is not None:
            progress(value=value, max_value=total, message=message)

    results = []
    report(0, f"Preparing export of {len(selected)} episode(s)...")
    for index, source_episode in enumerate(selected, start=1):
        def episode_progress(message, advance=False, episode_name=source_episode.name, episode_index=index):
            nonlocal completed
            if advance:
                completed = min(total, completed + 1)
            report(completed, f"{episode_name} ({episode_index}/{len(selected)}): {message}")

        results.append(
            export_episode_dir(
                source_episode,
                destination_class_dir / source_episode.name,
                config,
                crop_request=crop_request if len(selected) == 1 else None,
                progress=episode_progress,
            )
        )
    report(total, f"Exported {len(results)} episode(s).")
    return {
        "ok": True,
        "class_name": class_name,
        "output_path": str(destination_class_dir),
        "exported_count": len(results),
        "episodes": results,
    }


def sanitize_class_name(name):
    clean = str(name or "local").strip().replace("\\", "_").replace("/", "_")
    return clean or "local"


def safe_upload_relative_path(raw_path):
    path = Path(str(raw_path or ""))
    if path.is_absolute():
        raise ValueError("Upload path must be relative.")
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe upload path: {raw_path}")
    if not parts[0].startswith("episode_"):
        raise ValueError(f"Upload path must start with an episode folder: {raw_path}")
    return Path(*parts)


def browser_video_path(source_path):
    source_path = Path(source_path).resolve()
    stat = source_path.stat()
    key = hashlib.sha1(f"{source_path}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")).hexdigest()
    cache_dir = Path("/tmp/ee_trajectory_visualizer_video_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path = cache_dir / f"{key}.mp4"
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    with VIDEO_TRANSCODE_LOCK:
        if output_path.exists() and output_path.stat().st_size > 0:
            return output_path
        temp_path = output_path.with_suffix(".tmp.mp4")
        if temp_path.exists():
            temp_path.unlink()
        command = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source_path),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temp_path),
        ]
        subprocess.run(command, check=True)
        temp_path.replace(output_path)
    return output_path


def make_handler(class_path, config_path):
    try:
        initial_class_path, initial_episode = normalize_class_selection(class_path)
    except (FileNotFoundError, ValueError):
        initial_class_path, initial_episode = None, ""
    resolved_config_path = config_path.resolve()
    server_state = {
        "class_path": initial_class_path,
        "selected_episode": initial_episode,
    }
    export_jobs = {}
    export_jobs_lock = threading.Lock()
    upload_jobs = {}
    upload_jobs_lock = threading.Lock()

    def set_export_job(job_id, **updates):
        with export_jobs_lock:
            job = export_jobs.setdefault(
                job_id,
                {
                    "id": job_id,
                    "status": "queued",
                    "value": 0,
                    "max": 1,
                    "message": "Queued export.",
                    "result": None,
                    "error": "",
                },
            )
            job.update(updates)
            return dict(job)

    def get_export_job(job_id):
        with export_jobs_lock:
            job = export_jobs.get(job_id)
            return dict(job) if job else None

    def set_upload_job(upload_id, **updates):
        with upload_jobs_lock:
            job = upload_jobs.setdefault(upload_id, {"id": upload_id})
            job.update(updates)
            return dict(job)

    def get_upload_job(upload_id):
        with upload_jobs_lock:
            job = upload_jobs.get(upload_id)
            return dict(job) if job else None

    def pop_upload_job(upload_id):
        with upload_jobs_lock:
            return upload_jobs.pop(upload_id, None)

    def run_export_job(job_id, scope, selected, destination_class_dir, class_name, config, crop_request):
        def progress(value, max_value, message):
            set_export_job(
                job_id,
                status="running",
                value=value,
                max=max_value,
                message=message,
            )

        try:
            set_export_job(job_id, status="running", value=0, max=max(1, len(selected) * EXPORT_PHASES_PER_EPISODE), message="Starting export...")
            result = export_selected_episodes(
                selected,
                destination_class_dir,
                class_name,
                config,
                crop_request=crop_request,
                progress=progress,
            )
            result["scope"] = scope
            set_export_job(
                job_id,
                status="done",
                value=max(1, len(selected) * EXPORT_PHASES_PER_EPISODE),
                max=max(1, len(selected) * EXPORT_PHASES_PER_EPISODE),
                message=f"Exported {result['exported_count']} episode(s).",
                result=result,
            )
        except (OSError, ValueError, csv.Error, KeyError, subprocess.CalledProcessError) as exc:
            set_export_job(job_id, status="error", message=f"Export failed: {exc}", error=str(exc))
        except Exception as exc:
            set_export_job(job_id, status="error", message=f"Export failed: {exc}", error=str(exc))

    def run_uploaded_export_job(job_id, upload_job, selected, destination_class_dir, class_name, config, crop_request):
        try:
            run_export_job(job_id, upload_job["scope"], selected, destination_class_dir, class_name, config, crop_request)
        finally:
            shutil.rmtree(upload_job["temp_dir"], ignore_errors=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def send_json(self, payload, status=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_text(self, body, content_type="text/plain; charset=utf-8", status=200):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_file(self, path, content_type="application/octet-stream"):
            size = path.stat().st_size
            range_header = self.headers.get("Range")
            start = 0
            end = size - 1
            status = 200
            if range_header:
                try:
                    unit, values = range_header.split("=", 1)
                    if unit.strip() == "bytes":
                        first, _, last = values.partition("-")
                        if first:
                            start = int(first)
                        if last:
                            end = int(last)
                        status = 206
                except ValueError:
                    start = 0
                    end = size - 1
                    status = 200
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            length = end - start + 1

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with path.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)

        def read_json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send_text(HTML, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/config":
                self.send_json(load_config(resolved_config_path))
                return
            if parsed.path == "/api/export_progress":
                query = parse_qs(parsed.query)
                job_id = query.get("id", [""])[0]
                job = get_export_job(job_id)
                if job is None:
                    self.send_text(f"Unknown export job: {job_id}", status=404)
                    return
                self.send_json(job)
                return
            if parsed.path == "/api/episodes":
                query = parse_qs(parsed.query)
                requested_path = query.get("path", [""])[0].strip()
                if requested_path:
                    try:
                        class_path_value, selected_episode = normalize_class_selection(Path(requested_path))
                    except (FileNotFoundError, OSError) as exc:
                        self.send_text(str(exc), status=404)
                        return
                    server_state["class_path"] = class_path_value
                    server_state["selected_episode"] = selected_episode
                resolved_class_path = server_state["class_path"]
                episodes = []
                for episode_dir in episode_dirs(resolved_class_path):
                    data_csv = episode_dir / EEF_POSE_DIR / "data.csv"
                    if data_csv.exists():
                        episodes.append(episode_dir.name)
                selected_episode = server_state["selected_episode"]
                if selected_episode not in episodes:
                    selected_episode = ""
                self.send_json(
                    {
                        "class_name": class_name_for_path(resolved_class_path),
                        "class_path": str(resolved_class_path) if resolved_class_path is not None else "",
                        "episodes": episodes,
                        "selected_episode": selected_episode,
                    }
                )
                return
            if parsed.path == "/api/episode_csv":
                query = parse_qs(parsed.query)
                episode = query.get("episode", [""])[0]
                resolved_class_path = server_state["class_path"]
                if resolved_class_path is None:
                    self.send_text("No server class path was provided.", status=404)
                    return
                valid = {path.name: path for path in episode_dirs(resolved_class_path)}
                if episode not in valid:
                    self.send_text(f"Unknown episode: {episode}", status=404)
                    return
                data_csv = valid[episode] / EEF_POSE_DIR / "data.csv"
                if not data_csv.exists():
                    self.send_text(f"Missing {EEF_POSE_DIR}/data.csv for {episode}", status=404)
                    return
                self.send_text(data_csv.read_text(encoding="utf-8"), "text/csv; charset=utf-8")
                return
            if parsed.path == "/api/episode_assets":
                query = parse_qs(parsed.query)
                episode = query.get("episode", [""])[0]
                resolved_class_path = server_state["class_path"]
                if resolved_class_path is None:
                    self.send_text("No server class path was provided.", status=404)
                    return
                valid = {path.name: path for path in episode_dirs(resolved_class_path)}
                if episode not in valid:
                    self.send_text(f"Unknown episode: {episode}", status=404)
                    return
                videos = {}
                for side, stream in VIDEO_STREAMS.items():
                    stream_dir = valid[episode] / stream
                    video_path = stream_dir / "video.mp4"
                    timestamp_path = stream_dir / "timestamps.csv"
                    if video_path.exists() and timestamp_path.exists():
                        videos[side] = {
                            "url": f"/api/video?episode={episode}&side={side}",
                            "timestamps": timestamp_values(timestamp_path),
                        }
                    else:
                        videos[side] = {"url": "", "timestamps": []}
                self.send_json({"videos": videos})
                return
            if parsed.path == "/api/video":
                query = parse_qs(parsed.query)
                episode = query.get("episode", [""])[0]
                side = query.get("side", [""])[0]
                resolved_class_path = server_state["class_path"]
                if resolved_class_path is None:
                    self.send_text("No server class path was provided.", status=404)
                    return
                if side not in VIDEO_STREAMS:
                    self.send_text(f"Unknown video side: {side}", status=404)
                    return
                valid = {path.name: path for path in episode_dirs(resolved_class_path)}
                if episode not in valid:
                    self.send_text(f"Unknown episode: {episode}", status=404)
                    return
                video_path = valid[episode] / VIDEO_STREAMS[side] / "video.mp4"
                if not video_path.exists():
                    self.send_text(f"Missing video for {episode} {side}", status=404)
                    return
                try:
                    compatible_path = browser_video_path(video_path)
                except (OSError, subprocess.CalledProcessError) as exc:
                    self.send_text(f"Failed to transcode video for browser playback: {exc}", status=500)
                    return
                self.send_file(compatible_path, "video/mp4")
                return
            self.send_text("Not found", status=404)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/upload_export/start":
                try:
                    payload = self.read_json_body()
                    scope = str(payload.get("scope") or "episode")
                    if scope not in {"episode", "class"}:
                        raise ValueError(f"Unsupported export scope: {scope}")
                    selected_episodes = payload.get("selected_episodes")
                    if not isinstance(selected_episodes, list) or not selected_episodes:
                        raise ValueError("selected_episodes must be a non-empty list.")
                    selected_episodes = [str(name) for name in selected_episodes if str(name).startswith("episode_")]
                    if not selected_episodes:
                        raise ValueError("No valid episode names were provided.")
                    crop_request = payload.get("partial") if isinstance(payload.get("partial"), dict) else {"enabled": False}
                    if crop_request.get("enabled") and scope != "episode":
                        raise ValueError("Partial export is only supported for a single episode.")
                    config = payload.get("config")
                    if not isinstance(config, dict):
                        config = load_config(resolved_config_path)
                    upload_id = uuid.uuid4().hex
                    temp_dir = Path(tempfile.mkdtemp(prefix="ee_visualizer_upload_"))
                    set_upload_job(
                        upload_id,
                        status="uploading",
                        temp_dir=temp_dir,
                        scope=scope,
                        class_name=sanitize_class_name(payload.get("class_name") or "local"),
                        selected_episodes=selected_episodes,
                        output_dir=str(payload.get("output_dir") or "outputs"),
                        config=config,
                        crop_request=crop_request,
                    )
                    self.send_json({"ok": True, "upload_id": upload_id})
                except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self.send_text(f"Upload export start failed: {exc}", status=400)
                return

            if parsed.path == "/api/upload_export/file":
                query = parse_qs(parsed.query)
                upload_id = query.get("id", [""])[0]
                relative_path = query.get("path", [""])[0]
                upload_job = get_upload_job(upload_id)
                if upload_job is None:
                    self.send_text(f"Unknown upload job: {upload_id}", status=404)
                    return
                try:
                    safe_path = safe_upload_relative_path(relative_path)
                except ValueError as exc:
                    self.send_text(str(exc), status=400)
                    return
                destination = Path(upload_job["temp_dir"]) / safe_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                remaining = int(self.headers.get("Content-Length") or 0)
                try:
                    with destination.open("wb") as f:
                        while remaining > 0:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            f.write(chunk)
                            remaining -= len(chunk)
                except OSError as exc:
                    self.send_text(f"Upload file failed: {exc}", status=500)
                    return
                self.send_json({"ok": True, "path": safe_path.as_posix()})
                return

            if parsed.path == "/api/upload_export/finish":
                try:
                    payload = self.read_json_body()
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self.send_text(f"Invalid JSON body: {exc}", status=400)
                    return
                upload_id = str(payload.get("id") or "")
                upload_job = pop_upload_job(upload_id)
                if upload_job is None:
                    self.send_text(f"Unknown upload job: {upload_id}", status=404)
                    return
                temp_dir = Path(upload_job["temp_dir"])
                selected = [temp_dir / name for name in upload_job["selected_episodes"]]
                missing = [path.name for path in selected if not path.exists()]
                if missing:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    self.send_text(f"Uploaded episode folder(s) are missing: {', '.join(missing)}", status=400)
                    return
                output_root = resolve_output_root(upload_job["output_dir"])
                destination_class_dir = output_root / upload_job["class_name"]
                job_id = uuid.uuid4().hex
                set_export_job(
                    job_id,
                    status="queued",
                    value=0,
                    max=max(1, len(selected) * EXPORT_PHASES_PER_EPISODE),
                    message="Queued uploaded export.",
                )
                thread = threading.Thread(
                    target=run_uploaded_export_job,
                    args=(
                        job_id,
                        upload_job,
                        selected,
                        destination_class_dir,
                        upload_job["class_name"],
                        upload_job["config"],
                        upload_job["crop_request"] if upload_job["scope"] == "episode" else None,
                    ),
                    daemon=True,
                )
                thread.start()
                self.send_json({"ok": True, "job_id": job_id})
                return

            if parsed.path == "/api/select_source_dir":
                try:
                    payload = self.read_json_body()
                    selected = choose_directory(str(payload.get("current") or "."))
                except (OSError, RuntimeError, ValueError) as exc:
                    self.send_text(f"Folder picker failed: {exc}", status=500)
                    return
                self.send_json({"path": selected})
                return

            if parsed.path == "/api/select_output_dir":
                try:
                    payload = self.read_json_body()
                    selected = choose_directory(str(payload.get("current") or "outputs"))
                except (OSError, RuntimeError, ValueError) as exc:
                    self.send_text(f"Folder picker failed: {exc}", status=500)
                    return
                self.send_json({"path": selected})
                return

            if parsed.path != "/api/export":
                self.send_text("Not found", status=404)
                return

            resolved_class_path = server_state["class_path"]
            if resolved_class_path is None:
                self.send_text("No server class path was provided.", status=404)
                return

            try:
                payload = self.read_json_body()
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self.send_text(f"Invalid JSON body: {exc}", status=400)
                return

            scope = str(payload.get("scope") or "episode")
            if scope not in {"episode", "class"}:
                self.send_text(f"Unsupported export scope: {scope}", status=400)
                return
            crop_request = payload.get("partial") if isinstance(payload.get("partial"), dict) else {"enabled": False}
            if crop_request.get("enabled") and scope != "episode":
                self.send_text("Partial export is only supported for a single episode.", status=400)
                return

            valid = {path.name: path for path in episode_dirs(resolved_class_path)}
            if scope == "episode":
                episode = str(payload.get("episode") or server_state.get("selected_episode") or "")
                if episode not in valid:
                    self.send_text(f"Unknown episode: {episode}", status=404)
                    return
                selected = [valid[episode]]
            else:
                selected = [path for path in episode_dirs(resolved_class_path) if (path / EEF_POSE_DIR / "data.csv").exists()]
                if not selected:
                    self.send_text("No exportable episodes were found for this class.", status=404)
                    return

            output_root = resolve_output_root(str(payload.get("output_dir") or "outputs"))
            destination_class_dir = output_root / class_name_for_path(resolved_class_path)
            config = payload.get("config")
            if not isinstance(config, dict):
                config = load_config(resolved_config_path)

            if bool(payload.get("async")):
                job_id = uuid.uuid4().hex
                set_export_job(job_id, status="queued", value=0, max=max(1, len(selected) * EXPORT_PHASES_PER_EPISODE), message="Queued export.")
                thread = threading.Thread(
                    target=run_export_job,
                    args=(job_id, scope, selected, destination_class_dir, class_name_for_path(resolved_class_path), config, crop_request if scope == "episode" else None),
                    daemon=True,
                )
                thread.start()
                self.send_json({"ok": True, "job_id": job_id})
                return

            try:
                result = export_selected_episodes(
                    selected,
                    destination_class_dir,
                    class_name_for_path(resolved_class_path),
                    config,
                    crop_request=crop_request if scope == "episode" else None,
                )
                result["scope"] = scope
            except (OSError, ValueError, csv.Error, KeyError, subprocess.CalledProcessError) as exc:
                self.send_text(f"Export failed: {exc}", status=500)
                return

            self.send_json(result)

    return Handler


def port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def choose_port(host, start_port):
    for port in range(start_port, start_port + 50):
        if port_available(host, port):
            return port
    raise RuntimeError(f"No free port found from {start_port} to {start_port + 49}")


def main():
    args = parse_args()
    load_config(args.config)
    if args.class_path is not None:
        normalized_class_path, selected_episode = normalize_class_selection(args.class_path)
    else:
        normalized_class_path, selected_episode = None, ""
    port = choose_port(args.host, args.port)
    server = ThreadingHTTPServer((args.host, port), make_handler(args.class_path, args.config))
    url = f"http://{args.host}:{port}/"
    print(f"Serving EEF trajectory visualizer at {url}")
    print(f"Config path: {args.config.resolve()}")
    if normalized_class_path is not None:
        print(f"Class path: {normalized_class_path}")
        if selected_episode:
            print(f"Default episode: {selected_episode}")
    else:
        print("No class path supplied. Use the folder picker in the web page.")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
