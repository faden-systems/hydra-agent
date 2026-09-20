#!/usr/bin/env python3
"""python3 tools/factory/spec_watch.py --once [--api <base>] [--token-file <path>] [--webhook <url>] --repo <owner/name>
                                          [--loop <seconds> [--max-cycles <n>]]

The spec-review watcher (M3): Hermes reviews every spec PR without being asked. Lists open PRs (GitHub REST via
urllib, token from ~/.ghtok or --token-file; `gh` is never used here), finds the ones whose changed files include
one or more loops/<id>.md, resolves the PR's head SHA once and reads every matching spec and its paired
loops/<id>.exit.sh straight from that immutable revision (tools/factory/spec_review.py's `fetch_content`); the rest
of the review's material (MAPPING.md, recent notes, polish rules, other loops' fences) comes from the local checkout
through spec_review.review()'s normal `gather()`.

Every id in a PR is reviewed (sorted order) and the findings are aggregated into ONE PR comment:

    ## Spec review · <sha7> · <model> via <transport>
    round <n> of 4

    ### <id>
    ...

    Totals: blockers=<b> should=<s> nits=<k>

A PR already carrying a comment whose first line starts with `## Spec review · <sha7> ·` for the CURRENT head is
skipped (prefix match on the line, exact match on the sha7). The round policy (docs/factory/spec-review.md): at most
four `## Spec review` comments per PR - a PR already at four gets the `review: capped` label and no fifth comment; a
round that posts with zero blockers gets the `review: ready` label (nothing more is posted for that head - the sha7
check above already covers that once the head stays put).

After a successful post, one line goes to #faden-relay (SLACK_BUILDLOG_WEBHOOK from ~/.faden.env, or --webhook) with
the PR URL and the counts; nothing is sent when the review or the comment post failed.

Prints one line per PR actually reviewed: `reviewed <ids> #<n> blockers=<b> should=<s> nits=<k>` (ids comma-joined,
sorted). Exit 0 when every attempted review posted, 1 when any failed. `--loop <seconds>` repeats forever (or
`--max-cycles <n>` times - a test seam); PRs within one poll are reviewed one at a time, never concurrently, so at
most one review is ever in flight.

Never edits a spec, never merges a PR, never launches a loop - see docs/factory/watchers.md.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import spec_review  # tools/factory/spec_review.py, same directory

DEFAULT_API = "https://api.github.com"
MAX_ROUNDS = 4
_SEVERITIES = ("blocker", "should", "nit")
_LOOP_MD = re.compile(r"^loops/([a-z][a-z0-9]*)\.md$")
_REVIEW_HEADER = "## Spec review · "


# --- configuration ---------------------------------------------------------------------------------------------

def default_repo(root) -> str | None:
    """`owner/name` parsed from the local checkout's `origin` remote, for a deployment that does not pass --repo."""
    out = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        return None
    m = re.search(r"github\.com[:/]+([^/]+/[^/.]+?)(?:\.git)?$", out.stdout.strip())
    return m.group(1) if m else None


def slack_webhook(webhook=None) -> str | None:
    """--webhook, else SLACK_BUILDLOG_WEBHOOK from the environment, else the same variable in ~/.faden.env."""
    if webhook:
        return webhook
    env = os.environ.get("SLACK_BUILDLOG_WEBHOOK")
    if env:
        return env
    path = Path.home() / ".faden.env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("SLACK_BUILDLOG_WEBHOOK="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    return None


# --- GitHub (spec_review.http_request/http_json/fetch_content: no `gh`) ------------------------------------------

def list_open_prs(api, repo, token) -> list:
    return spec_review.http_json("GET", f"{api}/repos/{repo}/pulls?state=open&per_page=100", token=token) or []


def pr_files(api, repo, number, token) -> list:
    data = spec_review.http_json("GET", f"{api}/repos/{repo}/pulls/{number}/files?per_page=100", token=token) or []
    return [f["filename"] for f in data]


