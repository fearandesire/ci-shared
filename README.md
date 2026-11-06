# ci-shared

Shared GitHub Actions workflows.

## ready-when-green

Flips a draft PR to ready for review once every check on its head commit is green.
Only touches same-repo PRs on `agent/*`, `claude/*` or `codex/*` branches, and skips any PR
labelled `agent-needs-human`.

Add it to a repo (opens a PR with the caller stub; dry run unless `--apply`):

```bash
CENTRAL=fearandesire/ci-shared REF=v1 ./rollout.sh --apply owner/repo [owner/repo ...]
```

The stub lists every workflow in the target repo under `workflow_run`; the last one to finish
re-checks the whole check set. Trim that list if it is noisy.

Notes:
- Both the caller and reusable job need `actions: read`: `gh pr list --json
  statusCheckRollup` also queries `checkSuite.workflowRun`. Without it, GitHub returns
  `Resource not accessible by integration` before the PR can be marked ready.
  Existing callers must add this permission as well as adopt the updated reusable workflow;
  a called workflow cannot increase its caller's permissions. Normal inline rollouts include
  it too. For `--readonly`, the supplied `READY_TOKEN` needs the corresponding Actions read
  access in addition to its PR/check/status permissions.
- Uses `GITHUB_TOKEN` with `contents: write` (needed by `gh pr ready`). Pass a `READY_TOKEN`
  secret (PAT or App token) only if `ready_for_review` workflows must fire.
- This repo is private. Its Actions access is set to "accessible from repositories owned by the
  user", so only private repos under the same account can call it.
- Pin callers to a tag (`@v1`). Move the tag deliberately after testing a change.
