# QA Pipeline Design Decisions

This document records every design decision, threshold choice, and logic change
made during pipeline development. It serves as the source of truth for why each
rule exists and what it filters.

Last updated: 2026-06-04

---

## Architecture

### State storage: SQLite

**Decision:** Use a single SQLite file (`outputs/qa_pipeline.db`) as intermediate
state storage instead of per-phase CSV files or per-episode JSON files.

**Reason:** 4000+ episodes across multiple phases. CSV chain (phase N reads phase
N-1 output) is brittle and loses detail. Per-episode JSON requires scanning
thousands of files on resume. SQLite gives atomic writes, SQL queries for
aggregation, and single-file portability.

**Tables:** `episodes` (one row per episode, JSON-serialized phase_status and
metrics) and `findings` (one row per finding, all phases combined).

### Phase connection: linear with early exit

**Decision:** Phases run in order 1→2→3→4→5→6→7. Episodes with
`phase_status[1] == "fail"` are skipped by all subsequent phases.

**Reason:** Phase 1 failures mean metadata or files are unreadable. Later phases
cannot run meaningfully without readable metadata.

### Resume: skip completed phases

**Decision:** Each phase checks `phases_completed` before processing an episode.
If the phase number is already in the list, it is skipped.

**Reason:** Phase 4 (video) and Phase 5 (robot state) are expensive. A pipeline
interrupted halfway through should resume from the last incomplete episode, not
restart from scratch.

### SQL safety

**Decision:** All SQL uses `?` parameterized placeholders. No f-string SQL anywhere.

**Reason:** Input comes from file paths and metadata fields. While injection risk
is low (no external input), parameterized queries are required as a code standard.

---

## Status and severity values

| Status | Meaning |
|---|---|
| `pass` | No critical or major issues |
| `warning` | Minor quality issues, data is usable |
| `needs_review` | Statistically anomalous or uncertain, human judgment required |
| `fail` | Critical or multiple major issues, data should not be used for training |

| Severity | Meaning |
|---|---|
| `critical` | File missing, unreadable, or fundamentally broken |
| `major` | Physically impossible values, severe data corruption |
| `minor` | Slight deviation from expected, non-blocking |
| `info` | Statistical context, does not affect status |

---

## Phase 1 — Structure and metadata checks

### Episode discovery

**Decision:** `discover_episodes` includes any directory whose name starts with
`episode_`, regardless of whether `metadata.json` exists.

**Reason (2026-06-04):** Original design required `metadata.json` to exist for
discovery. This silently ignored episodes with missing metadata. Changed so that
Phase 1 can detect and report missing metadata as a `critical/fail` finding.

### actions modality mapping

**Decision:** When `metadata["modalities"]` contains the bare key `"actions"`,
look for `action.*` and `actions.*` subdirectories instead of a folder literally
named `actions/`.

**Reason (2026-06-04):** Real dataset uses `"actions"` as an aggregate key in
metadata, but actual directories are named `actions.joint_position`,
`action.eef_pose`, etc. Direct name match always failed.

---

## Phase 2 — Duration and count consistency

### IQR grouping

**Decision:** Group episodes by `task_key` for duration outlier detection.
Require at least 5 episodes in a group before computing IQR statistics.

**Reason:** Smaller groups produce unstable IQR estimates. Fall back to no
outlier check for small groups rather than producing unreliable results.

### IQR threshold

**Decision:** Flag episodes whose duration deviates more than 3.0 IQR from the
group median as `needs_review`.

**Reason:** 3.0 IQR is a standard conservative outlier threshold. At this
setting, roughly 0.3% of normally distributed data would be flagged. The intent
is to catch genuine anomalies, not normal variation.

### Absolute duration thresholds (added 2026-06-04)

**Decision:** Add absolute duration limits relative to task median, in addition
to IQR method.

**Reason:** For this dataset, median=74.9s, IQR=31.3s. The IQR lower bound is
negative (74.9 - 3×31.3 = -18.9s), so episodes as short as 2.2s were not
flagged. These are almost certainly interrupted recordings or test captures with
no training value.

**Rules:**
- `duration < task_median × 0.20` → `major/fail`
  (e.g. < 15s when median is 74.9s)
- `duration < task_median × 0.40` → `minor/needs_review`
  (e.g. < 30s when median is 74.9s)
- `duration > task_median × 2.50` → `minor/needs_review`
  (e.g. > 187s when median is 74.9s; likely forgot to stop recording)

These ratios are configurable and intended to be tuned per task as more data
is collected.

---

## Phase 3 — Timestamp synchronization

### Primary timestamp source

**Decision:** Use `timestamps.csv` as primary source for image modalities, not
`timestamps_raw.csv`.

**Reason:** `timestamps.csv` is the processed version aligned with the exported
`video.mp4`. `timestamps_raw.csv` is the pre-processed capture-side record.
QA and training should use the aligned version.

### is_new=0 handling

**Decision:** For image modalities, filter to `is_new=1` rows only for monotonic
and duplicate checks. Do not flag `is_new=0` rows as duplicates.

**Reason:** `is_new=0` rows are padding frames (repeated from previous frame to
maintain continuous timeline). These are expected and normal. Only `is_new=1`
rows represent real new captures.

### timestamps_raw consistency threshold

**Decision:** Flag raw vs processed row count difference only when difference > 2.

**Reason (2026-06-04):** Observed that almost all episodes have exactly 1 row
difference (raw has one extra row at the end, trimmed to align with video).
Original 5% threshold was too loose for short episodes and too strict for
long ones. Fixed threshold of 2 rows is more meaningful.

