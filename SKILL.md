---
name: github-perf-evidence
description: Gather defensible performance-review evidence for GitHub users over a date window. Load when asked to summarize what someone shipped, build promo/review packets from GitHub, compare engineers' output, audit contribution volume, count bugs caught in review, or find team patterns a manager might be missing. Separates hand-authored code from generated and vendored content, detects fork double-counting, discovers formal ownership roles, classifies review comments to count real defects caught, and produces an interactive HTML report. Use for any "what did <user> do from X to Y" request.
---

# GitHub Performance-Review Evidence

**YOU run this pipeline. The user should never be asked to run a Python command.**

Run every step yourself with Bash. Report findings in chat and hand the user a
finished HTML report. The only things you ask the user for are decisions you
cannot make: who is in scope, what date window, and identity confirmation.

## What the user gets

A single interactive `report.html` (no server, no network) plus per-person YAML
and JSON. Tabs: Cohort, Insights, Rankings, Team risk, and one per person.

## The problem this solves

Raw GitHub line counts are wrong, usually by 2x-30x, always in the flattering
direction. Generated code, vendored content, license manifests and fork
duplicates all land in `additions`. A small codegen fix can look like a
22,000-line contribution. Reviews built on those numbers fall apart the moment
someone opens one PR.

## Hard rules

1. **Never cite raw `additions`.** Classify first, then cite hand-authored volume.
2. **Verify every classification rule against the actual repo.** Read the file
   header for a codegen marker, read the `generate.sh`, read the vendoring
   README. Never pattern-match on a hunch.
3. **Run the audit step and act on it.** Do not skip it because numbers "look
   fine" -- it exists because plausibility checks catch real bugs.
4. **One classifier version per cohort.** If you fix a rule, re-run *everyone*.
5. **Volume is not impact.** Surface ownership, review depth and defects caught
   alongside line counts, and say plainly that volume rankings rank activity.
6. **Low activity is a question, never a finding.** Most engineering work is
   invisible here. Say so every time you report.
7. **Never invent a number.** Every figure you state must come from the tool
   output. If you did not measure it, do not say it.

## Before you start: two things to ask the user

Ask these together, once, then proceed without further check-ins:

1. **Who and what window?** GitHub logins (or an org to discover from) and dates.
2. **Any private repos?** Default is public-only. `--visibility all` includes
   private repos their token can read, and makes the output confidential.

Then **resolve identity yourself** before scanning: check `gh api users/<login>`
for name and company, and look for a local doc that states the handle. Write what
you found into `identity_evidence` per person. If a profile has no name and you
inferred the mapping, say so in the roster AND flag it in your final report --
the user must confirm before using that file. Never infer pronouns or personal
attributes from a name; use they/them.

## Working directory

Write output **outside** this skill directory, somewhere the user can find it,
e.g. `~/perf-review-<window>/`. Add a `.gitignore` containing `*` on the first
run: the output holds named employees' performance data and must never reach a
repo.

## Pipeline (you run all of this)

```bash
S=~/.claude/skills/github-perf-evidence/scripts
OUT=~/perf-review-2026-h1     # outside the skill dir

python3 $S/discover.py  --org ORG --days 180 -o $OUT/roster.json   # optional
#   then EDIT roster.json yourself: drop non-team members, fill identity_evidence

python3 $S/fetch.py     --roster $OUT/roster.json --outdir $OUT --dry-run
python3 $S/fetch.py     --roster $OUT/roster.json --outdir $OUT --compare-window
python3 $S/cache.py     --outdir $OUT --all
python3 $S/classify.py  --outdir $OUT --profile kubernetes web    # --list-profiles
python3 $S/audit.py     --outdir $OUT          # READ IT, fix patterns.json, loop
python3 $S/ownership.py --outdir $OUT
python3 $S/build.py     --outdir $OUT --roster $OUT/roster.json --own-orgs ORG
python3 $S/report.py    --outdir $OUT
```

**Run the long steps with `run_in_background: true`.** A real 8-person / 7-month
scan makes ~1,700 unique PR lookups and takes over two hours across `fetch.py`
and `cache.py`. Poll the output file; do not block the conversation. Tell the
user up front that it will take hours and that it is resumable.

