"""Opt-in, atomic local result feed. No network or operator notification.

Enable with QA_QC_FEED_DIR and QA_QC_SOURCE_ROOT. A manifest is pinned before
checking and compared at completion; resumed/stale inputs cannot become passed.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from functools import lru_cache

from .qa_config import load_quality_config


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def manifest(path):
    root=Path(path);result={}
    if not root.is_dir() or root.is_symlink():raise ValueError('invalid episode directory')
    for file in sorted(root.rglob('*')):
        if file.is_symlink():raise ValueError('symlink in recording')
        if file.is_file():
            stat=file.stat()
            result[file.relative_to(root).as_posix()]=[stat.st_size,stat.st_mtime_ns]
    return result


@lru_cache(maxsize=1)
def rules_version():
    code={str(p.relative_to(Path(__file__).parent)):hashlib.sha256(p.read_bytes()).hexdigest()
          for p in Path(__file__).parent.rglob('*.py')}
    return digest([load_quality_config(),code])


def prepare_state(state):
    if not os.getenv('QA_QC_FEED_DIR'):return
    version=rules_version()
    try: current=manifest(state.episode_path)
    except (OSError,ValueError):current=None
    old=state.metrics.get('qc_input')
    if old is None or old.get('manifest')!=current or old.get('rules')!=version:
        # Rechecking means reloading metadata, too. Do not retain an old failure.
        from .qa_core import load_metadata
        state.metadata,_=load_metadata(state.episode_path)
        state.phases_completed=[];state.phase_status={};state.findings=[];state.metrics={}
    state.metrics['qc_input']={'manifest':current,'rules':version,'started_ns':time.time_ns()}


def export_results(db_path):
    folder=os.getenv('QA_QC_FEED_DIR');root=os.getenv('QA_QC_SOURCE_ROOT')
    if not folder:return
    if not root:raise ValueError('QA_QC_SOURCE_ROOT is required with QA_QC_FEED_DIR')
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);root=Path(root).resolve()
    with sqlite3.connect('file:'+str(Path(db_path).resolve())+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        for row in db.execute('SELECT * FROM episodes'):
            path=Path(row['episode_path']).resolve()
            if not path.is_relative_to(root):continue
            relative=path.relative_to(root).as_posix()
            metrics=json.loads(row['metrics'] or '{}');pin=metrics.get('qc_input',{})
            try: unchanged=pin.get('manifest') is not None and manifest(path)==pin['manifest']
            except (OSError,ValueError):unchanged=False
            findings=[]
            for f in db.execute('SELECT phase,check_name,status,severity,message,details FROM findings WHERE episode_path=?',(str(path),)):
                item=dict(f);item['details']=json.loads(item['details'] or '{}')
                if item['phase'] in json.loads(row['phases_completed'] or '[]'):findings.append(item)
            try:metadata=json.loads((path/'metadata.json').read_text())
            except (OSError,ValueError):metadata={}
            if not isinstance(metadata,dict):metadata={}
            trajectory=metrics.get('p8_trajectory',{})
            result={'schema':'pipeline-quality.v2','episode_id':relative,'run_id':str(Path(db_path).resolve()),
                'generation':pin.get('started_ns',0),'rule_version':pin.get('rules',''),
                'source_receipt':os.getenv('QA_QC_RECEIPT_ID') or None,
                'source_manifest':pin.get('manifest'),'source_unchanged':unchanged,
                'effective_config':effective_config(),
                'coverage':json.loads(row['phases_completed'] or '[]'),'findings':findings,
                'trajectory':trajectory,'identity':verified_identity(metadata),
                'recorded_at':metadata.get('start_time') or metadata.get('recording_start_time') or '',
                'calibration_id':metadata.get('calibration_id') or '',
                'context':{k:row[k] for k in ('task','date','operator','robot')},
                'test_only':any(metadata.get(k) is True for k in ('test_only','test_mode','is_test','practice')),
                'produced_at':time.time()}
            result=stable_result(result)
            result['result_version']=digest({k:v for k,v in result.items() if k not in ('produced_at','run_id','generation')})
            # Append-only indexed outbox: readers resume by sequence, not directory scans.
            with sqlite3.connect(folder/'quality-feed.sqlite',timeout=30) as outbox:
                outbox.execute('CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY AUTOINCREMENT,episode TEXT,generation INTEGER,version TEXT,payload TEXT,UNIQUE(episode,generation,version))')
                outbox.execute('INSERT OR IGNORE INTO results(episode,generation,version,payload) VALUES(?,?,?,?)',
                    (relative,result['generation'],result['result_version'],json.dumps(result,ensure_ascii=False,allow_nan=False)))


def verified_identity(metadata):
    ids={str(metadata[k]).strip() for k in ('collector_id','recording_collector_id') if metadata.get(k)}
    devices={str(metadata[k]).strip() for k in ('collector_device_id','device_id') if metadata.get(k)}
    return {'collector_id':next(iter(ids)) if len(ids)==1 else '',
            'device_id':next(iter(devices)) if len(devices)==1 else '',
            'error':'' if len(ids)==len(devices)==1 else 'missing_or_conflicting_identity'}


def stable_result(value):
    if isinstance(value,dict):
        return {k:stable_result(v) for k,v in value.items() if k not in ('detector_runtime_s','runtime_s','elapsed_s')}
    if isinstance(value,list):return [stable_result(v) for v in value]
    return value


def effective_config():
    import copy
    from .umi_trajectory.detector import settings
    config=copy.deepcopy(load_quality_config())
    phase=config.setdefault('phase8_umi_trajectory',{})
    phase['detector']=settings(phase.get('detector',{}))
    return config