def pr_comments(api, repo, number, token) -> list:
    data = spec_review.http_json("GET", f"{api}/repos/{repo}/issues/{number}/comments?per_page=100", token=token) or []
    return [c.get("body") or "" for c in data]


def post_pr_comment(api, repo, number, body, token):
    spec_review.http_json("POST", f"{api}/repos/{repo}/issues/{number}/comments", token=token, data={"body": body})


def add_labels(api, repo, number, labels, token):
    spec_review.http_json("POST", f"{api}/repos/{repo}/issues/{number}/labels", token=token, data={"labels": list(labels)})


def post_slack(webhook, text):
    spec_review.http_request("POST", webhook, data={"text": text})


# --- review --------------------------------------------------------------------------------------------------

def loop_ids_in(files) -> list:
    """Sorted, deduplicated loop ids whose spec is among the PR's changed files."""
    ids = set()
    for f in files:
        m = _LOOP_MD.match(f)
        if m:
            ids.add(m.group(1))
    return sorted(ids)


def tally(results) -> dict:
    counts = {s: 0 for s in _SEVERITIES}
    for result in results.values():
        for finding in result.get("findings") or []:
            counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return counts


def render_aggregate(sha7, model_desc, round_num, results) -> str:
    model_id = model_desc.get("model_id", "?")
    transport = model_desc.get("transport", "?")
    lines = [f"{_REVIEW_HEADER}{sha7} · {model_id} via {transport}", f"round {round_num} of {MAX_ROUNDS}", ""]
    for loop_id in sorted(results):
        lines += [f"### {loop_id}", "", spec_review.render(results[loop_id]).strip(), ""]
    counts = tally(results)
    lines.append(f"Totals: blockers={counts['blocker']} should={counts['should']} nits={counts['nit']}")
    return "\n".join(lines) + "\n"


def review_pr(pr, api, repo, token, model_holder, root, webhook=None) -> tuple:
    """Reviews one PR if it needs it. Returns (status, line) where `status` is "reviewed", "capped", "skipped" or
    "failed", and `line` is the stdout line to print (None for a silent skip). `model_holder` is a one-element list
    used to build the reviewer model at most once per run and reuse it across PRs."""
    number = pr["number"]
    head = pr["head"]["sha"]
    sha7 = head[:7]
    url = pr.get("html_url", "")
    files = pr_files(api, repo, number, token)
    ids = loop_ids_in(files)
    if not ids:
        return "skipped", None

    comments = pr_comments(api, repo, number, token)
    review_comments = [c for c in comments if c.startswith(_REVIEW_HEADER)]
    if any(c.startswith(f"{_REVIEW_HEADER}{sha7} ·") for c in review_comments):
        return "skipped", None  # this head is already reviewed
    if len(review_comments) >= MAX_ROUNDS:
        add_labels(api, repo, number, ["review: capped"], token)
        return "capped", f"capped {','.join(ids)} #{number} rounds={len(review_comments)}"

    if model_holder[0] is None:
        model_holder[0] = spec_review.build_model()
    model = model_holder[0]

    results = {}
    for loop_id in ids:
        try:
            spec_text = spec_review.fetch_content(api, repo, f"loops/{loop_id}.md", head, token)
        except spec_review.GitHubError as exc:
            return "failed", f"error: PR #{number}: could not read loops/{loop_id}.md at {sha7}: {exc}"
        try:
            exit_text = spec_review.fetch_content(api, repo, f"loops/{loop_id}.exit.sh", head, token)
        except spec_review.GitHubError:
            exit_text = None
        # the exit-owned harness and any scenario module the harness imports live on the PR branch too (n2 review,
        # 2026-09-14: the reviewer could not see them because they were read from the local checkout)
        acceptance_text = None
        for name in (f"loops/{loop_id}.acceptance.py", f"loops/{loop_id}_scenarios.py"):
            try:
                text = spec_review.fetch_content(api, repo, name, head, token)
            except spec_review.GitHubError:
                continue
            acceptance_text = (acceptance_text or "") + f"# ---- {name} ----\n{text}\n"
        try:
            results[loop_id] = spec_review.review(loop_id, model=model, root=root,
                                                   spec_text=spec_text, exit_text=exit_text, acceptance_text=acceptance_text)
        except Exception as exc:  # noqa: BLE001  a model or transport failure fails the whole PR, closed
            return "failed", f"error: PR #{number}: review of {loop_id} failed: {type(exc).__name__}: {str(exc)[:300]}"

    round_num = len(review_comments) + 1
    body = render_aggregate(sha7, results[ids[0]]["model"], round_num, results)
    try:
        post_pr_comment(api, repo, number, body, token)
    except spec_review.GitHubError as exc:
        return "failed", f"error: PR #{number}: could not post the review comment: {exc}"

    counts = tally(results)
    if counts["blocker"] == 0:
        try:
            add_labels(api, repo, number, ["review: ready"], token)
        except spec_review.GitHubError as exc:
            print(f"warning: PR #{number}: could not add the ready label: {exc}", file=sys.stderr)

    hook = slack_webhook(webhook)
    if hook:
        try:
            post_slack(hook, f"Spec review posted: {url} blockers={counts['blocker']} "
                             f"should={counts['should']} nits={counts['nit']}")
        except Exception as exc:  # noqa: BLE001  a Slack failure never fails the review itself
            print(f"warning: PR #{number}: Slack post failed: {exc}", file=sys.stderr)

    return "reviewed", f"reviewed {','.join(ids)} #{number} blockers={counts['blocker']} should={counts['should']} nits={counts['nit']}"


