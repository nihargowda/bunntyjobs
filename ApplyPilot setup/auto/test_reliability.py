"""Regression tests; all state lives in temporary folders."""
import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import apply_control as control
import dashboard
from job_url import app_key


class Reliability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'auto').mkdir()
        (self.root / 'Applications').mkdir()
        self.log = self.root / 'Applications/applications_log.csv'
        self.patches = [patch.object(control, 'ROOT', self.root),
                        patch.object(control, 'STATE', self.root / 'auto/control.sqlite3'),
                        patch.object(control, 'day', return_value='2026-10-27'),
                        patch.object(dashboard, 'APPS_CSVS', {'Codex': self.log})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def log_rows(self, rows):
        with self.log.open('w', newline='') as f:
            csv.writer(f).writerows([['date', 'company', 'title', 'status', 'notes'], *rows])

    def test_distinct_employers_do_not_collapse(self):
        self.assertNotEqual(app_key('First Example Bank', 'PM'), app_key('First National Bank', 'PM'))
        self.assertEqual(app_key('Quillo Technologies', 'Sr. PM'), app_key('Quillo', 'Senior Product Manager'))

    def test_aliases_cannot_claim_same_job_under_different_links(self):
        self.assertTrue(control.claim('Claude', 'https://example.com/1', 'Quillo Technologies', 'Sr. PM')['ok'])
        self.assertFalse(control.claim('Codex', 'https://example.com/2', 'Quillo', 'Senior Product Manager')['ok'])

    def test_relogged_submission_not_counted_again_today(self):
        self.log_rows([['2026-10-26', 'Acme', 'PM', 'submitted', ''],
                       ['2026-10-27', 'Acme', 'PM', 'submitted', ''],
                       ['2026-10-27', 'Other', 'PM', 'submitted', '']])
        self.assertEqual(control.progress(), 1)

    def test_waiting_does_not_hide_confirmed_submission(self):
        self.log_rows([['2026-10-27', 'Acme', 'PM', 'submitted', 'confirmed'],
                       ['2026-10-27', 'Acme', 'PM', 'WAITING - needs you', 'stale']])
        self.assertEqual(dashboard.read_applications()['rows'][0]['kind'], 'applied')


class LightweightStatus(unittest.TestCase):
    def test_payload_never_loads_jobs_or_scans_pipeline(self):
        with patch.object(dashboard, 'load_jobs', side_effect=AssertionError('heavy jobs load')), \
             patch.object(dashboard, 'read_activity', side_effect=AssertionError('pipeline scan')), \
             patch.object(dashboard, 'running_procs', side_effect=AssertionError('process scan')), \
             patch.object(dashboard, 'read_applications', return_value={'rows': []}), \
             patch.object(dashboard, 'inbox_status', return_value={'todo': []}), \
             patch.object(control, 'status', return_value={'submitted': 2}), \
             patch.object(control, 'day', return_value='2026-10-27'), \
             patch.object(dashboard, 'application_ledger', return_value={
                 'a': {'date': '2026-10-27'}, 'b': {'date': '2026-10-27'},
                 'c': {'date': '2026-10-26'}}):
            result = dashboard.application_status()
        self.assertNotIn('jobs', result)
        self.assertEqual(result['appliedByDay'], {'2026-10-27': 2, '2026-10-26': 1})
        self.assertEqual(result['today'], '2026-10-27')

    def test_http_status_route_bypasses_full_cache_and_disables_browser_caching(self):
        import io
        import json
        handler = dashboard.H.__new__(dashboard.H)
        handler.path = '/application-status'
        handler.wfile = io.BytesIO()
        handler.send_response = unittest.mock.Mock()
        handler.send_header = unittest.mock.Mock()
        handler.end_headers = unittest.mock.Mock()
        with patch.object(dashboard, 'application_status', return_value={'apps': {'rows': []}}), \
             patch.object(dashboard, 'cached_data', side_effect=AssertionError('full rebuild')):
            handler.do_GET()
        self.assertEqual(json.loads(handler.wfile.getvalue()), {'apps': {'rows': []}})
        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_any_call('Cache-Control', 'no-store')

    def test_client_refresh_preserves_jobs_and_skips_unchanged_render(self):
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js unavailable')
        code = dashboard.PAGE.split("let applicationsBusy=false,applicationsVersion='';", 1)[1]
        code = "let applicationsBusy=false,applicationsVersion='';" + code.split('(async function loop()', 1)[0]
        harness = """
const assert=require('node:assert/strict');
const jobs=[{url:'keep-me'}];let D={jobs,ver:'full-version'},todayStr='old',calls=0;
const elements={appupd:{},k7:{}};const $=id=>elements[id];
const today=()=>calls++,inbox=()=>{},recruit=()=>{},daily=()=>{},badges=()=>{},controls=()=>{};
const anim=(el,n)=>el.value=n;
const payload={apps:{rows:[]},inbox:{todo:[]},applicationControl:{submitted:2},
 appliedByDay:{'2026-10-27':2},today:'2026-10-27'};
let fetch=async()=>({ok:true,json:async()=>payload});
""" + code + """
(async()=>{
 await refreshApplications();assert.equal(D.jobs,jobs);assert.equal(D.ver,'full-version');
 assert.equal(elements.k7.value,2);assert.equal(todayStr,'2026-10-27');assert.equal(calls,1);
 await refreshApplications();assert.equal(calls,1);
 fetch=async()=>{throw new Error('offline')};await refreshApplications();
 assert.match(elements.appupd.textContent,/retrying/);assert.equal(applicationsBusy,false);
 fetch=async()=>({ok:true,json:async()=>payload});await refreshApplications();
 assert.equal(elements.appupd.title,'Application status refreshed');assert.equal(calls,2);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result = subprocess.run([node, '-e', harness], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_busy_status_database_returns_retryable_error(self):
        import io
        import json
        import sqlite3
        handler = dashboard.H.__new__(dashboard.H)
        handler.path = '/application-status'
        handler.wfile = io.BytesIO()
        handler.send_response = unittest.mock.Mock()
        handler.send_header = unittest.mock.Mock()
        handler.end_headers = unittest.mock.Mock()
        with patch.object(dashboard, 'application_status', side_effect=sqlite3.OperationalError('busy')):
            handler.do_GET()
        self.assertEqual(json.loads(handler.wfile.getvalue()), {'error': 'busy'})


if __name__ == '__main__':
    unittest.main()
