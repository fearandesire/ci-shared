#!/usr/bin/env bash
# Add (or refresh) the ready-when-green stub in each target repo, via a PR.
# Idempotent: re-running updates the existing PR branch, and skips repos already on this REF.
# Dry run by default: renders and formats the stub, prints the diff, pushes nothing.
#
#   CENTRAL=owner/ci-shared REF=v2 ./rollout.sh [--apply] [--inline|--readonly] owner/repo ...
#
#   (default)   ~15-line caller of the reusable workflow in CENTRAL.
#   --inline    Self-contained copy for repos whose policy bans cross-repo reusable workflows
#               (every job must declare its own Ubicloud runner). Logic is copied from the
#               local checkout of the reusable workflow, so there is one source of truth.
#   --readonly  Inline, and job permissions are only `contents: read`. Needs a READY_TOKEN
#               secret (PAT or App token with pull-requests write) in the target repo.
set -euo pipefail

CENTRAL="${CENTRAL:?set CENTRAL=owner/central-repo}"
REF="${REF:?set REF to a tag or commit SHA of the central repo}"
APPLY=0; MODE=caller
while [ "${1:-}" = "--apply" ] || [ "${1:-}" = "--inline" ] || [ "${1:-}" = "--readonly" ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --inline) MODE=inline ;;
    --readonly) MODE=readonly ;;
  esac
  shift
done
[ "$#" -gt 0 ] || { echo "usage: $0 [--apply] [--inline|--readonly] owner/repo ..." >&2; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST_WF="$HERE/.github/workflows/ready-when-green.yml"
FILE=".github/workflows/ready-when-green.yml"
BRANCH="chore/ready-when-green"
MSG="ci: mark agent draft PRs ready when checks pass"

render() { # $1 = JSON-quoted, comma-separated workflow names
  local list="$1" out
  if [ "$MODE" = caller ]; then
    out="$(cat "$HERE/caller.yml.tmpl")"
  else
    out="$(cat "$HERE/inline.yml.tmpl")"
    local steps perms
    steps="$(sed -n '/^      - name: Mark green draft PRs ready/,$p' "$HOST_WF")"
    if [ "$MODE" = readonly ]; then perms="      contents: read"
    else perms="$(printf '      contents: write\n      pull-requests: write\n      checks: read\n      statuses: read')"; fi
    out="${out//__STEPS__/"$steps"}"
    out="${out//__JOBPERMS__/"$perms"}"
  fi
  out="${out//__WORKFLOWS__/"$list"}"
  out="${out//__CENTRAL__/"$CENTRAL"}"
  out="${out//__REF__/"$REF"}"
  printf '%s\n' "$out"
}

# Format $FILE with whatever formatter the repo already enforces, so its format check stays green.
format_stub() {
  if [ -f .oxfmtrc.json ] || [ -f .oxfmtrc.jsonc ] || grep -q '"oxfmt"' package.json 2>/dev/null; then
    npx --yes oxfmt "$FILE" >/dev/null
  elif ls .prettierrc* prettier.config.* >/dev/null 2>&1 || grep -q '"prettier"' package.json 2>/dev/null; then
    # Repo plugins (e.g. prettier-plugin-astro) are not installed in a bare clone and are irrelevant
    # to YAML, so on failure retry with the repo's JSON config minus plugins/overrides.
    npx --yes prettier@3 --write "$FILE" >/dev/null 2>&1 || {
      cfg="$(mktemp --suffix=.json)"
      node -e 'const fs=require("fs");let c={};for(const f of [".prettierrc",".prettierrc.json"]){if(fs.existsSync(f)){c=JSON.parse(fs.readFileSync(f,"utf8"));break}}delete c.plugins;delete c.overrides;fs.writeFileSync(process.argv[1],JSON.stringify(c))' "$cfg"
      npx --yes prettier@3 --config "$cfg" --write "$FILE" >/dev/null
    }
  fi
}

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT

for repo in "$@"; do
  # Fail closed on unreadable inventory; explicit adoption is not drift to repair.
  owner_controlled="$(python3 - "$HERE/owner-controlled-repos.json" "$repo" <<'PYGUARD'
import json, sys
print("yes" if sys.argv[2].lower() in {r.lower() for r in json.load(open(sys.argv[1]))} else "no")
PYGUARD
)"
  if [ "$owner_controlled" = yes ]; then
    echo "== $repo: owner-controlled pstack flow; rollout skipped"
    continue
  fi
  echo "== $repo ($MODE)"
  dir="$work/${repo//\//_}"
  gh repo clone "$repo" "$dir" -- --depth 1 -q
  cd "$dir"
  def="$(git symbolic-ref --short HEAD)"

  # Every workflow in the repo except the stub itself, as JSON strings (valid YAML flow scalars).
  list=$(gh workflow list -R "$repo" --all --json name,path \
    --jq "[.[] | select(.path != \"$FILE\") | .name | @json] | join(\", \")")
  if [ -z "$list" ]; then echo "   no workflows to watch, skipping"; cd "$work"; continue; fi

  if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    git fetch -q --depth 1 origin "$BRANCH"; git checkout -q -B "$BRANCH" FETCH_HEAD
  else
    git checkout -q -b "$BRANCH"
  fi

  mkdir -p "$(dirname "$FILE")"
  render "$list" > "$FILE"
  format_stub

  if git cat-file -e "$def:$FILE" 2>/dev/null && git diff --quiet "$def" -- "$FILE"; then
    echo "   default branch already has the rendered caller"
    cd "$work"
    continue
  fi

  if [ -z "$(git status --porcelain -- "$FILE")" ]; then echo "   branch already up to date"
  elif [ "$APPLY" -eq 0 ]; then echo "   [dry run] would commit to $BRANCH:"; git diff HEAD -- "$FILE" | sed 's/^/   | /'
  else
    git add "$FILE" && git commit -q -m "$MSG" && git push -q origin "HEAD:$BRANCH"
    echo "   pushed $BRANCH"
  fi

  if [ "$APPLY" -eq 1 ] && [ -z "$(gh pr list -R "$repo" --head "$BRANCH" --state open --json number --jq '.[0].number // empty')" ]; then
    gh pr create -R "$repo" --base "$def" --head "$BRANCH" --title "$MSG" \
      --body "Adds the \`ready-when-green\` workflow ($MODE; source \`$CENTRAL@$REF\`). Draft PRs on \`agent/*\`, \`claude/*\` and \`codex/*\` branches flip to ready once every check on their head commit is green. Skips PRs labelled \`agent-needs-human\`. Runs on \`ubicloud-standard-2\`."
  fi
  cd "$work"
done