def run_once(args) -> int:
    """One poll: list open PRs, review the ones that need it, one at a time. Returns 0 when every attempted review
    posted, 1 when any failed."""
    api = args.api or DEFAULT_API
    token = spec_review.github_token(args.token_file)
    prs = list_open_prs(api, args.repo, token)
    model_holder = [None]
    ok = True
    for pr in prs:
        status, line = review_pr(pr, api, args.repo, token, model_holder, args.root, webhook=args.webhook)
        if status == "failed":
            ok = False
        if line:
            print(line, file=sys.stderr if status == "failed" else sys.stdout)
    return 0 if ok else 1


# --- entry -----------------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 tools/factory/spec_watch.py",
                                     description="Review every open spec PR without being asked (M3).")
    parser.add_argument("--once", action="store_true", help="poll exactly once (the default when --loop is absent)")
    parser.add_argument("--loop", type=float, default=None, help="seconds between polls; repeats until stopped")
    parser.add_argument("--max-cycles", type=int, default=None, help="test seam: stop --loop after this many polls")
    parser.add_argument("--api", default=DEFAULT_API, help="GitHub API base")
    parser.add_argument("--repo", default=None, help="owner/name (default: parsed from the local checkout's origin remote)")
    parser.add_argument("--token-file", default=None, help="GitHub token file (default ~/.ghtok)")
    parser.add_argument("--webhook", default=None, help="Slack webhook (default SLACK_BUILDLOG_WEBHOOK)")
    parser.add_argument("--root", default=str(spec_review.ROOT), help="local checkout for review context (default: this checkout)")
    args = parser.parse_args(argv)
    if not args.repo:
        args.repo = default_repo(args.root)
    if not args.repo:
        print("error: --repo owner/name is required (could not parse it from the origin remote)", file=sys.stderr)
        return 2

    if args.loop:
        ok = True
        cycles = 0
        while args.max_cycles is None or cycles < args.max_cycles:
            ok = run_once(args) == 0 and ok
            cycles += 1
            if args.max_cycles is not None and cycles >= args.max_cycles:
                break
            time.sleep(args.loop)
        return 0 if ok else 1
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())
