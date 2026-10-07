"""Synchronize private activity artifacts with the cloud dashboard."""
import argparse
import datetime as dt
import importlib.util
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
import agent


def request(path, payload=None, content_type='application/json'):
    origin = os.environ.get('DASHBOARD_URL', '').rstrip('/')
    parsed = urlparse(origin)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.chatgpt.site'):
        raise ValueError('DASHBOARD_URL must be the private HTTPS Site origin')
    if parsed.path not in ('', '/') or parsed.username or parsed.password:
        raise ValueError('Invalid dashboard origin')
    token = os.environ.get('DASHBOARD_SERVICE_TOKEN', '')
    if not token: raise ValueError('Acquire private Site service access before syncing')
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(origin + path, data=body, headers={'Content-Type':content_type,'OAI-Sites-Authorization':'Bearer '+token}, method='POST' if body is not None else 'GET')
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def pull():
    state = request('/api/state')
    settings = state['settings']
    if not isinstance(settings['enabled'], bool) or not isinstance(settings['auto_submit'], bool):
        raise ValueError('Invalid cloud controls')
    if not isinstance(settings['max_applications_per_day'], int) or not 1 <= settings['max_applications_per_day'] <= 5:
        raise ValueError('Invalid application limit')
    config = agent.load('config.json')
    for key in ('enabled','auto_submit','max_applications_per_day','skipped_job_ids'):
        config[key] = settings[key]
    path = agent.ROOT / 'config.json'
    pending = path.with_suffix('.tmp')
    pending.write_text(json.dumps(config, indent=2));pending.replace(path)
    print(json.dumps({'enabled':config['enabled'],'auto_submit':config['auto_submit'],'daily_limit':config['max_applications_per_day'],'run_requested_at':settings.get('run_requested_at')}))
    return state


def snapshot():
    with agent.connect() as db:
        rows = db.execute('SELECT * FROM jobs ORDER BY updated DESC LIMIT 1000').fetchall()
        jobs = []
        for row in rows:
            j = json.loads(row['data']); folder = agent.STATE / 'applications' / row['id']
            jobs.append({'id':row['id'],'title':j['title'],'company':j['company'],'location':j['location'],'url':j['url'],'score':row['score'],'status':row['status'],'note':row['note'],'updated_at':row['updated'],'has_cv':(folder/'resume.pdf').exists()})
        runs = [{'at':r['at'],'report':json.loads(r['report'])} for r in db.execute('SELECT * FROM runs ORDER BY rowid DESC LIMIT 200')]
    available = False
    if importlib.util.find_spec('playwright'):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            available = Path(p.chromium.executable_path).exists()
    note = 'Browser is installed. Applications still stop on missing answers or CAPTCHA.' if available else 'Browser unavailable: job search and CV preparation work; live submission is blocked.'
    return {'status':'ready' if available else 'browser_unavailable','jobs':jobs,'runs':runs,'runtime_note':note,'counts':runs[0]['report'] if runs else {}}


def push():
    data = snapshot()
    # Upload documents first so the dashboard never advertises an absent CV.
    for j in data['jobs']:
        if j['has_cv']:
            pdf = agent.STATE / 'applications' / j['id'] / 'resume.pdf'
            request('/api/cv/'+j['id'],pdf.read_bytes(),'application/pdf')
    result = request('/api/snapshot',data)
    print(json.dumps(result))


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['pull','push']);args=parser.parse_args()
    if args.command=='pull': pull()
    else: push()