**Order matters.** `fetch.py` rewrites the bundles and clears classification, so
after any re-fetch you must re-run `classify.py` before `build.py`. `build.py`
fails with instructions if you forget.

**Everything is resumable.** `cache.py` flushes every 25 PRs and skips what it
already has. If the user needs to stop, tell them the exact command to re-run --
nothing is lost and nothing is re-fetched.

**Rate limits are expected**, not exceptional. GitHub's secondary limit triggers
on sustained request rate even with quota left. Transient 403/429s retry
automatically; a hard failure raises loudly rather than recording zero. If you
hit one, wait ~10 minutes and re-run the same command.

## Two steps where YOU are the model

These are the only steps that need an LLM. **You are that LLM** -- do not hand
the prompt to the user.

### Defects caught in review

```bash
python3 $S/comments.py --outdir $OUT --fetch
python3 $S/comments.py --outdir $OUT --prompt --limit 250 > $OUT/p.txt
#   READ p.txt, classify every thread, write your JSON answer to $OUT/answers.json
python3 $S/comments.py --outdir $OUT --load $OUT/answers.json
```

Read the threads and classify each as `bug`, `design_flaw`, `correctness_risk`,
`test_gap`, `style_nit`, `question`, `praise`, `logistics` or `other`, with a
severity and whether the author acknowledged it. The prompt file states the exact
schema; follow it exactly, including the thread ids.

Be strict. Err toward the lower category. An inflated defect count is worse than
a missing one because it lands in someone's performance review.

Keywords cannot do this job: on real PRs explicit acknowledgements appear in only
0-2 comments out of 8-30, and no regex separates a real correctness bug from
"nit: move this to validation.go".

**The author side is not a quality score.** `review_rigor_received` rises with
ambitious code, early drafts and thorough reviewers; it falls with trivial code
and rubber-stamping. The only useful reading is the inverse: a *low* number on
high shipped volume means nobody reviewed it properly, which is a process problem
the manager owns. Never rank anyone on it, and say this when you report it.

`commentcache.json` holds verbatim text engineers wrote about each other's code.
It is the most sensitive artifact here. Never paste it into chat wholesale, never
quote it back at a person.

### Insights

```bash
python3 $S/insights.py --outdir $OUT --prompt > $OUT/ip.txt
#   READ ip.txt, write your JSON answer to $OUT/notes.json
python3 $S/insights.py --outdir $OUT --load $OUT/notes.json
python3 $S/report.py   --outdir $OUT       # re-render with insights
```

Twelve deterministic detectors find the patterns and produce every number:
release toil, theme mix, review reciprocity, knowledge silos, review load
balance, depth-vs-volume rank disagreement, stalled work, trajectory shifts,
external visibility, bus factor, defect catching, measurement risk.

Your job is to say what the manager should DO. The split exists so you can only
interpret measured figures -- **never cite a number the detectors did not
produce.**

When you narrate:

- Prefer insights crossing two or more detectors. Single-metric observations are
  already visible in the report tables.
- Team-level findings (toil, silos, bus factor, review imbalance) are the
  manager's problems to fix, not individual failings. Frame them that way.
- Every insight needs a `caveat` saying what would make the reading wrong.
- Never rank people by volume. Never treat low activity as a finding.

## Reporting to the user

Lead with what the evidence supports, not the table:

- The standout, and *why* -- ownership roles, defects caught and external merges
  beat volume.
- Anyone whose ratio is unusual, and what to **ask** them, not what to conclude.
- Anything unresolved: identity uncertainty, truncated counts, big open PRs.
- Then the numbers, with the caveat that they rank activity.
- Finish with the report path and how to open it.

Quote `hand_additions_canonical_merged` for volume. Never quote raw additions.

## References

- `references/classification.md` -- verified pattern table, ecosystem gotchas,
  how to verify a new rule
- `references/methodology.md` -- API ceilings, fork double-counting, fairness
- `ETHICS.md` -- what these numbers do and do not mean. Read before first use.

## Tests

```bash
python3 tests/test_pipeline.py     # 232 assertions, offline, no gh needed
```
