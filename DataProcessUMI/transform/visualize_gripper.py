#!/usr/bin/env python3
"""Visualize original vs repaired gripper width with wrist-view video.

The viewer serves one episode or a class directory of ``episode_*`` folders. It
plays the selected wrist-view video and shows two front-end metric cards for the
current timestamp: the original gripper width from
``observation.state.gripper_original/data.csv`` and the repaired width from
``observation.state.gripper/data.csv``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import socket
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_ROOT = Path("/home/ubuntu/data/curated_samples/success")

SIDES = ("left", "right")
VIDEO_STREAMS = {
    "left": "observation.image.left_wrist_view",
    "right": "observation.image.right_wrist_view",
}
ORIGINAL_GRIPPER_DIR = "observation.state.gripper_original"
REPAIRED_GRIPPER_DIR = "observation.state.gripper"
VIDEO_TRANSCODE_LOCK = threading.Lock()


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gripper Recovery Viewer</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --line: #d7dee8;
      --text: #172033;
      --muted: #647084;
      --accent: #0f766e;
      --raw: #2563eb;
      --fixed: #0f766e;
      --warn: #b45309;
      --bad: #b91c1c;
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
      gap: 14px;
      padding: 13px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      letter-spacing: 0;
      white-space: nowrap;
    }
    .status-pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 10px;
      color: var(--muted);
      background: #fbfcfe;
      font-size: 12px;
      max-width: 52vw;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    main {
      display: grid;
      grid-template-columns: minmax(260px, 330px) minmax(0, 1fr);
      gap: 14px;
      padding: 14px;
    }
    aside, .stage, .metric-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    aside {
      padding: 14px;
      height: calc(100vh - 84px);
      overflow: auto;
    }
    .path-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 8px;
      align-items: center;
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
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .meta strong { color: var(--text); font-weight: 620; }
    .stage {
      min-width: 0;
      overflow: hidden;
    }
    .video-wrap {
      background: #101318;
      display: grid;
      place-items: center;
      min-height: 420px;
      height: min(68vh, 760px);
    }
    video {
      display: block;
      max-width: 100%;
      max-height: 100%;
      width: 100%;
      height: 100%;
      object-fit: contain;
      background: #101318;
    }
    .cards {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      padding: 12px;
      border-top: 1px solid var(--line);
    }
    .metric-card {
      padding: 12px;
      min-height: 104px;
    }
    .metric-title {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 10px;
    }
    .metric-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      display: inline-block;
      margin-right: 6px;
      vertical-align: -1px;
    }
    .metric-value {
      font-size: 34px;
      line-height: 1;
      font-weight: 720;
      letter-spacing: 0;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }
    .metric-unit {
      color: var(--muted);
      font-size: 13px;
      margin-left: 4px;
      font-weight: 520;
    }
    .metric-sub {
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .controls {
      display: grid;
      grid-template-columns: 80px minmax(0, 1fr) 88px;
      gap: 10px;
      align-items: center;
      padding: 11px 12px 12px;
      border-top: 1px solid var(--line);
    }
    input[type="range"] { width: 100%; }
    .detail {
      padding: 0 12px 12px;
      color: var(--muted);
      font-size: 12px;
      display: grid;
      gap: 4px;
      overflow-wrap: anywhere;
    }
    .ok { color: var(--accent); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      aside { height: auto; }
      .video-wrap { height: 52vh; min-height: 320px; }
      .cards { grid-template-columns: 1fr; }
      .metric-value { font-size: 30px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Gripper Recovery Viewer</h1>
    <div id="headerStatus" class="status-pill">loading</div>
  </header>
  <main>
    <aside>
      <div class="field">
        <label>Episode/Class path</label>
        <div class="path-row">
          <input id="pathInput" type="text" placeholder="/home/ubuntu/data/.../episode_0001">
          <button id="browsePathButton" class="secondary">Browse</button>
          <button id="loadPathButton">Open</button>
        </div>
      </div>
      <div class="field">
        <label>Episode</label>
        <select id="episodeSelect"></select>
      </div>
      <div class="field">
        <label>Wrist view</label>
        <select id="sideSelect"></select>
      </div>
      <div class="field" style="display:flex; gap:8px;">
        <button id="reloadButton">Reload</button>
        <button id="playButton" class="secondary">Play</button>
      </div>
      <div class="field meta">
        <div>Source: <strong id="sourcePath">-</strong></div>
        <div>Timestamp: <strong id="timestampLabel">-</strong></div>
        <div>Frame index: <strong id="frameLabel">-</strong></div>
      </div>
    </aside>
    <section class="stage">
      <div class="video-wrap">
        <video id="video" muted playsinline preload="metadata" controls></video>
      </div>
      <div class="cards">
        <div class="metric-card">
          <div class="metric-title"><span><span class="metric-dot" style="background:var(--raw)"></span>Original gripper width</span><span id="originalState">-</span></div>
          <div><span id="originalValue" class="metric-value">-</span><span class="metric-unit">m</span></div>
          <div id="originalSub" class="metric-sub">from observation.state.gripper_original</div>
        </div>
        <div class="metric-card">
          <div class="metric-title"><span><span class="metric-dot" style="background:var(--fixed)"></span>Repaired gripper width</span><span id="recoveredState">-</span></div>
          <div><span id="recoveredValue" class="metric-value">-</span><span class="metric-unit">m</span></div>
          <div id="recoveredSub" class="metric-sub">from observation.state.gripper</div>
        </div>
      </div>
      <div class="controls">
        <button id="stepBack" class="secondary">-1 frame</button>
        <input id="timeline" type="range" min="0" max="0" value="0" step="1">
        <button id="stepForward" class="secondary">+1 frame</button>
      </div>
      <div class="detail">
        <div>Original source: <strong id="originalSource">-</strong></div>
        <div>Repaired source: <strong id="repairedSource">-</strong></div>
        <div>Loaded rows: <strong id="loadedRows">-</strong> · current delta: <strong id="currentDelta">-</strong> m</div>
      </div>
    </section>
  </main>
  <script>
    const state = {
      episodes: [],
      episode: null,
      side: "",
      data: null,
      seekingFromSlider: false,
      raf: 0,
    };

    const $ = (id) => document.getElementById(id);
    const fmt = (v, digits = 6) => Number.isFinite(v) ? v.toFixed(digits) : "-";
    const cls = (ok) => ok ? "ok" : "warn";

    async function fetchJson(url) {
      const res = await fetch(url);
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }

    function nearestIndex(timestamps, videoSeconds) {
      if (!timestamps.length) return 0;
      const ms = videoSeconds * 1000;
      let lo = 0, hi = timestamps.length - 1;
      while (lo < hi) {
        const mid = Math.floor((lo + hi) / 2);
        if (timestamps[mid] < ms) lo = mid + 1;
        else hi = mid;
      }
      if (lo > 0 && Math.abs(timestamps[lo - 1] - ms) < Math.abs(timestamps[lo] - ms)) return lo - 1;
      return lo;
    }

    function setStatus(text) {
      $("headerStatus").textContent = text;
    }

    function populateEpisodes(payload) {
      state.episodes = payload.episodes || [];
      $("pathInput").value = payload.class_path || "";
      const select = $("episodeSelect");
      select.innerHTML = "";
      for (const ep of state.episodes) {
        const opt = document.createElement("option");
        opt.value = ep.name;
        opt.textContent = ep.name;
        select.appendChild(opt);
      }
      const selected = payload.selected_episode || (state.episodes[0] && state.episodes[0].name) || "";
      select.value = selected;
      state.episode = selected;
    }

    function populateSides(sides, preferred) {
      const select = $("sideSelect");
      select.innerHTML = "";
      for (const side of sides) {
        const opt = document.createElement("option");
        opt.value = side;
        opt.textContent = `${side}_wrist_view`;
        select.appendChild(opt);
      }
      state.side = sides.includes(preferred) ? preferred : (sides[0] || "");
      select.value = state.side;
    }

    function updateAt(index, seekVideo = false) {
      const data = state.data;
      if (!data || !data.timestamps.length) return;
      index = Math.max(0, Math.min(index, data.timestamps.length - 1));
      $("timeline").value = String(index);
      const ts = data.timestamps[index];
      $("timestampLabel").textContent = `${ts} ms`;
      $("frameLabel").textContent = String(index);
      const original = data.original[index];
      const repaired = data.recovered[index];
      $("originalValue").textContent = fmt(original);
      $("recoveredValue").textContent = fmt(repaired);
      $("originalState").textContent = Number.isFinite(original) ? "loaded" : "missing";
      $("recoveredState").textContent = Number.isFinite(repaired) ? "ready" : "missing";
      const delta = Number.isFinite(original) && Number.isFinite(repaired) ? repaired - original : NaN;
      $("originalSub").textContent = Number.isFinite(delta) ? `delta repaired-original: ${fmt(delta)}` : "from observation.state.gripper_original";
      $("recoveredSub").textContent = "from observation.state.gripper";
      $("currentDelta").textContent = fmt(delta);
      if (seekVideo) {
        const video = $("video");
        if (Number.isFinite(ts)) video.currentTime = ts / 1000.0;
      }
    }

    function updateDataDetails() {
      if (!state.data) return;
      $("originalSource").textContent = state.data.original_source || "-";
      $("repairedSource").textContent = state.data.repaired_source || "-";
      $("loadedRows").textContent = String(state.data.timestamps.length);
    }

    function syncFromVideo() {
      if (!state.data) return;
      updateAt(nearestIndex(state.data.timestamps, $("video").currentTime), false);
    }

    function syncLoop() {
      syncFromVideo();
      if (!$("video").paused && !$("video").ended) {
        state.raf = requestAnimationFrame(syncLoop);
      }
    }

    async function loadEpisode() {
      const ep = $("episodeSelect").value;
      if (!ep) {
        state.data = null;
        $("video").removeAttribute("src");
        $("video").load();
        $("sourcePath").textContent = "-";
        $("timestampLabel").textContent = "-";
        $("frameLabel").textContent = "-";
        $("originalValue").textContent = "-";
        $("recoveredValue").textContent = "-";
        $("loadedRows").textContent = "-";
        setStatus("choose an episode path");
        return;
      }
      let side = $("sideSelect").value || state.side || "";
      setStatus("loading episode");
      const firstUrl = `/api/episode?episode=${encodeURIComponent(ep)}${side ? `&side=${encodeURIComponent(side)}` : ""}`;
      const data = await fetchJson(firstUrl);
      populateSides(data.available_sides, data.side);
      state.data = data;
      state.episode = data.episode;
      state.side = data.side;
      $("sourcePath").textContent = data.source_path;
      $("timeline").max = String(Math.max(0, data.timestamps.length - 1));
      $("timeline").value = "0";
      $("video").src = data.video_url;
      $("video").load();
      updateDataDetails();
      updateAt(0, false);
      setStatus(`${data.episode} · ${data.side}_wrist_view`);
    }

    async function loadPath() {
      const path = $("pathInput").value.trim();
      setStatus("loading path");
      const payload = await fetchJson(`/api/episodes?path=${encodeURIComponent(path)}`);
      populateEpisodes(payload);
      await loadEpisode();
    }

    async function browsePath() {
      setStatus("opening folder picker");
      const current = $("pathInput").value.trim();
      const res = await fetch(`/api/select_source_dir?current=${encodeURIComponent(current)}`);
      if (!res.ok) throw new Error(await res.text());
      const payload = await res.json();
      if (!payload.path) {
        setStatus("folder selection cancelled");
        return;
      }
      $("pathInput").value = payload.path;
      await loadPath();
    }

    async function init() {
      try {
        const payload = await fetchJson("/api/episodes");
        populateEpisodes(payload);
        if (state.episode) {
          await loadEpisode();
        } else {
          setStatus("choose an episode path");
        }
      } catch (err) {
        setStatus(String(err.message || err));
      }
    }

    $("loadPathButton").addEventListener("click", () => loadPath().catch(err => setStatus(err.message)));
    $("browsePathButton").addEventListener("click", () => browsePath().catch(err => setStatus(err.message)));
    $("pathInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadPath().catch(err => setStatus(err.message));
    });
    $("reloadButton").addEventListener("click", () => loadEpisode().catch(err => setStatus(err.message)));
    $("episodeSelect").addEventListener("change", () => loadEpisode().catch(err => setStatus(err.message)));
    $("sideSelect").addEventListener("change", () => loadEpisode().catch(err => setStatus(err.message)));
    $("timeline").addEventListener("input", (event) => updateAt(Number(event.target.value), true));
    $("stepBack").addEventListener("click", () => updateAt(Number($("timeline").value) - 1, true));
    $("stepForward").addEventListener("click", () => updateAt(Number($("timeline").value) + 1, true));
    $("playButton").addEventListener("click", () => {
      const video = $("video");
      if (video.paused) {
        video.play();
        $("playButton").textContent = "Pause";
      } else {
        video.pause();
        $("playButton").textContent = "Play";
      }
    });
    $("video").addEventListener("timeupdate", syncFromVideo);
    $("video").addEventListener("seeked", syncFromVideo);
    $("video").addEventListener("pause", () => $("playButton").textContent = "Play");
    $("video").addEventListener("play", () => {
      $("playButton").textContent = "Pause";
      cancelAnimationFrame(state.raf);
      state.raf = requestAnimationFrame(syncLoop);
    });

    init();
  </script>
</body>
</html>
"""


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=DEFAULT_DATA_ROOT,
        help="Episode directory, class directory, or dataset root containing episode_* children.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--side", choices=SIDES, help="Default wrist side.")
    parser.add_argument("--no-open", action="store_true")
    return parser.parse_args()


