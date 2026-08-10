# Standalone Episode Duration Anomaly Check

`Werkzeuge/check_unusual_episode_durations.py` identifies unusually long and
short finalized episodes without running the QA pipeline or event listener.
It never writes below the input root.

## Detection scope

- Only episodes whose label list is exactly `["完全正常"]` are indexed as
  baseline candidates. Episodes with another label are not reported.
- Baselines are calculated per task across the dates and operators currently
  present below the scan root.
- A task needs at least 30 positive durations before findings are generated.
- The detector uses a robust log-duration median/MAD/IQR profile. Long results
  are ranked before short results, then by normalized distance from the task
  median.
- Findings are triage hints only. They do not assign blame or change QA state.

## First and subsequent scans

From the DataPipeline repository root:

```bash
datapipeline-env/bin/python Werkzeuge/check_unusual_episode_durations.py scan \
  /mnt/nas/database/verified \
  --db outputs/duration_anomalies/duration_anomalies.sqlite \
  --full
```

Later runs can omit `--full`. The scanner still enumerates episode directories,
but unchanged metadata is reused using file size and nanosecond-mtime
fingerprints.

```bash
datapipeline-env/bin/python Werkzeuge/check_unusual_episode_durations.py scan \
  /mnt/nas/database/verified
```

Scan one task:

```bash
datapipeline-env/bin/python Werkzeuge/check_unusual_episode_durations.py scan \
  /mnt/nas/database/verified --task wipe_the_table_with_a_towel_UMI
```

## Review outputs

The SQLite file keeps current inventory, current task profiles, current
findings, and 90 days of compact run summaries. It does not duplicate all
results for every run.

Create a standalone HTML report and a complete CSV export:

```bash
datapipeline-env/bin/python Werkzeuge/check_unusual_episode_durations.py report \
  --output outputs/duration_anomalies/report.html

datapipeline-env/bin/python Werkzeuge/check_unusual_episode_durations.py export \
  --direction long \
  --output outputs/duration_anomalies/long_episodes.csv
```

The HTML report groups findings by task/date/operator and lists long episodes
first. It is generated from local SQLite and does not query NAS when opened.

Inspect the latest run without scanning:

```bash
datapipeline-env/bin/python Werkzeuge/check_unusual_episode_durations.py status
```

If one task contains unreadable or changing metadata, that task retains its
last completed results and the scan exits with code `3`. Fatal command errors
exit with code `2`.

## Scheduling and isolation

Example user-level systemd files are provided next to the script. Adjust paths,
copy them to `~/.config/systemd/user/`, remove `.example`, then enable the timer.
The service example declares the NAS root read-only.

This detector is intentionally not called by:

- `listen_episode_verified.py`;
- any QA pipeline phase;
- the DataPipeline control dashboard request handlers;
- QualityCheck.

This separation prevents single-event batches from producing unreliable task
baselines and prevents duration scans from slowing interactive dashboards.
