# Setting up a hydra-agents factory

What a repository and a machine need before the loops run. Extracted from the first deployment (Faden Systems);
paths and names are that deployment's defaults and are safe to keep.

## 1. The repository (once)

`scripts/setup-github.sh` configures: loop labels, auto-merge off with delete-branch-on-merge on, branch protection on
`main` (pull request required, the `run-exit` status check required, no force pushes), and the three secrets the CI
workflows use (`ANTHROPIC_API_KEY` for CI tools, `OPENAI_API_KEY` for the review bot, `SLACK_PR_WEBHOOK` for the PR
notifier). Needs the `gh` CLI logged in. Set `REPO=<org>/<repo>` before running.

CI workflows (copy `ci/*.yml` into `.github/workflows/`):
- `exit-criteria.yml`: the required `run-exit` check; runs a loop's exit script against its PR.
- `codex-review.yml`: the review bot that posts "Problems:" comments on PRs.
- `slack-notify.yml`: PR events to a Slack channel via the webhook secret.

Repository conventions the tools assume:
- `loops/<id>.md` (spec), `loops/<id>.exit.sh` (mechanical gate), `loops/<id>.acceptance.py` (exit-owned harness),
  `docs/notes/<id>.md` (what the loop built). See `docs/architecture.md`, section 3, "The coding piece".
- The exit script is run with `BASE_REF=origin/main` and decides PASS or FAIL; the launcher and CI both call it the
  same way.
- `docs/factory/spec-review.md` is the rule the spec reviewer enforces; `docs/factory/watchers.md` the standing rules
  for the background watchers.

## 2. A machine that runs loops (each operator machine)

What `scripts/launch-loop.sh` and `scripts/account.sh` expect:

| Item | Where | Notes |
|---|---|---|
| The clone | `~/factory/faden` (rename for your repo, and edit `REPO=` in the launcher) | loops run in worktrees, never in the clone |
| Worktrees, failed records, logs | `~/factory/wt-<loop>`, `~/factory/failed/`, `~/factory/logs/` | created by the launcher |
| Account selection | `~/factory/account`, written by `scripts/account.sh use <name>` | selects `CLAUDE_CONFIG_DIR=~/.claude-<name>`; `default` uses `~/.claude` |
| Environment file | `~/.faden.env` | sourced by the launcher and the watchers; keys: `ANTHROPIC_API_KEY` (the app and the simulator), `SLACK_BUILDLOG_WEBHOOK` (launch/exit lines), `OPENAI_API_KEY` only where an API transport is intended |
| Claude Code | on PATH, logged in per account (`claude auth login` under each `CLAUDE_CONFIG_DIR`, or a `claude setup-token` for headless use) | the launcher strips `ANTHROPIC_API_KEY` from every `claude` invocation so loops bill the plan, not the API |
| Codex CLI | `codex login --device-auth` on the machine that runs the spec reviewer and real-model runs | subscription login persists in `~/.codex/auth.json` |
| `gh` | logged in, with push rights to the repo | used to open PRs |
| Python 3.12, Node LTS | on PATH; `~/.local/bin` on PATH | exit scripts `pip install` and `playwright install chromium` on first run; pin both in an image |
| Playwright | installed by the exit scripts; on Linux add `--with-deps` and fonts (Inter, Noto, an emoji font) | screenshots must match across machines |

Launch: `scripts/launch-loop.sh <loop-id> [model] [max-attempts]`, detached (see the header comment). Attempt 1 is a
fresh session with the spec as the prompt; attempt 2 continues the same session with the exit failure fed in; attempt 3
starts fresh with the failure tail as a hint. A closed usage window is a wait, not a failed attempt. On PASS the
launcher pushes `loop/<id>`, opens the PR and stops (the manager verifies on a fresh clone and merges; am1,
2026-10-02); on the third FAIL it leaves the diff and tails under
`~/factory/failed/`.

## 3. The spec reviewer (one machine with the Codex login)

`scripts/spec-watch.sh` starts `tools/factory/spec_watch.py --loop 300` detached. It polls open PRs that touch
`loops/*.md`, reviews each spec with `tools/factory/spec_review.py` (the brief is `tools/factory/spec_review_prompt.md`),
posts one aggregated comment per PR, labels `review: ready` after a clean round or `review: capped` after the fourth,
and posts one line to the relay channel. It never launches, merges, or edits. Restart it the same way if it dies;
its state lives in the PR's comments and labels.

## 4. Slack

Two channels: a relay channel for instructions and replies (one assignee per message, machine named on the first
line) and a buildlog channel for the launcher's `[launch] <host>/<loop>: started | PASS -> PR | FAILED` lines
(`SLACK_BUILDLOG_WEBHOOK`). A third channel receives PR events from CI. The department-channel structure and the bot
identities are described in `docs/architecture.md`, section 3.

## 5. Not covered here

Installing an agent framework on a machine (Hermes Agent, OpenClaw, or a Claude Code Channels bot) and connecting it
to Slack are standard and documented by those projects.
