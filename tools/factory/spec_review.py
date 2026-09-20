#!/usr/bin/env python3
"""python3 tools/factory/spec_review.py <loop-id> [--report <design-report.md>] [--post] [--root DIR] [--json]

The spec reviewer (F1): reads a loop spec and its exit script before the loop is launched and returns structured
findings from a second-vendor model (faden.runtime.openai_model.OpenAIModel: FADEN_JUDGE2_MODEL, OPENAI_API_KEY).

Inputs, assembled deterministically (`gather`): loops/<id>.md, loops/<id>.exit.sh, loops/<id>.acceptance.py when present, docs/design/MAPPING.md, the notes
of the two most recently merged loops (docs/notes/, by mtime), tools/e2e/polish_rules.json, the Scope fence of every
other loop spec that is not yet merged as a note (so overlaps are visible), and the --report file when given.

The prompt (spec_review_prompt.md) asks for exactly eight numbered sections - claims the exit script does not test,
checks that pass without the requirement, symptom-as-cause decisions (against the report's evidence when given),
machine / path / file / service assumptions, ambiguities, fence overlaps and leaks, acceptance on fixtures where real
data exists, missing regression tests for the named defects - each finding with a severity (blocker / should / nit),
the quoted line and a suggested edit. The model answers JSON; `render` writes the markdown review with the blockers
first, then the eight numbered sections. Stdout gets the review; --post adds it as a comment on the open PR whose
branch contains the spec (`gh pr comment`). The reviewer never edits the spec.

FADEN_FACTORY_FAKE_MODEL=1 swaps the model for a canned reviewer (no network) so the exit scripts and tests can run it.
Exit 0 after a review (blockers included: the review is advisory), 2 when the spec is missing or the model failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from faden.runtime.anthropic_model import extract_object  # noqa: E402
from faden.runtime.codex_model import CodexModel  # noqa: E402
from faden.runtime.openai_model import OpenAIModel  # noqa: E402
from faden.runtime import model_select  # noqa: E402

PROMPT_PATH = Path(__file__).resolve().with_name("spec_review_prompt.md")
FAKE_MODEL_ENV = "FADEN_FACTORY_FAKE_MODEL"
RECENT_NOTES = 2
MAX_TOKENS = 8000
SEVERITIES = ("blocker", "should", "nit")
SECTIONS = {
    1: "Claims the exit script does not actually test",
    2: "Checks that would pass without the requirement being met",
    3: "Decisions that treat a symptom as a cause",
    4: "Assumptions about a machine, a path, a file or a service",
    5: "Ambiguities an implementer would have to guess at",
    6: "Fence overlaps with other unmerged loops, and what the fence lets through",
    7: "Acceptance measured on fixtures where real data exists",
    8: "Missing regression tests for the defects the spec names",
}
_LOOP_ID = re.compile(r"^[a-z][a-z0-9]*$")
_MERGED = (re.compile(r"loop/([a-z][a-z0-9]*)\b"), re.compile(r"^([a-z][a-z0-9]*): "))
_SPEC_COMMIT = re.compile(r"^([a-z][a-z0-9]*) loop spec\b")


# --- inputs ------------------------------------------------------------------------------------------------------

def gather(loop_id, root=ROOT, report=None, merged=None, spec_text=None, exit_text=None, acceptance_text=None) -> list:
    """The review's inputs, in a fixed order: [{"label", "path"|"text", "kind"}]. `kind` is "full" (the whole file),
    "text" (inline text, no path on disk - the m3 `--pr` mode: a spec/exit/acceptance read from a PR's head revision
    instead of the local checkout) or "fence" (only the Scope fence section of another unmerged loop's spec).
    `merged` is the set of loop ids already merged (default: read from git); a loop whose spec exists, whose note
    does not, and that is not merged is unmerged. `spec_text`/`exit_text`/`acceptance_text`, when given, replace the
    corresponding local file read entirely (and the spec need not exist on disk at all). Raises FileNotFoundError
    when the spec is missing both on disk and as `spec_text`."""
    root = Path(root)
    if not _LOOP_ID.match(str(loop_id or "")):
        raise ValueError(f"not a loop id: {loop_id!r}")
    items = []
    if spec_text is not None:
        items.append({"label": f"spec: loops/{loop_id}.md", "text": spec_text, "kind": "text"})
    else:
        spec = root / "loops" / f"{loop_id}.md"
        if not spec.is_file():
            raise FileNotFoundError(f"no spec at {spec}")
        items.append({"label": f"spec: loops/{loop_id}.md", "path": spec, "kind": "full"})
    if exit_text is not None:
        items.append({"label": f"exit script: loops/{loop_id}.exit.sh", "text": exit_text, "kind": "text"})
    else:
        exit_sh = root / "loops" / f"{loop_id}.exit.sh"
        if exit_sh.is_file():
            items.append({"label": f"exit script: loops/{loop_id}.exit.sh", "path": exit_sh, "kind": "full"})
    if acceptance_text is not None:
        items.append({"label": f"acceptance harness: loops/{loop_id}.acceptance.py", "text": acceptance_text, "kind": "text"})
    else:
        acceptance = root / "loops" / f"{loop_id}.acceptance.py"
        if acceptance.is_file():  # a reviewer-owned harness the exit script invokes (m3 review, 2026-09-12)
            items.append({"label": f"acceptance harness: loops/{loop_id}.acceptance.py", "path": acceptance, "kind": "full"})
    mapping = root / "docs" / "design" / "MAPPING.md"
    if mapping.is_file():
        items.append({"label": "design mapping: docs/design/MAPPING.md", "path": mapping, "kind": "full"})
    for note in recent_notes(root, exclude=loop_id):
        items.append({"label": f"recent loop note: docs/notes/{note.name}", "path": note, "kind": "full"})
    rules = root / "tools" / "e2e" / "polish_rules.json"
    if rules.is_file():
        items.append({"label": "polish rules: tools/e2e/polish_rules.json", "path": rules, "kind": "full"})
    for other in unmerged_loops(root, exclude=loop_id, merged=merged):
        items.append({"label": f"fence of unmerged loop {other}: loops/{other}.md", "path": root / "loops" / f"{other}.md", "kind": "fence"})
    if report:
        report = Path(report)
        if not report.is_file():
            raise FileNotFoundError(f"no report at {report}")
        items.append({"label": f"design report: {report}", "path": report, "kind": "full"})
    return items


def recent_notes(root, exclude=None, n=RECENT_NOTES) -> list:
    """The `n` most recently written loop notes (docs/notes/*.md by mtime, newest first; name breaks ties)."""
    notes_dir = Path(root) / "docs" / "notes"
    notes = [p for p in notes_dir.glob("*.md") if p.is_file() and p.stem != exclude]
    notes.sort(key=lambda p: (-p.stat().st_mtime, p.name))
    return notes[:n]


def unmerged_loops(root, exclude=None, merged=None) -> list:
    """Loop ids with a spec in loops/ but no note in docs/notes/ and no merge on the main branch, sorted."""
    root = Path(root)
    merged = merged_loop_ids(root) if merged is None else set(merged)
    out = []
    for spec in sorted((root / "loops").glob("*.md")):
        lid = spec.stem
        if not _LOOP_ID.match(lid) or lid == exclude or lid in merged:
            continue
        if (root / "docs" / "notes" / f"{lid}.md").is_file():
            continue
        out.append(lid)
    return out


def merged_loop_ids(root, ref="origin/main") -> set:
    """Loop ids the main branch already merged. Newest commit first, the first event for an id decides: a subject
    `loop/<id> (#N)` (the launcher's PR title) or `<id>: ...` (a loop's own commit) means merged; `<id> loop spec`
    means a spec was (re)added after any earlier work under that id, so the loop is still to run. Empty when git is
    not available."""
    for target in (ref, "HEAD"):
        try:
            out = subprocess.run(["git", "-C", str(root), "log", "--format=%s", target], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return set()
        if out.returncode != 0:
            continue
        decided, merged = set(), set()
        for line in out.stdout.splitlines():
            spec = _SPEC_COMMIT.match(line)
            if spec:
                decided.add(spec.group(1))
                continue
            for rx in _MERGED:
                m = rx.search(line)
                if m and m.group(1) not in decided:
                    decided.add(m.group(1))
                    merged.add(m.group(1))
        return merged
    return set()


def fence_of(text) -> str:
    """The `## Scope fence` section of a spec (heading included), or a line saying there is none."""
    lines = str(text or "").splitlines()
    out, inside = [], False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = "scope fence" in line.lower() or line.lower().strip("# ").startswith("fence")
        if inside:
            out.append(line)
    return "\n".join(out).strip() or "(no Scope fence section)"


def assemble(loop_id, items) -> str:
    """The user prompt: every input under its label, fenced; fence-only items reduced to their Scope fence."""
    parts = [f"Loop under review: {loop_id}", "",
             "The material follows. The first block is the spec; the exit script is what decides the loop is done."]
    for item in items:
        if item["kind"] == "text":
            text = item["text"]
        else:
            path = Path(item["path"])
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                text = f"(unreadable: {exc})"
            if item["kind"] == "fence":
                text = fence_of(text)
        parts += ["", f"### {item['label']}", "```", text.rstrip(), "```"]
    return "\n".join(parts) + "\n"


# --- the model -----------------------------------------------------------------------------------------------------

class FakeSpecModel:
    """FADEN_FACTORY_FAKE_MODEL=1: a canned reviewer with one finding per section and two blockers, so the exit
    scripts and the tests can run the whole pipeline without a network. Records the prompts it was given."""
    model_id = "fake-spec-reviewer"
    transport = "fake"

    def __init__(self):
        self.calls = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def generate(self, system, user, max_tokens=None) -> str:
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        loop = re.search(r"Loop under review: (\S+)", user)
        lid = loop.group(1) if loop else "?"
        findings = [
            {"section": 1, "severity": "should", "quote": "## Exit criteria", "finding": f"A requirement of {lid} has no check in the exit script.", "suggested_edit": "Add a grep or a test for it to the exit script."},
            {"section": 2, "severity": "blocker", "quote": "grep -q", "finding": "A grep on a file name passes when the string appears in a comment.", "suggested_edit": "Run the code path instead of grepping for the identifier."},
            {"section": 3, "severity": "nit", "quote": "## Purpose", "finding": "The purpose names the symptom before the cause.", "suggested_edit": "Lead with the cause the evidence supports."},
            {"section": 4, "severity": "blocker", "quote": "## Model", "finding": "The spec assumes a file outside the repository that the implementer's machine may not have.", "suggested_edit": "Name the fallback when the file is missing, or ship the file."},
            {"section": 5, "severity": "should", "quote": "## Requirements", "finding": "Two readings of one requirement lead to different work.", "suggested_edit": "State which reading is meant."},
            {"section": 6, "severity": "nit", "quote": "## Scope fence", "finding": "The fence admits a path the spec never needs.", "suggested_edit": "Drop the path from the fence."},
            {"section": 7, "severity": "should", "quote": "## Exit criteria", "finding": "Acceptance runs on canned input where recorded sessions exist.", "suggested_edit": "Run the check on the recorded run too."},
            {"section": 8, "severity": "nit", "quote": "## Purpose", "finding": "A named defect has no test that fails before the fix.", "suggested_edit": "Add the regression test to the requirements."},
        ]
        return json.dumps({"findings": findings, "summary": f"Fake review of {lid}: two blockers, not ready as written. Fix the machine assumption first."})


def build_model(env=None):
    env = os.environ if env is None else env
    if str(env.get(FAKE_MODEL_ENV, "")).strip().lower() in ("1", "true", "yes", "on"):
        return FakeSpecModel()
    if model_select.provider_for("openai", env=env) == "codex":
        return CodexModel(model_id=env.get("FADEN_JUDGE2_MODEL"))
    return OpenAIModel(model_id=env.get("FADEN_JUDGE2_MODEL"), api_key=env.get("OPENAI_API_KEY"))


def system_prompt(path=PROMPT_PATH) -> str:
    return Path(path).read_text(encoding="utf-8")


def parse_findings(text) -> tuple:
    """(findings, summary) from the model's reply: sections coerced to 1-8 (unknown -> 5, ambiguity), severities
    normalised (unknown -> should), every finding a full record. No JSON object -> ([], None)."""
    obj = extract_object(text)
    if not obj:
        return [], None
    findings = []
    for f in obj.get("findings") or []:
        if not isinstance(f, dict):
            continue
        try:
            section = int(f.get("section"))
        except (TypeError, ValueError):
            section = 5
        severity = str(f.get("severity") or "should").strip().lower()
        findings.append({"section": section if section in SECTIONS else 5,
                         "severity": severity if severity in SEVERITIES else "should",
                         "quote": " ".join(str(f.get("quote") or "").split()),
                         "finding": str(f.get("finding") or f.get("what") or "").strip(),
                         "suggested_edit": str(f.get("suggested_edit") or f.get("suggestion") or "").strip()})
    summary = obj.get("summary")
    return findings, (str(summary).strip() if summary else None)


def review(loop_id, model=None, root=ROOT, report=None, merged=None, env=None,
          spec_text=None, exit_text=None, acceptance_text=None) -> dict:
    """Gather, ask, parse. Returns {"loop_id", "model": {"model_id", "transport"}, "inputs", "findings", "summary",
    "raw", "usage"}. `transport` is "codex" when the reviewer ran on the Codex CLI subscription (M2), else the
    model's own vendor ("openai", or "fake" under FADEN_FACTORY_FAKE_MODEL). `spec_text`/`exit_text`/
    `acceptance_text` review a PR's head revision instead of the local checkout (m3's `--pr` mode and spec_watch.py);
    the rest of the material (MAPPING.md, recent notes, polish rules, other loops' fences) still comes from `root`."""
    model = model or build_model(env)
    items = gather(loop_id, root=root, report=report, merged=merged,
                   spec_text=spec_text, exit_text=exit_text, acceptance_text=acceptance_text)
    raw = model.generate(system_prompt(), assemble(loop_id, items), max_tokens=MAX_TOKENS)
    findings, summary = parse_findings(raw)
    model_desc = {"model_id": getattr(model, "model_id", None) or "?", "transport": getattr(model, "transport", "openai")}
    return {"loop_id": loop_id, "model": model_desc, "inputs": [i["label"] for i in items],
            "findings": findings, "summary": summary, "raw": raw, "usage": dict(getattr(model, "usage", {}) or {})}


# --- the markdown --------------------------------------------------------------------------------------------------

def render(result) -> str:
    """The review as markdown: header, the blockers first, then the eight numbered sections (every one present, its
    findings ordered blocker > should > nit), then the reviewer's raw reply when it was not JSON."""
    findings = sorted(result.get("findings") or [], key=lambda f: (SEVERITIES.index(f["severity"]), f["section"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    model = result.get("model") or {}
    model_desc = model.get("model_id", "?") if isinstance(model, dict) else model
    transport = model.get("transport") if isinstance(model, dict) else None
    lines = [f"# Spec review: {result.get('loop_id')}", "",
             f"Reviewer `{model_desc}`" + (f" (via {transport})" if transport else "") +
             f"; {counts['blocker']} blocker(s), {counts['should']} should, {counts['nit']} nit(s). "
             "Advisory: blockers are answered in the spec or the PR before launch; should items are answered or declined in the PR."]
    if result.get("summary"):
        lines += ["", result["summary"]]
    lines += ["", "Inputs: " + "; ".join(result.get("inputs") or []) + ".", "", "## Blockers", ""]
    blockers = [f for f in findings if f["severity"] == "blocker"]
    if not blockers:
        lines.append("None.")
    for i, f in enumerate(blockers, 1):
        lines += _finding_lines(f"B{i}", f, with_section=True)
    for n, title in SECTIONS.items():
        lines += ["", f"## {n}. {title}", ""]
        own = [f for f in findings if f["section"] == n]
        if not own:
            lines.append("Nothing found.")
        for k, f in enumerate(own, 1):
            lines += _finding_lines(f"{n}.{k}", f)
    if not result.get("findings") and result.get("raw"):
        lines += ["", "## Reviewer's reply (no structured findings could be read from it)", "", "```",
                  str(result["raw"]).strip()[:6000], "```"]
    return "\n".join(lines) + "\n"


def _finding_lines(tag, f, with_section=False) -> list:
    head = f"**{tag}** [{f['severity']}]" + (f" (section {f['section']}: {SECTIONS[f['section']]})" if with_section else "")
    out = [f"- {head} {f['finding']}"]
    if f.get("quote"):
        out.append(f"  - line: > {f['quote']}")
    if f.get("suggested_edit"):
        out.append(f"  - suggested edit: {f['suggested_edit']}")
    return out


# --- GitHub REST (m3: the `--pr` mode and spec_watch.py share this; no `gh`, no third-party HTTP library) ----------

class GitHubError(RuntimeError):
    """A non-2xx GitHub (or webhook) HTTP response. `status` is the HTTP status code (e.g. 404 for a path that does
    not exist at the requested ref)."""

    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


def github_token(token_file=None) -> str | None:
    """The token for GitHub REST calls: `token_file` when given, else `~/.ghtok`. None when neither exists (an
    unauthenticated call, fine for a public repo's read endpoints; writes will fail)."""
    path = Path(token_file) if token_file else Path.home() / ".ghtok"
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    return None


def http_request(method, url, token=None, data=None, accept="application/vnd.github+json", timeout=30) -> str:
    """One urllib call. `data`, when given, is sent as a JSON body. Returns the response body as text (never
    parsed: GitHub's JSON endpoints and Slack's webhook and the raw-content endpoint all come back through here).
    Raises GitHubError(status, detail) on a non-2xx response."""
    import urllib.error
    import urllib.request
    headers = {"Accept": accept, "User-Agent": "faden-spec-watch"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise GitHubError(exc.code, exc.read().decode("utf-8", "replace")[:300]) from None


def http_json(method, url, token=None, data=None, timeout=30):
    """`http_request`, parsed as JSON (GitHub's list/get endpoints)."""
    text = http_request(method, url, token=token, data=data, timeout=timeout)
    return json.loads(text) if text else None


def fetch_pr(api, repo, number, token=None) -> dict:
    """The PR itself (head sha, html_url, ...) - GET /repos/{repo}/pulls/{number}."""
    return http_json("GET", f"{api}/repos/{repo}/pulls/{number}", token=token)


def fetch_content(api, repo, path, ref, token=None) -> str:
    """The raw content of `path` at the immutable revision `ref` (GitHub's contents API, Accept: raw - no base64
    decoding needed). Raises GitHubError(404, ...) when the path does not exist at that ref."""
    from urllib.parse import quote
    url = f"{api}/repos/{repo}/contents/{quote(path)}?ref={quote(ref)}"
    return http_request("GET", url, token=token, accept="application/vnd.github.raw")


# --- posting -------------------------------------------------------------------------------------------------------

def find_pr(loop_id, run=None) -> int | None:
    """The open PR whose branch carries loops/<id>.md: the PR whose file list contains the spec, else the PR whose
    head branch is named for the loop (loop/<id>, spec/<id>, <id>)."""
    run = run or subprocess.run
    out = run(["gh", "pr", "list", "--state", "open", "--limit", "100", "--json", "number,headRefName,files"],
              capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {(out.stderr or '').strip()[:200]}")
    try:
        prs = json.loads(out.stdout or "[]")
    except ValueError:
        raise RuntimeError("gh pr list returned no JSON")
    spec = f"loops/{loop_id}.md"
    for pr in prs:
        if any((f or {}).get("path") == spec for f in pr.get("files") or []):
            return int(pr["number"])
    names = {f"loop/{loop_id}", f"spec/{loop_id}", loop_id}
    for pr in prs:
        head = str(pr.get("headRefName") or "")
        if head in names or head.endswith(f"/{loop_id}"):
            return int(pr["number"])
    return None


def post_comment(loop_id, text, run=None) -> int:
    """Adds the review as a comment on the loop's open PR (gh pr comment). Returns the PR number; raises when there
    is no such PR or gh fails."""
    run = run or subprocess.run
    number = find_pr(loop_id, run=run)
    if number is None:
        raise RuntimeError(f"no open PR carries loops/{loop_id}.md")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        body = fh.name
    try:
        out = run(["gh", "pr", "comment", str(number), "--body-file", body], capture_output=True, text=True)
    finally:
        try:
            os.unlink(body)
        except OSError:
            pass
    if out.returncode != 0:
        raise RuntimeError(f"gh pr comment failed: {(out.stderr or '').strip()[:200]}")
    return number


# --- entry ---------------------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 tools/factory/spec_review.py",
                                     description="Review a loop spec and its exit script before launch (F1).")
    parser.add_argument("loop_id")
    parser.add_argument("--report", default=None, help="a design-report.md whose evidence the symptom/cause check is judged against")
    parser.add_argument("--post", action="store_true", help="add the review as a comment on the open PR that carries the spec")
    parser.add_argument("--root", default=str(ROOT), help="repository root (default: this checkout)")
    parser.add_argument("--json", action="store_true", help="print the structured result instead of markdown")
    parser.add_argument("--pr", type=int, default=None,
                        help="review loops/<id>.md and its exit script as they stand at this PR's head revision "
                             "(GitHub REST via urllib; requires --repo, needs no local checkout of the branch)")
    parser.add_argument("--api", default="https://api.github.com", help="GitHub API base (--pr mode)")
    parser.add_argument("--repo", default=None, help="owner/name (--pr mode)")
    parser.add_argument("--ref", default=None, help="pin the revision instead of resolving the PR's current head (--pr mode)")
    parser.add_argument("--token-file", default=None, help="GitHub token file (default ~/.ghtok; --pr mode)")
    args = parser.parse_args(argv)
    spec_text = exit_text = acceptance_text = None
    if args.pr is not None:
        if not args.repo:
            print("error: --pr requires --repo owner/name", file=sys.stderr)
            return 2
        token = github_token(args.token_file)
        try:
            ref = args.ref or fetch_pr(args.api, args.repo, args.pr, token)["head"]["sha"]
            spec_text = fetch_content(args.api, args.repo, f"loops/{args.loop_id}.md", ref, token)
            try:
                exit_text = fetch_content(args.api, args.repo, f"loops/{args.loop_id}.exit.sh", ref, token)
            except GitHubError:
                exit_text = None
            try:
                acceptance_text = fetch_content(args.api, args.repo, f"loops/{args.loop_id}.acceptance.py", ref, token)
            except GitHubError:
                acceptance_text = None
        except GitHubError as exc:
            print(f"error: could not read PR #{args.pr}: {exc}", file=sys.stderr)
            return 2
    try:
        result = review(args.loop_id, root=args.root, report=args.report,
                        spec_text=spec_text, exit_text=exit_text, acceptance_text=acceptance_text)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001  a model or network failure
        print(f"error: the reviewer did not answer: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 2
    text = render(result)
    if (result.get("model") or {}).get("transport") == "codex":
        print(f"note: reviewed on the Codex CLI transport; usage: {result.get('usage')}", file=sys.stderr)
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "raw"}, indent=1, sort_keys=True))
    else:
        print(text, end="")
    if args.post:
        try:
            number = post_comment(args.loop_id, text)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"posted to PR #{number}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
