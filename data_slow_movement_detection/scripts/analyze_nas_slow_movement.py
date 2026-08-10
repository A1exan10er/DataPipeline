#!/usr/bin/env python3
"""Analyze slow movement episodes on a Synology NAS.

The script logs into DSM File Station, samples episodes under /database/verified
by task key, downloads only metadata.json and eef_pose CSV files, and applies
the same primary-hand slow movement detector used locally.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import getpass
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


DEFAULT_DSM_URL = "http://192.168.50.2:5000"
DEFAULT_VERIFIED_ROOT = "/database/verified"
EPISODE_RE = re.compile(r"^episode_(\d+)(?:_|$)")
SIDES = ("left", "right")


class DSMAPIError(RuntimeError):
    def __init__(self, operation: str, code: Any, response: Any = None) -> None:
        self.operation = operation
        self.code = code
        self.response = response
        super().__init__(f"{operation} failed, DSM error code: {code}")


class DSMClient:
    def __init__(self, base_url: str, timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.entry_url = f"{self.base_url}/webapi/entry.cgi"
        self.timeout = timeout
        self.sid: str | None = None
        self.account: str | None = None
        self.password: str | None = None
        self.cookie_id: str | None = None

    @staticmethod
    def _decode_response(operation: str, status: int, body: bytes) -> dict[str, Any]:
        if status < 200 or status >= 300:
            raise RuntimeError(f"{operation} HTTP failed: status={status}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            excerpt = body[:300].decode("utf-8", errors="replace").replace("\n", " ")
            raise RuntimeError(f"{operation} returned non-JSON response: {excerpt}") from exc
        if not payload.get("success"):
            error = payload.get("error") or {}
            raise DSMAPIError(operation, error.get("code", "unknown"), payload)
        return payload

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        operation: str,
        expect_json: bool = True,
    ) -> tuple[int, bytes, dict[str, str]]:
        encoded = urllib.parse.urlencode(params).encode("utf-8")
        headers: dict[str, str] = {}
        data = None
        url = self.entry_url
        if method == "GET":
            url = f"{url}?{encoded.decode('utf-8')}"
        else:
            data = encoded
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookie_id:
            headers["Cookie"] = f"id={self.cookie_id}"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                status = response.status
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{operation} HTTP request failed: {exc}") from exc
        if expect_json:
            self._decode_response(operation, status, body)
        return status, body, response_headers

    def login(self, account: str, password: str) -> None:
        params = {
            "api": "SYNO.API.Auth",
            "version": 6,
            "method": "login",
            "account": account,
            "passwd": password,
            "session": "FileStation",
            "format": "sid",
        }
        status, body, _ = self._request("GET", params, "DSM login", expect_json=False)
        payload = self._decode_response("DSM login", status, body)
        sid = payload.get("data", {}).get("sid")
        if not sid:
            raise RuntimeError("DSM login succeeded but no sid was returned")
        self.sid = sid
        self.account = account
        self.password = password
        self.cookie_id = sid

    def relogin(self) -> None:
        if not self.account or self.password is None:
            raise RuntimeError("Cannot relogin because credentials are unavailable")
        self.login(self.account, self.password)

    def logout(self) -> None:
        if not self.sid:
            return
        params = {
            "api": "SYNO.API.Auth",
            "version": 6,
            "method": "logout",
            "session": "FileStation",
            "_sid": self.sid,
        }
        try:
            status, body, _ = self._request("GET", params, "DSM logout", expect_json=False)
            self._decode_response("DSM logout", status, body)
        finally:
            self.sid = None
            self.password = None
            self.cookie_id = None

    def _params(self, api: str, method: str, **kwargs: Any) -> dict[str, Any]:
        if not self.sid:
            raise RuntimeError("Not logged into DSM")
        return {"api": api, "version": 2, "method": method, "_sid": self.sid, **kwargs}

    def _post_with_retry(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, 6):
            try:
                status, body, _ = self._request("POST", params, operation, expect_json=False)
                if status in {502, 503, 504} and attempt < 5:
                    delay = 2 ** (attempt - 1)
                    print(f"[retry] {operation}: HTTP {status}, retry in {delay}s", file=sys.stderr)
                    time.sleep(delay)
                    continue
                return self._decode_response(operation, status, body)
            except DSMAPIError as exc:
                if exc.code in {105, 106, 107, 119} and attempt < 5:
                    print(f"[relogin] {operation}: DSM session error {exc.code}", file=sys.stderr)
                    self.relogin()
                    params["_sid"] = self.sid
                    continue
                raise
            except RuntimeError as exc:
                if attempt >= 5:
                    raise RuntimeError(f"{operation} failed after retries: {exc}") from exc
                delay = 2 ** (attempt - 1)
                print(f"[retry] {operation}: {exc}, retry in {delay}s", file=sys.stderr)
                time.sleep(delay)
        raise RuntimeError(f"{operation} failed after retries")

    def list_folder(self, remote_dir: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        page_size = 200
        offset = 0
        while True:
            params = self._params(
                "SYNO.FileStation.List",
                "list",
                folder_path=remote_dir,
                offset=offset,
                limit=page_size,
                additional=json.dumps(["size", "time"]),
            )
            payload = self._post_with_retry(f"list {remote_dir}", params)
            data = payload.get("data", {})
            page = data.get("files", [])
            entries.extend(page)
            offset += len(page)
            total = data.get("total")
            if not page or len(page) < page_size:
                break
            if isinstance(total, int) and offset >= total:
                break
        return entries

    def exists(self, remote_path: str) -> bool:
        params = self._params(
            "SYNO.FileStation.List",
            "getinfo",
            path=json.dumps([remote_path]),
            additional=json.dumps(["size"]),
        )
        try:
            payload = self._post_with_retry(f"getinfo {remote_path}", params)
        except DSMAPIError as exc:
            if exc.code == 408:
                return False
            raise
        files = payload.get("data", {}).get("files", [])
        return any(not item.get("error") for item in files)

    def download_bytes(self, remote_path: str) -> bytes:
        params = self._params(
            "SYNO.FileStation.Download",
            "download",
            path=remote_path,
            mode="download",
        )
        params.pop("_sid", None)
        for attempt in range(1, 6):
            try:
                status, body, headers = self._request("GET", params, f"download {remote_path}", expect_json=False)
                if status in {502, 503, 504} and attempt < 5:
                    delay = 2 ** (attempt - 1)
                    print(f"[retry] download {remote_path}: HTTP {status}, retry in {delay}s", file=sys.stderr)
                    time.sleep(delay)
                    continue
                if status == 404:
                    raise RuntimeError(f"download {remote_path} HTTP failed: status=404")
                if status < 200 or status >= 300:
                    raise RuntimeError(f"download {remote_path} HTTP failed: status={status}")
                if headers.get("content-type", "").lower().startswith("application/json"):
                    self._decode_response(f"download {remote_path}", status, body)
                return body
            except DSMAPIError as exc:
                if exc.code in {105, 106, 107, 119} and attempt < 5:
                    print(f"[relogin] download {remote_path}: DSM session error {exc.code}", file=sys.stderr)
                    self.relogin()
                    continue
                raise
            except RuntimeError as exc:
                if "status=404" in str(exc):
                    raise
                if attempt >= 5:
                    raise RuntimeError(f"download {remote_path} failed after retries: {exc}") from exc
                delay = 2 ** (attempt - 1)
                print(f"[retry] download {remote_path}: {exc}, retry in {delay}s", file=sys.stderr)
                time.sleep(delay)
        raise RuntimeError(f"download {remote_path} failed after retries")

    def download_text(self, remote_path: str) -> str:
        return self.download_bytes(remote_path).decode("utf-8")

    def try_download_text_optional(self, remote_path: str) -> str | None:
        try:
            return self.download_text(remote_path)
        except DSMAPIError as exc:
            if exc.code in {408, 414}:
                return None
            raise
        except RuntimeError as exc:
            message = str(exc)
            if "status=404" in message or "error code: 408" in message:
                return None
            raise


@dataclass(frozen=True)
class Segment:
    start_idx: int
    end_idx: int
    start_s: float
    end_s: float
    duration_s: float
    path_m: float
    avg_speed_m_s: float


def login_with_fallback(args: argparse.Namespace) -> DSMClient:
    client = DSMClient(args.dsm_url, args.timeout)
    env_account = os.environ.get("DSM_ACCOUNT") or os.environ.get("NAS_ACCOUNT")
    env_password = os.environ.get("DSM_PASSWORD") or os.environ.get("NAS_PASSWORD")
    if args.account:
        env_account = args.account

    if env_account and env_password:
        try:
            print(f"[login] trying environment credentials for {args.dsm_url}", file=sys.stderr)
            client.login(env_account, env_password)
            print("[login] success", file=sys.stderr)
            return client
        except Exception as exc:  # noqa: BLE001 - fallback path must catch all login failures
            print(f"[login] environment login failed: {exc}", file=sys.stderr)

    if args.non_interactive:
        raise RuntimeError("No valid DSM credentials. Set DSM_ACCOUNT and DSM_PASSWORD.")

    attempt = 1
    while True:
        account = args.account or input("DSM account: ").strip()
        password = getpass.getpass("DSM password: ")
        if not account or not password:
            print("[login] account and password cannot be empty", file=sys.stderr)
            continue
        client = DSMClient(args.dsm_url, args.timeout)
        try:
            print(f"[login] connecting to {args.dsm_url}, attempt {attempt}", file=sys.stderr)
            client.login(account, password)
            print("[login] success", file=sys.stderr)
            return client
        except Exception as exc:  # noqa: BLE001 - login retry loop
            print(f"[login] failed: {exc}. Re-enter credentials or press Ctrl+C.", file=sys.stderr)
            attempt += 1


def posix_join(*parts: str) -> str:
    cleaned = [part.strip("/") for part in parts if part]
    return "/" + "/".join(cleaned)


def normalize_cli_path(path: str) -> str:
    return "".join(part.strip() for part in path.splitlines()).rstrip("/")


def is_dir(entry: dict[str, Any]) -> bool:
    return bool(entry.get("isdir"))


def child_path(parent: str, entry: dict[str, Any]) -> str:
    path = entry.get("path")
    if isinstance(path, str) and path:
        return path
    return posix_join(parent, str(entry.get("name", "")))


def discover_task_dirs(client: DSMClient, verified_root: str, task_keys: set[str] | None) -> list[str]:
    task_dirs: list[str] = []
    for entry in client.list_folder(verified_root):
        if not is_dir(entry):
            continue
        name = str(entry.get("name", ""))
        if task_keys and name not in task_keys:
            continue
        task_dirs.append(child_path(verified_root, entry))
    return sorted(task_dirs)


def task_key_from_path(path: str, verified_root: str) -> str:
    path_parts = PurePosixPath(path).parts
    root_parts = PurePosixPath(verified_root).parts
    if len(path_parts) > len(root_parts) and path_parts[: len(root_parts)] == root_parts:
        return path_parts[len(root_parts)]
    return PurePosixPath(path).name


def summarize_task(task_key: str, episode_dirs: list[str], task_results: list[dict[str, Any]], dry_run: bool) -> dict[str, Any]:
    analyzed = [item for item in task_results if "error" not in item]
    grippers = [
        item.get("gripper") or {}
        for item in analyzed
        if (item.get("gripper") or {}).get("available")
    ]
    combined_segments = [
        item.get("combined_longest_slow_segment") or {}
        for item in analyzed
    ]
    avg_segment_speeds = [float(segment.get("avg_speed_cm_s", 0.0)) for segment in combined_segments]
    return {
        "task_key": task_key,
        "episodes_discovered": len(episode_dirs),
        "episodes_analyzed": len(task_results) if not dry_run else 0,
        "error_episodes": sum(1 for item in task_results if item.get("error")),
        "flagged_episodes": sum(1 for item in task_results if item.get("flagged")),
        "eef_slow_candidates": sum(1 for item in task_results if item.get("eef_flagged")),
        "suppressed_by_gripper_activity": sum(
            1 for item in task_results if item.get("suppressed_by_gripper_activity")
        ),
        "suppressed_by_dropper_relaxed_mode": sum(
            1 for item in task_results if item.get("suppressed_by_dropper_relaxed_mode")
        ),
        "suppressed_by_rotation_activity": sum(
            1 for item in task_results if item.get("suppressed_by_rotation_activity")
        ),
        "max_slow_segment_s": max(
            [float(item.get("longest_slow_segment_s", 0.0)) for item in task_results] or [0.0]
        ),
        "avg_longest_slow_segment_speed_cm_s": round(sum(avg_segment_speeds) / len(avg_segment_speeds), 3)
        if avg_segment_speeds
        else 0.0,
        "avg_gripper_bouts_per_min": round(
            sum(float(item.get("movement_bouts_per_min", 0.0)) for item in grippers) / len(grippers),
            3,
        )
        if grippers
        else 0.0,
        "avg_gripper_active_fraction": round(
            sum(float(item.get("active_fraction", 0.0)) for item in grippers) / len(grippers),
            5,
        )
        if grippers
        else 0.0,
        "episode_paths": [volume1_path(path) for path in episode_dirs],
    }


def find_episode_dirs(
    client: DSMClient,
    root: str,
    max_episodes: int,
    max_depth: int,
    sampling: str,
    progress_interval: int,
    max_discovered: int,
    date_from: str | None,
    date_to: str | None,
) -> list[str]:
    found: list[str] = []
    stack: list[tuple[str, int]] = [(root, 0)]
    scanned_dirs = 0
    while stack:
        current, depth = stack.pop()
        scanned_dirs += 1
        if progress_interval and scanned_dirs % progress_interval == 0:
            print(
                f"[discover episodes] scanned_dirs={scanned_dirs} "
                f"found_episodes={len(found)} pending_dirs={len(stack)} current={current}",
                flush=True,
            )
        try:
            entries = client.list_folder(current)
        except DSMAPIError as exc:
            if exc.code == 408:
                continue
            raise
        child_dirs = [entry for entry in entries if is_dir(entry)]
        for entry in sorted(child_dirs, key=lambda item: str(item.get("name", "")), reverse=True):
            name = str(entry.get("name", ""))
            path = child_path(current, entry)
            if date_dir_outside_range(name, date_from, date_to):
                continue
            if EPISODE_RE.match(name):
                episode_date = extract_episode_date(path)
                in_date_range = (
                    (not date_from or (episode_date is not None and episode_date >= date_from))
                    and (not date_to or (episode_date is not None and episode_date <= date_to))
                )
                if in_date_range and client.exists(posix_join(path, "observation.state.eef_pose", "data.csv")):
                    found.append(path)
                    if max_discovered and len(found) >= max_discovered:
                        stack.clear()
                        break
                continue
            if depth + 1 < max_depth:
                stack.append((path, depth + 1))
    ordered = sorted(found, key=episode_sort_key)
    selected = sample_episode_dirs(ordered, max_episodes, sampling)
    print(
        f"[discover episodes] done scanned_dirs={scanned_dirs} "
        f"found_episodes={len(ordered)} selected={len(selected)}",
        flush=True,
    )
    return selected


def sample_episode_dirs(ordered_episode_dirs: list[str], max_episodes: int, sampling: str) -> list[str]:
    if max_episodes == 0:
        return ordered_episode_dirs
    if len(ordered_episode_dirs) <= max_episodes:
        return ordered_episode_dirs
    if sampling == "latest":
        return ordered_episode_dirs[-max_episodes:]
    if sampling == "earliest":
        return ordered_episode_dirs[:max_episodes]

    # Evenly sample across the full ordered list. For 10 total and 5 requested,
    # this chooses 1-based positions 2, 4, 6, 8, 10.
    total = len(ordered_episode_dirs)
    selected_indices: list[int] = []
    seen: set[int] = set()
    for idx in range(max_episodes):
        selected_index = math.floor((idx + 1) * total / max_episodes) - 1
        selected_index = max(0, min(total - 1, selected_index))
        if selected_index not in seen:
            selected_indices.append(selected_index)
            seen.add(selected_index)

    # Guard against rare duplicate indices if max_episodes is close to total.
    fallback_index = 0
    while len(selected_indices) < max_episodes and fallback_index < total:
        if fallback_index not in seen:
            selected_indices.append(fallback_index)
            seen.add(fallback_index)
        fallback_index += 1

    return [ordered_episode_dirs[index] for index in sorted(selected_indices)]


def episode_sort_key(path: str) -> tuple[int, str]:
    name = PurePosixPath(path).name
    match = EPISODE_RE.match(name)
    return (int(match.group(1)) if match else -1, name)


def extract_episode_date(path: str) -> str | None:
    episode_name = PurePosixPath(path).name
    match = re.search(r"episode_\d+_(20\d{6})-", episode_name)
    if match:
        return match.group(1)
    parts = PurePosixPath(path).parts
    for part in reversed(parts):
        if re.fullmatch(r"20\d{6}", part):
            return part
    match = re.search(r"(20\d{6})", path)
    return match.group(1) if match else None


def date_dir_outside_range(name: str, date_from: str | None, date_to: str | None) -> bool:
    if not re.fullmatch(r"20\d{6}", name):
        return False
    return bool((date_from and name < date_from) or (date_to and name > date_to))


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def rolling_path_speed(step_distances_m: list[float], step_times_s: list[float], window_s: float) -> list[float]:
    queue: deque[tuple[float, float]] = deque()
    path_sum = 0.0
    speeds: list[float] = []
    for distance_m, time_s in zip(step_distances_m, step_times_s):
        queue.append((time_s, distance_m))
        path_sum += distance_m
        while queue and time_s - queue[0][0] > window_s:
            _, old_distance_m = queue.popleft()
            path_sum -= old_distance_m
        if len(queue) < 2:
            speeds.append(0.0)
            continue
        elapsed_s = min(window_s, max(1e-9, queue[-1][0] - queue[0][0]))
        speeds.append(path_sum / elapsed_s)
    return speeds


def slow_segments(
    timestamps_s: list[float],
    step_times_s: list[float],
    step_distances_m: list[float],
    speeds_m_s: list[float],
    max_speed_m_s: float,
) -> list[Segment]:
    segments: list[Segment] = []
    start_idx: int | None = None
    for idx, speed_m_s in enumerate(speeds_m_s):
        is_slow = speed_m_s <= max_speed_m_s
        if is_slow and start_idx is None:
            start_idx = idx
        if (not is_slow or idx == len(speeds_m_s) - 1) and start_idx is not None:
            end_idx = idx - 1 if not is_slow else idx
            start_s = step_times_s[start_idx]
            end_s = step_times_s[end_idx]
            duration_s = max(0.0, end_s - start_s)
            path_m = sum(step_distances_m[start_idx : end_idx + 1])
            segments.append(
                Segment(
                    start_idx,
                    end_idx,
                    start_s - timestamps_s[0],
                    end_s - timestamps_s[0],
                    duration_s,
                    path_m,
                    path_m / duration_s if duration_s > 0 else 0.0,
                )
            )
            start_idx = None
    return segments


def merge_segments(segments: list[Segment], step_distances_m: list[float], max_gap_s: float) -> list[Segment]:
    if max_gap_s <= 0:
        return segments
    merged: list[Segment] = []
    for segment in segments:
        if merged and segment.start_s - merged[-1].end_s <= max_gap_s:
            previous = merged[-1]
            path_m = sum(step_distances_m[previous.start_idx : segment.end_idx + 1])
            duration_s = segment.end_s - previous.start_s
            merged[-1] = Segment(
                previous.start_idx,
                segment.end_idx,
                previous.start_s,
                segment.end_s,
                duration_s,
                path_m,
                path_m / duration_s if duration_s > 0 else 0.0,
            )
        else:
            merged.append(segment)
    return merged


def find_xyz_columns(fieldnames: list[str], prefix: str) -> tuple[str, str, str] | None:
    candidates = [
        (f"{prefix}_x", f"{prefix}_y", f"{prefix}_z"),
        (f"{prefix}.x", f"{prefix}.y", f"{prefix}.z"),
        (f"{prefix}_position_x", f"{prefix}_position_y", f"{prefix}_position_z"),
        (f"{prefix}_eef_x", f"{prefix}_eef_y", f"{prefix}_eef_z"),
    ]
    field_set = set(fieldnames)
    for columns in candidates:
        if all(column in field_set for column in columns):
            return columns
    return None


def infer_eef_position_columns(fieldnames: list[str]) -> dict[str, tuple[str, str, str]]:
    columns: dict[str, tuple[str, str, str]] = {}
    for side in SIDES:
        side_columns = find_xyz_columns(fieldnames, side)
        if side_columns:
            columns[side] = side_columns
    if columns:
        return columns

    field_set = set(fieldnames)
    single_arm_candidates = [
        ("x", "y", "z"),
        ("eef_x", "eef_y", "eef_z"),
        ("ee_x", "ee_y", "ee_z"),
        ("tcp_x", "tcp_y", "tcp_z"),
        ("position_x", "position_y", "position_z"),
        ("actual_TCP_pose_0", "actual_TCP_pose_1", "actual_TCP_pose_2"),
        ("actual_tcp_pose_0", "actual_tcp_pose_1", "actual_tcp_pose_2"),
        ("tcp_pose_0", "tcp_pose_1", "tcp_pose_2"),
        ("pose_0", "pose_1", "pose_2"),
    ]
    for candidate in single_arm_candidates:
        if all(column in field_set for column in candidate):
            return {"left": candidate}

    prefixes: set[str] = set()
    for field in fieldnames:
        if field.endswith("_x"):
            prefixes.add(field[:-2])
    for prefix in sorted(prefixes):
        candidate = (f"{prefix}_x", f"{prefix}_y", f"{prefix}_z")
        if all(column in field_set for column in candidate):
            return {"left": candidate}

    raise KeyError(
        "No supported eef position columns found. Expected left/right xyz, x/y/z, eef_x/eef_y/eef_z, or tcp pose columns."
    )


def read_eef_csv(text: str) -> tuple[list[float], dict[str, list[tuple[float, float, float]]], dict[str, Any]]:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return [], {side: [] for side in SIDES}, {"mode": "empty", "columns": {}}
    position_columns = infer_eef_position_columns(list(rows[0].keys()))
    timestamps_s: list[float] = []
    positions = {side: [] for side in SIDES}
    for row in rows:
        timestamps_s.append(float(row["timestamp_ms"]) / 1000.0)
        for side in SIDES:
            if side in position_columns:
                positions[side].append(tuple(float(row[column]) for column in position_columns[side]))
    if position_columns:
        reference_side = next(iter(position_columns))
        reference_start = positions[reference_side][0] if positions[reference_side] else (0.0, 0.0, 0.0)
        for side in SIDES:
            if side not in position_columns:
                positions[side] = [reference_start for _ in timestamps_s]
    mode = "dual_arm" if len(position_columns) > 1 else "single_arm"
    return timestamps_s, positions, {"mode": mode, "columns": position_columns}


def analyze_eef_csv(args: argparse.Namespace, text: str) -> dict[str, Any]:
    timestamps_s, positions, eef_schema = read_eef_csv(text)
    if len(timestamps_s) < 2:
        raise ValueError("need at least two eef_pose samples")

    side_outputs: list[dict[str, Any]] = []
    side_steps: dict[str, list[float]] = {}
    side_speeds: dict[str, list[float]] = {}
    shared_step_times_s: list[float] = []
    for side in SIDES:
        step_times_s: list[float] = []
        step_distances_m: list[float] = []
        for idx in range(1, len(timestamps_s)):
            if timestamps_s[idx] <= timestamps_s[idx - 1]:
                continue
            step_times_s.append(timestamps_s[idx])
            step_distances_m.append(math.dist(positions[side][idx], positions[side][idx - 1]))
        speeds_m_s = rolling_path_speed(step_distances_m, step_times_s, args.window_s)
        side_steps[side] = step_distances_m
        side_speeds[side] = speeds_m_s
        if not shared_step_times_s:
            shared_step_times_s = step_times_s
        segments = slow_segments(
            timestamps_s,
            step_times_s,
            step_distances_m,
            speeds_m_s,
            args.max_speed_cm_s / 100.0,
        )
        segments = merge_segments(segments, step_distances_m, args.merge_gap_s)
        total_duration_s = max(1e-9, timestamps_s[-1] - timestamps_s[0])
        total_path_m = sum(step_distances_m)
        longest = max(segments, key=lambda item: item.duration_s, default=None)
        side_outputs.append(
            {
                "side": side,
                "total_path_cm": round(total_path_m * 100.0, 3),
                "avg_speed_cm_s": round(total_path_m / total_duration_s * 100.0, 3),
                "median_rolling_speed_cm_s": round(percentile(speeds_m_s, 50) * 100.0, 3),
                "p90_rolling_speed_cm_s": round(percentile(speeds_m_s, 90) * 100.0, 3),
                "slow_fraction": round(sum(segment.duration_s for segment in segments) / total_duration_s, 4),
                "longest_slow_segment": segment_to_dict(longest),
            }
        )

    primary = max(side_outputs, key=lambda item: item["total_path_cm"])
    primary["role"] = "primary"
    for side_output in side_outputs:
        if "role" not in side_output:
            side_output["role"] = "secondary"
            side_output["flagged"] = False

    combined_speeds_m_s = [
        max(side_speeds[side][idx] for side in SIDES)
        for idx in range(min(len(side_speeds["left"]), len(side_speeds["right"])))
    ]
    combined_steps_m = [
        sum(side_steps[side][idx] for side in SIDES)
        for idx in range(min(len(side_steps["left"]), len(side_steps["right"])))
    ]
    combined_segments = slow_segments(
        timestamps_s,
        shared_step_times_s[: len(combined_speeds_m_s)],
        combined_steps_m,
        combined_speeds_m_s,
        args.max_speed_cm_s / 100.0,
    )
    combined_segments = merge_segments(combined_segments, combined_steps_m, args.merge_gap_s)
    combined_longest = max(combined_segments, key=lambda item: item.duration_s, default=None)

    primary_longest = primary["longest_slow_segment"]
    if args.flag_mode == "primary":
        trigger_segment = primary_longest
        flagged = primary_longest["duration_s"] >= args.min_duration_s
    else:
        trigger_segment = segment_to_dict(combined_longest)
        flagged = trigger_segment["duration_s"] >= args.min_duration_s
    primary["flagged"] = args.flag_mode == "primary" and flagged
    return {
        "flagged": flagged,
        "flag_mode": args.flag_mode,
        "primary_side": primary["side"],
        "longest_slow_segment_s": trigger_segment["duration_s"],
        "longest_slow_segment_path_cm": trigger_segment["path_cm"],
        "combined_longest_slow_segment": segment_to_dict(combined_longest),
        "eef_schema": eef_schema,
        "sides": side_outputs,
    }


def segment_to_dict(segment: Segment | None) -> dict[str, float]:
    if segment is None:
        return {"start_s": 0.0, "end_s": 0.0, "duration_s": 0.0, "path_cm": 0.0, "avg_speed_cm_s": 0.0}
    return {
        "start_s": round(segment.start_s, 3),
        "end_s": round(segment.end_s, 3),
        "duration_s": round(segment.duration_s, 3),
        "path_cm": round(segment.path_m * 100.0, 3),
        "avg_speed_cm_s": round(segment.avg_speed_m_s * 100.0, 3),
    }


def close_gripper_directional_segment(
    segments: list[dict[str, Any]],
    side: str,
    sign: int,
    start_s: float,
    end_s: float,
    total_motion: float,
) -> None:
    duration_s = max(0.0, end_s - start_s)
    if duration_s <= 0:
        return
    segments.append(
        {
            "side": side,
            "direction": "increasing" if sign > 0 else "decreasing",
            "start_s": round(start_s, 3),
            "end_s": round(end_s, 3),
            "duration_s": round(duration_s, 3),
            "total_motion": round(total_motion, 5),
            "avg_speed": round(total_motion / duration_s, 5),
        }
    )


def scalar_directional_segments(
    timestamps_s: list[float],
    values: dict[str, list[float]],
    deadband: float,
    merge_gap_s: float,
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    t0 = timestamps_s[0]
    for side, side_values in values.items():
        active: dict[str, Any] | None = None
        neutral_since_s: float | None = None
        for idx in range(1, len(side_values)):
            dt_s = timestamps_s[idx] - timestamps_s[idx - 1]
            if dt_s <= 0:
                continue
            diff = side_values[idx] - side_values[idx - 1]
            step_start_s = timestamps_s[idx - 1] - t0
            step_end_s = timestamps_s[idx] - t0
            if abs(diff) <= deadband:
                if active and neutral_since_s is None:
                    neutral_since_s = step_end_s
                if active and neutral_since_s is not None and step_end_s - neutral_since_s > merge_gap_s:
                    close_gripper_directional_segment(
                        segments,
                        side,
                        int(active["sign"]),
                        float(active["start_s"]),
                        neutral_since_s,
                        float(active["total_motion"]),
                    )
                    active = None
                    neutral_since_s = None
                continue

            sign = 1 if diff > 0 else -1
            if active and sign == active["sign"]:
                active["end_s"] = step_end_s
                active["total_motion"] += abs(diff)
                neutral_since_s = None
                continue

            if active:
                close_gripper_directional_segment(
                    segments,
                    side,
                    int(active["sign"]),
                    float(active["start_s"]),
                    float(active["end_s"]),
                    float(active["total_motion"]),
                )
            active = {
                "sign": sign,
                "start_s": step_start_s,
                "end_s": step_end_s,
                "total_motion": abs(diff),
            }
            neutral_since_s = None
        if active:
            close_gripper_directional_segment(
                segments,
                side,
                int(active["sign"]),
                float(active["start_s"]),
                float(active["end_s"]),
                float(active["total_motion"]),
            )
    return segments


def analyze_scalar_motion_csv(
    text: str,
    field_suffix: str,
    directional_deadband: float = 1e-4,
    directional_merge_gap_s: float = 0.5,
    min_speed_threshold: float = 0.02,
) -> dict[str, Any]:
    rows = list(csv.DictReader(io.StringIO(text)))
    if len(rows) < 2:
        return {
            "available": False,
            "total_motion": 0.0,
            "active_fraction": 0.0,
            "movement_bouts": 0,
            "movement_bouts_per_min": 0.0,
        }

    timestamps_s = [float(row["timestamp_ms"]) / 1000.0 for row in rows]
    duration_s = max(1e-9, timestamps_s[-1] - timestamps_s[0])
    values = {
        side: [float(row[f"{side}_{field_suffix}"]) for row in rows]
        for side in SIDES
        if f"{side}_{field_suffix}" in rows[0]
    }
    if not values:
        return {
            "available": False,
            "total_motion": 0.0,
            "active_fraction": 0.0,
            "movement_bouts": 0,
            "movement_bouts_per_min": 0.0,
        }

    side_metrics: dict[str, dict[str, float]] = {}
    max_speeds: list[float] = []
    speed_times: list[float] = []
    total_motion = 0.0
    for side, side_values in values.items():
        diffs: list[float] = []
        speeds: list[float] = []
        for idx in range(1, len(side_values)):
            dt_s = timestamps_s[idx] - timestamps_s[idx - 1]
            if dt_s <= 0:
                continue
            diff = abs(side_values[idx] - side_values[idx - 1])
            diffs.append(diff)
            speeds.append(diff / dt_s)
        total = sum(diffs)
        total_motion += total
        side_metrics[side] = {
            "total_motion": round(total, 5),
            "p90_speed": round(percentile(speeds, 90), 5),
            "max_speed": round(max(speeds or [0.0]), 5),
        }

    for idx in range(1, len(rows)):
        dt_s = timestamps_s[idx] - timestamps_s[idx - 1]
        if dt_s <= 0:
            continue
        max_speed = max(
            abs(values[side][idx] - values[side][idx - 1]) / dt_s
            for side in values
        )
        max_speeds.append(max_speed)
        speed_times.append(timestamps_s[idx] - timestamps_s[0])

    threshold = max(min_speed_threshold, percentile(max_speeds, 75) * 0.5)
    active_mask = [speed > threshold for speed in max_speeds]
    active_time_s = 0.0
    bouts = 0
    start_idx: int | None = None
    for idx, active in enumerate(active_mask):
        if active and start_idx is None:
            start_idx = idx
        if (not active or idx == len(active_mask) - 1) and start_idx is not None:
            end_idx = idx - 1 if not active else idx
            duration = max(0.0, speed_times[end_idx] - speed_times[start_idx])
            if duration >= 0.15:
                bouts += 1
                active_time_s += duration
            start_idx = None

    directional_segments = scalar_directional_segments(
        timestamps_s,
        values,
        directional_deadband,
        directional_merge_gap_s,
    )
    longest_directional = max(directional_segments, key=lambda item: float(item["duration_s"]), default=None)

    return {
        "available": True,
        "total_motion": round(total_motion, 5),
        "active_fraction": round(active_time_s / duration_s, 5),
        "movement_bouts": bouts,
        "movement_bouts_per_min": round(bouts / duration_s * 60.0, 3),
        "speed_threshold": round(threshold, 5),
        "directional_deadband": directional_deadband,
        "longest_directional_segment": longest_directional
        or {
            "side": "",
            "direction": "",
            "start_s": 0.0,
            "end_s": 0.0,
            "duration_s": 0.0,
            "total_motion": 0.0,
            "avg_speed": 0.0,
        },
        "sides": side_metrics,
    }


def analyze_gripper_csv(text: str) -> dict[str, Any]:
    metrics = analyze_scalar_motion_csv(
        text,
        "gripper",
        directional_deadband=1e-4,
        directional_merge_gap_s=0.5,
        min_speed_threshold=0.02,
    )
    timestamps_s, values = read_side_scalar_csv(text, "gripper")
    metrics["_timestamps_s"] = timestamps_s
    metrics["_values"] = values
    return metrics


def analyze_raw_gripper_rotation_csv(text: str) -> dict[str, Any]:
    metrics = analyze_scalar_motion_csv(
        text,
        "raw_gripper_rotation",
        directional_deadband=0.01,
        directional_merge_gap_s=0.5,
        min_speed_threshold=0.2,
    )
    timestamps_s, values = read_side_scalar_csv(text, "raw_gripper_rotation")
    metrics["_timestamps_s"] = timestamps_s
    metrics["_values"] = values
    return metrics


def read_side_scalar_csv(text: str, field_suffix: str) -> tuple[list[float], dict[str, list[float]]]:
    rows = list(csv.DictReader(io.StringIO(text)))
    if len(rows) < 2:
        return [], {}
    timestamps_s = [float(row["timestamp_ms"]) / 1000.0 for row in rows]
    values = {
        side: [float(row[f"{side}_{field_suffix}"]) for row in rows]
        for side in SIDES
        if f"{side}_{field_suffix}" in rows[0]
    }
    return timestamps_s, values


def read_named_scalar_csv(text: str, exclude_suffixes: tuple[str, ...] = ()) -> tuple[list[float], dict[str, list[float]]]:
    rows = list(csv.DictReader(io.StringIO(text)))
    if len(rows) < 2:
        return [], {}
    fields = [
        field
        for field in rows[0]
        if field != "timestamp_ms" and not any(field.endswith(suffix) for suffix in exclude_suffixes)
    ]
    timestamps_s = [float(row["timestamp_ms"]) / 1000.0 for row in rows]
    values = {field: [float(row[field]) for row in rows] for field in fields}
    return timestamps_s, values


def window_scalar_motion(
    timestamps_s: list[float],
    values: dict[str, list[float]],
    start_s: float,
    end_s: float,
) -> dict[str, Any]:
    if not timestamps_s or not values or end_s <= start_s:
        return {"available": False, "total_motion": 0.0, "avg_speed": 0.0, "max_field": "", "max_field_motion": 0.0}
    t0 = timestamps_s[0]
    indices = [idx for idx, time_s in enumerate(timestamps_s) if start_s <= time_s - t0 <= end_s]
    if len(indices) < 2:
        return {"available": False, "total_motion": 0.0, "avg_speed": 0.0, "max_field": "", "max_field_motion": 0.0}
    field_motion: dict[str, float] = {}
    for field, field_values in values.items():
        total = 0.0
        for previous_idx, current_idx in zip(indices, indices[1:]):
            total += abs(field_values[current_idx] - field_values[previous_idx])
        field_motion[field] = total
    max_field, max_motion = max(field_motion.items(), key=lambda item: item[1])
    total_motion = sum(field_motion.values())
    duration_s = end_s - start_s
    return {
        "available": True,
        "total_motion": round(total_motion, 5),
        "avg_speed": round(total_motion / duration_s, 5),
        "max_field": max_field,
        "max_field_motion": round(max_motion, 5),
        "max_field_avg_speed": round(max_motion / duration_s, 5),
    }


def attach_window_motion(metrics: dict[str, Any], slow_segment: dict[str, Any], key: str) -> None:
    timestamps_s = metrics.get("_timestamps_s") or []
    values = metrics.get("_values") or {}
    metrics[key] = window_scalar_motion(
        timestamps_s,
        values,
        float(slow_segment.get("start_s", 0.0)),
        float(slow_segment.get("end_s", 0.0)),
    )


def strip_internal_series(metrics: dict[str, Any]) -> None:
    metrics.pop("_timestamps_s", None)
    metrics.pop("_values", None)


def analyze_joint_position_csv(text: str, slow_segment: dict[str, Any]) -> dict[str, Any]:
    timestamps_s, values = read_named_scalar_csv(text, exclude_suffixes=("_gripper",))
    if not timestamps_s or not values:
        return {"available": False}
    return {
        "available": True,
        "slow_window": window_scalar_motion(
            timestamps_s,
            values,
            float(slow_segment.get("start_s", 0.0)),
            float(slow_segment.get("end_s", 0.0)),
        ),
    }


def apply_gripper_logic(
    args: argparse.Namespace,
    task_key: str,
    analysis: dict[str, Any],
    gripper: dict[str, Any],
    raw_gripper_rotation: dict[str, Any],
    joint_position: dict[str, Any],
) -> None:
    eef_flagged = bool(analysis.get("flagged"))
    analysis["eef_flagged"] = eef_flagged
    analysis["gripper_flagged"] = False
    analysis["suppressed_by_gripper_activity"] = False
    analysis["suppressed_by_dropper_relaxed_mode"] = False
    analysis["suppressed_by_rotation_activity"] = False
    analysis["flag_reason"] = "eef_slow" if eef_flagged else "none"
    relaxed_dropper = (
        args.dropper_relaxed_mode
        and "dropper" in task_key.lower()
        and task_key not in args.disable_dropper_relaxed_task_key
    )

    if relaxed_dropper and eef_flagged:
        segment = analysis.get("combined_longest_slow_segment") or {}
        dropper_extreme_eef_slow = (
            float(segment.get("duration_s", 0.0)) >= args.dropper_extreme_duration_s
            and float(segment.get("avg_speed_cm_s", 0.0)) <= args.dropper_extreme_max_speed_cm_s
        )
        if dropper_extreme_eef_slow:
            analysis["flag_reason"] = "dropper_extreme_eef_slow"
        else:
            analysis["flagged"] = False
            analysis["suppressed_by_dropper_relaxed_mode"] = True
            analysis["flag_reason"] = "eef_slow_suppressed_by_dropper_relaxed_mode"

    gripper_window = gripper.get("slow_window") or {}
    joint_window = joint_position.get("slow_window") or {}
    gripper_window_active = (
        bool(gripper_window.get("available"))
        and float(gripper_window.get("total_motion", 0.0)) >= args.gripper_window_min_motion
        and float(gripper_window.get("avg_speed", 0.0)) >= args.gripper_window_min_avg_speed
    )
    joint_window_active = (
        bool(joint_window.get("available"))
        and float(joint_window.get("max_field_motion", 0.0)) >= args.joint_rotation_min_window_motion
        and float(joint_window.get("max_field_avg_speed", 0.0)) >= args.joint_rotation_min_avg_speed
    )
    raw_rotation_window_active = (
        task_matches_any_pattern(task_key, args.raw_rotation_gate_task_pattern)
        and bool(raw_rotation_window.get("available"))
        and float(raw_rotation_window.get("max_field_motion", 0.0)) >= args.raw_rotation_window_min_motion
        and float(raw_rotation_window.get("max_field_avg_speed", 0.0)) >= args.raw_rotation_window_min_avg_speed
    )

    longest_directional = gripper.get("longest_directional_segment") or {}
    gripper_slow = bool(gripper.get("available")) and (
        float(longest_directional.get("duration_s", 0.0)) >= args.gripper_slow_min_duration_s
    )
    analysis["gripper_flagged"] = gripper_slow
    task_gripper_gate_enabled = task_key not in args.disable_gripper_gate_task_key

    if analysis["flagged"] and eef_flagged and task_gripper_gate_enabled:
        if args.gripper_activity_gate and gripper_window_active and not gripper_slow:
            analysis["flagged"] = False
            analysis["suppressed_by_gripper_activity"] = True
            analysis["flag_reason"] = "eef_slow_suppressed_by_gripper_window_activity"
        elif raw_rotation_window_active:
            analysis["flagged"] = False
            analysis["suppressed_by_rotation_activity"] = True
            analysis["flag_reason"] = "eef_slow_suppressed_by_raw_rotation_window_activity"
        elif joint_window_active:
            analysis["flagged"] = False
            analysis["suppressed_by_rotation_activity"] = True
            analysis["flag_reason"] = "eef_slow_suppressed_by_joint_rotation"

    if gripper_slow:
        analysis["flagged"] = True
        analysis["flag_reason"] = "eef_and_gripper_slow" if eef_flagged else "gripper_slow"


def analyze_remote_episode(client: DSMClient, args: argparse.Namespace, task_key: str, episode_path: str) -> dict[str, Any]:
    metadata_path = posix_join(episode_path, "metadata.json")
    csv_path = posix_join(episode_path, "observation.state.eef_pose", "data.csv")
    gripper_path = posix_join(episode_path, "observation.state.gripper", "data.csv")
    raw_gripper_rotation_path = posix_join(episode_path, "observation.state.raw_gripper_rotation", "data.csv")
    joint_position_path = posix_join(episode_path, "observation.state.joint_position", "data.csv")
    metadata: dict[str, Any] = {}
    if client.exists(metadata_path):
        metadata_text = client.download_text(metadata_path)
        metadata = json.loads(metadata_text)
    csv_text = client.download_text(csv_path)
    analysis = analyze_eef_csv(args, csv_text)
    gripper_metrics = {"available": False}
    gripper_text = client.try_download_text_optional(gripper_path)
    if gripper_text is not None:
        gripper_metrics = analyze_gripper_csv(gripper_text)
    raw_gripper_rotation_metrics = {"available": False}
    raw_gripper_rotation_text = client.try_download_text_optional(raw_gripper_rotation_path)
    if raw_gripper_rotation_text is not None:
        raw_gripper_rotation_metrics = analyze_raw_gripper_rotation_csv(raw_gripper_rotation_text)
    slow_segment = analysis.get("combined_longest_slow_segment") or {}
    if gripper_metrics.get("available"):
        attach_window_motion(gripper_metrics, slow_segment, "slow_window")
    if raw_gripper_rotation_metrics.get("available"):
        attach_window_motion(raw_gripper_rotation_metrics, slow_segment, "slow_window")
    joint_position_metrics = {"available": False}
    joint_position_text = client.try_download_text_optional(joint_position_path)
    if joint_position_text is not None:
        joint_position_metrics = analyze_joint_position_csv(
            joint_position_text,
            slow_segment,
        )
    apply_gripper_logic(args, task_key, analysis, gripper_metrics, raw_gripper_rotation_metrics, joint_position_metrics)
    strip_internal_series(gripper_metrics)
    strip_internal_series(raw_gripper_rotation_metrics)
    episode_id = metadata.get("episode_id") or PurePosixPath(episode_path).name
    return {
        "task_key": task_key,
        "episode_id": episode_id,
        "episode_path": episode_path,
        "episode_nas_path": volume1_path(episode_path),
        "metadata_path": metadata_path,
        "eef_pose_csv_path": csv_path,
        "gripper_csv_path": gripper_path,
        "raw_gripper_rotation_csv_path": raw_gripper_rotation_path,
        "joint_position_csv_path": joint_position_path,
        "duration_s": float(metadata.get("duration_seconds", 0.0)),
        "fps_actual": metadata.get("fps_actual"),
        "username": metadata.get("username"),
        "gripper": gripper_metrics,
        "raw_gripper_rotation": raw_gripper_rotation_metrics,
        "joint_position": joint_position_metrics,
        **analysis,
    }


def volume1_path(file_station_path: str) -> str:
    if file_station_path.startswith("/volume1/"):
        return file_station_path
    if file_station_path.startswith("/"):
        return f"/volume1{file_station_path}"
    return f"/volume1/{file_station_path}"


def parse_task_keys(values: Iterable[str]) -> set[str] | None:
    keys: set[str] = set()
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                keys.add(part)
    return keys or None


def parse_patterns(values: Iterable[str]) -> list[str]:
    patterns: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip().lower()
            if part:
                patterns.append(part)
    return patterns


def task_matches_any_pattern(task_key: str, patterns: Iterable[str]) -> bool:
    task_key_lower = task_key.lower()
    return any(pattern in task_key_lower for pattern in patterns)


def episode_number(episode_id: str) -> str:
    match = EPISODE_RE.match(episode_id)
    if not match:
        return episode_id
    return f"{int(match.group(1)):04d}"


def common_episode_parent(paths: list[str]) -> str:
    if not paths:
        return ""
    split_paths = [path.strip("/").split("/")[:-1] for path in paths]
    common: list[str] = []
    for parts in zip(*split_paths):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return "/" + "/".join(common) if common else "/"


def write_outputs(args: argparse.Namespace, results: list[dict[str, Any]], task_summaries: list[dict[str, Any]]) -> None:
    jsonl_path, summary_path, report_path = write_output_files(args, results, task_summaries)
    print(f"[output] {jsonl_path}")
    print(f"[output] {summary_path}")
    print(f"[output] {report_path}")


def write_output_files(
    args: argparse.Namespace,
    results: list[dict[str, Any]],
    task_summaries: list[dict[str, Any]],
) -> tuple[Path, Path, Path]:
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "nas_slow_movement_results.jsonl"
    summary_path = output_dir / "nas_slow_movement_summary.json"
    report_path = output_dir / "nas_slow_movement_report.md"
    jsonl_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in results),
        encoding="utf-8",
    )
    summary = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "dsm_url": args.dsm_url,
        "verified_root": args.verified_root,
        "task_key_count": len(task_summaries),
        "episode_count": len(results),
        "error_episode_count": sum(1 for result in results if result.get("error")),
        "flagged_episode_count": sum(1 for result in results if result["flagged"]),
        "eef_slow_candidate_count": sum(1 for result in results if result.get("eef_flagged")),
        "suppressed_by_gripper_activity_count": sum(
            1 for result in results if result.get("suppressed_by_gripper_activity")
        ),
        "suppressed_by_dropper_relaxed_mode_count": sum(
            1 for result in results if result.get("suppressed_by_dropper_relaxed_mode")
        ),
        "suppressed_by_rotation_activity_count": sum(
            1 for result in results if result.get("suppressed_by_rotation_activity")
        ),
        "thresholds": {
            "max_speed_cm_s": args.max_speed_cm_s,
            "min_duration_s": args.min_duration_s,
            "merge_gap_s": args.merge_gap_s,
            "window_s": args.window_s,
            "flag_mode": args.flag_mode,
            "episode_sampling": args.episode_sampling,
            "date_from": args.date_from,
            "date_to": args.date_to,
            "gripper_activity_gate": args.gripper_activity_gate,
            "gripper_window_min_motion": args.gripper_window_min_motion,
            "gripper_window_min_avg_speed": args.gripper_window_min_avg_speed,
            "gripper_slow_min_duration_s": args.gripper_slow_min_duration_s,
            "dropper_relaxed_mode": args.dropper_relaxed_mode,
            "dropper_extreme_duration_s": args.dropper_extreme_duration_s,
            "dropper_extreme_max_speed_cm_s": args.dropper_extreme_max_speed_cm_s,
            "joint_rotation_min_window_motion": args.joint_rotation_min_window_motion,
            "joint_rotation_min_avg_speed": args.joint_rotation_min_avg_speed,
            "raw_rotation_gate_task_pattern": args.raw_rotation_gate_task_pattern,
            "raw_rotation_window_min_motion": args.raw_rotation_window_min_motion,
            "raw_rotation_window_min_avg_speed": args.raw_rotation_window_min_avg_speed,
        },
        "tasks": task_summaries,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(render_report(summary, results), encoding="utf-8")
    return jsonl_path, summary_path, report_path


def refresh_outputs(args: argparse.Namespace, results: list[dict[str, Any]], task_summaries: list[dict[str, Any]]) -> None:
    if args.no_incremental_output:
        return
    _, _, report_path = write_output_files(args, results, task_summaries)
    print(f"[incremental output] refreshed {report_path}", flush=True)


def render_report(summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines = [
        "# NAS Slow Movement Analysis",
        "",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Verified root: `{summary['verified_root']}`",
        f"- Task keys analyzed: `{summary['task_key_count']}`",
        f"- Episodes analyzed: `{summary['episode_count']}`",
        f"- Error episodes: `{summary.get('error_episode_count', 0)}`",
        f"- Slow episodes: `{summary['flagged_episode_count']}`",
        f"- EEF slow candidates: `{summary.get('eef_slow_candidate_count', 0)}`",
        f"- Suppressed by gripper activity: `{summary.get('suppressed_by_gripper_activity_count', 0)}`",
        f"- Suppressed by dropper relaxed mode: `{summary.get('suppressed_by_dropper_relaxed_mode_count', 0)}`",
        f"- Suppressed by rotation activity: `{summary.get('suppressed_by_rotation_activity_count', 0)}`",
        f"- Rule: rolling path speed <= `{summary['thresholds']['max_speed_cm_s']}` cm/s, "
        f"merge gaps <= `{summary['thresholds']['merge_gap_s']}` s, duration >= "
        f"`{summary['thresholds']['min_duration_s']}` s, mode `{summary['thresholds']['flag_mode']}`",
        f"- Date filter: `{summary['thresholds']['date_from'] or '-inf'}` to "
        f"`{summary['thresholds']['date_to'] or '+inf'}`",
        f"- Gripper gate: `{summary['thresholds']['gripper_activity_gate']}`, "
        f"slow-window motion >= `{summary['thresholds'].get('gripper_window_min_motion', 0)}` and avg >= "
        f"`{summary['thresholds'].get('gripper_window_min_avg_speed', 0)}`; sustained one-direction gripper motion >= "
        f"`{summary['thresholds']['gripper_slow_min_duration_s']}` s is slow",
        f"- Dropper relaxed mode: `{summary['thresholds'].get('dropper_relaxed_mode', False)}`, "
        f"extreme if duration >= `{summary['thresholds'].get('dropper_extreme_duration_s', 0)}` s and avg speed <= "
        f"`{summary['thresholds'].get('dropper_extreme_max_speed_cm_s', 0)}` cm/s",
        f"- Rotation gate: joint window motion >= `{summary['thresholds'].get('joint_rotation_min_window_motion', 0)}` "
        f"and avg >= `{summary['thresholds'].get('joint_rotation_min_avg_speed', 0)}`",
        f"- Raw rotation gate patterns: `{','.join(summary['thresholds'].get('raw_rotation_gate_task_pattern', []))}`",
        "",
        "## Task Summary",
        "",
        "| Task key | Episodes analyzed | Errors | EEF slow candidates | Slow episodes | Gripper suppressed | Dropper suppressed | Rotation suppressed | Max slow segment | Avg slow speed | Avg gripper bouts/min | Avg gripper active |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for task in summary["tasks"]:
        lines.append(
            f"| `{task['task_key']}` | {task['episodes_analyzed']} | "
            f"{task.get('error_episodes', 0)} | "
            f"{task.get('eef_slow_candidates', 0)} | "
            f"{task['flagged_episodes']} | "
            f"{task.get('suppressed_by_gripper_activity', 0)} | "
            f"{task.get('suppressed_by_dropper_relaxed_mode', 0)} | "
            f"{task.get('suppressed_by_rotation_activity', 0)} | "
            f"{task['max_slow_segment_s']:.2f}s | "
            f"{float(task.get('avg_longest_slow_segment_speed_cm_s', 0.0)):.2f}cm/s | "
            f"{float(task.get('avg_gripper_bouts_per_min', 0.0)):.2f} | "
            f"{float(task.get('avg_gripper_active_fraction', 0.0)):.3f} |"
        )
    lines.extend(["", "## Slow Episodes", ""])
    slow_results = [result for result in results if result["flagged"]]
    if not slow_results:
        lines.append("No slow episodes were detected.")
    else:
        by_task: dict[str, list[dict[str, Any]]] = {}
        for result in slow_results:
            by_task.setdefault(str(result["task_key"]), []).append(result)
        for task_key in sorted(by_task):
            task_results = sorted(by_task[task_key], key=lambda item: episode_sort_key(str(item["episode_path"])))
            parent = common_episode_parent([str(item["episode_nas_path"]) for item in task_results])
            lines.extend([
                f"### `{task_key}`",
                "",
                f"- NAS directory: `{parent}`",
                "",
                "| Episode # | Episode id | Slow window | Duration | Avg speed | Path | Grip win motion | Raw rot win | Joint max motion | Reason | Primary hand |",
                "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
            ])
            for result in task_results:
                episode_id = str(result["episode_id"])
                segment = result.get("combined_longest_slow_segment") or {}
                gripper = result.get("gripper") or {}
                start_s = float(segment.get("start_s", 0.0))
                end_s = float(segment.get("end_s", 0.0))
                avg_speed = float(segment.get("avg_speed_cm_s", 0.0))
                path_cm = float(segment.get("path_cm", 0.0))
                gripper_window = gripper.get("slow_window") or {}
                raw_rotation_window = ((result.get("raw_gripper_rotation") or {}).get("slow_window") or {})
                gripper_window_motion = float(gripper_window.get("total_motion", 0.0))
                raw_rotation_window_motion = float(raw_rotation_window.get("max_field_motion", 0.0))
                joint_window = ((result.get("joint_position") or {}).get("slow_window") or {})
                joint_max_motion = float(joint_window.get("max_field_motion", 0.0))
                lines.append(
                    f"| `{episode_number(episode_id)}` | `{episode_id}` | "
                    f"{start_s:.2f}-{end_s:.2f}s | "
                    f"{result['longest_slow_segment_s']:.2f}s | "
                    f"{avg_speed:.2f}cm/s | {path_cm:.2f}cm | "
                    f"{gripper_window_motion:.3f} | {raw_rotation_window_motion:.2f} | "
                    f"{joint_max_motion:.2f} | "
                    f"`{result.get('flag_reason', 'unknown')}` | "
                    f"`{result['primary_side']}` |"
                )
            lines.append("")
    suppressed_results = [result for result in results if result.get("suppressed_by_gripper_activity")]
    lines.extend(["", "## EEF Slow Candidates Suppressed By Gripper", ""])
    if not suppressed_results:
        lines.append("No EEF slow candidates were suppressed by gripper activity.")
    else:
        by_task = {}
        for result in suppressed_results:
            by_task.setdefault(str(result["task_key"]), []).append(result)
        for task_key in sorted(by_task):
            task_results = sorted(by_task[task_key], key=lambda item: episode_sort_key(str(item["episode_path"])))
            parent = common_episode_parent([str(item["episode_nas_path"]) for item in task_results])
            lines.extend([
                f"### `{task_key}`",
                "",
                f"- NAS directory: `{parent}`",
                "",
                "| Episode # | Episode id | EEF slow window | Duration | Avg speed | Gripper bouts/min | Gripper active |",
                "| ---: | --- | --- | ---: | ---: | ---: | ---: |",
            ])
            for result in task_results:
                episode_id = str(result["episode_id"])
                segment = result.get("combined_longest_slow_segment") or {}
                gripper = result.get("gripper") or {}
                lines.append(
                    f"| `{episode_number(episode_id)}` | `{episode_id}` | "
                    f"{float(segment.get('start_s', 0.0)):.2f}-{float(segment.get('end_s', 0.0)):.2f}s | "
                    f"{float(result.get('longest_slow_segment_s', 0.0)):.2f}s | "
                    f"{float(segment.get('avg_speed_cm_s', 0.0)):.2f}cm/s | "
                    f"{float(gripper.get('movement_bouts_per_min', 0.0)):.2f} | "
                    f"{float(gripper.get('active_fraction', 0.0)):.3f} |"
                )
            lines.append("")
    dropper_suppressed_results = [result for result in results if result.get("suppressed_by_dropper_relaxed_mode")]
    lines.extend(["", "## EEF Slow Candidates Suppressed By Dropper Relaxed Mode", ""])
    if not dropper_suppressed_results:
        lines.append("No EEF slow candidates were suppressed by dropper relaxed mode.")
    else:
        by_task = {}
        for result in dropper_suppressed_results:
            by_task.setdefault(str(result["task_key"]), []).append(result)
        for task_key in sorted(by_task):
            task_results = sorted(by_task[task_key], key=lambda item: episode_sort_key(str(item["episode_path"])))
            parent = common_episode_parent([str(item["episode_nas_path"]) for item in task_results])
            lines.extend([
                f"### `{task_key}`",
                "",
                f"- NAS directory: `{parent}`",
                "",
                "| Episode # | Episode id | EEF slow window | Duration | Avg speed | Gripper bouts/min | Gripper active |",
                "| ---: | --- | --- | ---: | ---: | ---: | ---: |",
            ])
            for result in task_results:
                episode_id = str(result["episode_id"])
                segment = result.get("combined_longest_slow_segment") or {}
                gripper = result.get("gripper") or {}
                lines.append(
                    f"| `{episode_number(episode_id)}` | `{episode_id}` | "
                    f"{float(segment.get('start_s', 0.0)):.2f}-{float(segment.get('end_s', 0.0)):.2f}s | "
                    f"{float(result.get('longest_slow_segment_s', 0.0)):.2f}s | "
                    f"{float(segment.get('avg_speed_cm_s', 0.0)):.2f}cm/s | "
                    f"{float(gripper.get('movement_bouts_per_min', 0.0)):.2f} | "
                    f"{float(gripper.get('active_fraction', 0.0)):.3f} |"
                )
            lines.append("")
    rotation_suppressed_results = [result for result in results if result.get("suppressed_by_rotation_activity")]
    lines.extend(["", "## EEF Slow Candidates Suppressed By Rotation Activity", ""])
    if not rotation_suppressed_results:
        lines.append("No EEF slow candidates were suppressed by rotation activity.")
    else:
        by_task = {}
        for result in rotation_suppressed_results:
            by_task.setdefault(str(result["task_key"]), []).append(result)
        for task_key in sorted(by_task):
            task_results = sorted(by_task[task_key], key=lambda item: episode_sort_key(str(item["episode_path"])))
            parent = common_episode_parent([str(item["episode_nas_path"]) for item in task_results])
            lines.extend([
                f"### `{task_key}`",
                "",
                f"- NAS directory: `{parent}`",
                "",
                "| Episode # | Episode id | EEF slow window | Duration | Avg speed | Joint field | Joint motion | Joint avg speed | Reason |",
                "| ---: | --- | --- | ---: | ---: | --- | ---: | ---: | --- |",
            ])
            for result in task_results:
                episode_id = str(result["episode_id"])
                segment = result.get("combined_longest_slow_segment") or {}
                joint_window = ((result.get("joint_position") or {}).get("slow_window") or {})
                lines.append(
                    f"| `{episode_number(episode_id)}` | `{episode_id}` | "
                    f"{float(segment.get('start_s', 0.0)):.2f}-{float(segment.get('end_s', 0.0)):.2f}s | "
                    f"{float(result.get('longest_slow_segment_s', 0.0)):.2f}s | "
                    f"{float(segment.get('avg_speed_cm_s', 0.0)):.2f}cm/s | "
                    f"`{joint_window.get('max_field', '')}` | "
                    f"{float(joint_window.get('max_field_motion', 0.0)):.2f} | "
                    f"{float(joint_window.get('max_field_avg_speed', 0.0)):.3f} | "
                    f"`{result.get('flag_reason', 'unknown')}` |"
                )
            lines.append("")
    error_results = [result for result in results if result.get("error")]
    lines.extend(["", "## Error Episodes", ""])
    if not error_results:
        lines.append("No episode analysis errors.")
    else:
        lines.extend([
            "| Task key | Episode # | Error | NAS path |",
            "| --- | ---: | --- | --- |",
        ])
        for result in error_results:
            episode_id = str(result.get("episode_id", ""))
            error_text = str(result.get("error", "")).replace("|", "\\|")
            lines.append(
                f"| `{result.get('task_key', '')}` | `{episode_number(episode_id)}` | "
                f"`{error_text}` | `{result.get('episode_nas_path', '')}` |"
            )
    lines.extend(["", "## Slow Episode Full Paths", ""])
    if not slow_results:
        lines.append("No slow episode paths.")
    else:
        lines.extend([
            "| Task key | Episode # | Slow window | NAS path |",
            "| --- | ---: | --- | --- |",
        ])
        for result in slow_results:
            episode_id = str(result["episode_id"])
            segment = result.get("combined_longest_slow_segment") or {}
            start_s = float(segment.get("start_s", 0.0))
            end_s = float(segment.get("end_s", 0.0))
            lines.append(
                f"| `{result['task_key']}` | `{episode_number(episode_id)}` | "
                f"{start_s:.2f}-{end_s:.2f}s | "
                f"`{result['episode_nas_path']}` |"
            )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze slow movement episodes on Synology NAS.")
    parser.add_argument("--dsm-url", default=DEFAULT_DSM_URL)
    parser.add_argument("--verified-root", default=DEFAULT_VERIFIED_ROOT)
    parser.add_argument("--account", help="DSM account; password is read from DSM_PASSWORD or prompted")
    parser.add_argument("--non-interactive", action="store_true", help="do not prompt; require env credentials")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--task-key", action="append", default=[], help="task key to analyze; repeat or comma-separate")
    parser.add_argument(
        "--root-path",
        action="append",
        default=[],
        help=(
            "specific NAS directory to scan for episodes; repeat for multiple directories. "
            "When set, task discovery under --verified-root is skipped."
        ),
    )
    parser.add_argument("--max-task-keys", type=int, default=0, help="maximum task keys to analyze; 0 means all discovered")
    parser.add_argument("--max-episodes-per-task", type=int, default=20, help="0 means all discovered episodes")
    parser.add_argument(
        "--max-discovered-episodes-per-task",
        type=int,
        default=200,
        help="stop episode discovery after this many candidates per task; 0 means no limit",
    )
    parser.add_argument(
        "--episode-sampling",
        choices=("even", "latest", "earliest"),
        default="even",
        help="how to choose episodes when a task has more than --max-episodes-per-task",
    )
    parser.add_argument("--max-depth", type=int, default=5, help="directory search depth below each task key")
    parser.add_argument("--date-from", help="only analyze episodes on/after YYYYMMDD")
    parser.add_argument("--date-to", help="only analyze episodes on/before YYYYMMDD")
    parser.add_argument("--discover-progress-interval", type=int, default=25, help="print progress every N scanned directories while discovering episodes; 0 disables")
    parser.add_argument("--output-dir", type=Path, default=Path("nas_slow_analysis"))
    parser.add_argument("--window-s", type=float, default=1.0)
    parser.add_argument("--max-speed-cm-s", type=float, default=3.0)
    parser.add_argument("--merge-gap-s", type=float, default=2.0)
    parser.add_argument("--min-duration-s", type=float, default=10.0)
    parser.add_argument(
        "--gripper-activity-gate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "when enabled, suppress an EEF slow candidate if gripper motion is clearly active; "
            "sustained one-direction gripper motion can still flag the episode"
        ),
    )
    parser.add_argument("--gripper-window-min-motion", type=float, default=0.03)
    parser.add_argument("--gripper-window-min-avg-speed", type=float, default=0.002)
    parser.add_argument("--gripper-slow-min-duration-s", type=float, default=15.0)
    parser.add_argument(
        "--dropper-relaxed-mode",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="use stricter slow criteria for task keys containing 'dropper'",
    )
    parser.add_argument("--dropper-extreme-duration-s", type=float, default=45.0)
    parser.add_argument("--dropper-extreme-max-speed-cm-s", type=float, default=1.0)
    parser.add_argument(
        "--raw-rotation-gate-task-pattern",
        action="append",
        default=["rice", "pour", "tea", "cup", "water"],
        help="task-key substring for which raw gripper rotation in the slow window can suppress EEF slow",
    )
    parser.add_argument("--raw-rotation-window-min-motion", type=float, default=1.0)
    parser.add_argument("--raw-rotation-window-min-avg-speed", type=float, default=0.07)
    parser.add_argument("--joint-rotation-min-window-motion", type=float, default=1.5)
    parser.add_argument("--joint-rotation-min-avg-speed", type=float, default=0.1)
    parser.add_argument(
        "--disable-dropper-relaxed-task-key",
        action="append",
        default=[],
        help="dropper task key for which relaxed mode should not be applied",
    )
    parser.add_argument(
        "--disable-gripper-gate-task-key",
        action="append",
        default=[],
        help="task key for which EEF slow candidates should not be downgraded by gripper activity",
    )
    parser.add_argument(
        "--flag-mode",
        choices=("all-hands-slow", "primary"),
        default="all-hands-slow",
        help=(
            "all-hands-slow flags only when both hands are slow at the same time; "
            "primary preserves the older primary-hand-only behavior"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="discover episodes but do not download/analyze CSV")
    parser.add_argument("--no-incremental-output", action="store_true", help="only write report files at the end")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_episodes_per_task < 0:
        raise SystemExit("--max-episodes-per-task must be >= 0")
    if args.max_task_keys < 0:
        raise SystemExit("--max-task-keys must be >= 0")
    for option_name in ("date_from", "date_to"):
        value = getattr(args, option_name)
        if value and not re.fullmatch(r"20\d{6}", value):
            raise SystemExit(f"--{option_name.replace('_', '-')} must use YYYYMMDD format")
    if args.date_from and args.date_to and args.date_from > args.date_to:
        raise SystemExit("--date-from must be <= --date-to")
    args.disable_gripper_gate_task_key = parse_task_keys(args.disable_gripper_gate_task_key) or set()
    args.disable_dropper_relaxed_task_key = parse_task_keys(args.disable_dropper_relaxed_task_key) or set()
    args.raw_rotation_gate_task_pattern = parse_patterns(args.raw_rotation_gate_task_pattern)

    task_filter = parse_task_keys(args.task_key)
    client = login_with_fallback(args)
    results: list[dict[str, Any]] = []
    task_summaries: list[dict[str, Any]] = []
    try:
        explicit_task_dirs = {normalize_cli_path(path) for path in args.root_path}
        task_dirs = sorted(explicit_task_dirs)
        discovered_task_dirs: list[str] = []
        if task_filter:
            discovered_task_dirs = discover_task_dirs(client, args.verified_root, task_filter)
        elif not task_dirs:
            discovered_task_dirs = discover_task_dirs(client, args.verified_root, None)
        seen_task_dirs = set(task_dirs)
        for task_dir in discovered_task_dirs:
            if task_dir not in seen_task_dirs:
                task_dirs.append(task_dir)
                seen_task_dirs.add(task_dir)
        print(f"[discover] task roots selected: {len(task_dirs)}")
        analyzed_task_count = 0
        for task_index, task_dir in enumerate(task_dirs, 1):
            if args.max_task_keys and analyzed_task_count >= args.max_task_keys:
                break
            task_key = task_key_from_path(task_dir, args.verified_root)
            print(f"[task {task_index}/{len(task_dirs)}] {task_key}")
            episode_dirs = find_episode_dirs(
                client,
                task_dir,
                args.max_episodes_per_task,
                args.max_depth,
                args.episode_sampling,
                args.discover_progress_interval,
                args.max_discovered_episodes_per_task,
                args.date_from,
                args.date_to,
            )
            print(
                f"[task] {task_key}: episodes selected={len(episode_dirs)} "
                f"(sampling={args.episode_sampling})"
            )
            if not episode_dirs and task_dir not in explicit_task_dirs:
                print(f"[task] {task_key}: skip empty task for --max-task-keys count")
                continue
            analyzed_task_count += 1
            task_results: list[dict[str, Any]] = []
            if not args.dry_run:
                for episode_index, episode_path in enumerate(episode_dirs, 1):
                    print(f"[episode {episode_index}/{len(episode_dirs)}] {episode_path}")
                    try:
                        result = analyze_remote_episode(client, args, task_key, episode_path)
                    except Exception as exc:  # noqa: BLE001 - keep batch running
                        result = {
                            "task_key": task_key,
                            "episode_id": PurePosixPath(episode_path).name,
                            "episode_path": episode_path,
                            "episode_nas_path": volume1_path(episode_path),
                            "flagged": False,
                            "error": str(exc),
                        }
                    task_results.append(result)
                    results.append(result)
            task_summaries.append(summarize_task(task_key, episode_dirs, task_results, args.dry_run))
            refresh_outputs(args, results, task_summaries)
        write_outputs(args, results, task_summaries)
    finally:
        client.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
