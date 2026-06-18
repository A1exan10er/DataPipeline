# QA Database Query Guide

This guide uses Python's built-in SQLite support because the `sqlite3` command
line tool may not be installed on the server.

Run commands from the project root:

```bash
cd ~/DataPipeline
```

Default database used in the examples:

```text
./outputs/qa_verified/qa_pipeline.db
```

If your database is somewhere else, replace `./outputs/qa_verified/qa_pipeline.db`
in the commands.

Check that the database exists before querying:

```bash
ls -lh ./outputs/qa_verified/qa_pipeline.db
```

## Count state_csv_row_count_mismatch by Robot

Use this to check which robot types are affected by Phase 2
`state_csv_row_count_mismatch`, and which robot types have non-zero actual CSV
rows among those mismatches.

For the June 15, 2026 Phase 1-3 run, set:

```text
db = "./outputs/qa_20260615_phase1_3/qa_pipeline.db"
```

Command:

```bash
python3 - <<'PY'
import collections
import json
import sqlite3

db = "./outputs/qa_20260615_phase1_3/qa_pipeline.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

rows = con.execute("""
SELECT f.episode_path, e.robot, e.controller, f.details
FROM findings f
LEFT JOIN episodes e ON e.episode_path = f.episode_path
WHERE f.check_name = 'state_csv_row_count_mismatch'
ORDER BY f.episode_path
""").fetchall()

robot_counts = collections.Counter()
robot_episodes = collections.defaultdict(set)
nonzero_counts = collections.Counter()
nonzero_episodes = collections.defaultdict(set)

for row in rows:
    episode_path = row["episode_path"]
    robot = row["robot"] or "<blank>"
    detail = json.loads(row["details"] or "{}")

    robot_counts[robot] += 1
    robot_episodes[robot].add(episode_path)

    if int(detail.get("csv_rows", 0)) != 0:
        nonzero_counts[robot] += 1
        nonzero_episodes[robot].add(episode_path)

print("All state_csv_row_count_mismatch by robot:")
for robot, count in robot_counts.most_common():
    print(robot, "findings=", count, "episodes=", len(robot_episodes[robot]))

print("\nNon-zero actual rows by robot:")
for robot, count in nonzero_counts.most_common():
    print(robot, "findings=", count, "episodes=", len(nonzero_episodes[robot]))

con.close()
PY
```

Reference output from `outputs/qa_20260615_phase1_3/qa_pipeline.db`:

```text
All state_csv_row_count_mismatch by robot:
umi findings= 11349 episodes= 2844
arx5 findings= 4600 episodes= 1150
flexiv findings= 2896 episodes= 1448
aloha findings= 2536 episodes= 634
ur findings= 1802 episodes= 901

Non-zero actual rows by robot:
umi findings= 9 episodes= 9
```

## Important Copy Rule

For multi-line commands, the final `PY` must be at the beginning of the line.
There must be no spaces or tabs before it.

If the terminal opens a text input prompt instead of running the command, press
`Ctrl+C`, then paste the command again and make sure the last line is exactly:

```text
PY
```

## Count frequency_group_outlier by Robot

```bash
python3 - <<'PY'
import sqlite3
from collections import Counter, defaultdict

db = "./outputs/qa_verified/qa_pipeline.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

all_eps = defaultdict(set)
for ep, robot in con.execute("SELECT episode_path, robot FROM episodes"):
    all_eps[robot or "<blank>"].add(ep)

marked = defaultdict(set)
finding_counts = Counter()

for ep, robot in con.execute("""
SELECT f.episode_path, e.robot
FROM findings f
LEFT JOIN episodes e ON e.episode_path = f.episode_path
WHERE f.check_name = 'frequency_group_outlier'
"""):
    robot = robot or "<blank>"
    marked[robot].add(ep)
    finding_counts[robot] += 1

for robot, eps in sorted(marked.items(), key=lambda x: len(x[1]) / len(all_eps[x[0]]), reverse=True):
    total = len(all_eps[robot])
    count = len(eps)
    percent = count / total * 100 if total else 0.0
    print(f"{robot}: {count}/{total} episodes = {percent:.2f}% ({finding_counts[robot]} findings)")

con.close()
PY
```

Reference output from the verified server database:

```text
umi: 17693/47600 episodes = 37.17% (40576 findings)
arx5: 902/7583 episodes = 11.90% (3224 findings)
flexiv: 129/1762 episodes = 7.32% (129 findings)
franka: 4/241 episodes = 1.66% (12 findings)
ur: 33/2897 episodes = 1.14% (89 findings)
aloha: 13/2263 episodes = 0.57% (31 findings)
```

## List Episodes with frequency_group_outlier

```bash
python3 - <<'PY'
import sqlite3
import json

db = "./outputs/qa_verified/qa_pipeline.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

for row in con.execute("""
SELECT
    f.episode_path,
    e.task,
    e.date,
    e.operator,
    e.robot,
    e.final_status,
    f.details
FROM findings f
LEFT JOIN episodes e ON e.episode_path = f.episode_path
WHERE f.check_name = 'frequency_group_outlier'
ORDER BY f.episode_path
LIMIT 50
"""):
    episode_path, task, date, operator, robot, final_status, details = row
    d = json.loads(details or "{}")
    print()
    print("episode:", episode_path)
    print("task:", task, "date:", date, "operator:", operator, "robot:", robot, "status:", final_status)
    print("modality:", d.get("modality"))
    print("actual_fps:", d.get("actual_fps"))
    print("median_fps:", d.get("median_fps"))
    print("iqr:", d.get("iqr"))
    print("iqr_distance:", d.get("iqr_distance"))
    print("group:", d.get("group"))

con.close()
PY
```

