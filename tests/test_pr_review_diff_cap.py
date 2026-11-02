"""tools/factory/pr_review.sh, the diff cap (n2026-11-02-6): a diff over the cap is cut by whole files from the end, the
banner names every file left out, product files come before tests/ and docs/, and a diff under the cap goes in whole.

The script runs end to end against fake `gh` and `codex` executables on PATH (no GitHub, no model) and a local git
origin that carries the PR head as refs/pull/7/head; the fakes record the prompt and the posted comment."""
import os
import re
import shutil
import stat
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "factory", "pr_review.sh")

FAKE_GH = r'''#!/usr/bin/env python3
import os, shutil, sys
d = os.environ["FAKE_DIR"]; a = sys.argv[1:]
if a[:2] == ["pr", "diff"]:
    sys.stdout.buffer.write(open(os.path.join(d, "diff"), "rb").read())
elif a[:2] == ["pr", "comment"]:
    shutil.copy(a[a.index("--body-file") + 1], os.path.join(d, "comment.md")); print("https://example.invalid/c/1")
elif a[:2] == ["pr", "view"]:
    j = a[a.index("--json") + 1]
    if j == "headRefOid": print(open(os.path.join(d, "head")).read().strip())
    elif j == "headRefName": print("fix/test")
    else: print("Title: a test PR\nBranch: fix/test -> main\n\nbody")
else:
    sys.exit("fake gh: unexpected " + " ".join(a))
'''

FAKE_CODEX = r'''#!/usr/bin/env python3
import os, sys
open(os.path.join(os.environ["FAKE_DIR"], "prompt.txt"), "wb").write(sys.stdin.buffer.read())
print("APPROVE: fake review.")
'''


def section(path, nbytes, new=False):
    """One file's diff section, `nbytes` long, ending in a newline."""
    head = f"diff --git a/{path} b/{path}\n"
    head += ("new file mode 100644\n--- /dev/null\n" if new else f"index 1111111..2222222 100644\n--- a/{path}\n")
    head += f"+++ b/{path}\n@@ -0,0 +1,1 @@\n"
    body_len = nbytes - len(head.encode())
    assert body_len > 2
    return head + "+" + "x" * (body_len - 2) + "\n"


def rename_section(old, new, nbytes):
    head = f"diff --git a/{old} b/{new}\nsimilarity index 90%\nrename from {old}\nrename to {new}\n" \
           f"--- a/{old}\n+++ b/{new}\n@@ -1 +1 @@\n"
    return head + "+" + "y" * (nbytes - len(head.encode()) - 2) + "\n"


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "fake"; fake.mkdir()
    bindir = tmp_path / "bin"; bindir.mkdir()
    for n, body in (("gh", FAKE_GH), ("codex", FAKE_CODEX)):
        p = bindir / n; p.write_text(body); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    g = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
             GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    src, clone = tmp_path / "origin", tmp_path / "clone"
    run = lambda *c, **k: subprocess.run(c, check=True, capture_output=True, text=True, env=g, **k)
    run("git", "init", "-q", "-b", "main", str(src))
    (src / "README").write_text("x\n")
    run("git", "-C", str(src), "add", "README"); run("git", "-C", str(src), "commit", "-qm", "init")
    sha = run("git", "-C", str(src), "rev-parse", "HEAD").stdout.strip()
    run("git", "-C", str(src), "update-ref", "refs/pull/7/head", sha)
    run("git", "clone", "-q", str(src), str(clone))
    (fake / "head").write_text(sha + "\n")
    e = dict(g, FAKE_DIR=str(fake), PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    for k in ("PR_REVIEW_DIFF_CAP", "PR_REVIEW_EXCLUDE", "PR_REVIEW_EVIDENCE"):
        e.pop(k, None)
    return {"fake": fake, "clone": clone, "env": e}


def review(env, diff, cap=None):
    (env["fake"] / "diff").write_bytes(diff.encode())
    e = dict(env["env"])
    if cap is not None:
        e["PR_REVIEW_DIFF_CAP"] = str(cap)
    r = subprocess.run(["bash", SCRIPT, "o/r", "7", str(env["clone"])], capture_output=True, text=True, env=e)
    m = re.search(r"artifacts in (\S+)\)", r.stdout)
    if m and m.group(1).startswith(tempfile.gettempdir()):
        shutil.rmtree(m.group(1), ignore_errors=True)
    assert r.returncode == 0, r.stderr
    prompt = (env["fake"] / "prompt.txt").read_bytes().decode()
    return prompt.split("\n\n## Diff\n\n", 1)[1], prompt, (env["fake"] / "comment.md").read_text()


def banner(text):
    m = re.search(r"DIFF CUT: (\d+) files? left out: (.*?) \(the diff is", text)
    assert m, text
    return int(m.group(1)), m.group(2).split(", ")


def test_over_cap_cuts_whole_files_product_first_and_names_the_rest(env):
    secs = {
        "tests/invariants/test_c1_planning.py": section("tests/invariants/test_c1_planning.py", 30000),
        "faden/coach/core.py": section("faden/coach/core.py", 40000),
        "docs/notes/c1.md": section("docs/notes/c1.md", 5000),
        "tests/invariants/test_c1_review_r1.py": section("tests/invariants/test_c1_review_r1.py", 8000, new=True),
        "faden/coach/tools.py": section("faden/coach/tools.py", 20000),
    }
    full = "".join(secs.values())
    # the cap falls inside the tests: the product (60000) and the planning test (30000) fit, the rest does not
    sent, prompt, comment = review(env, full, cap=94000)
    want = (secs["faden/coach/core.py"] + secs["faden/coach/tools.py"]
            + secs["tests/invariants/test_c1_planning.py"])
    assert sent == want  # whole sections, product first, the PR's order inside each group, nothing past the cut
    n, left = banner(prompt)
    assert (n, left) == (2, ["docs/notes/c1.md", "tests/invariants/test_c1_review_r1.py"])
    assert banner(comment) == (n, left)
    assert f"the diff is {len(full)} bytes" in comment


def test_cut_never_lands_mid_file(env):
    names = [f"pkg/m{i}.py" for i in range(6)]
    secs = [section(p, 10000 + 1000 * i) for i, p in enumerate(names)]
    full = "".join(secs)
    sent, prompt, _ = review(env, full, cap=40000)  # a byte cut would fall inside pkg/m3.py
    assert len(sent.encode()) <= 40000
    assert sent == "".join(secs[:3])
    assert sent.endswith("\n") and full.startswith(sent)
    assert banner(prompt) == (3, names[3:])


def test_under_cap_passes_whole(env):
    full = section("tests/test_a.py", 3000) + section("src/a.py", 4000) + section("docs/x.md", 2000)
    sent, prompt, comment = review(env, full, cap=9000)
    assert sent == full  # byte-identical, PR order kept: nothing reordered when nothing is cut
    assert "DIFF CUT" not in prompt and "DIFF CUT" not in comment


def test_default_cap_takes_a_diff_over_the_old_200k(env):
    full = section("faden/big.py", 150000) + section("tests/test_big.py", 100000)
    sent, prompt, _ = review(env, full)
    assert sent == full and "DIFF CUT" not in prompt


def test_left_out_names_renames_and_paths_with_spaces(env):
    full = (section("src/keep.py", 5000) + rename_section("tests/old_t.py", "tests/new_t.py", 3000)
            + section("src/my file.py", 6000))
    sent, prompt, _ = review(env, full, cap=9000)
    assert sent == section("src/keep.py", 5000)
    assert banner(prompt) == (2, ["src/my file.py", "tests/new_t.py"])
