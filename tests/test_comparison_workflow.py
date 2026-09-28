import csv
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

spec=importlib.util.spec_from_file_location('comparison',Path(__file__).resolve().parents[1]/'scripts/compare_trajectory_versions.py')
comparison=importlib.util.module_from_spec(spec);spec.loader.exec_module(comparison)

@pytest.fixture
def worksheet(tmp_path):
    manifest=tmp_path/'holdout.json';labels=tmp_path/'labels.csv';output=tmp_path/'imported.json'
    manifest.write_text(json.dumps({'cases':[{'episode':'task/episode_1','sha256':'pinned','pose_file':'private-pose','label':None,'review_needed':None,'label_status':'unlabeled','reviewer':''}]}))
    args=SimpleNamespace(manifest=manifest,labels=labels,output=labels)
    comparison.labels_export(args);args.output=output
    return args

def test_blank_labels_remain_unproven_and_keep_pinned_input(worksheet):
    comparison.labels_import(worksheet)
    case=json.loads(worksheet.output.read_text())['cases'][0]
    assert case['label_status']=='unlabeled' and case['review_needed'] is None
    assert case['pose_file']=='private-pose'

@pytest.mark.parametrize('change',['hash','duplicate','confirmed_without_reviewer'])
def test_import_rejects_corrupted_or_unaccountable_labels(worksheet,change):
    with worksheet.labels.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    if change=='hash':rows[0]['sha256']='changed'
    elif change=='duplicate':rows.append(dict(rows[0]))
    else:rows[0].update(label_status='confirmed',label='normal',review_needed='false')
    with worksheet.labels.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,comparison.LABEL_COLUMNS);writer.writeheader();writer.writerows(rows)
    with pytest.raises(ValueError):comparison.labels_import(worksheet)
    assert not worksheet.output.exists()

def test_usability_requires_human_measurement_and_access_to_evidence(tmp_path):
    path=tmp_path/'tasks.json';out=tmp_path/'result.json'
    tasks=[dict(episode=str(i),order='AB' if i%2 else 'BA',a_seconds=10,b_seconds=5,a_errors=0,b_errors=0) for i in range(20)]
    data=dict(reviewer='reviewer',baseline_revision='old',new_source_snapshot='new',tasks=tasks)
    path.write_text(json.dumps(data));args=SimpleNamespace(manifest=path,output=out)
    comparison.usability(args);assert json.loads(out.read_text())['status']=='insufficient'
    for t in tasks:t.update(a_evidence_access=True,b_evidence_access=False)
    path.write_text(json.dumps(data));comparison.usability(args)
    assert json.loads(out.read_text())['status']=='fail'

def test_changed_labeled_input_fails_instead_of_dropping_case(tmp_path):
    root=tmp_path/'source';episode=root/'episode';episode.mkdir(parents=True)
    (episode/'pose.csv').write_text('changed');(episode/'metadata.json').write_text('{}')
    fixture=tmp_path/'development';fixture.mkdir();(fixture/'manifest.json').write_text('{"cases":[]}')
    manifest=tmp_path/'holdout.json';manifest.write_text(json.dumps({'source_root':str(root),'cases':[{'episode':'episode','pose_file':'episode/pose.csv','sha256':'old','metadata_sha256':'old','label_status':'confirmed','reviewer':'human','label':'major','review_needed':True,'split':'holdout'}]}))
    out=tmp_path/'result.json';comparison.evaluate(SimpleNamespace(manifest=manifest,development=fixture,output=out))
    report=json.loads(out.read_text());assert report['status']=='fail' and report['evaluated']==0
    assert report['issues'][0]['reason']=='labeled input changed'
