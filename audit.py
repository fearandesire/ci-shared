#!/usr/bin/env python3
"""Read-only inventory of ready-when-green callers, including open rollout PRs."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent
FILE = '.github/workflows/ready-when-green.yml'


def gh(*args):
    result = subprocess.run(['gh', *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def run_block(text):
    return text.split('        run: |\n', 1)[-1].rstrip()


def inspect(repo, ref):
    row = {'repo': repo, 'installed': True}
    owner_controlled = repo.lower() in {
        name.lower() for name in json.loads((ROOT / 'owner-controlled-repos.json').read_text())
    }
    try:
        try:
            entry = gh('api', f'repos/{repo}/contents/{FILE}')
        except RuntimeError as error:
            if '404' not in str(error):
                raise
            prs = gh('pr', 'list', '--repo', repo, '--state', 'open', '--limit', '100',
                     '--json', 'number,title,headRefName,url')
            prs = [p for p in prs if p['title'] == 'ci: mark agent draft PRs ready when checks pass']
            if owner_controlled:
                return dict(row, installed=False, mode='owner-controlled',
                            status='retired-pending-rollout' if prs else 'owner-controlled',
                            pendingPRs=prs)
            if len(prs) != 1:
                return dict(row, installed=False, status='missing', pendingPRs=prs)
            row.update(installed=False, pr=prs[0]['number'], url=prs[0]['url'])
            entry = gh('api', f'repos/{repo}/contents/{FILE}?ref={prs[0]["headRefName"]}')
        if owner_controlled:
            return dict(row, mode='owner-controlled', status='retired-caller-installed')
        body = base64.b64decode(entry['content']).decode()
        uses = re.findall(r'(?m)^\s*uses:\s*(\S*ci-shared/\S+)\s*$', body)
        if uses:
            expected = f'fearandesire/ci-shared/{FILE}@{ref}'
            row.update(mode='shared', ref=uses[0])
            permissions = ('contents: write', 'pull-requests: write', 'checks: read', 'statuses: read')
            row['status'] = 'current' if uses == [expected] and all(p in body for p in permissions) else 'drift'
        else:
            readonly = 'pull-requests: write' not in body
            row.update(mode='inline-token' if readonly else 'inline')
            equal = run_block(body) == run_block((ROOT / FILE).read_text())
            row['status'] = 'current-token-required' if equal and readonly else ('current' if equal else 'drift')
        if not row['installed']:
            row['status'] = 'pending-' + row['status']
        return row
    except (RuntimeError, ValueError, KeyError) as error:
        return dict(row, status='error', error=str(error)[:500])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repos-file', type=Path, default=ROOT / 'rollout-repos.json')
    parser.add_argument('--ref', default='v2')
    parser.add_argument('--expected-sha', help='Verify the shared release tag resolves to this tested commit')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--check', action='store_true', help='Fail for missing, pending or drifting callers')
    args = parser.parse_args()
    repos = json.loads(args.repos_file.read_text())
    with ThreadPoolExecutor(max_workers=5) as pool:
        rows = list(pool.map(lambda repo: inspect(repo, args.ref), repos))
    tag = gh('api', f'repos/fearandesire/ci-shared/git/ref/tags/{args.ref}')['object']
    if tag['type'] == 'tag':
        tag = gh('api', f'repos/fearandesire/ci-shared/git/tags/{tag["sha"]}')['object']
    result = {'ref': args.ref, 'resolvedSha': tag['sha'], 'repos': rows}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f'{args.ref}: {tag["sha"]}')
        for row in rows:
            print(f'{row["repo"]:45} {row["status"]:30} {row.get("mode", "")}')
    bad = any(row['status'] not in ('current', 'current-token-required', 'owner-controlled') for row in rows)
    bad = bad or bool(args.expected_sha and args.expected_sha != tag['sha'])
    return 1 if args.check and bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
