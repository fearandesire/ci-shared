#!/usr/bin/env bash
# Open a PR in each target repo adding the ready-when-green caller stub.
# Dry run by default: prints the rendered stub and changes nothing.
#
#   CENTRAL=owner/central-repo REF=v1 ./rollout.sh [--apply] owner/repo [owner/repo ...]
set -euo pipefail

CENTRAL="${CENTRAL:?set CENTRAL=owner/central-repo}"
REF="${REF:?set REF to a tag or commit SHA of the central repo}"
APPLY=0
if [ "${1:-}" = "--apply" ]; then APPLY=1; shift; fi
[ "$#" -gt 0 ] || { echo "usage: $0 [--apply] owner/repo ..." >&2; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
TMPL="$(cat "$HERE/caller.yml.tmpl")"
FILE=".github/workflows/ready-when-green.yml"
BRANCH="chore/ready-when-green"

for repo in "$@"; do
  echo "== $repo"
  def=$(gh repo view "$repo" --json defaultBranchRef --jq .defaultBranchRef.name)

  if gh api "repos/$repo/contents/$FILE?ref=$def" >/dev/null 2>&1; then
    echo "   already has $FILE, skipping"; continue
  fi

  # Every workflow in the repo except the stub itself, as JSON strings (valid YAML flow scalars).
  list=$(gh workflow list -R "$repo" --all --json name,path \
    --jq "[.[] | select(.path != \"$FILE\") | .name | @json] | join(\", \")")
  if [ -z "$list" ]; then echo "   no workflows to watch, skipping"; continue; fi

  stub="${TMPL//__WORKFLOWS__/"$list"}"
  stub="${stub//__CENTRAL__/"$CENTRAL"}"
  stub="${stub//__REF__/"$REF"}"

  if [ "$APPLY" -eq 0 ]; then
    echo "   [dry run] would open PR '$BRANCH' -> $def with:"; printf '%s\n' "$stub" | sed 's/^/   | /'; continue
  fi

  sha=$(gh api "repos/$repo/git/ref/heads/$def" --jq .object.sha)
  gh api -X POST "repos/$repo/git/refs" -f "ref=refs/heads/$BRANCH" -f "sha=$sha" >/dev/null
  gh api -X PUT "repos/$repo/contents/$FILE" \
    -f "message=ci: mark agent draft PRs ready when checks pass" \
    -f "content=$(printf '%s\n' "$stub" | base64 | tr -d '\n')" \
    -f "branch=$BRANCH" >/dev/null
  gh pr create -R "$repo" --base "$def" --head "$BRANCH" \
    --title "ci: mark agent draft PRs ready when checks pass" \
    --body "Adds a caller for \`$CENTRAL\`'s reusable \`ready-when-green\` workflow (pinned to \`$REF\`). Draft PRs on \`agent/*\` branches flip to ready once every check on their head commit is green."
done
