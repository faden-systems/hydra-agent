# hydra-agents

A software factory framework for companies run by AI agents: the loops that build and verify software, the graph that
coordinates agents and humans, the org structure with autonomy levels and gates, and the discipline that keeps an
agent-run company honest (exit-owned verification, evidence in the repo, cross-family review).

hydra-agents is **not** an agent runtime and not a model. It assumes you bring coding agents (Claude Code, Codex, or
others behind an adapter), a chat workspace (Slack), and a git host (GitHub), and it tells you how to organize them
into a company that ships: who does what, how work moves, what stops it, and how it learns.

The first company built with it is Faden Systems (the Faden runtime framework and the FitFlow app). That deployment is
the worked example throughout the documentation.

## What is here

- `docs/architecture.md` (and `architecture.pdf`): the whole picture. Principles, the company (nodes, autonomy
  levels, verifiers, escalation), the graph and its cycles, the coding loop end to end, deployment, state and memory,
  verification and security, scaling rules, a template for non-engineering teams, build-versus-buy, and the order of
  work. Rev 3, 2026-09-20.
- `docs/figures/`: the org chart, the loop graph, the coding loop, and the deployment view.

- `docs/setup.md`: what a repository and a machine need before loops run (repo configuration, the machine layout the
  launcher expects, the spec reviewer, Slack channels).
- `scripts/`: `launch-loop.sh` (the coding-loop launcher: worktree, attempts, exit script, opens the PR),
  `account.sh` (per-account Claude Code config dirs), `spec-watch.sh` (starts the spec reviewer), `setup-github.sh`
  (labels, auto-merge off, branch protection, secrets).
- `tools/factory/`: the spec reviewer (`spec_review.py`, its brief `spec_review_prompt.md`) and the watcher
  (`spec_watch.py`).
- `docs/factory/`: the standing rules the reviewer and the watchers enforce.
- `ci/`: the three GitHub Actions workflows (exit-criteria check, review bot, Slack notify); copy into
  `.github/workflows/`.

## Status

Design stage. The framework is being extracted from a running deployment; the reusable pieces (the supervisor, the
Slack bridge, the exit library, the graph file format, the loop spec format, the acceptance-harness pattern) will land
here as they are separated from that deployment's repo.

## License

Not yet chosen. Until a LICENSE file exists, all rights are reserved. The options under consideration and the
reasoning are in `docs/LICENSE-OPTIONS.md`.
