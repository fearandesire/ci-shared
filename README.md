# ci-shared

Public, centrally maintained GitHub Actions workflows. Repository callers track the
tested `v2` channel; fixes reach those callers on their next workflow run without
copying scripts or editing every repository.

## ready-when-green

Marks a draft PR ready only when its current head matches the completed successful
workflow run and every check/status on that head is complete and green, neutral or
skipped. Restricts promotion to same-repository `agent/*`, `claude/*` and `codex/*`
branches. `agent-needs-human` holds the PR. Empty check sets, fork heads, changed
heads, API errors and uninspected label pages never promote. Check results paginate
past 100 so a late failure cannot be missed.

The caller token needs `contents: write`, `pull-requests: write`, `checks: read` and
`statuses: read`. **Actions read is not needed.** The implementation requests only
check/status fields; `gh pr list --json statusCheckRollup` expands workflow metadata
and requires an extra permission, so do not reintroduce that expansion.

`READY_TOKEN` is optional when `ready_for_review` must trigger other Actions
workflows, or required for a repository whose policy keeps `GITHUB_TOKEN` read-only.
Never print the token. The readonly inline mode preserves that repository policy;
the supplied token must have the same effective read/write access as above.

## Install or refresh callers

Dry run first; add `--apply` to update the existing rollout branch/PR or open one:

```bash
CENTRAL=fearandesire/ci-shared REF=v2 ./rollout.sh owner/repo
CENTRAL=fearandesire/ci-shared REF=v2 ./rollout.sh --apply owner/repo
```

The caller listens for the target repository's workflow names and invokes the one
shared implementation. Prefer reusable callers. Use `--inline` only when repository
policy forbids reusable jobs, and `--readonly` when that inline job must also retain
a read-only `GITHUB_TOKEN`. Refresh inline copies after shared implementation changes.
The renderer compares actual bytes instead of skipping merely because a file already
names the channel or has an inline header.

## Audit all rollout repositories

```bash
python3 audit.py
python3 audit.py --json --check --expected-sha <tested-release-commit>
```

`rollout-repos.json` inventories the 19 rollout repositories. The audit is read-only:
it reports installed callers, open rollout PRs, reference/permission drift and inline
copies. `current-token-required` verifies inline bytes, not the secret's existence or
permissions; verify those with a real canary in that repository.

## Test and advance the release channel

```bash
python3 -m unittest discover -s tests -v
bash -n rollout.sh
```

PR CI runs regression cases and executes the candidate query using a real token with
no Actions permission. After merge, create a draft PR on `agent/ready-canary-*`, wait
for its checks, and dispatch `ready-canary` with its PR number. The canary uses the
original caller permissions, can touch only that dedicated branch prefix in this
repository, and fails unless the PR actually becomes ready. Close the canary afterward.

Only after the canary succeeds, advance `v2` to the exact tested commit. Existing
version-specific immutable tags remain immutable; `v2` is the deliberately moving
compatibility channel. Run the audit, refresh inline consumers, and verify actual
consumer workflow runs. Changes to the permission contract require a new channel and
an explicit migration instead of silently breaking existing callers.
