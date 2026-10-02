#!/usr/bin/env bash
# Configures labels, auto-merge, branch protection, and secrets.
# Needs: gh CLI, logged in (gh auth login). Run once, from anywhere.
set -euo pipefail
REPO="${REPO:-<org>/<repo>}"

echo "== Labels =="
for L in loop:a1 loop:a2 loop:a3 loop:a4 loop:a5 loop:a6 loop:b1 loop:b2 loop:b3 loop:b4 gate blocker smoke; do
  gh label create "$L" --repo "$REPO" --force --color "0e8a16" || true
done

echo "== Auto-merge off on the repo =="
gh api -X PATCH "repos/$REPO" -f allow_auto_merge=false -f delete_branch_on_merge=true >/dev/null
echo "auto-merge: off (the manager merges after verification), delete-branch-on-merge: on"

echo "== Branch protection on main =="
gh api -X PUT "repos/$REPO/branches/main/protection" \
  -H "Accept: application/vnd.github+json" \
  --input - << 'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["run-exit"] },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
echo "branch protection: PR + green exit-criteria required, no force push"

echo "== Secrets (you will be prompted; paste values) =="
echo "Anthropic API key (for CI/Claude tools):"
gh secret set ANTHROPIC_API_KEY --repo "$REPO"
echo "OpenAI API key (for Codex review):"
gh secret set OPENAI_API_KEY --repo "$REPO"
echo "Slack webhook for #faden-pr:"
gh secret set SLACK_PR_WEBHOOK --repo "$REPO"

echo "== Check visibility =="
VIS=$(gh api "repos/$REPO" --jq .private)
if [ "$VIS" = "true" ]; then echo "Repo is PRIVATE. Good."; else echo "WARNING: repo is PUBLIC. Fix: gh repo edit $REPO --visibility private"; fi

echo "Done."
