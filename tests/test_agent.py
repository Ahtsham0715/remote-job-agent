import unittest
import sys
import json
import tempfile
import contextlib
import io
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import eligible, score, identity, load
from apply import allowed_url, answer_for
import agent

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.config = load('config.json')
        self.profile = load('profile.json')
        self.job = dict(title='Flutter Engineer', location='Worldwide', description='Flutter Dart Firebase', url='https://example.com/job')
    def test_geography(self):
        self.assertTrue(eligible(self.job, self.config))
        for location in ('US', 'Europe', '', 'Asia', 'Not worldwide'):
            j = dict(self.job, location=location)
            self.assertFalse(eligible(j, self.config))
        self.assertFalse(eligible(dict(self.job, description='Flutter hybrid'), self.config))
    def test_score_and_identity(self):
        self.assertEqual(score(self.job, self.profile), 40)
        self.assertEqual(identity(self.job), identity(dict(self.job, url=self.job['url']+'/')))
        lever = dict(self.job,url='https://jobs.lever.co/test/123')
        self.assertEqual(identity(lever),identity(dict(lever,url=lever['url']+'/apply?source=search')))
    def test_submission_hosts(self):
        self.assertTrue(allowed_url('https://jobs.lever.co/acme/123/apply'))
        for url in ('http://jobs.lever.co/a', 'https://jobs.lever.co.evil.test/a', 'https://user@jobs.lever.co/a', 'https://127.0.0.1/a'):
            self.assertFalse(allowed_url(url))
    def test_no_invented_answers(self):
        self.assertIsNone(answer_for('sponsorship','Do you need sponsorship?',self.profile))
        self.assertEqual(answer_for('email','Email',self.profile), self.profile['email'])

    def test_interrupted_checkpoint_is_not_retried(self):
        job = dict(self.job, company='Test', url='https://jobs.lever.co/test/id/apply')
        profile = dict(self.profile, verified=True, email='candidate@example.com')
        with tempfile.TemporaryDirectory() as temp, patch.object(agent,'STATE',Path(temp)), patch.object(agent,'load',side_effect=lambda n: self.config if n=='config.json' else profile), patch.object(agent,'discover',return_value=([],[])), contextlib.redirect_stdout(io.StringIO()):
            with agent.connect() as db:
                db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',(identity(job),json.dumps(job),30,'prepared','','now'))
            agent.staged_apply(identity(job))
            with self.assertRaises(ValueError): agent.staged_apply(identity(job))
            agent.run(prepare_only=True)
            with agent.connect() as db:
                self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'uncertain')
                self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],1)
            with self.assertRaises(ValueError): agent.staged_apply(identity(job),submit=True)

if __name__ == '__main__': unittest.main()