def is_episode_dir(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("episode_")


def episode_sort_key(path: Path):
    suffix = path.name.removeprefix("episode_")
    if suffix.isdigit():
        return (0, int(suffix), path.name)
    return (1, 0, path.name)


def normalize_source(path: Path | None) -> tuple[Path | None, str]:
    if path is None:
        return None, ""
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Path does not exist: {resolved}")
    if is_episode_dir(resolved):
        return resolved.parent, resolved.name
    if resolved.is_dir() and any(is_episode_dir(child) for child in resolved.iterdir()):
        return resolved, ""
    if resolved.is_dir() and any(is_episode_dir(child) for child in resolved.rglob("episode_*")):
        return resolved, ""
    raise ValueError("Expected an episode_XXX directory, class directory, or dataset root containing episode_* subdirectories.")


def episode_dirs(class_path: Path | None) -> list[Path]:
    if class_path is None:
        return []
    direct = [p for p in class_path.iterdir() if is_episode_dir(p)]
    if direct:
        return sorted(direct, key=episode_sort_key)
    return sorted((p for p in class_path.rglob("episode_*") if is_episode_dir(p)), key=lambda p: (*episode_sort_key(p), str(p)))


def choose_directory(initial_dir: str):
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError(f"tkinter directory picker is unavailable: {exc}") from exc

    initial_path = Path(initial_dir or DEFAULT_DATA_ROOT).expanduser()
    if not initial_path.exists():
        initial_path = DEFAULT_DATA_ROOT
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(
            title="Select gripper episode or class folder",
            initialdir=str(initial_path.resolve()),
        )
    finally:
        root.destroy()


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def side_col(fieldnames: list[str], side: str, suffix: str, single_name: str | None = None):
    prefixed = f"{side}_{suffix}"
    if prefixed in fieldnames:
        return prefixed
    if single_name and single_name in fieldnames:
        return single_name
    return None


def available_sides(episode: Path) -> list[str]:
    out = []
    for side, stream in VIDEO_STREAMS.items():
        if (episode / stream / "video.mp4").exists():
            out.append(side)
    return out


def load_gripper_series(episode: Path, side: str, directory: str) -> tuple[list[str], list[float | None], Path]:
    path = episode / directory / "data.csv"
    if not path.exists():
        return [], [], path
    rows = read_csv_rows(path)
    if not rows:
        return [], [], path
    fields = list(rows[0].keys())
    col = side_col(fields, side, "gripper", "gripper")
    if col is None:
        return [], [], path
    timestamps = []
    values: list[float | None] = []
    for row in rows:
        ts = row.get("timestamp_ms")
        if ts is None or ts == "":
            continue
        timestamps.append(ts)
        try:
            values.append(float(row[col]))
        except (TypeError, ValueError):
            values.append(None)
    return timestamps, values, path


def align_values(target_timestamps: list[str], source_timestamps: list[str], source_values: list[float | None]):
    values_by_ts = dict(zip(source_timestamps, source_values))
    return [values_by_ts.get(ts) for ts in target_timestamps]


def build_episode_payload(episode_key: str, episode: Path, side: str):
    sides = available_sides(episode)
    if not sides:
        raise FileNotFoundError(f"No wrist-view video found in {episode}")
    if side not in sides:
        side = sides[0]

    repaired_ts, repaired_values, repaired_path = load_gripper_series(episode, side, REPAIRED_GRIPPER_DIR)
    original_ts, original_values, original_path = load_gripper_series(episode, side, ORIGINAL_GRIPPER_DIR)
    timestamps = repaired_ts or original_ts
    original = align_values(timestamps, original_ts, original_values)
    repaired = align_values(timestamps, repaired_ts, repaired_values)
    return {
        "episode": episode_key,
        "episode_name": episode.name,
        "source_path": str(episode),
        "side": side,
        "available_sides": sides,
        "video_url": f"/api/video?episode={episode_key}&side={side}",
        "timestamps": [int(float(ts)) for ts in timestamps],
        "original": original,
        "recovered": repaired,
        "original_source": str(original_path),
        "repaired_source": str(repaired_path),
    }


def browser_video_path(source_path: Path) -> Path:
    source_path = Path(source_path).resolve()
    stat = source_path.stat()
    key = hashlib.sha1(f"{source_path}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")).hexdigest()
    cache_dir = Path("/tmp/gripper_visualizer_video_cache")
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


