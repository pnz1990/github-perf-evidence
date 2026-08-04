# github-perf-evidence

**Performance-review evidence from GitHub that survives being fact-checked.**

A [Claude Code](https://claude.com/claude-code) skill (and a standalone Python
pipeline) for engineering managers who need to know what their team actually
shipped over a review period.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3](https://img.shields.io/badge/python-3.8%2B-blue)
![Dependencies: none](https://img.shields.io/badge/dependencies-stdlib%20only-green)
![Tests](https://img.shields.io/badge/tests-175%20offline-brightgreen)

---

## The problem

You ask a tool "what did this engineer ship last quarter?" It reports **+22,720
lines**. You put that in a review packet.

Then someone opens the PR. The real change was **153 lines** of logic. The other
22,567 were a vendored SDK schema that a code generator pulled in.

That is a 148x overstatement, and it is the *normal* case. GitHub's `additions`
field counts everything in the diff:

| What it silently counts | Real example found while building this |
|---|---|
| Vendored dependency schemas | **22,567** of 22,720 lines in one "feature" PR |
| Downloaded API specs | **636,236** lines — seven `curl`-ed JSON files |
| Generated code (codegen, CRDs, mocks) | **30x** overstatement on one engineer's total |
| Generated license manifests | 1,700–2,600 lines in *every* new-service PR |
| Dashboard JSON exported from a UI | **16,470** lines across two files |
| Copied subtrees inside one PR | **16,163** lines counted twice |
| Fork PRs duplicating their upstream twin | Same work counted twice |

Every one of these errors makes someone look **better**, which is precisely why
nobody questions them until the review is already written.

## What this does instead

Splits every line into **hand-authored**, **generated**, and **vendored** — then
refuses to let you quote the wrong number.

```yaml
summary:
  recommended_metric: hand_additions_canonical_merged
  hand_additions_canonical_merged: 9752      # <- QUOTE THIS
  generated_additions_excluded: 59642
  vendored_additions_excluded: 496
  raw_additions_total: 88482                 # DO NOT CITE
```

Every misleading number ships with a machine-readable caveat naming *how* it
misleads, so a reader (or a downstream agent) can't quote it naked.

## But volume is the least useful thing here

Here is a real engineer from a test run:

| | |
|---|---|
| PRs authored | 2 |
| Hand-authored lines | 6 |
| **Reviews given** | **8** |
| **Discussion threads on others' work** | **13** |

A line-count report calls this person inactive. They are doing 21 units of review
and unblocking work — holding the team up, invisibly.

So the output leads with the things that actually indicate contribution.

### Review *depth*, not review count

`reviews_given` counts PRs. Measured on real PRs, one reviewer had **52 review
events and 11 inline comments** while another had **9 events and 50 inline
comments**. A count-only metric ranks the first 5.8x higher; only the second was
engaging deeply. So the report also carries inline-comment counts, verdict mix
(`changes_requested` is senior behaviour that costs social capital), and a
bare-approval rate.

### Everything else it surfaces

- **`ownership_roles`** — read out of `OWNERS` / `CODEOWNERS`. Externally
  granted, independently verifiable, and it distinguishes a *maintainer* tier
  from a *reviewer* tier automatically.
- **`de_facto_ownership`** — subsystems where they wrote most of the commits.
  Seniority evidence without a formal grant.
- **`collaboration`** — discussion threads on other people's PRs and issues.
  Design debate, triage, mentoring. Invisible in every PR-count tool.
- **`trajectory`** — the window split in half, with deltas. Growth answers "is
  this person expanding scope?", which a snapshot cannot.
- **`authored_commit_subjects`** — what they *built*. Line counts can't tell a
  refactor from a feature.
- **`external_upstream_contributions`** — merged PRs to projects outside your
  org, weighted by stars.
- Plus `language_mix`, `delivery` (cycle time, test share), `cadence`,
  `by_repo`, `fork_activity`, `open_and_wip`, `issues_opened`, and a full
  per-PR record. 19 sections per person.

### And one section that is about the team, not a person

`ownership.py` reports **bus-factor risk**: subsystems where a single person
wrote at least half the commits.

```
BUS-FACTOR RISK -- one person is >=50% of commits:
  someproject/pkg/ipam   somedev   24/47 commits
  someproject/pkg/aws    somedev   11/17 commits
```

That is a staffing decision you own. Not a credit to award, and not a problem to
raise with the person who happens to be the sole author.

---

## Install

**As a Claude Code skill** (recommended):

```bash
git clone https://github.com/pnz1990/github-perf-evidence.git \
  ~/.claude/skills/github-perf-evidence
```

Claude Code discovers it automatically. Ask it *"what did octocat ship from
January to June?"* and it runs the pipeline and reports back.

**As a standalone pipeline** — clone anywhere and run the scripts directly.

### Requirements

- [`gh` CLI](https://cli.github.com/), authenticated: `gh auth login`
- Python 3.8+ — **stdlib only**, nothing to `pip install`
- `pyyaml` in *some* interpreter if you want to validate the YAML (on macOS
  `/usr/bin/python3` usually has it when Homebrew's does not). The HTML report
  needs no YAML parser at all.

---

## Use

```bash
cd ~/.claude/skills/github-perf-evidence
S=./scripts
OUT=/tmp/perf_evidence
```

### 1. Build a roster

```bash
python3 $S/discover.py --org YOUR_ORG --days 180 -o roster.json
# or: --repo owner/repo-a --repo owner/repo-b --start 2026-01-01 --end 2026-06-30
```

**Then edit `roster.json`.** Discovery finds everyone who opened a PR, including
outside contributors, and deliberately leaves `identity_evidence` blank — it
proves a *login* was active, not who the human is. Fill that in. See
[`roster.example.json`](roster.example.json).

```json
{
  "window": {"start": "2026-01-01", "end": "2026-06-30"},
  "people": [
    {"github_login": "octocat", "id": "ocat", "name": "Octo Cat",
     "team": "Platform", "level": "senior",
     "identity_evidence": "Internal directory lists this handle. Confirmed."}
  ]
}
```

Only `github_login` is required. Everything else flows through to the output.

### 2. Collect

```bash
python3 $S/fetch.py --roster roster.json --outdir $OUT --dry-run   # cost estimate
python3 $S/fetch.py --roster roster.json --outdir $OUT --compare-window
python3 $S/cache.py --outdir $OUT --all
```

| Flag | |
|---|---|
| `--dry-run` | estimate API calls before committing to a scan |
| `--compare-window` | also scan two half-windows for trajectory |
| `--visibility all` | include **private** repos (needs `repo` scope) |
| `cache.py --all` | file lists + commit subjects + review depth |

`cache.py` stores every PR's file list on disk so reclassification is instant.
Without it, each correction in step 3 costs another full crawl — and you'd stop
correcting.

**Order matters:** `fetch.py` rewrites the bundles and clears classification, so
re-fetching means re-running `classify.py`. `build.py` stops with instructions if
you forget.

**Budget hours, not minutes.** A measured 8-person / 7-month scan made ~1,700
unique PR lookups and took over two hours across `fetch.py` and `cache.py`. Run
`--dry-run` first, then run the long steps in the background.

**Everything is resumable.** `cache.py` flushes every 25 PRs and skips whatever
it already has, so you can stop any time and re-run the identical command to
continue — nothing is re-fetched and nothing is lost. It prints a live ETA.

GitHub's *secondary* rate limit triggers on sustained request rate even with
quota remaining. Transient 403/429s are retried with exponential backoff and
search calls are paced; a hard failure raises loudly rather than recording zero,
because "this person did nothing" is the worst possible silent error in a
performance review.

### 3. Classify

```bash
python3 $S/classify.py --outdir $OUT --list-profiles
python3 $S/classify.py --outdir $OUT --profile kubernetes web
```

The default rules are deliberately narrow. Ecosystem-specific rules are opt-in,
because a rule that wrongly marks authored code as *generated* silently
**erases** someone's work — the one failure mode worse than inflation.

Bundled profiles: `kubernetes`, `web`, `aws-ack`.

### 4. Audit — do not skip this

```bash
python3 $S/audit.py --outdir $OUT
```

This is the step that makes everything else trustworthy. It flags implausible
classifications and names the exact files:

```
someuser  someorg/someproject#1294  [open]
  hand=636236  (97% of this person's hand total)
  FLAG low-file-count         636236 lines across only 17 hand files
  FLAG suspicious-extension   635240 of 636236 are data/config files
    102285   cmd/cli/embeddedschemas/swagger_v1.36.json
```

Check whether those files are really hand-written:

```bash
gh api repos/OWNER/REPO/contents/PATH --jq .content | base64 -d | head -5
```

Look for `Code generated`, `DO NOT EDIT`, a `generate.sh`, or a vendoring
README. Found one? Add the pattern to `$OUT/patterns.json` **with the evidence**,
then re-run `classify.py` and `audit.py` for *everyone*.

Expect **2–4 rounds** on an unfamiliar codebase. In the example above, two
verified rules took that person from 655,476 lines to 20,236.

### 5. Map ownership (optional)

```bash
python3 $S/ownership.py --outdir $OUT
```

De-facto ownership per subsystem, plus the bus-factor risk table. Bounded per
directory (`--timeout`) and it reports anything it skipped.

### 6. Build and read

```bash
python3 $S/build.py  --outdir $OUT --roster roster.json --own-orgs YOUR_ORG
python3 $S/report.py --outdir $OUT --open
```

```
$OUT/
  report.html              # <- interactive, self-contained, start here
  COHORT-INDEX.yaml/.json  # comparison table, rankings, caveats
  <id>-evidence.yaml/.json # one per person, 19 sections
  ownership.json           # de-facto owners + bus factor
  patterns.json            # your classifier rules + why each was added
  audit-report.txt
```

Every file is written twice: `.yaml` for humans and diffs, `.json` for tools.

**`report.html`** is one self-contained file — no server, no network calls, no
build step. Tabs for the cohort, rankings, team risk, and each person. Sortable
tables, searchable commit subjects. Caveats render as visible banners rather than
footnotes you can skip, and raw additions appear struck through next to the
number you should quote.

---

## Reading the output

Open `report.html` first, or `COHORT-INDEX.yaml` if you prefer text.

**Quote `hand_additions_canonical_merged`** — hand-authored, merged, in the
canonical repo. The most conservative defensible figure.

**Never quote raw additions.**

The rankings each measure something different and **none of them measures
impact**. A ten-line fix in a code generator can propagate to a hundred
downstream repos. The report says so on the page.

---

## Please read ETHICS.md

This tool makes it easy to put numbers next to people's names.
[`ETHICS.md`](ETHICS.md) is short and states the rules plainly:

1. **Never rank people by line count.** Volume orders activity, not value.
2. **Low numbers are a question, not a finding.** The work is usually somewhere
   GitHub can't see. Ask before concluding.
3. **Never paste a ranking into a review without its caveats.**
4. **Resolve identity first.** A login is not a person. Never infer pronouns or
   personal attributes from a name or handle.
5. **Be as skeptical of high numbers as low ones.**
6. **Tell people you're running it.** An unannounced activity scan on your
   reports is surveillance, not management.
7. **Review depth is not a productivity target.** Announce it and you get
   comment padding, not better reviews.
8. **Trajectory is not a verdict.** A declining half can be leave, on-call, or
   one big project landing on the other side of the split.

Not for stack ranking, PIP justification, or layoff selection. If you need
evidence for a hard conversation, the evidence is the work — which means reading
the PRs.

---

## What it cannot see

Public GitHub by default (`--visibility all` adds private repos your token can
read). Excluded by construction: internal code review systems, ticketing,
on-call load, incident response, design documents, mentoring, and interviewing.
For many teams that is the **majority** of the work.

Merge with your internal sources before drawing conclusions. Every output file
says this.

Known limits, all surfaced as caveats rather than hidden: GitHub search caps at
1,000 results per query; `reviewed-by:` counts PRs rather than review events;
path-based classification is a heuristic and a good-faith lower bound.

## Tests

```bash
python3 tests/test_pipeline.py
```

175 assertions, fully offline — no network, no `gh`, no pip. Exit code is
non-zero on failure so it drops into CI as-is.

Every real bug found while building this has a named regression test: variable
shadowing, a helper leaking raw text where a dict was expected, project-named
bots evading the filter (`<project>-bot` authored 9 of 14 PRs in one real window
and would have topped the roster), a stale-pipeline `KeyError`, report
field-name drift, and trend windows double-counting the split date. Deleting a
single classifier rule makes six of them fail — including the one that reports
5,200 lines instead of 200.

## Docs

| | |
|---|---|
| [`SKILL.md`](SKILL.md) | what Claude Code loads; the operating rules |
| [`ETHICS.md`](ETHICS.md) | what the numbers do and don't mean |
| [`references/classification.md`](references/classification.md) | verified pattern table, per-ecosystem gotchas, how to verify a new rule |
| [`references/methodology.md`](references/methodology.md) | API ceilings, fork double-counting, fairness |
| [`tests/test_pipeline.py`](tests/test_pipeline.py) | 175 offline assertions |

## Contributing

Verified classifier patterns for ecosystems not yet covered are the most useful
contribution. One rule: `verified` must say *how you confirmed it* — a codegen
marker, a generator script, a vendoring README. `"assumed"` is not acceptable.
Unverified rules are how a review gets embarrassed.

## License

MIT
