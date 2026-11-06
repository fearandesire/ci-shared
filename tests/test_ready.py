import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/ready-when-green.yml'
CALLER_TEMPLATE = ROOT / 'caller.yml.tmpl'
INLINE_TEMPLATE = ROOT / 'inline.yml.tmpl'
TEST_WORKFLOW = ROOT / '.github/workflows/test.yml'
CANARY_WORKFLOW = ROOT / '.github/workflows/ready-canary.yml'


def script():
    text = WORKFLOW.read_text().split('        run: |\n', 1)[1]
    return ''.join(line[10:] if line.startswith('          ') else line for line in text.splitlines(True))


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
a = sys.argv[1:]
f = json.loads(pathlib.Path(os.environ['FIXTURE']).read_text())
with open(os.environ['CALLS'], 'a') as out:
    out.write(json.dumps(a) + '\n')
def emit(data):
    p = subprocess.run(['jq', '-r', a[a.index('--jq')+1]], input=json.dumps(data), text=True)
    if p.returncode:
        sys.exit(p.returncode)
if a[:2] == ['pr', 'list']:
    assert 'statusCheckRollup' not in a[a.index('--json')+1]
    emit(f['candidates'])
elif a[:2] == ['pr', 'view']:
    assert '--repo' in a and a[a.index('--repo')+1] == os.environ['GH_REPO']
    print(json.dumps(f['manual_pr']))
elif a[:2] == ['api', 'graphql']:
    query = next(x[6:] for x in a if x.startswith('query='))
    assert 'workflowRun' not in query and 'checkSuite' not in query
    assert 'after: $endCursor' in query and '--paginate' in a
    if f.get('api_error'):
        print('GraphQL query failed', file=sys.stderr)
        sys.exit(1)
    rounds = f.get('rounds')
    if rounds:
        calls = [json.loads(line) for line in pathlib.Path(os.environ['CALLS']).read_text().splitlines()]
        index = sum(c[:2] == ['api', 'graphql'] for c in calls) - 1
        pages = rounds[min(index, len(rounds)-1)]
    else:
        pages = f['pages']
    for page in pages:
        emit(page)
elif a[:2] == ['pr', 'ready']:
    if f.get('mutation_error'):
        sys.exit(1)
    print('ready', a[2])
else:
    raise AssertionError(a)
