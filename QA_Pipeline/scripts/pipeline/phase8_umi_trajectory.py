"""UMI-only, review-only trajectory discontinuity phase."""
from datetime import datetime
import time

from .qa_config import config_value
from .qa_core import Finding, decide_status, load_metadata, save_episode_state, save_findings
from .umi_trajectory import inspect_episode

PHASE_NUMBER = 8


def run_phase(states, db_path, progress_callback=None, workers=1):
    pending = [s for s in states if PHASE_NUMBER not in s.phases_completed]
    cfg = config_value(['phase8_umi_trajectory', 'detector'], {})
    for index, state in enumerate(pending, 1):
        if not state.metadata:
            state.metadata, _ = load_metadata(state.episode_path)
        started=time.monotonic()
        result = inspect_episode(state.episode_path, state.metadata, cfg)
        result['detector_runtime_s']=time.monotonic()-started
        findings = []
        def add(name, status, message, details):
            findings.append(Finding(str(state.episode_path), 8, name,
                                    'info' if status=='pass' else 'major', status, message, details))
        if result['intervals']:
            add('umi_trajectory_suspected', 'needs_review', '疑似定位轨迹跳变，原因待人工确认', result)
        if result['status']=='incomplete':
            add('umi_trajectory_incomplete', 'needs_review', 'UMI 轨迹检查未完成，不能视为通过', result)
        elif result['status']=='not_applicable':
            add('umi_trajectory_not_applicable', 'pass', '非 UMI 设备，不适用 UMI 轨迹检查', result)
        elif not result['intervals']:
            add('umi_trajectory_clear', 'pass', '本次 UMI 轨迹检查未发现疑似跳变', result)
        state.metrics['p8_trajectory'] = result
        state.phases_completed.append(8)
        state.phase_status[8] = decide_status(findings)
        state.findings.extend(findings)
        state.last_updated = datetime.now().isoformat()
        save_episode_state(db_path, state)
        save_findings(db_path, findings, phase=8, episode_path=str(state.episode_path))
        if progress_callback: progress_callback(index, len(pending))
    return states
