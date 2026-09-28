"""Prepare real, unlabeled holdouts and compare frozen detector configurations.

Never invent human labels. Missing/disputed labels yield insufficient evidence.
The baseline is the current detector at original sensitivity 1.0, not a claim
that the entire historical software implementation has been reproduced.
"""
import argparse
import csv
from collections import Counter
from datetime import datetime,timezone
import gzip
import hashlib
import json
import platform
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'QA_Pipeline'))
from scripts.pipeline.umi_trajectory.detector import inspect_csv,settings,VERSION,SOURCES


def sha(data):return hashlib.sha256(data).hexdigest()
def dump(path,value):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def safe(root,name):
    p=root/name
    if '..' in Path(name).parts or Path(name).is_absolute() or not p.resolve().is_relative_to(root.resolve()) or p.is_symlink():raise ValueError('unsafe input path')
    return p

LABEL_COLUMNS=('episode','sha256','label','review_needed','reviewer','label_status','notes')

def labels_export(args):
    manifest=json.loads(args.manifest.read_text())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('w',newline='',encoding='utf-8-sig') as file:
        writer=csv.DictWriter(file,fieldnames=LABEL_COLUMNS);writer.writeheader()
        for case in manifest['cases']:
            writer.writerow({k:(str(case[k]).lower() if type(case.get(k)) is bool else case.get(k,'')) for k in LABEL_COLUMNS})
    print('Exported blank/recorded human labels; detector predictions are excluded.')

def labels_import(args):
    manifest=json.loads(args.manifest.read_text());cases={c['episode']:c for c in manifest['cases']};seen=set()
    with args.labels.open(newline='',encoding='utf-8-sig') as file:
        for row in csv.DictReader(file):
            name=row.get('episode');case=cases.get(name)
            if not case or name in seen or row.get('sha256')!=case['sha256']:raise ValueError('unknown, duplicate, or changed case: '+str(name))
            seen.add(name);state=row.get('label_status','').strip() or 'unlabeled'
            label=row.get('label','').strip();needed=row.get('review_needed','').strip().lower();reviewer=row.get('reviewer','').strip()
            if state not in ('unlabeled','confirmed','disputed'):raise ValueError('invalid label status')
            if state=='confirmed' and (label not in ('normal','minor','major') or needed not in ('true','false') or not reviewer):raise ValueError('confirmed labels need severity, review_needed and reviewer')
            if label and label not in ('normal','minor','major'):raise ValueError('invalid severity')
            case.update(label=label or None,review_needed={'true':True,'false':False}.get(needed),reviewer=reviewer,label_status=state,notes=row.get('notes',''))
    if len(seen)!=len(cases):raise ValueError('label worksheet must retain all cases; mark disputed instead of deleting')
    dump(args.output,manifest);print('Imported human labels; input paths and hashes preserved.')