'''


def fixture():
    return {
        'candidates': [{'number': 11, 'headRefOid': 'abc123', 'labels': []}],
        'manual_pr': {'number': 11, 'state': 'OPEN', 'isDraft': True, 'headRefName': 'agent/example',
                      'headRefOid': 'abc123', 'headRepository': {'name': 'repo'},
                      'headRepositoryOwner': {'login': 'owner'}},
        'pages': [{'data': {'repository': {'pullRequest': {
            'state': 'OPEN', 'headRefOid': 'abc123', 'isDraft': True,
            'headRepository': {'nameWithOwner': 'owner/repo'},
            'labels': {'nodes': [], 'labelPage': {'hasNextPage': False}},
            'commits': {'nodes': [{'commit': {'statusCheckRollup': {'contexts': {
                'nodes': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}],
                'pageInfo': {'hasNextPage': False, 'endCursor': None},
            }}}}]},
        }}}}],
    }


def pr(data, page=0):
    return data['pages'][page]['data']['repository']['pullRequest']


def checks(data, page=0):
    return pr(data, page)['commits']['nodes'][0]['commit']['statusCheckRollup']['contexts']['nodes']


class ReadyTests(unittest.TestCase):
    def run_case(self, data, dry=False, wait=0, event='workflow_run', pr_input='', app_slug='external-provider',
                 branch='agent/example', sha='abc123'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = root / 'gh'
            gh.write_text(FAKE_GH)
            gh.chmod(0o755)
            (root / 'fixture.json').write_text(json.dumps(data))
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       FIXTURE=str(root / 'fixture.json'), CALLS=str(root / 'calls.jsonl'),
                       GH_REPO='owner/repo', BRANCH=branch, SHA=sha,
                       EVENT_NAME=event, PR_NUMBER=pr_input,
                       RUN_HEAD_REPOSITORY='owner/repo', CHECK_SUITE_APP_SLUG=app_slug,
                       DRY_RUN='1' if dry else '0', WAIT_SECONDS=str(wait), POLL_SECONDS='0')
            result = subprocess.run(['bash', '-c', script()], env=env, text=True, capture_output=True)
            calls_file = root / 'calls.jsonl'
            calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
            return result, [c for c in calls if c[:2] == ['pr', 'ready']]

    def assert_held(self, data):
        result, mutations = self.run_case(data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mutations, [])

    def test_green_skipped_neutral_and_status_success_promote(self):
        data = fixture()
        checks(data).extend([{'status': 'COMPLETED', 'conclusion': value}
                             for value in ('SKIPPED', 'NEUTRAL')])
        checks(data).append({'state': 'SUCCESS'})
        result, mutations = self.run_case(data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mutations, [['pr', 'ready', '11']])

    def test_check_suite_completion_wakes_and_checks_current_pr(self):
        result, mutations = self.run_case(fixture(), event='check_suite')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mutations, [['pr', 'ready', '11']])

    def test_check_suite_failure_and_ineligible_branch_do_not_promote(self):
        data = fixture()
        checks(data).append({'status': 'COMPLETED', 'conclusion': 'FAILURE'})
        result, mutations = self.run_case(data, event='check_suite')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mutations, [])
        result, mutations = self.run_case(fixture(), event='check_suite', branch='wip/example')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no eligible source branch', result.stdout)
        self.assertEqual(mutations, [])

    def test_github_actions_check_suite_does_not_self_trigger(self):
        result, mutations = self.run_case(fixture(), event='check_suite', app_slug='github-actions')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ignoring a GitHub Actions check suite', result.stdout)
        self.assertEqual(mutations, [])

    def test_manual_positive_pr_number_resolves_same_repo_draft(self):
        result, mutations = self.run_case(fixture(), event='workflow_dispatch', pr_input='11')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mutations, [['pr', 'ready', '11']])

    def test_manual_missing_nonnumeric_and_nonpositive_pr_numbers_fail_closed(self):
        for number in ('', 'abc', '0', '-2', ' 11'):
            with self.subTest(number=number):
                result, mutations = self.run_case(fixture(), event='workflow_dispatch', pr_input=number)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(mutations, [])

    def test_manual_fork_and_unallowed_branch_are_refused(self):
        for field, value in ((('headRepositoryOwner', 'login'), 'attacker'),
                             (('headRefName',), 'wip/example'),
                             (('state',), 'CLOSED')):
            data = fixture()
            if len(field) == 2:
                data['manual_pr'][field[0]][field[1]] = value
            else:
                data['manual_pr'][field[0]] = value
            result, mutations = self.run_case(data, event='workflow_dispatch', pr_input='11')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('not an eligible same-repository draft', result.stdout)
            self.assertEqual(mutations, [])

    def test_manual_stale_head_hold_and_failed_check_do_not_promote(self):
        data = fixture()
        pr(data)['headRefOid'] = 'raced-head'
        result, mutations = self.run_case(data, event='workflow_dispatch', pr_input='11')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mutations, [])
        data = fixture()
        pr(data)['labels']['nodes'].append({'name': 'agent-needs-human'})
        result, mutations = self.run_case(data, event='workflow_dispatch', pr_input='11')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mutations, [])
        data = fixture()
        checks(data).append({'status': 'COMPLETED', 'conclusion': 'FAILURE'})
        result, mutations = self.run_case(data, event='workflow_dispatch', pr_input='11')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mutations, [])

    def test_pending_failed_cancelled_and_unknown_checks_hold(self):
        for check in ({'status': 'IN_PROGRESS'}, {'state': 'PENDING'},
                      {'state': 'FAILURE'}, {'status': 'COMPLETED', 'conclusion': 'FAILURE'},
                      {'status': 'COMPLETED', 'conclusion': 'CANCELLED'}, {}):
            with self.subTest(check=check):
                data = fixture()
                checks(data).append(check)
                self.assert_held(data)

    def test_zero_checks_hold(self):
        data = fixture()
        checks(data).clear()
        self.assert_held(data)

    def test_hold_label_before_or_after_candidate_query(self):
        data = fixture()
        data['candidates'][0]['labels'] = [{'name': 'agent-needs-human'}]
        self.assert_held(data)
        data = fixture()
        pr(data)['labels']['nodes'].append({'name': 'agent-needs-human'})
        self.assert_held(data)

    def test_changed_head_and_non_draft_hold(self):
        for field, value in (('headRefOid', 'new-head'), ('isDraft', False)):
            data = fixture()
            pr(data)[field] = value
            self.assert_held(data)

    def test_forks_and_uninspected_labels_hold(self):
        data = fixture()
        pr(data)['headRepository']['nameWithOwner'] = 'fork/repo'
        self.assert_held(data)
        data = fixture()
        pr(data)['labels']['labelPage']['hasNextPage'] = True
        self.assert_held(data)

    def test_failure_beyond_first_page_holds(self):
        data = fixture()
        checks(data)[:] = checks(data) * 100
        data['pages'].append(copy.deepcopy(data['pages'][0]))
        checks(data, 1)[:] = [{'status': 'COMPLETED', 'conclusion': 'FAILURE'}]
        self.assert_held(data)

    def test_all_green_pages_promote(self):
        data = fixture()
        data['pages'].append(copy.deepcopy(data['pages'][0]))
        result, mutations = self.run_case(data)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(mutations), 1)

    def test_api_failure_and_ready_failure_fail_job(self):
        for failure in ('api_error', 'mutation_error'):
            data = fixture()
            data[failure] = True
            result, mutations = self.run_case(data)
            self.assertNotEqual(result.returncode, 0)
            if failure == 'api_error':
                self.assertEqual(mutations, [])

    def test_dry_run_never_mutates(self):
        result, mutations = self.run_case(fixture(), dry=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('would mark #11 ready', result.stdout)
        self.assertEqual(mutations, [])

    def test_permission_contract_and_same_repo_agent_guard(self):
        text = WORKFLOW.read_text()
        self.assertNotIn('      actions:', text)
        self.assertIn("github.event_name == 'workflow_run'", text)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", text)
        self.assertIn("github.event_name == 'check_suite'", text)
        self.assertIn("github.event.check_suite.app.slug != 'github-actions'", text)
        self.assertIn("github.event_name == 'workflow_dispatch'", text)
        for prefix in ('agent/', 'claude/', 'codex/'):
            self.assertIn("head_branch, '" + prefix + "'", text)

    def test_caller_templates_preserve_workflow_run_and_add_external_and_manual_wakeups(self):
        for template in (CALLER_TEMPLATE, INLINE_TEMPLATE):
            text = template.read_text()
            self.assertIn('  workflow_run:', text)
            self.assertIn('  check_suite:', text)
            self.assertIn('    types: [completed]', text)
            self.assertIn('  workflow_dispatch:', text)
            self.assertIn('      pr:', text)
            self.assertIn('        type: string', text)

    def test_repository_script_callers_provide_event_mode(self):
        self.assertIn('EVENT_NAME: workflow_run', TEST_WORKFLOW.read_text())
        self.assertIn('RUN_HEAD_REPOSITORY:', TEST_WORKFLOW.read_text())
        self.assertIn('EVENT_NAME: workflow_dispatch', CANARY_WORKFLOW.read_text())

    def test_pending_review_is_rechecked_then_promoted(self):
        data = fixture()
        pending = copy.deepcopy(data['pages'])
        pending[0]['data']['repository']['pullRequest']['commits']['nodes'][0]['commit']['statusCheckRollup']['contexts']['nodes'].append({'status': 'IN_PROGRESS'})
        data['rounds'] = [pending, data['pages']]
        result, mutations = self.run_case(data, wait=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rechecking', result.stdout)
        self.assertEqual(len(mutations), 1)

    def test_head_or_hold_change_while_waiting_never_promotes(self):
        for change in ('head', 'hold'):
            data = fixture()
            pending = copy.deepcopy(data['pages'])
            pending[0]['data']['repository']['pullRequest']['commits']['nodes'][0]['commit']['statusCheckRollup']['contexts']['nodes'].append({'status': 'IN_PROGRESS'})
            changed = copy.deepcopy(data['pages'])
            if change == 'head':
                changed[0]['data']['repository']['pullRequest']['headRefOid'] = 'new-head'
            else:
                changed[0]['data']['repository']['pullRequest']['labels']['nodes'] = [{'name': 'agent-needs-human'}]
            data['rounds'] = [pending, changed]
            result, mutations = self.run_case(data, wait=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(mutations, [])

    def test_dry_run_does_not_wait_on_its_own_check(self):
        data = fixture()
        checks(data).append({'status': 'IN_PROGRESS'})
        result, mutations = self.run_case(data, dry=True, wait=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('rechecking', result.stdout)
        self.assertEqual(mutations, [])


if __name__ == '__main__':
    if sys.argv[1:] == ['--emit-script']:
        print(script(), end='')
    else:
        unittest.main()
