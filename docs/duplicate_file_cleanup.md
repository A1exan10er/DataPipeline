# Duplicate File Cleanup

Date: 2026-07-22

This note records the cleanup of local untracked duplicate files whose names ended in ` 2`.

## Removed files

- `QA_Pipeline/configs/report_rule_explanations_zh 2.json`
- `QA_Pipeline/configs/work_session_report 2.json`
- `QA_Pipeline/scripts/generate_work_session_report 2.py`
- `QA_Pipeline/scripts/pipeline/qa_dcs_notifier 2.py`
- `QA_Pipeline/tests/test_work_session_device_summary 2.py`
- `dcp-sdk/demo 2.py`
- `dcp-sdk/tests/test_event_listener 2.py`

## Comparison result

Five duplicate files were byte-for-byte identical to their canonical counterparts:

- `QA_Pipeline/configs/report_rule_explanations_zh 2.json`
- `QA_Pipeline/configs/work_session_report 2.json`
- `QA_Pipeline/scripts/pipeline/qa_dcs_notifier 2.py`
- `dcp-sdk/demo 2.py`
- `dcp-sdk/tests/test_event_listener 2.py`

Two duplicate files had only small differences:

- `QA_Pipeline/scripts/generate_work_session_report 2.py`
  - Limited the rendered device failure list to the first 10 devices.
  - Added one summary line for remaining devices.
- `QA_Pipeline/tests/test_work_session_device_summary 2.py`
  - Updated the related assertion to expect the capped 10-device behavior.

The canonical tracked files keep the current repository behavior, where the main device summary includes every collector rather than hiding entries after the first 10. The duplicate files were therefore treated as local stale copies and removed.
