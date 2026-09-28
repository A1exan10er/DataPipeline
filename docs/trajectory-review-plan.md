# Trajectory review and calibration — local implementation, 2026-09-28

## Intent and boundaries

DataPipeline produces evidence. QualityCheck owns reviewer decisions, targeted notifications and calibration history. Neither detector confidence nor a calibration acknowledgement constitutes robot-execution validation. Small deviations remain reviewable records; large discontinuities have urgent priority. Notifications always require an authenticated reviewer, a current result and an unambiguous collector/device identity. Development does not enable production phases, restart services, or publish messages.

## Implementation sequence

1. Preserve all three working trees on GitHub and Gitea. The six verified snapshot references are in `trajectory-backup-20260928.json`. Deployment branches remain untouched.
2. Port the existing UMI Phase 8 detector to DataPipeline. Default small-drift sensitivity is 0.5 of the previous setting. Sensitivity controls review emphasis; observations found at the original sensitivity remain in the register, including those below the new review threshold. Keep the large-step/residual guard independent of that control; add an absolute critical-step guard. Preserve raw observations, interval timestamps, severity evidence, source hash and effective detector settings.
3. Publish atomic, versioned local result files after pipeline runs. Pin the source file inventory before checking; invalidate resumed checks when inputs or rules change. This feed is opt-in and contains no operator message publication.
4. Import the feed into QualityCheck's local index. Filter before sampling and pagination: default shows complete hard-check passes and soft review items; separate views expose hard failures, pending checks and all records. Brief issue explanations accompany episodes. Missing, changed or incomplete evidence is pending, never passed.
5. Provide a review workbench with a persistent small/large drift register, authenticated notification approval, CSV export, calibration completion records and reviewer-confirmed follow-up outcomes. A successful recalibration requires at least three distinct clean, same-device recordings made after calibration and checked under the same rules; recurrent drift is an engineering escalation, not automatic closure.
6. Regression-test detector invariants, stale input/order protection, pre-pagination gates, authentication, duplicate approvals, restart-safe delivery, and post-calibration validation. Run an isolated localhost preview using synthetic recordings and an inert notification sender.

## Sensitivity and robot validation

Small sensitivity is a triage setting, not a claim of training suitability. The provisional large guard is 60 mm per sample plus 35 mm prediction error and a contextual step ratio; an unexplained 250 mm step cannot be hidden by the contextual ratio. These are engineering starting points, not universal robot limits. Pure gradual drift, constant frame offsets and orientation-only faults need independent reference measurements or additional detectors. No detector can guarantee every physically wrong trajectory from position samples alone.

Before adopting production thresholds: label held-out normal/abnormal recordings by device and task; report large-drift recall and small-drift false-positive rate separately. Test the actual robot conversion/replay chain in simulation first, measuring end-effector error, velocity, acceleration, jerk and joint/workspace violations. A smooth preview alone does not demonstrate executable motion. Preserve the dataset, rule version and robot/controller configuration with acceptance results.

## Delivery and rollout

Default notification transport is disabled. Approval creates one idempotent durable outbox item. A separately enabled publisher uses the existing collector queue and rechecks result freshness and approval before each attempt. Broker acceptance and observed PC display are distinct states. Never infer recipients from operator names or filenames.

Local acceptance precedes any server rollout. After the user confirms: configure the shared feed path and verified source root, run shadow import and compare counts, enable the review gate, then enable Phase 8 on selected uploads. Keep notification transport disabled until a targeted PC delivery test is approved. Roll back by disabling the feed/gate and Phase 8; retain review/calibration databases and raw recordings.

## Local verification outcome

Automated checks passed: DataPipeline 168 (7 skipped), QualityCheck 97, selected DQC regressions 65, and frontend tests 4. TypeScript checking and the frontend build passed. Browser acceptance and validation scope are recorded in `evidence/local-acceptance-20260928.json`.

The localhost 1344 preview runs the actual DataPipeline CLI on six synthetic episodes. Results: 2 hard failures (missing metadata, duplicate timestamps), 2 soft review cases (large drift, pause), and 2 hard-check passes (clean, low-priority small drift). All six remain accessible; the default main queue contains four. Test-only records cannot enqueue an operator notification. Existing live services were not restarted.

Retained development examples: all 34 large/mixed drift episodes remain classified as large at sensitivity 0.25, 0.5, 1 and 2. At default 0.5, 7 of 11 small-local-jump episodes are emphasized, and all 11 remain recorded. The 6 normal references remain unflagged. These are reused development examples, not independent accuracy estimates; full results are in `evidence/trajectory-sensitivity-20260928.json`.

Robot execution acceptance must consider controller-specific velocity, acceleration and jerk constraints. See [Franka's official interface requirements](https://frankarobotics.github.io/docs/robot_specifications.html); other robot types require their own limits. No physical robot or production PC notification was tested during this change.

Usage and rollback instructions: [QualityCheck local acceptance](../../QualityCheck/docs/pipeline-trajectory-local.md).
