# github-perf-evidence

Gathers defensible performance-review evidence for GitHub users over a date
window. Emits caveat-annotated YAML per person plus a cohort comparison index.

## Why this exists

Raw GitHub line counts are wrong, usually by 2x-30x, and always in the
flattering direction. Real cases this skill was built from:

- a **22,720-line** PR whose actual change was **10 lines** — the rest was an
  AWS SDK service model
- a **636,236-line** PR that was seven `curl`-downloaded Kubernetes swagger specs
- a **16,470-line** PR that was two Grafana dashboard exports
- a person's total overstated **30x** by generated + vendored content
- **16,163 duplicated lines** inside one PR from a copied subtree
- fork PRs double-counting the same work as their upstream twins

Any review built on unclassified `additions` falls apart the first time someone
opens one of those PRs.

## Install

Drop the directory into `~/.claude/skills/` (Claude Code picks it up
automatically), or clone it anywhere and run the scripts directly.

Requires the [`gh` CLI](https://cli.github.com/) authenticated (`gh auth login`).
The pipeline is Python-3 stdlib only. Validating the YAML needs `pyyaml` in some
interpreter; on macOS `/usr/bin/python3` usually has it when Homebrew's does not.

## Usage

```bash
S=./scripts
OUT=/tmp/perf_evidence

# 0. discover who was active (or hand-write a roster from roster.example.json)
python3 $S/discover.py --org myorg --days 180 -o roster.json
#    EDIT roster.json: drop non-team-members, fill in identity_evidence

python3 $S/fetch.py    --roster roster.json --outdir $OUT
python3 $S/cache.py    --outdir $OUT --commits
python3 $S/classify.py --outdir $OUT --profile kubernetes   # --list-profiles
python3 $S/audit.py    --outdir $OUT     # review, fix patterns.json, repeat
python3 $S/build.py    --outdir $OUT --roster roster.json --own-orgs myorg
```

Expect 2-4 audit rounds on an unfamiliar codebase. `cache.py` makes each
reclassification instant, so iterating is cheap.

## The audit loop is the whole point

`audit.py` flags implausible classifications and names the files driving them.
It caught every bug listed above. Work its output top-down, verify each
suspicious file in-repo, then add the pattern to `patterns.json` with the
evidence and re-run classification **for everyone** so the cohort stays
comparable.

## Files

| | |
|---|---|
| `SKILL.md` | instructions loaded into context |
| `ETHICS.md` | **read this** — what the numbers do and do not mean |
| `scripts/discover.py` | find active contributors → starter roster |
| `scripts/fetch.py` | search API → per-person JSON bundles |
| `scripts/cache.py` | per-PR file lists, fetched once |
| `scripts/classify.py` | hand / generated / vendored split |
| `scripts/audit.py` | plausibility flags — **do not skip** |
| `scripts/build.py` | render evidence YAML + cohort index |
| `references/classification.md` | verified pattern table, per-ecosystem gotchas |
| `references/methodology.md` | API ceilings, what numbers can't tell you, fairness |

## What each person file contains

`summary` · `ownership_roles` (read from OWNERS/CODEOWNERS, not self-reported) ·
`external_upstream_contributions` · `fork_activity` · `collaboration`
(discussion on others' work) · `language_mix` · `delivery` (cycle time, test
share) · `authored_commit_subjects` (**what** they built) · `cadence` ·
`by_repo` · `reviews` · `largest_hand_authored` · `open_and_wip` ·
`issues_opened` · `entries`

## Reading the output

Quote `hand_additions_canonical_merged`. Never quote raw additions.

And treat the rankings as what they are: orderings of activity. Formal ownership
roles, review load, discussion reach, and external upstream merges usually say
more about contribution than volume does. Low GitHub volume is a question to ask
the person, not a conclusion about them.

See [ETHICS.md](ETHICS.md) before using this on a real team.

## License

MIT. Contributions welcome, especially verified classifier patterns for
ecosystems not covered yet.
