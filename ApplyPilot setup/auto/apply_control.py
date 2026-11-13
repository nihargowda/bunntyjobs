"""Shared application requests and exclusive claims. No browser or model calls here."""
import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from job_url import app_key, is_submitted, normalize
from settings import SETTINGS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'auto' / 'application_control.sqlite3'
TARGET = int(SETTINGS['daily_target'])
TZ = ZoneInfo(SETTINGS['timezone'])

def day():
    return datetime.now(TZ).date().isoformat()

def connect():
    c = sqlite3.connect(STATE, timeout=5)
    c.row_factory = sqlite3.Row
    c.executescript('''CREATE TABLE IF NOT EXISTS requests (
      day TEXT, agent TEXT, state TEXT DEFAULT 'queued', updated TEXT,
      PRIMARY KEY(day,agent));
      CREATE TABLE IF NOT EXISTS claims (
      job TEXT PRIMARY KEY, agent TEXT, url TEXT, created TEXT);
      CREATE TABLE IF NOT EXISTS readers (
      agent TEXT PRIMARY KEY, seen TEXT);''')
    return c

def progress():
    first_submitted = {}
    paths = [ROOT / 'Applications/applications_log.csv']
    for p in paths:
        if not p.exists(): continue
        with p.open(newline='') as f:
            for r in csv.DictReader(f):
                if not is_submitted(r.get('status')): continue
                date = (r.get('date') or '').strip()
                key = app_key(r.get('company'), r.get('title'))
                if not date or not key[1]: continue
                first_submitted[key] = min(date, first_submitted.get(key, date))
    today = day()
    return sum(date == today for date in first_submitted.values())

def status():
    with connect() as c:
        requests = [dict(r) for r in c.execute('SELECT * FROM requests WHERE day=?', (day(),))]
        readers = [dict(r) for r in c.execute('SELECT * FROM readers')]
    n = progress()
    return dict(day=day(), target=TARGET, submitted=n, remaining=max(0,TARGET-n), requests=requests, readers=readers)

def request(agents=None):
    if agents is None:
        agents = ['Claude', 'Codex']  # preserve CLI behavior
    if not isinstance(agents, list) or not agents or any(a not in ('Claude', 'Codex') for a in agents):
        raise ValueError('Select Claude, Codex, or both.')
    agents = list(dict.fromkeys(agents))
    if progress() >= TARGET: return 'Daily target reached. More applications can be requested tomorrow.'
    with connect() as c:
        for agent in agents:
            c.execute("INSERT INTO requests VALUES (?,?,'queued',?) ON CONFLICT(day,agent) DO UPDATE SET state=CASE WHEN requests.state IN ('active','queued') THEN requests.state ELSE 'queued' END, updated=excluded.updated", (day(),agent,datetime.now().isoformat()))
    return 'Request queued for ' + ' and '.join(agents) + '. Waiting for acknowledgement.'

def acknowledge(agent):
    with connect() as c:
        c.execute('INSERT OR REPLACE INTO readers VALUES (?,?)',(agent,datetime.now().isoformat()))
        c.execute("UPDATE requests SET state='active', updated=? WHERE day=? AND agent=? AND state='queued'",(datetime.now().isoformat(),day(),agent))
    return status()

def claim(agent, url, company, title):
    # Employer/title key also catches the same job advertised under different URLs.
    key = hashlib.sha256(json.dumps(app_key(company, title)).encode()).hexdigest()
    legacy_key = hashlib.sha256((' '.join(company.lower().split())+'|de|'+ ' '.join(title.lower().split())).encode()).hexdigest()
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM claims WHERE job IN (?, ?)', (key, legacy_key)).fetchone():
            return dict(ok=False, reason='Already claimed; do not open or resubmit')
        canonical_url = normalize(url)
        if any(normalize(r['url']) == canonical_url for r in c.execute('SELECT url FROM claims')):
            return dict(ok=False, reason='Application URL already claimed')
        for legacy in (ROOT/'auto/claims').glob('*/*'):
            if not legacy.is_file(): continue
            try:
                record = json.loads(legacy.read_text())
            except (ValueError, OSError):
                continue
            job = record.get('job', {})
            if (normalize(job.get('apply', '')) == canonical_url or
                (' '.join(job.get('company','').lower().split()) == ' '.join(company.lower().split()) and
                 ' '.join(job.get('title','').lower().split()) == ' '.join(title.lower().split()))):
                return dict(ok=False, reason='Existing agent claim; verify outcome before retrying')
        for p in (ROOT/'Applications/applications_log.csv',):
            if not p.exists(): continue
            with p.open(newline='') as f:
                for r in csv.DictReader(f):
                    if app_key(r.get('company'), r.get('title')) == app_key(company, title):  # catches 'Quillo' vs 'Quillo Technologies' too
                        if r.get('status','').lower().startswith(('submit','applied','filled','waiting')):
                            return dict(ok=False, reason='Already submitted or awaiting verification in application history')
        try:
            c.execute('INSERT INTO claims VALUES (?,?,?,?)',(key,agent,url,datetime.now().isoformat()))
        except sqlite3.IntegrityError:
            return dict(ok=False, reason='Already claimed; do not open or resubmit')
    return dict(ok=True, agent=agent, job=key)

if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('action', choices=['status','request','ack','claim','finish'])
    p.add_argument('--agent',choices=['Claude','Codex'])
    p.add_argument('--url'); p.add_argument('--company'); p.add_argument('--title')
    a=p.parse_args()
    if a.action in ('ack','claim','finish') and not a.agent: p.error('--agent required')
    if a.action == 'claim' and not all((a.url,a.company,a.title)): p.error('claim requires URL, company and title')
    if a.action=='status': result=status()
    elif a.action=='request': result=request()
    elif a.action=='ack': result=acknowledge(a.agent)
    elif a.action=='claim': result=claim(a.agent,a.url,a.company,a.title)
    else:
        with connect() as c: c.execute("UPDATE requests SET state='finished', updated=? WHERE day=? AND agent=?",(datetime.now().isoformat(),day(),a.agent))
        result=status()
    print(json.dumps(result,indent=2))


def workflow_status():
    """Cheap live view of reported events; never infers browser actions from a claim."""
    state = status()
    with connect() as c:
        claims = [dict(r) for r in c.execute("SELECT agent,url,created FROM claims WHERE substr(created,1,10)=? ORDER BY created DESC LIMIT 20", (day(),))]
        count = c.execute("SELECT COUNT(*) FROM claims WHERE substr(created,1,10)=?", (day(),)).fetchone()[0]
    try:
        queue = json.loads((ROOT/'auto/queue.json').read_text())
        queued = len(queue)
    except (OSError, ValueError):
        queued = None
    latest = {}
    try:
        with (ROOT/'Applications/applications_log.csv').open(newline='') as f:
            for row in csv.DictReader(f):
                if row.get('date') != day(): continue
                key = tuple(' '.join(row.get(k,'').lower().split()) for k in ('company','title'))
                if key in latest: del latest[key]
                latest[key] = row
    except OSError:
        pass
    outcomes = list(latest.values())
    blocked = sum('needs you' in r.get('status','').lower() or 'waiting' in r.get('status','').lower() for r in outcomes)
    return dict(**state, cached_candidates=queued, claims_today=count, recent_claims=claims,
                blocked=blocked, events=[{k:r.get(k,'') for k in ('company','title','status','notes')} for r in outcomes[-8:]][::-1],
                observed_at=datetime.now(TZ).isoformat())
