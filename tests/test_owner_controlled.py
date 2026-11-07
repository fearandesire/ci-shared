import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('audit', ROOT / 'audit.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class OwnerControlledTests(unittest.TestCase):
    def test_adopted_inventory_preserved(self):
        names = json.loads((ROOT / 'owner-controlled-repos.json').read_text())
        self.assertEqual(len(names), 5)
        self.assertTrue(set(names) <= set(json.loads((ROOT / 'rollout-repos.json').read_text())))

    def test_rollout_skips_all_modes_before_external_calls(self):
        with tempfile.TemporaryDirectory() as d:
            gh = Path(d) / 'gh'
            gh.write_text('#!/bin/sh\necho external-call >&2\nexit 99\n')
            gh.chmod(0o755)
            env = dict(os.environ, CENTRAL='fearandesire/ci-shared', REF='v2',
                       PATH=d + ':' + os.environ['PATH'])
            repos = [s.upper() for s in json.loads((ROOT / 'owner-controlled-repos.json').read_text())]
            for mode in ([], ['--apply'], ['--apply', '--inline'], ['--apply', '--readonly']):
                run = subprocess.run(['bash', str(ROOT / 'rollout.sh'), *mode, *repos],
                                     env=env, capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stdout.count('rollout skipped'), len(repos))
                self.assertNotIn('external-call', run.stderr)
            run = subprocess.run(['bash', str(ROOT / 'rollout.sh'), 'other/repo'],
                                 env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 99)

    def test_intentional_absence_is_healthy(self):
        with patch.object(audit, 'gh', side_effect=[RuntimeError('404'), []]):
            self.assertEqual(audit.inspect('fearandesire/QUIETDIFF', 'v2')['status'], 'owner-controlled')

    def test_retired_installed_caller_is_conflict(self):
        with patch.object(audit, 'gh', return_value={}):
            self.assertEqual(audit.inspect('fearandesire/ai', 'v2')['status'], 'retired-caller-installed')

    def test_pending_restoration_is_conflict(self):
        prs = [{'title': 'ci: mark agent draft PRs ready when checks pass', 'number': 1}]
        with patch.object(audit, 'gh', side_effect=[RuntimeError('404'), prs]):
            row = audit.inspect('fearandesire/khronos', 'v2')
            self.assertEqual(row['status'], 'retired-pending-rollout')
            self.assertEqual(row['pendingPRs'], prs)

    def test_legacy_missing_still_fails(self):
        with patch.object(audit, 'gh', side_effect=[RuntimeError('404'), []]):
            self.assertEqual(audit.inspect('other/repo', 'v2')['status'], 'missing')