def prepare(args):
    cases=[];seen=set();db=sqlite3.connect(args.ledger.resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    for row in db.execute("SELECT * FROM q_task_observations WHERE source='realtime_upload' ORDER BY id DESC LIMIT 2000"):
        if len(cases)>=args.limit:break
        if 'umi' not in row['path'].lower():continue
        try:
            episode=safe(args.root,row['path']);metadata=json.loads((episode/'metadata.json').read_text())
            if str(metadata.get('robot','')).lower()!='umi' or any(metadata.get(k) is True for k in ('test_only','test_mode','is_test','practice')):continue
            file=next((episode/source for source in SOURCES if (episode/source).is_file()),None)
            if file is None or file.stat().st_size>32*1024**2:continue
            before=file.stat();data=file.read_bytes();after=file.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):continue
            digest=sha(data)
            if digest in seen:continue
            seen.add(digest)
            cases.append({'episode':row['path'],'pose_file':file.relative_to(args.root).as_posix(),'sha256':digest,
                'metadata_sha256':sha((episode/'metadata.json').read_bytes()),'receipt_event':row['event_id'],
                'label':None,'review_needed':None,'reviewer':'','label_status':'unlabeled','split':'holdout'})
        except (OSError,ValueError,StopIteration):continue
    db.close()
    dump(args.output,{'schema':'trajectory-holdout.v1','source_root':str(args.root.resolve()),'created_at':datetime.now(timezone.utc).isoformat(),
        'holdout_attestation':'','instructions':'请独立查看证据后填写标签 normal/minor/major、review_needed、reviewer，并把 label_status 改为 confirmed。无法判断的标 disputed。holdout_attestation 由负责人说明这些样本未用于调参。','cases':cases})
    worksheet={'schema':'review-usability.v1','instructions':'人工完成同样的 20 个任务；记录秒数、错误数、能否找到支持证据。界面 A=原始 QualityCheck，B=新专注界面。不要用脚本耗时代替人工复核。',
        'reviewer':'','baseline_revision':'','new_source_snapshot':'','tasks':[{'episode':c['episode'],'order':'AB' if i%2==0 else 'BA','a_seconds':None,'b_seconds':None,'a_errors':None,'b_errors':None,'a_evidence_access':None,'b_evidence_access':None} for i,c in enumerate(cases[:20])]}
    dump(args.output.with_name('usability-worksheet.json'),worksheet)
    print('Prepared',len(cases),'unlabeled, content-pinned real candidates; no accuracy claim.')


def evaluate(args):
    manifest=json.loads(args.manifest.read_text());root=Path(manifest['source_root']);seen=set();rows=[];issues=[]
    development=set()
    fixture=ROOT.parent/'DataQualityControlLoop/integration/tests/fixtures/umi_trajectory'
    if args.development:fixture=args.development
    if not (fixture/'manifest.json').exists():raise ValueError('development manifest required to exclude reused examples')
    for item in json.loads((fixture/'manifest.json').read_text())['cases']:
        development.add(sha(gzip.decompress((fixture/item['file']).read_bytes())))
    baseline=settings({'small_drift_sensitivity':1.0});candidate=settings({'small_drift_sensitivity':.5})
    code={str(p.relative_to(ROOT)):sha(p.read_bytes()) for p in (ROOT/'QA_Pipeline/scripts/pipeline/umi_trajectory').rglob('*.py')}
    for case in manifest['cases']:
        if case.get('label_status')!='confirmed' or not case.get('reviewer') or case.get('label') not in ('normal','minor','major') or type(case.get('review_needed')) is not bool:continue
        try:
            if case.get('split')!='holdout':raise ValueError('not holdout')
            data=safe(root,case['pose_file']).read_bytes();digest=sha(data)
            if digest!=case['sha256'] or sha((safe(root,case['episode'])/'metadata.json').read_bytes())!=case['metadata_sha256']:raise ValueError('labeled input changed')
            if digest in development or digest in seen:raise ValueError('reused or duplicate example')
            seen.add(digest);results=[]
            for config in (baseline,candidate):
                result=inspect_csv(data,config);intervals=result['intervals']
                results.append({'large':any(i['severity']=='large' for i in intervals),'observed':bool(intervals),'prompt':any(i['review_recommended'] for i in intervals),'status':result['status']})
            rows.append({**case,'baseline':results[0],'candidate':results[1]})
        except (OSError,ValueError,KeyError) as exc:issues.append({'episode':case.get('episode'),'reason':str(exc)})
    labels=Counter(r['label'] for r in rows)
    metrics={}
    for name in ('baseline','candidate'):
        metrics[name]={'major_detected':sum(r['label']=='major' and r[name]['large'] for r in rows),
            'major_missed':sum(r['label']=='major' and not r[name]['large'] for r in rows),
            'unnecessary_prompts':sum(not r['review_needed'] and r[name]['prompt'] for r in rows),
            'minor_retained':sum(r['label']=='minor' and r[name]['observed'] for r in rows),
            'normal_flagged':sum(r['label']=='normal' and r[name]['observed'] for r in rows),
            'incomplete':sum(r[name]['status'] not in ('clear','suspected') for r in rows)}
    old=metrics['baseline']['unnecessary_prompts'];new=metrics['candidate']['unnecessary_prompts']
    reduction=(old-new)/old if old else None
    enough=len(rows)>=200 and labels['major']>=30 and labels['minor']>=30 and labels['normal']>=100 and bool(manifest.get('holdout_attestation'))
    safety=metrics['candidate']['major_missed']==0 and metrics['candidate']['major_detected']>=metrics['baseline']['major_detected'] and metrics['candidate']['incomplete']==0
    improved=reduction is not None and reduction>=.3 and metrics['candidate']['minor_retained']==labels['minor'] and metrics['candidate']['normal_flagged']<=metrics['baseline']['normal_flagged']
    result={'schema':'trajectory-comparison.v1','status':'fail' if issues or (labels['major'] and not safety) else 'pass' if enough and improved else 'insufficient' if not enough else 'fail',
        'label_counts':dict(labels),'evaluated':len(rows),'unlabeled_or_disputed':len(manifest['cases'])-len(rows)-len(issues),
        'unnecessary_prompt_reduction':reduction,'metrics':metrics,'issues':issues,'cases':rows,
        'provenance':{'generated_at':datetime.now(timezone.utc).isoformat(),'manifest_sha256':sha(args.manifest.read_bytes()),'detector_version':VERSION,'source_files':code,'baseline_config':baseline,'candidate_config':candidate,'host':{'os':platform.system(),'machine':platform.machine(),'python':platform.python_version()},'git_head':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()},
        'limits':['仅比较同一检测器的原始与当前灵敏度配置','开发样本已按内容排除','有限样本中零漏检不保证检出全部漂移','硬性检查与失效保护由独立回归验证；不从轨迹分数推断','不证明机器人可安全执行小幅漂移数据']}
    dump(args.output,result);print('Comparison:',result['status'],'labeled cases:',len(rows))


