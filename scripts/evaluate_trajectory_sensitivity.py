"""Summarize retained examples; labels are development references, not new ground truth."""
import argparse
import collections
import gzip
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'QA_Pipeline'))
from scripts.pipeline.umi_trajectory import inspect_csv

p=argparse.ArgumentParser(description=__doc__);p.add_argument('fixtures',type=Path);p.add_argument('output',type=Path);args=p.parse_args()
cases=json.loads((args.fixtures/'manifest.json').read_text())['cases'];summary={}
for sensitivity in (.25,.5,1,2):
    categories=collections.defaultdict(lambda:{'episodes':0,'observed':0,'review_recommended':0,'large':0,'intervals':0})
    for case in cases:
        r=inspect_csv(gzip.decompress((args.fixtures/case['file']).read_bytes()),{'small_drift_sensitivity':sensitivity})
        x=categories[case['category']];x['episodes']+=1;x['observed']+=bool(r['intervals'])
        x['review_recommended']+=any(i['review_recommended'] for i in r['intervals'])
        x['large']+=any(i['severity']=='large' for i in r['intervals']);x['intervals']+=len(r['intervals'])
    summary[str(sensitivity)]=dict(categories)
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps({'scope':'53 retained development examples, not independent accuracy validation','results':summary},ensure_ascii=False,indent=2)+'\n')
print('Wrote',args.output)
