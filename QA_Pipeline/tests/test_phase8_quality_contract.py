import json
from pathlib import Path
import sys
import numpy as np
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.pipeline.umi_trajectory import analyze
from scripts.pipeline.umi_trajectory.detector import settings
from scripts.pipeline.qa_core import EpisodeState,init_db,save_episode_state,Finding,save_findings
from scripts.pipeline.quality_contract import prepare_state,export_results
from scripts.pipeline.qa_dcs_notifier import DcsIssueNotifier


def signal(amplitude):
    t=np.arange(301)*1000/30;p=np.zeros((301,3));p[150:,0]=amplitude
    return t,{'left':p,'right':np.zeros_like(p)}


@pytest.mark.parametrize('sensitivity',[.25,.5,1,2])
def test_large_jump_never_disabled_by_small_sensitivity(sensitivity):
    r=analyze(*signal(.3),{'small_drift_sensitivity':sensitivity})
    assert any(i['severity']=='large' for i in r['intervals'])
    assert r['config']['small_drift_sensitivity']==sensitivity


def test_sensitivity_reduces_small_candidates_but_preserves_evidence():
    t,p=signal(.018)
    assert analyze(t,p,{'small_drift_sensitivity':1})['intervals']
    conservative=analyze(t,p,{'small_drift_sensitivity':.5})
    assert conservative['intervals']  # small drift records are retained
    assert not any(i['review_recommended'] for i in conservative['intervals'])
    assert analyze(*signal(.03))['intervals'][0]['severity']=='small_or_uncertain'
    assert not analyze(*signal(0))['intervals']


@pytest.mark.parametrize('value',[0,-1,3,True,float('nan')])
def test_invalid_sensitivity_refused(value):
    with pytest.raises(ValueError):settings({'small_drift_sensitivity':value})


def test_phase8_never_uses_automatic_dcs_even_overrides(tmp_path,monkeypatch):
    monkeypatch.setenv('QA_DCS_NOTIFY_ENABLED','true');monkeypatch.setenv('QA_DCS_NOTIFY_STATUSES','fail,needs_review')
    notifier=DcsIssueNotifier(tmp_path,'test')
    for status in ('fail','needs_review'):
        assert notifier._decision({'phase':8,'check_name':'umi_trajectory_suspected','status':status})=='human_review_required'


def test_atomic_contract_and_resume_invalidates_changed_source(tmp_path,monkeypatch):
    root=tmp_path/'source';episode=root/'task/20260928/operator/episode_1';episode.mkdir(parents=True)
    (episode/'metadata.json').write_text('{"robot":"umi","collector_id":"pc","collector_device_id":"device"}')
    feed=tmp_path/'feed';monkeypatch.setenv('QA_QC_FEED_DIR',str(feed));monkeypatch.setenv('QA_QC_SOURCE_ROOT',str(root))
    state=EpisodeState(episode,'task','20260928','operator','umi','');prepare_state(state)
    state.phases_completed=[1,2,3,8]
    state.metrics['p8_trajectory']={'status':'clear','intervals':[]}
    db=tmp_path/'qa.db';init_db(db);save_episode_state(db,state);export_results(db)
    first=json.loads(next(feed.glob('*.json')).read_text())
    assert first['source_unchanged'] and first['identity']['collector_id']=='pc'
    (episode/'new.csv').write_text('changed')
    export_results(db);changed=json.loads(next(feed.glob('*.json')).read_text());assert not changed['source_unchanged']
    prepare_state(state);assert state.phases_completed==[]
    assert state.metadata['robot']=='umi'


def test_soft_failure_does_not_skip_timestamp_or_trajectory_checks(tmp_path):
    from scripts.run_pipeline import _has_failed_prior_phase
    state=EpisodeState(tmp_path,'t','d','o','umi','')
    for phase,name,next_phase in [(2,'duration_task_outlier',3),(7,'operator_sustained_slow_motion',8)]:
        state.phase_status={phase:'fail'}
        state.findings=[Finding(str(tmp_path),phase,name,'major','fail','review required')]
        assert not _has_failed_prior_phase(state,next_phase)
    state.findings=[Finding(str(tmp_path),1,'metadata_exists','critical','fail','missing')]
    state.phase_status={1:'fail'}
    assert _has_failed_prior_phase(state,8)


def test_no_usable_image_timestamps_is_pending_not_pass(tmp_path):
    from scripts.pipeline.phase3_timestamp import _read_image_timestamps
    path=tmp_path/'timestamps.csv';path.write_text('timestamp_ms\n0\n33\n')
    assert _read_image_timestamps(path) is None
    path.write_text('timestamp_ms,is_new\n0,true\n33,1\n')
    assert _read_image_timestamps(path)==[0.,33.]
