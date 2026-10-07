"""Local single-user job agent. No secrets or invented qualifications required."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import html
import json
import re
import sqlite3
import time
import urllib.request
from urllib.parse import urlparse, urlunparse
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state'

def load(name):
    path = ROOT / name
    if name == 'profile.json' and not path.exists():
        path = ROOT / 'profile.example.json'
    return json.loads(path.read_text())

def plain(value):
    return html.unescape(re.sub(r'<[^>]+>', ' ', value or ''))

def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'PersonalJobAgent/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)

def connect():
    STATE.mkdir(mode=0o700, exist_ok=True)
    db = sqlite3.connect(STATE / 'jobs.sqlite')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, data TEXT NOT NULL, score INTEGER NOT NULL,
        status TEXT NOT NULL, note TEXT DEFAULT '', updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY, job_id TEXT, day TEXT, status TEXT, note TEXT);
        CREATE TABLE IF NOT EXISTS runs (at TEXT, report TEXT);''')
    return db

def identity(job):
    parsed = urlparse(job['url'])
    path = parsed.path.rstrip('/')
    if parsed.hostname in {'jobs.lever.co','jobs.eu.lever.co'}:
        path = re.sub(r'/apply$', '', path)
        canonical = urlunparse((parsed.scheme, parsed.netloc.lower(), path, '', '', ''))
    else:
        canonical = job['url'].rstrip('/')
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]

def eligible(job, config):
    # Unknown geography is queued for human checking, never treated as worldwide.
    location = job.get('location', '').lower()
    if re.search(r'\b(not|except|excluding)\b', location):
        return False
    if not any(re.search(r'\b' + re.escape(x) + r'\b', location) for x in config['allowed_locations']):
        return False
    text = plain(job['title'] + ' ' + job['description']).lower()
    if job.get('remote') is False:
        return False
    if job.get('source') in ('Lever', 'Greenhouse') and not job.get('remote'):
        return False
    if re.search(r'\b(hybrid|on[- ]site|us only|usa only|united states only|eu only)\b', text):
        return False
    return any(k in job['title'].lower() for k in config['keywords'])

def score(job, profile):
    text = plain(job['title'] + ' ' + job['description']).lower()
    title_bonus = 10 if any(s.lower() in job['title'].lower() for s in ('Flutter','Dart') if s in profile['skills']) else 0
    return title_bonus + sum(10 for s in profile['skills'] if re.search(r'(?<!\w)' + re.escape(s.lower()) + r'(?!\w)', text))

def discover(config):
    jobs, errors = [], []
    cache = STATE / 'remotive.json'
    try:
        if not cache.exists() or time.time() - cache.stat().st_mtime > config['remotive_cache_hours'] * 3600:
            data = fetch('https://remotive.com/api/remote-jobs?category=software-dev')
            cache.write_text(json.dumps(data))
        for j in json.loads(cache.read_text())['jobs']:
            jobs.append(dict(title=j['title'], company=j['company_name'], location=j.get('candidate_required_location', ''), description=j['description'], url=j['url'], source='Remotive'))
    except Exception as e:
        errors.append('Remotive: ' + str(e))
    for company in config['lever_companies']:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', company):
            errors.append('Invalid Lever company slug'); continue
        try:
            for j in fetch(f'https://api.lever.co/v0/postings/{company}?mode=json'):
                jobs.append(dict(title=j['text'], company=company, location=j.get('categories', {}).get('location', ''), description=j.get('descriptionPlain', '') + ' ' + plain(j.get('additional', '')) + ' ' + ' '.join(plain(x.get('content','')) for x in j.get('lists',[])), url=j['applyUrl'], source='Lever', remote=j.get('workplaceType') == 'remote'))
        except Exception as e:
            errors.append(company + ': ' + str(e))
    for board in config['greenhouse_boards']:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', board):
            errors.append('Invalid Greenhouse slug'); continue
        try:
            for j in fetch(f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true')['jobs']:
                jobs.append(dict(title=j['title'], company=board, location=j.get('location', {}).get('name', ''), description=plain(j.get('content', '')), url=j['absolute_url'], source='Greenhouse', remote='remote' in (j.get('location', {}).get('name', '') + plain(j.get('content',''))).lower()))
        except Exception as e:
            errors.append(board + ': ' + str(e))
    inbox = ROOT / 'inbox.json'
    if inbox.exists():
        try:
            for job in json.loads(inbox.read_text()):
                if all(isinstance(job.get(k), str) for k in ('title','company','location','description','url')):
                    jobs.append(job)
        except Exception as e:
            errors.append('Inbox: ' + str(e))
    return jobs, errors

from resume import documents

@contextlib.contextmanager
def locked():
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / 'run.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield

def run(fixture=None, prepare_only=False):
    from apply import apply_lever
    with locked(), connect() as db:
        config, profile = load('config.json'), load('profile.json')
        now = dt.datetime.now(ZoneInfo(config['timezone']))
        if not config.get('enabled', True):
            report = {'discovered':0,'prepared':0,'submitted':0,'blocked':0,'errors':[],'paused':True}
            db.execute('INSERT INTO runs VALUES (?,?)',(now.isoformat(),json.dumps(report)))
            print(json.dumps(report)); return
        jobs, errors = (json.loads(Path(fixture).read_text()), []) if fixture else discover(config)
        counts = {'discovered': len(jobs), 'prepared': 0, 'submitted': 0, 'blocked': 0, 'errors': errors}
        for job in jobs:
            if identity(job) in config.get('skipped_job_ids', []): continue
            if not eligible(job, config) or score(job, profile) < config['minimum_score']:
                continue
            db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?)', (identity(job), json.dumps(job), score(job, profile), 'new', '', now.isoformat()))
        db.commit()
        # Crash after a submit click must never cause an automatic duplicate.
        db.execute("UPDATE jobs SET status='uncertain',note='Interrupted attempt; verify with employer before retry' WHERE status='submitting'")
        db.execute("UPDATE attempts SET status='uncertain' WHERE status='submitting'")
        db.commit()
        rows = db.execute("SELECT * FROM jobs WHERE status IN ('new','prepared','blocked') ORDER BY score DESC LIMIT ?", (config['max_prepared_per_run'],)).fetchall()
        for row in rows:
            job = json.loads(row['data'])
            if row['id'] in config.get('skipped_job_ids', []):
                db.execute("UPDATE jobs SET status='skipped' WHERE id=?",(row['id'],)); continue
            folder = STATE / 'applications' / row['id']
            resume = documents(job, profile, folder, config.get('max_projects_on_cv',5))
            counts['prepared'] += 1
            status, note = 'prepared', 'Submission disabled'
            used = db.execute('SELECT count(*) FROM attempts WHERE day=?', (now.date().isoformat(),)).fetchone()[0]
            if config['auto_submit'] and not fixture and not prepare_only:
                if not profile['verified'] or not profile['email']:
                    status, note = 'blocked', 'Complete email and verify profile facts first'
                elif used >= config['max_applications_per_day']:
                    status, note = 'prepared', 'Daily attempt limit reached'
                else:
                    db.execute("UPDATE jobs SET status='submitting' WHERE id=?", (row['id'],))
                    attempt = db.execute('INSERT INTO attempts(job_id,day,status,note) VALUES (?,?,?,?)', (row['id'], now.date().isoformat(), 'submitting', '')).lastrowid
                    db.commit()
                    status, note = apply_lever(job, profile, resume, folder)
                    db.execute('UPDATE attempts SET status=?,note=? WHERE id=?', (status, note, attempt))
            db.execute('UPDATE jobs SET status=?,note=?,updated=? WHERE id=?', (status, note, now.isoformat(), row['id']))
            db.commit()
            counts['submitted'] += status == 'submitted'
            counts['blocked'] += status == 'blocked'
        db.execute('INSERT INTO runs VALUES (?,?)', (now.isoformat(), json.dumps(counts)))
        (STATE / 'latest-run.json').write_text(json.dumps(counts, indent=2))
        print(json.dumps(counts, indent=2))

def staged_apply(job_id, submit=False):
    """Checkpoint a claim before an application, for ephemeral scheduled runners."""
    from apply import apply_lever
    with locked(), connect() as db:
        config, profile = load('config.json'), load('profile.json')
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row: raise ValueError('Unknown job id')
        job = json.loads(row['data'])
        if not config.get('enabled',True) or job_id in config.get('skipped_job_ids',[]):
            raise ValueError('Agent paused or job skipped')
        if not config['auto_submit'] or not profile['verified'] or not profile['email']:
            raise ValueError('Submission disabled or profile incomplete')
        if not eligible(job,config): raise ValueError('Job is no longer eligible')
        if submit:
            if row['status'] != 'submitting': raise ValueError('Claim and persist this job first')
            folder = STATE / 'applications' / job_id
            status,note = apply_lever(job,profile,folder/'resume.pdf',folder)
            db.execute('UPDATE attempts SET status=?,note=? WHERE job_id=? AND status=?', (status,note,job_id,'submitting'))
            db.execute('UPDATE jobs SET status=?,note=? WHERE id=?',(status,note,job_id))
            print(json.dumps({'id':job_id,'status':status,'note':note}))
        else:
            from apply import allowed_url
            if row['status'] not in ('prepared','blocked'): raise ValueError('Job already attempted or unprepared')
            if not allowed_url(job['url']): raise ValueError('Unsupported host')
            day = dt.datetime.now(ZoneInfo(config['timezone'])).date().isoformat()
            used = db.execute('SELECT count(*) FROM attempts WHERE day=?',(day,)).fetchone()[0]
            if used >= config['max_applications_per_day']: raise ValueError('Daily limit reached')
            db.execute("UPDATE jobs SET status='submitting' WHERE id=?",(job_id,))
            db.execute('INSERT INTO attempts(job_id,day,status,note) VALUES (?,?,?,?)',(job_id,day,'submitting',''))
            print(json.dumps({'id':job_id,'status':'submitting','next':'Persist the checkpoint before submit'}))

def daemon():
    while True:
        config = load('config.json')
        now = dt.datetime.now(ZoneInfo(config['timezone']))
        slot = f'{now.date()}-{now.hour}'
        marker = STATE / 'last-slot.txt'
        if now.hour in config['hours'] and (not marker.exists() or marker.read_text() != slot):
            try:
                run(); marker.write_text(slot)
            except Exception as e:
                print('Run failed:', type(e).__name__, str(e), flush=True)
        time.sleep(30)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['run', 'prepare', 'claim', 'submit', 'daemon', 'status'])
    parser.add_argument('--fixture')
    parser.add_argument('--job-id')
    args = parser.parse_args()
    if args.command == 'run':
        if args.fixture:
            import tempfile
            with tempfile.TemporaryDirectory() as temp:
                STATE = Path(temp)
                run(args.fixture)
        else:
            run()
    elif args.command == 'prepare': run(prepare_only=True)
    elif args.command in ('claim','submit'):
        if not args.job_id: parser.error('--job-id is required')
        staged_apply(args.job_id,args.command == 'submit')
    elif args.command == 'daemon': daemon()
    else:
        with connect() as db:
            for row in db.execute('SELECT id,status,note FROM jobs ORDER BY updated DESC'):
                print(dict(row))