## Count frequency_group_outlier by Task and Robot Group

```bash
python3 - <<'PY'
import sqlite3
import json
from collections import Counter

db = "./outputs/qa_verified/qa_pipeline.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

counts = Counter()
episodes = {}

for ep, details in con.execute("""
SELECT episode_path, details
FROM findings
WHERE check_name = 'frequency_group_outlier'
"""):
    d = json.loads(details or "{}")
    group = d.get("group", "<unknown>")
    counts[group] += 1
    episodes.setdefault(group, set()).add(ep)

for group, count in counts.most_common():
    print(f"{count} findings, {len(episodes[group])} episodes, group={group}")

con.close()
PY
```

## Count frequency_group_outlier by Robot Since June 1, 2026

The pipeline stores dates as `YYYYMMDD` strings, so June 1, 2026 and after is:

```text
20260601
```

```bash
python3 - <<'PY'
import sqlite3
from collections import Counter, defaultdict

db = "./outputs/qa_verified/qa_pipeline.db"
start_date = "20260601"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

all_eps = defaultdict(set)
for ep, robot in con.execute("""
SELECT episode_path, robot
FROM episodes
WHERE date >= ?
""", (start_date,)):
    all_eps[robot or "<blank>"].add(ep)

marked = defaultdict(set)
finding_counts = Counter()

for ep, robot in con.execute("""
SELECT f.episode_path, e.robot
FROM findings f
LEFT JOIN episodes e ON e.episode_path = f.episode_path
WHERE f.check_name = 'frequency_group_outlier'
  AND e.date >= ?
""", (start_date,)):
    robot = robot or "<blank>"
    marked[robot].add(ep)
    finding_counts[robot] += 1

for robot, eps in sorted(marked.items(), key=lambda x: len(x[1]) / len(all_eps[x[0]]), reverse=True):
    total = len(all_eps[robot])
    count = len(eps)
    percent = count / total * 100 if total else 0.0
    print(f"{robot}: {count}/{total} episodes = {percent:.2f}% ({finding_counts[robot]} findings)")

con.close()
PY
```

## List Episodes with frequency_group_outlier Since June 1, 2026

```bash
python3 - <<'PY'
import sqlite3
import json

db = "./outputs/qa_verified/qa_pipeline.db"
start_date = "20260601"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

for row in con.execute("""
SELECT
    f.episode_path,
    e.task,
    e.date,
    e.operator,
    e.robot,
    e.final_status,
    f.details
FROM findings f
LEFT JOIN episodes e ON e.episode_path = f.episode_path
WHERE f.check_name = 'frequency_group_outlier'
  AND e.date >= ?
ORDER BY e.date, e.robot, f.episode_path
LIMIT 100
""", (start_date,)):
    episode_path, task, date, operator, robot, final_status, details = row
    d = json.loads(details or "{}")
    print()
    print("episode:", episode_path)
    print("task:", task, "date:", date, "operator:", operator, "robot:", robot, "status:", final_status)
    print("modality:", d.get("modality"))
    print("actual_fps:", d.get("actual_fps"))
    print("median_fps:", d.get("median_fps"))
    print("iqr:", d.get("iqr"))
    print("iqr_distance:", d.get("iqr_distance"))
    print("group:", d.get("group"))

con.close()
PY
```

## Show Small FPS Difference Cases

This helps identify likely noisy statistical outliers where the FPS difference is
very small.

```bash
python3 - <<'PY'
import sqlite3
import json

db = "./outputs/qa_verified/qa_pipeline.db"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

rows = []
for ep, details in con.execute("""
SELECT episode_path, details
FROM findings
WHERE check_name = 'frequency_group_outlier'
"""):
    d = json.loads(details or "{}")
    actual = d.get("actual_fps")
    median = d.get("median_fps")
    if actual is None or median is None:
        continue
    d["episode_path"] = ep
    d["abs_diff"] = abs(actual - median)
    rows.append(d)

for d in sorted(rows, key=lambda x: x["abs_diff"])[:50]:
    print(
        f"diff={d['abs_diff']:.6f}",
        f"actual={d['actual_fps']:.6f}",
        f"median={d['median_fps']:.6f}",
        f"iqr={d.get('iqr', 0):.6f}",
        f"iqr_distance={d.get('iqr_distance', 0):.2f}",
        f"modality={d.get('modality')}",
        f"episode={d['episode_path']}",
    )

con.close()
PY
```

## One-Line Robot Percentage Command

Use this if multi-line paste is inconvenient.

```bash
python3 -c 'import sqlite3; from collections import Counter,defaultdict; con=sqlite3.connect("file:./outputs/qa_verified/qa_pipeline.db?mode=ro", uri=True); all_eps=defaultdict(set); marked=defaultdict(set); finding_counts=Counter(); [all_eps[r or "<blank>"].add(e) for e,r in con.execute("SELECT episode_path, robot FROM episodes")]; [marked[r or "<blank>"].add(e) or finding_counts.update([r or "<blank>"]) for e,r in con.execute("SELECT f.episode_path, e.robot FROM findings f LEFT JOIN episodes e ON e.episode_path=f.episode_path WHERE f.check_name='\''frequency_group_outlier'\''")]; [print(f"{r}: {len(es)}/{len(all_eps[r])} episodes = {len(es)/len(all_eps[r])*100:.2f}% ({finding_counts[r]} findings)") for r,es in sorted(marked.items(), key=lambda x: len(x[1])/len(all_eps[x[0]]), reverse=True)]; con.close()'
```