### Frame drop check: metadata-first

**Decision:** Use `frame_integrity` fields from `metadata.json` for drop
statistics instead of scanning `timestamps.csv`.

**Reason:** `frame_integrity[modality]["total_drops"]` and
`max_consecutive_drops` are pre-computed and exactly match what scanning
`timestamps.csv` would produce. Reading metadata is O(1) vs O(n) for CSV scan.

### Frame drop thresholds

**Decision:**
- RGB-like video `total_drops / frame_count > 0.10` → `major/fail`
- tactile video `total_drops / frame_count > 0.15` → `major/fail`
- No warning tier for drop ratio; values within threshold pass.

**Reason (updated 2026-06-16):** RGB-like videos allow up to 10% frame drops,
and tactile videos allow up to 15%. Above this the episode is considered
unusable for training.

### Consecutive drop threshold: statistical (IQR)

**Decision:** Use `median + 3.0 × IQR` within `task + robot` group to flag
episodes with abnormally high `max_consecutive_drops`.

**Reason:** A fixed threshold (e.g. "30 consecutive drops = fail") does not
adapt to different tasks or hardware. Some camera setups have inherently more
jitter. IQR-based detection flags episodes that are anomalous relative to their
peer group, not relative to an arbitrary global constant.

**Fallback:** When group size < 5, use fixed threshold of `max_consecutive >= 10`
as `minor/warning`.

### Large gap severity

**Decision:** Two-tier gap severity:
- `gap > 5× median interval` → `minor/warning`
- `gap > 20× median interval` → `major/needs_review`

**Reason (2026-06-04):** Original single threshold of 5× produced `major/fail`
for episodes with a single brief gap, which is common in real-robot data.
A single gap does not make data unusable. Only gaps of 20× or more (roughly
660ms at 30fps) indicate a serious capture problem.

### Duplicate and monotonic severity

**Decision:** Ratio-based severity for both checks:
- ratio < 1% → `minor/warning`
- ratio 1–5% → `major/needs_review`
- ratio >= 5% → `major/fail`

**Reason (2026-06-04):** 2 duplicate timestamps in 4000 rows (0.05%) is a
known timestamp write error in the capture system, not a sign of unusable data.
Fixed `major/fail` for any duplicate was too aggressive.

### Cross-modality alignment threshold

**Decision:** Flag start or end time spread across modalities > 500ms.

**Reason:** At 30fps, 500ms = 15 frames. A spread larger than this indicates
that one modality started or stopped significantly earlier/later than others,
which breaks temporal alignment between visual and state data.

### Grouping for group-level checks

**Decision:** Group by `task + robot` for frequency and consecutive drop outlier
detection.

**Reason:** Different robots have different hardware characteristics affecting
sampling stability. Mixing robot types in the same group would distort the
baseline.

---

## Phase 4 — Video health checks

### Sampling strategy

**Decision:** Sample 8 frames per video: first, last, and 6 evenly distributed
positions between 15% and 90% of total frames.

**Reason:** Full decode is too slow for 263+ episodes × 7+ cameras. 8 frames
covers start, end, and middle content with acceptable coverage for detecting
systematic issues (frozen video, all-black recording).

### Resolution mismatch: letterbox exception

**Decision:** If actual width matches expected width and actual height is larger
than expected height, skip the resolution mismatch finding.

**Reason (2026-06-04):** Some capture platforms store 640×360 content in a
640×480 container with black bars top and bottom. This is intentional formatting,
not a quality issue. 932 false positive warnings were eliminated by this rule.

### Black/white frame thresholds

**Decision:**
- Sampled frame mean < 5.0 → black frame
- Sampled frame mean > 250.0 → white frame
- All consecutive sampled frame pairs frozen (diff < 1.0) → `major/fail`
- Any black/white frames → `major/needs_review`
- More than half of sampled frames black/white → `critical/fail`

### Flow modalities excluded

**Decision:** Do not check `observation.image.flow_*` videos.

**Reason:** Flow videos are derived streams, not raw camera captures. Their
content characteristics differ fundamentally from camera videos. Quality issues
in flow videos are downstream of the source camera quality.

---

## Phase 5 — Robot state reasonableness (planned)

### Robot config registry

**Decision:** Define thresholds in a `ROBOT_CONFIGS` dict keyed by robot name,
with a fallback to arx5 defaults.

**Reason:** Different robots have different joint counts, limits, and velocity
characteristics. Hard-coding thresholds would require code changes to support
new robots; the registry pattern requires only adding a new dict entry.

### Velocity estimation

**Decision:** If `observation.state.joint_velocity` exists, use it directly.
Otherwise estimate from position differences and `dt` from timestamps.

**Reason:** Estimated velocity is noisier than measured velocity. Using
`needs_review` instead of `fail` for estimated velocity violations accounts
for this noise.

### Jitter check: moving average

**Decision:** Use simple moving average (window=5) for smoothing, not
Savitzky-Golay or other advanced filters.

**Reason:** Avoids `scipy` dependency. Moving average is sufficient for
detecting high-frequency noise in joint trajectories at this stage.

---

## Output format

### quality_report.csv

One row per episode. `reasons` field contains semicolon-separated unique
`check_name` values from non-pass findings. Does not repeat check names
even if multiple modalities triggered the same check.

### quality_findings.jsonl

One line per finding. Full detail including modality, phase, and numeric
context. Used for debugging and human review.

### quality_summary.md

Human-readable rollup. Flagged episodes section clipped to half of total
episode count to keep the file readable when many episodes fail.