def make_handler(initial_class_path: Path | None, selected_episode: str, args):
    server_state = {
        "class_path": initial_class_path,
        "selected_episode": selected_episode,
    }
    cache: dict[tuple[str, str], dict] = {}
    cache_lock = threading.Lock()

    def current_episodes():
        class_path = server_state["class_path"]
        if class_path is None:
            return {}
        return {str(p.relative_to(class_path)): p for p in episode_dirs(class_path)}

    def current_default_episode(episodes):
        selected = server_state["selected_episode"]
        return selected if selected in episodes else (next(iter(episodes), ""))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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

        def send_file(self, path: Path, content_type="application/octet-stream"):
            size = path.stat().st_size
            start = 0
            end = size - 1
            status = 200
            range_header = self.headers.get("Range")
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

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/":
                self.send_text(HTML, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/episodes":
                requested_path = query.get("path", [""])[0].strip()
                if requested_path:
                    try:
                        class_path, selected_episode = normalize_source(Path(requested_path))
                    except (FileNotFoundError, ValueError, OSError) as exc:
                        self.send_text(str(exc), status=404)
                        return
                    server_state["class_path"] = class_path
                    server_state["selected_episode"] = selected_episode
                    with cache_lock:
                        cache.clear()
                episodes = current_episodes()
                default_episode = current_default_episode(episodes)
                payload = {
                    "class_path": str(server_state["class_path"]) if server_state["class_path"] is not None else "",
                    "selected_episode": default_episode,
                    "episodes": [
                        {"name": name, "sides": available_sides(path)}
                        for name, path in episodes.items()
                    ],
                }
                self.send_json(payload)
                return
            if parsed.path == "/api/episode":
                episodes = current_episodes()
                default_episode = current_default_episode(episodes)
                episode_name = query.get("episode", [default_episode])[0]
                side = query.get("side", [args.side or ""])[0]
                if episode_name not in episodes:
                    self.send_text(f"Unknown episode: {episode_name}", status=404)
                    return
                key = (episode_name, side or "")
                with cache_lock:
                    payload = cache.get(key)
                if payload is None:
                    try:
                        payload = build_episode_payload(episode_name, episodes[episode_name], side)
                    except (OSError, ValueError, KeyError) as exc:
                        self.send_text(str(exc), status=500)
                        return
                    with cache_lock:
                        cache[key] = payload
                self.send_json(payload)
                return
            if parsed.path == "/api/select_source_dir":
                current = query.get("current", [""])[0].strip()
                try:
                    selected = choose_directory(current)
                except Exception as exc:
                    self.send_text(str(exc), status=500)
                    return
                self.send_json({"path": selected})
                return
            if parsed.path == "/api/video":
                episodes = current_episodes()
                episode_name = query.get("episode", [""])[0]
                side = query.get("side", [""])[0]
                if episode_name not in episodes:
                    self.send_text(f"Unknown episode: {episode_name}", status=404)
                    return
                if side not in VIDEO_STREAMS:
                    self.send_text(f"Unknown side: {side}", status=404)
                    return
                video_path = episodes[episode_name] / VIDEO_STREAMS[side] / "video.mp4"
                if not video_path.exists():
                    self.send_text(f"Missing video: {video_path}", status=404)
                    return
                try:
                    playable = browser_video_path(video_path)
                except (OSError, subprocess.CalledProcessError) as exc:
                    self.send_text(f"Failed to prepare browser video: {exc}", status=500)
                    return
                self.send_file(playable, "video/mp4")
                return
            self.send_text("Not found", status=404)

    return Handler


def port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def choose_port(host: str, start_port: int) -> int:
    for port in range(start_port, start_port + 50):
        if port_available(host, port):
            return port
    raise RuntimeError(f"No free port found from {start_port} to {start_port + 49}")


def main():
    args = parse_args()
    class_path, selected_episode = normalize_source(args.path)
    port = choose_port(args.host, args.port)
    server = ThreadingHTTPServer(
        (args.host, port),
        make_handler(class_path, selected_episode, args),
    )
    url = f"http://{args.host}:{port}/"
    print(f"Serving gripper visualizer at {url}")
    if class_path is None:
        print("No episode/class path supplied. Enter a path in the web page.")
    else:
        print(f"Class path: {class_path}")
        if selected_episode:
            print(f"Default episode: {selected_episode}")
    print(f"Original gripper: {ORIGINAL_GRIPPER_DIR}/data.csv")
    print(f"Repaired gripper: {REPAIRED_GRIPPER_DIR}/data.csv")
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
