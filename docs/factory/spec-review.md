# Spec review - a second opinion before a loop is launched

Two specs sent a loop to fix a symptom instead of a cause (ui2's bottom anchor, ui3's stretch). Since F1 every loop
spec is reviewed by a model from a different vendor than the one that writes and grades the loops, before the spec
is merged. The reviewer reads the spec, its exit script and the material around it, and returns structured findings.
It never edits the spec.

## The rule

1. A spec is reviewed before it is merged. The review is run against the spec branch and posted on its PR
   (`--post`), so the spec writer and the reviewer's findings sit together.
2. The reviewer's **blockers** are answered before launch: in the spec (an edit) or in the PR (a reply saying why
   the finding does not apply). A blocker without an answer is a spec that is not ready.
3. **Should** items are answered or explicitly declined in the PR. A decline is one line; silence is not a decline.
4. **Nits** are the writer's call.
5. The review is advisory. It does not gate the merge mechanically, because the reviewer can be wrong too - but it
   must have been read, and rules 2 and 3 say what "read" means.

## How to run it

```
python3 tools/factory/spec_review.py <loop-id> [--report <design-report.md>] [--post] [--json]
```

- `<loop-id>` names `loops/<id>.md` (and `loops/<id>.exit.sh` when it exists).
- `--report` hands the reviewer a `design-report.md` from a review run (`tools/e2e/review.py`). With it, the
  symptom-versus-cause section is judged against the report's evidence, which is the check that would have caught
  ui2 and ui3. Use the most recent run the spec quotes (`~/factory/reviews/<run>/design-report.md`).
- `--post` adds the review as a comment on the open PR whose branch carries the spec, through `gh pr comment`
  (the PR whose file list contains `loops/<id>.md`; else the PR whose head branch is `loop/<id>`, `spec/<id>` or
  `<id>`). Without `--post` the review goes to stdout only.
- `--json` prints the structured result (findings, inputs, model, usage) instead of markdown.
- Model: `faden.runtime.openai_model.OpenAIModel` - `FADEN_JUDGE2_MODEL` (default `gpt-6-astra`), key
  `OPENAI_API_KEY`, fallback `FADEN_JUDGE2_FALLBACK` (default `gpt-5.6-sol`) when the model is not available to the
  account; the fallback is printed, never silent. `FADEN_FACTORY_FAKE_MODEL=1` swaps in a canned reviewer for tests.
- Exit 0 after a review (blockers included), 2 when the spec is missing, the model did not answer, or `--post` found
  no open PR.

## What it reads

Assembled in a fixed order (`spec_review.gather`; a test pins the list):

1. `loops/<id>.md` - the spec.
2. `loops/<id>.exit.sh` - the exit script, the thing that decides the loop is done.
3. `docs/design/MAPPING.md` - the atlas-to-shell mapping every screens loop follows.
4. The two most recently written loop notes in `docs/notes/` (by mtime) - what the last loops built and warned about.
5. `tools/e2e/polish_rules.json` - the layout gate's constants.
6. The Scope fence of every other loop spec that is not yet merged as a note (a spec in `loops/` with no
   `docs/notes/<id>.md` and no `loop/<id>` merge on main), so fence overlaps between loops that may run at the same
   time are visible.
7. The `--report` file when given.

## What it returns

A markdown review: a header (reviewer model, counts, the reviewer's two-sentence summary, the inputs), then
**Blockers** first, then exactly eight numbered sections, each finding with a severity, the quoted line it refers to
and a suggested edit:

1. Claims the exit script does not actually test (a requirement with no check).
2. Checks that would pass without the requirement being met.
3. Decisions that treat a symptom as a cause, judged against the report's evidence when a report is given.
4. Assumptions about a machine, a path, a file or a service that the implementer may not have.
5. Ambiguities an implementer would have to guess at.
6. Fence overlaps with other unmerged loops, and anything the fence lets through that it should not.
7. Acceptance measured on fixtures where real data exists.
8. Missing regression tests for the defects the spec names.

A section with nothing to report says "Nothing found." When the model's reply carries no structured findings, the
eight sections are still printed (empty) and the raw reply follows, so nothing is lost.

The prompt is `tools/factory/spec_review_prompt.md`. Change the sections there and in `spec_review.SECTIONS`
together; the exit scripts grep for the eight numbered headings.

## Rounds (policy, 2026-09-12)
- A spec gets at most **four** review rounds.
- A round with **no blockers** means the spec is ready: at most one further round, and only when something material
  changed. Should-fix and nit items are taken or declined with a one-line reason on the PR; they never trigger a round
  on their own.
- After round four the reviewer stops. The steering conversation lists the remaining findings on the PR as accepted
  risks, merges, and launches. Nothing loops on minor items.
- The watcher (m3) enforces this: it counts `## Spec review` comments per PR, refuses to post a fifth, and marks the
  PR `review: ready` after a clean round.
