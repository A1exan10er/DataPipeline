"""Position discontinuities, not operator-performance judgments.

All distances are metres and all windows are seconds. Never smooth or rewrite
the recorded source. The observation is authoritative: UMI actions can be a
one-frame-shifted copy and are not independent corroborating measurements.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import numpy as np

VERSION = 'umi-trajectory-1.3.0'
DEFAULTS = {
    'small_drift_sensitivity': .5, 'critical_step_m': .25,
    'context_s': 1.0, 'mad_multiplier': 8.0,
    'jump_residual_m': .012, 'large_step_m': .06,
    'large_residual_m': .035, 'step_context_ratio': 2.5,
    'jitter_windows_s': [.5, 1., 2.], 'jitter_path_m': .03,
    'jitter_extent_m': .10, 'jitter_path_extent_ratio': 3.5,
    'jitter_roughness': 1.15, 'jitter_residual95_m': .002,
    'jitter_peak_residual_m': .006, 'jitter_min_duration_s': .3, 'merge_gap_s': .15,
    'gap_limit_s': .25, 'min_samples': 8,
}
SOURCES = ('observation.state.eef_pose/data_raw.csv', 'observation/state/eef_pose/data_raw.csv', 'observation.state.eef_pose/data.csv', 'observation/state/eef_pose/data.csv')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def identity(metadata, episode_name=''):
    """Cross-system identity, independent of NAS paths and post-record labels."""
    return {
        'collector_id': str(metadata.get('collector_id') or ''),
        'device_id': str(metadata.get('collector_device_id') or ''),
        'recording_session_id': str(metadata.get('recording_session_id') or metadata.get('session_id') or ''),
        'episode_id': str(metadata.get('episode_id') or episode_name),
    }


def settings(overrides=None):
    cfg = {**DEFAULTS, **(overrides or {})}
    if set(cfg) != set(DEFAULTS):
        raise ValueError('unknown trajectory configuration key')
    for key, value in cfg.items():
        values = value if isinstance(value, list) else [value]
        if not values or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v) or v <= 0 for v in values):
            raise ValueError('invalid trajectory setting: ' + key)
    if cfg['min_samples'] < 3 or int(cfg['min_samples']) != cfg['min_samples']:
        raise ValueError('min_samples must be an integer >= 3')
    if not .25 <= cfg['small_drift_sensitivity'] <= 2:
        raise ValueError('small_drift_sensitivity must be between 0.25 and 2')
    if cfg['large_residual_m'] >= cfg['large_step_m'] or cfg['critical_step_m'] < cfg['large_step_m']:
        raise ValueError('large jump thresholds are inconsistent')
    return cfg


def _rolling(values, width):
    width = max(3, min(len(values) | 1, int(width) | 1, 301))
    pad = width // 2
    return np.lib.stride_tricks.sliding_window_view(np.pad(values, (pad, pad), mode='reflect'), width)


def _side(t, p, cfg):
    dt = np.diff(t)
    delta = np.diff(p, axis=0)
    step = np.linalg.norm(delta, axis=1)
    velocity = delta / dt[:, None]
    # Two-sided prediction error: smooth fast motion has low acceleration;
    # a spike/offset has an unexplained velocity change at its boundary.
    innovation = np.linalg.norm(velocity[1:] - velocity[:-1], axis=1) * np.minimum(dt[1:], dt[:-1])
    residual = np.maximum(np.r_[0.,innovation], np.r_[innovation,0.])
    width = int(cfg['context_s'] / np.median(dt))
    context = _rolling(residual, width)
    median = np.median(context, axis=1)
    mad = 1.4826 * np.median(np.abs(context - median[:, None]), axis=1)
    local_step = np.median(_rolling(step, width), axis=1)
    sensitivity = cfg['small_drift_sensitivity']
    unexpected = residual > np.maximum(cfg['jump_residual_m'] / sensitivity, median + cfg['mad_multiplier'] * np.maximum(mad, .0005))
    large = (step > cfg['large_step_m']) & (residual > cfg['large_residual_m']) & (step > cfg['step_context_ratio'] * np.maximum(local_step, .001))
    # Absolute emergency floor cannot be masked by noisy surrounding motion.
    large |= (step >= cfg['critical_step_m']) & (residual >= cfg['large_residual_m'])
    jump = unexpected & (step > cfg['step_context_ratio'] * np.maximum(local_step, .001)) | large
    large_indices = set(map(int, np.flatnonzero(large & jump)))
    events = [(max(0, int(i)-1), min(len(t)-1, int(i)+2), 'position_jump') for i in np.flatnonzero(jump)]
    # Evaluate multi-scale jitter on a time grid only inside a continuous span.
    # This avoids making fixed-frame thresholds depend on source FPS. The raw
    # samples above remain the evidence for instantaneous jumps.
    grid = np.arange(t[0], t[-1] + 1e-8, 1/30)
    if len(grid) >= 8:
        q = np.column_stack([np.interp(grid, t, p[:, k]) for k in range(3)])
        d = np.linalg.norm(np.diff(q, axis=0), axis=1)
        a = np.linalg.norm(np.diff(q, n=2, axis=0), axis=1)
        for seconds in cfg['jitter_windows_s']:
            size = max(8, int(round(seconds * 30)))
            if len(q) < size: continue
            stride = 3
            windows = np.lib.stride_tricks.sliding_window_view(q, size, axis=0)[::stride]
            paths = np.lib.stride_tricks.sliding_window_view(d, size-1)[::stride].sum(axis=1)
            accelerations = np.lib.stride_tricks.sliding_window_view(a, size-2)[::stride]
            extent = np.linalg.norm(np.ptp(windows, axis=2), axis=1)
            roughness = accelerations.sum(axis=1) / np.maximum(paths, 1e-9)
            residual95 = np.percentile(accelerations, 95, axis=1)
            hits = (paths >= cfg['jitter_path_m']) & (extent <= cfg['jitter_extent_m']) & (paths >= cfg['jitter_path_extent_ratio'] * np.maximum(extent, .001)) & (roughness >= cfg['jitter_roughness']) & (residual95 >= cfg['jitter_residual95_m'] / sensitivity)
            for j in np.flatnonzero(hits):
                start, end = grid[j*stride], grid[j*stride+size-1]
                lo, hi = int(np.searchsorted(t, start)), min(len(t)-1, int(np.searchsorted(t, end)))
                if residual[max(0,lo-1):max(1,hi)].max() >= cfg['jitter_peak_residual_m'] / sensitivity:
                    events.append((lo, hi, 'local_jitter'))
    merged = []
    for start, end, kind in sorted(events):
        if merged and t[start] - t[merged[-1][1]] <= cfg['merge_gap_s']:
            merged[-1][1] = max(end, merged[-1][1]); merged[-1][2].add(kind)
        else: merged.append([start, end, {kind}])
    output = []
    for start, end, kinds in merged:
        if kinds == {'local_jitter'} and t[end]-t[start] < cfg['jitter_min_duration_s']: continue
        lo, hi = max(0, start-1), max(1, end)
        excursion = float(np.linalg.norm(p[start:end+1]-p[start],axis=1).max())
        returned = float(np.linalg.norm(p[end]-p[start]))
        if t[end]-t[start] <= 1 and excursion >= cfg['jump_residual_m'] and returned < .4*excursion:
            kinds.add('brief_excursion_return')
        # Severity belongs to constituent detected steps, never interval travel.
        hits = [i for i in range(max(0,start), min(len(step),end)) if i in large_indices]
        severity = 'large' if hits else 'small_or_uncertain'
        evidence = [{'timestamp_ms':round(float(t[i+1]*1000),3), 'step_m':round(float(step[i]),6),
                     'prediction_error_m':round(float(residual[i]),6), 'surrounding_step_m':round(float(local_step[i]),6),
                     'step_ratio':round(float(step[i]/max(local_step[i],.001)),3)} for i in hits]
        preview_eligible = severity != 'large' and ('local_jitter' in kinds or 'brief_excursion_return' in kinds) and returned <= max(.003,.4*excursion)
        output.append({'severity':severity, 'severity_evidence':evidence, 'preview_eligible':preview_eligible, 'peak_excursion_m':round(excursion,6), 'end_offset_m':round(returned,6), 'start_ms': round(float(t[start]*1000), 3), 'end_ms': round(float(t[end]*1000), 3),
                       'kinds': sorted(kinds), 'max_step_m': round(float(step[lo:hi].max()), 6),
                       'max_prediction_error_m': round(float(residual[lo:hi].max()), 6),
                       'max_speed_m_s': round(float(np.linalg.norm(velocity[lo:hi], axis=1).max()), 6)})
    return output


def analyze(timestamps_ms, positions, config=None):
    cfg = settings(config)
    result = {'schema_version': 2, 'detector_version': VERSION, 'rule_version': fingerprint([VERSION, cfg]),
              'status': 'clear', 'intervals': [], 'issues': [], 'config': cfg,
              'meaning': 'suspected tracking discontinuity; cause unconfirmed'}
    t = np.asarray(timestamps_ms, dtype=float) / 1000
    if t.ndim != 1 or len(t) < cfg['min_samples'] or not np.isfinite(t).all() or t[0] < 0 or np.any(np.diff(t) <= 0) or len(t)>30_000 or t[-1]-t[0]>3600:
        result.update(status='incomplete', issues=['invalid_or_insufficient_timestamps']); return result
    dt = np.diff(t)
    gaps = np.flatnonzero(dt > max(cfg['gap_limit_s'], 4*np.median(dt)))
    if len(gaps): result['issues'].append('timestamp_gaps')
    bounds = np.r_[0, gaps+1, len(t)]
    for side in ('left', 'right'):
        p = np.asarray(positions.get(side, []), dtype=float)
        if p.shape != (len(t), 3) or not np.isfinite(p).all():
            result['issues'].append(side + '_pose_invalid'); continue
        for a, b in zip(bounds[:-1], bounds[1:]):
            if b-a < cfg['min_samples']:
                result['issues'].append(side + '_span_too_short'); continue
            emphasized = _side(t[a:b], p[a:b], cfg)
            # Lower sensitivity reduces review emphasis, never erases small drift
            # observations that the original detector would have retained.
            observations = _side(t[a:b], p[a:b], {**cfg,'small_drift_sensitivity':1.}) if cfg['small_drift_sensitivity']<1 else emphasized
            for interval in observations:
                interval['review_recommended'] = interval['severity']=='large' or any(
                    hit['start_ms']<=interval['end_ms'] and hit['end_ms']>=interval['start_ms'] for hit in emphasized)
                interval['side'] = side
                interval['id'] = fingerprint([VERSION, side, interval])[:24]
                result['intervals'].append(interval)
    result['intervals'].sort(key=lambda item: (item['start_ms'], item['side']))
    result['status'] = 'incomplete' if result['issues'] else 'suspected' if result['intervals'] else 'clear'
    result['issues'] = sorted(set(result['issues']))
    result['duration_ms'] = round(float((t[-1]-t[0])*1000), 3)
    result['samples'] = len(t)
    return result


def inspect_csv(content, config=None):
    try:
        if len(content)>32*1024**2 or content.count(b'\n')>30002:
            result=analyze([],{},config);result['issues']=['pose_csv_resource_limit'];return result
        rows = np.genfromtxt(io.BytesIO(content), delimiter=',', names=True, encoding='utf-8-sig')
        rows = np.atleast_1d(rows)
        positions = {s: np.column_stack([rows[s+'_'+k] for k in 'xyz']) for s in ('left', 'right')
                     if all(s+'_'+k in (rows.dtype.names or ()) for k in 'xyz')}
        result = analyze(rows['timestamp_ms'], positions, config)
        # Optional 6D orientation evidence: supporting context only, never an
        # independent jump verdict. Columns are the first two matrix columns.
        result['rotation_context'] = {}
        for side in ('left','right'):
            names=[side+'_r'+str(k) for k in range(1,7)]
            if not all(k in rows.dtype.names for k in names):
                result['rotation_context'][side]='unavailable'; continue
            raw=np.column_stack([rows[k] for k in names])
            x=raw[:,:3];x=x/np.maximum(np.linalg.norm(x,axis=1)[:,None],1e-12)
            y=raw[:,3:]-np.sum(raw[:,3:]*x,axis=1)[:,None]*x
            yn=np.linalg.norm(y,axis=1);y=y/np.maximum(yn[:,None],1e-12)
            valid=np.isfinite(raw).all(axis=1)&(np.linalg.norm(raw[:,:3],axis=1)>1e-6)&(yn>1e-6)
            matrix=np.stack([x,y,np.cross(x,y)],axis=2)
            cosine=(np.einsum('nij,nij->n',matrix[1:],matrix[:-1])-1)/2
            degrees=np.degrees(np.arccos(np.clip(cosine,-1,1)))
            degrees=np.where(valid[1:]&valid[:-1],degrees,np.nan)
            result['rotation_context'][side]='available' if valid.all() else 'partially_invalid'
            for interval in result['intervals']:
                if interval['side']!=side:continue
                mask=(rows['timestamp_ms'][1:]>=interval['start_ms'])&(rows['timestamp_ms'][1:]<=interval['end_ms'])
                values=degrees[mask];values=values[np.isfinite(values)]
                interval['max_rotation_step_degrees']=round(float(values.max()),3) if len(values) else None
    except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
        result = analyze([], {}, config)
        result['issues'] = ['pose_csv_invalid']
    result['source_sha256'] = hashlib.sha256(content).hexdigest()
    return result


def inspect_episode(path, metadata=None, config=None):
    path = Path(path)
    if metadata is None:
        try: metadata = json.loads((path/'metadata.json').read_text())
        except (OSError, ValueError): metadata = {}
    robot = str(metadata.get('robot') or metadata.get('robot_sn') or '').strip().lower()
    if not robot:
        result = analyze([], {}, config); result['issues'] = ['device_type_unknown']
    elif robot != 'umi':
        result = analyze([], {}, config); result.update(status='not_applicable', issues=[])
    else:
        result = analyze([], {}, config); result['issues'] = ['pose_csv_missing']
        for source in SOURCES:
            if (path/source).exists():
                try:
                    if (path/source).stat().st_size>32*1024**2:
                        result['issues']=['pose_csv_resource_limit']
                    else: result = inspect_csv((path/source).read_bytes(), config)
                except OSError: result['issues'] = ['pose_csv_unreadable']
                result['source'] = source
                break
    result['identity'] = identity(metadata, path.name)
    result['tracker_context'] = {k: metadata[k] for k in ('tracker_quality_status', 'tracker_quality_events', 'tracker_quality_guard') if k in metadata}
    return result
