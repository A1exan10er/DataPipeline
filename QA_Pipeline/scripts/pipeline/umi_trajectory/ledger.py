"""Durable per-device last-five ledger shared by collector and quality loop.

Functions accept the caller's SQLite transaction, allowing a result and its
outbox notification to commit atomically. Receipt revisions never add episodes.
"""
from datetime import datetime, timezone
import json
import math

from .detector import fingerprint

EVENT = 'collector.umi_trajectory_checked'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS umi_episodes(
      key TEXT PRIMARY KEY, device TEXT NOT NULL, completed REAL NOT NULL,
      revision INTEGER NOT NULL, digest TEXT NOT NULL, production INTEGER NOT NULL,
      suspected INTEGER NOT NULL, payload TEXT NOT NULL, server_episode TEXT)''')
    db.execute('CREATE TABLE IF NOT EXISTS umi_receipt_history(key TEXT,revision INTEGER,payload TEXT,created TEXT,PRIMARY KEY(key,revision))')
    db.execute('CREATE INDEX IF NOT EXISTS umi_device_recent ON umi_episodes(device,completed DESC)')
    db.execute('''CREATE TABLE IF NOT EXISTS umi_alerts(
      device TEXT PRIMARY KEY, active INTEGER NOT NULL, generation INTEGER NOT NULL,
      acknowledged INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS umi_alert_history(
      id INTEGER PRIMARY KEY, device TEXT, generation INTEGER, action TEXT,
      actor TEXT, payload TEXT, created TEXT)''')


def now():
    return datetime.now(timezone.utc).isoformat()


def key_of(ident):
    required = ('collector_id', 'device_id', 'recording_session_id', 'episode_id')
    if not isinstance(ident, dict) or any(not isinstance(ident.get(k), str) or len(ident[k])>512 for k in required):
        raise ValueError('invalid collector episode identity')
    if not ident['collector_id'] or not ident['recording_session_id'] or not ident['episode_id']:
        raise ValueError('collector, session and episode identity required')
    return fingerprint({k: ident[k] for k in required})


def validate(payload):
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise ValueError('invalid trajectory receipt schema')
    key = key_of(payload.get('identity'))
    if type(payload.get('revision')) is not int or payload['revision'] < 1 or type(payload.get('production')) is not bool:
        raise ValueError('invalid receipt revision or production flag')
    completed = payload.get('completed_at')
    if isinstance(completed, bool) or not isinstance(completed, (int, float)) or not math.isfinite(completed) or completed <= 0:
        raise ValueError('invalid episode completion time')
    result = payload.get('result')
    if not isinstance(result, dict) or result.get('status') not in ('clear','suspected','incomplete','not_applicable','discarded'):
        raise ValueError('invalid trajectory result')
    if not isinstance(result.get('rule_version'),str) or not result['rule_version'] or not isinstance(result.get('detector_version'),str):
        raise ValueError('versioned detector result required')
    intervals = result.get('intervals')
    if not isinstance(intervals, list) or len(intervals)>2000 or (result['status']=='suspected' and not intervals):
        raise ValueError('invalid suspected intervals')
    for item in intervals:
        if not isinstance(item, dict) or item.get('side') not in ('left','right'):
            raise ValueError('invalid trajectory hand')
        a, b = item.get('start_ms'), item.get('end_ms')
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (a,b)) or a<0 or b<a:
            raise ValueError('invalid trajectory interval')
    if result['status'] in ('clear','not_applicable','discarded') and intervals:
        raise ValueError('clear receipt cannot contain doubts')
    for key_name in ('task','operator','episode_name'):
        if not isinstance(payload.get(key_name,''),str) or len(payload.get(key_name,''))>512:
            raise ValueError('invalid receipt display field')
    if len(encode(payload).encode())>900_000:
        raise ValueError('trajectory receipt too large')
    return key


def recent(db, device):
    return db.execute('''SELECT * FROM umi_episodes WHERE device=? AND production=1
      ORDER BY completed DESC,key DESC LIMIT 5''', (device,)).fetchall()


def record(db, payload):
    key = validate(payload)
    digest = fingerprint(payload)
    old = db.execute('SELECT * FROM umi_episodes WHERE key=?', (key,)).fetchone()
    if old and payload['revision'] <= old['revision']:
        if payload['revision']==old['revision'] and digest!=old['digest']:
            raise ValueError('trajectory receipt revision reused with different data')
        return {'key':key, 'applied':False}
    db.execute('INSERT INTO umi_receipt_history VALUES(?,?,?,?)',(key,payload['revision'],encode(payload),now()))
    ident = payload['identity']
    device = ident['device_id'] or ident['collector_id']
    if old and (old['device'] != device or old['completed'] != payload['completed_at']):
        raise ValueError('episode identity reused with different device or time')
    production = payload['production'] and payload['result']['status'] not in ('not_applicable','discarded')
    db.execute('''INSERT INTO umi_episodes(key,device,completed,revision,digest,production,suspected,payload)
      VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,
      production=excluded.production,suspected=excluded.suspected,payload=excluded.payload''',
      (key,device,payload['completed_at'],payload['revision'],digest,int(production),int(bool(payload['result']['intervals'])),encode(payload)))
    active = sum(row['suspected'] for row in recent(db,device)) >= 3
    alert = db.execute('SELECT * FROM umi_alerts WHERE device=?',(device,)).fetchone()
    generation = (alert['generation'] if alert else 0) + int(active and not (alert and alert['active']))
    acknowledged = alert['acknowledged'] if alert and bool(alert['active'])==active else 0
    db.execute('INSERT OR REPLACE INTO umi_alerts VALUES(?,?,?,?,?)',(device,int(active),generation,acknowledged,now()))
    if active != bool(alert and alert['active']):
        db.execute('INSERT INTO umi_alert_history(device,generation,action,actor,payload,created) VALUES(?,?,?,?,?,?)',
                   (device,generation,'triggered' if active else 'resolved','detector',encode([r['key'] for r in recent(db,device)]),now()))
    return {'key':key,'applied':True,'alert_active':active,'generation':generation}


def alerts(db, device=None):
    rows = db.execute('SELECT * FROM umi_alerts WHERE active=1'+(' AND device=?' if device else '')+' ORDER BY updated DESC',
                      (device,) if device else ()).fetchall()
    out=[]
    for row in rows:
        window=recent(db,row['device'])
        out.append({**dict(row), 'count':sum(r['suspected'] for r in window),'window_count':len(window),
                    'episodes':[{'key':r['key'],'name':json.loads(r['payload']).get('episode_name',''),
                                 'server_episode':r['server_episode']} for r in window if r['suspected']]})
    return out


def acknowledge(db, device, generation, actor):
    row=db.execute('SELECT * FROM umi_alerts WHERE device=?',(device,)).fetchone()
    if not row or not row['active'] or row['generation']!=generation:
        raise ValueError('alert changed; refresh first')
    if not row['acknowledged']:
        db.execute('UPDATE umi_alerts SET acknowledged=1 WHERE device=?',(device,))
        db.execute('INSERT INTO umi_alert_history(device,generation,action,actor,payload,created) VALUES(?,?,?,?,?,?)',
                   (device,generation,'acknowledged',actor,'{}',now()))
    return {'acknowledged':True}