def usability(args):
    data=json.loads(args.manifest.read_text());tasks=data.get('tasks',[])
    valid=[t for t in tasks if all(type(t.get(k)) in (int,float) and t[k]>=0 for k in ('a_seconds','b_seconds','a_errors','b_errors')) and t['a_seconds']>0 and t['b_seconds']>0 and all(type(t.get(k)) is bool for k in ('a_evidence_access','b_evidence_access'))]
    import statistics
    ratio=statistics.median(t['b_seconds']/t['a_seconds'] for t in valid) if valid else None
    enough=len(valid)>=20 and len({t['episode'] for t in valid})==len(valid) and bool(data.get('reviewer')) and bool(data.get('baseline_revision')) and bool(data.get('new_source_snapshot')) and sum(t.get('order')=='AB' for t in valid)>=10 and sum(t.get('order')=='BA' for t in valid)>=10
    errors_a=sum(t['a_errors'] for t in valid);errors_b=sum(t['b_errors'] for t in valid)
    access_a=sum(t['a_evidence_access'] for t in valid);access_b=sum(t['b_evidence_access'] for t in valid)
    dump(args.output,{'schema':'review-usability-result.v1','status':'insufficient' if not enough else 'pass' if ratio<=.8 and errors_b<=errors_a and access_b>=access_a else 'fail','completed':len(valid),'median_time_ratio':ratio,'baseline_errors':errors_a,'candidate_errors':errors_b,'baseline_evidence_access':access_a,'candidate_evidence_access':access_b,'worksheet_sha256':sha(args.manifest.read_bytes()),'reason':'人工交叉顺序复核；速度目标提升 20%，错误与证据访问不退步'})

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('prepare');p.add_argument('--ledger',type=Path,required=True);p.add_argument('--root',type=Path,required=True);p.add_argument('--limit',type=int,default=200);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('evaluate');p.add_argument('--manifest',type=Path,required=True);p.add_argument('--development',type=Path);p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('usability');p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    for name in ('labels-export','labels-import'):
        p=sub.add_parser(name);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
        if name=='labels-import':p.add_argument('--labels',type=Path,required=True)
    args=parser.parse_args();{'prepare':prepare,'evaluate':evaluate,'usability':usability,'labels-export':labels_export,'labels-import':labels_import}[args.action](args)
