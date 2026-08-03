---
name: github-perf-evidence
description: Gather defensible performance-review evidence for GitHub users over a date window. Load when asked to summarize what someone shipped, build promo/review packets from GitHub, compare engineers' output, or audit contribution volume. Separates hand-authored code from generated and vendored content, detects fork double-counting, discovers formal ownership roles, and emits caveat-annotated YAML per person plus a cohort comparison index. Use for any "what did <user> do from X to Y" request.
---

# GitHub Performance-Review Evidence

Produces per-person evidence files that a reviewer (or another agent) can quote
without accidentally citing an inflated or double-counted number.

**The core problem this solves:** raw GitHub line counts are wrong, usually by
2x-30x. Generated code, vendored third-party content, license manifests, and
fork duplicates all land in `additions`. A naive scan makes a small codegen fix
look like a 22,000-line contribution. Reviews built on those numbers are
indefensible the moment someone spot-checks one PR.

## Hard rules

1. **Never cite raw `additions`.** Always classify first, then cite
   hand-authored volume.
2. **Verify every classification rule against the actual repo** before
   trusting it. Read the file header for a codegen marker, read the
   `generate.sh`, read the vendoring README. Do not pattern-match on a hunch.
3. **Run the audit step and act on it.** It exists because plausibility
   checks catch real bugs. Do not skip it because the numbers "look fine."
4. **One classifier version per cohort.** Comparing people classified
   differently is a defect, not a nuance. If you fix a rule, re-run
   *everyone*.
5. **State what is missing.** For most orgs, public GitHub is a minority of
   total output. Say so in the output.
6. **Volume is not impact.** A 10-line fix in a code generator can propagate
   to 100+ downstream repos. Surface ownership roles and review load
   alongside line counts, and say plainly that ranking by volume ranks
   activity.

## Prerequisites

`gh` CLI authenticated (`gh auth status`). Scripts use only the stdlib, so any
`python3` works. YAML validation needs `pyyaml` in *some* interpreter; on macOS
`/usr/bin/python3` usually has it when homebrew python does not.

## Pipeline

```bash
S=~/.claude/skills/github-perf-evidence/scripts
OUT=/tmp/perf_evidence          # working dir; anything writable

# 0. optional: generate a starter roster instead of writing one by hand
python3 $S/discover.py  --org myorg --days 180 -o roster.json
#    then EDIT roster.json: drop non-team-members, fill identity_evidence

python3 $S/fetch.py     --roster roster.json --outdir $OUT
python3 $S/cache.py     --outdir $OUT --commits          # file lists + commit subjects
python3 $S/classify.py  --outdir $OUT --profile kubernetes web   # see --list-profiles
python3 $S/audit.py     --outdir $OUT                    # ← REVIEW OUTPUT, then loop
python3 $S/build.py     --outdir $OUT --roster roster.json --own-orgs myorg
```

`discover.py` finds who was active so you do not have to know every login.
It filters bots and leaves `identity_evidence` blank on purpose: it proves a
login was active, not who the human is.

`--commits` costs one extra API call per PR and buys the single most useful
section in the output: the commit subjects the person actually authored. Line
counts say how much; subjects say **what**. Use it.

`--profile` seeds ecosystem-specific classifier rules. The default seed is
deliberately narrow because a rule that wrongly marks authored code as
generated silently erases someone's work. `--list-profiles` to see them.

`audit.py` is not optional and not a formality. It prints a ranked list of
suspicious PRs and the specific files driving them. Work the list:

```bash
gh api repos/<owner>/<repo>/contents/<path> --jq .content | base64 -d | head -5
```

If you find a generated or vendored file counted as hand-authored, add the
pattern to `$OUT/patterns.json` (created by `classify.py` on first run), then
re-run `classify.py` → `audit.py` for **all** people. `cache.py` means
reclassification is instant, so iterate freely. Expect 2-4 rounds on a codebase
you have not scanned before.

Each round, record *why* a pattern was added, with the evidence, in
`patterns.json`'s `verified` field. That field is copied into the output so a
reader can check your work.

## Roster

`roster.json` — the only file you write by hand:

```json
{
  "window": {"start": "2026-01-01", "end": "2026-08-03"},
  "people": [
    {"github_login": "octocat",
     "id": "ocat",                       // internal alias; filename key
     "name": "Octo Cat",                 // optional
     "team": "Platform",                 // optional
     "level": "senior",                  // optional, free-text
     "title": "Software Engineer II",    // optional
     "manager": "mgr-alias",             // optional
     "identity_evidence": "how login->person was established"
    }
  ]
}
```

Only `github_login` is required; `id` defaults to the login. Everything else is
passthrough metadata that appears in the output.

**Resolve identity before scanning.** A GitHub login is not a person. Record how
you established the mapping in `identity_evidence`. If a profile has no name
field and you inferred the mapping, say so explicitly — the skill marks that
`LOW-verify` and a reviewer must confirm before using the file. Never infer
someone's pronouns or personal details from a name.

## Output

```
$OUT/
  COHORT-INDEX.yaml         # comparison table, rankings, cross-cohort caveats
  <id>-evidence.yaml        # one per person
  patterns.json             # classifier rules + verification notes
  audit-report.txt          # last audit run
  filecache.json            # per-PR file lists (reclassify without refetching)
  commitcache.json          # per-PR commit subjects (with --commits)
```

Each person file carries: `summary`, `ownership_roles`,
`external_upstream_contributions`, `fork_activity`, `collaboration` (discussion
on others' work), `language_mix`, `delivery` (cycle time, test share),
`authored_commit_subjects`, `cadence`, `by_repo`, `reviews`,
`largest_hand_authored`, `open_and_wip`, `issues_opened`, and `entries`.

Point downstream consumers at `COHORT-INDEX.yaml` first.

### Which number to quote

`hand_additions_canonical_merged` — hand-authored, merged, in the canonical
upstream repo. Most conservative defensible figure. The files also carry
`hand_additions_total`, `_merged`, `_open`, `_in_forks`, and a dedup estimate;
`summary.recommended_metric` names the right one.

## Reporting to a human

Lead with what the evidence supports, not the table. Useful shape:

- The standout, and *why* — formal roles and external validation beat volume.
- Anyone whose ratio is unusual (very high review load, very low volume) and
  what to ask them, not what to conclude about them.
- Anything unresolved: identity uncertainty, truncation, big open PRs.
- Then the numbers, with the caveat that they rank activity.

Low GitHub volume is a **question**, never a finding. The work may live in
internal code review, on-call, design, or mentoring. Say that.

## References

- `references/classification.md` — the verified pattern table, per-ecosystem
  gotchas, and how to verify a new rule
- `references/methodology.md` — search-API ceilings, fork double-counting,
  review-total truncation, what the numbers cannot tell you
