# Methodology, limits, and how to read the output

## API ceilings that silently lose data

**Search caps at 1000 results** (10 pages x 100) no matter what `total_count`
says. `fetch.py` records `total_count` separately from what it captured, so
truncation shows up as a caveat instead of a quietly low number. A prolific
reviewer will hit this: one real case had 738 reviews, of which 400 were
captured. The headline 738 is right; the per-repo breakdown is a sample.

If authored PRs truncate, split the window (`--only` one person, quarter at a
time) and merge. Do not report a truncated count as final.

**Rate limits:** search is 30 req/min, core 5000/hr. `cache.py` is the only
expensive step (one call per PR). It caches so you never pay twice.
`gh api rate_limit` to check headroom.

**`reviewed-by:` counts PRs, not review events.** Two review passes on one PR
count once. This understates review effort and cannot be fixed from search
alone.

**Excluded by construction:** private repos you can't see, force-pushed history,
squashed co-authorship, and PRs authored under a different account.

## Fork double-counting

Contributors often open a PR against their own fork for CI, then open the real
one upstream. Both match `author:`, so naive counting doubles the work.

`build.py` checks `fork`/`parent` on every repo and splits
`hand_additions_in_forks` out of the headline. Watch for shared staging/CI orgs that mirror an upstream
project — those are forks too, and their PRs are often invisible upstream.

Not every non-canonical repo is a duplicate. A merged PR to a genuinely
independent external project is the opposite: strong evidence. That's why
`external_upstream_contributions` is tracked separately and weighted by stars.

## Which number to quote

Preference order:

1. `hand_additions_canonical_merged` — hand-authored, merged, canonical repo.
   Most conservative defensible figure. **Default to this.**
2. `hand_additions_dedup_estimate` — also removes intra-PR duplicate subtrees.
3. `hand_additions_total` — includes open and fork work. Only with the caveat.
4. `raw_additions_total` — never cite. Present solely to show the gap.

## What the numbers cannot tell you

**Volume is not impact.** A 10-line fix in a code generator propagates to 100+
downstream repos. A 3,000-line controller bootstrap is mostly scaffolding. Two
engineers with identical line counts can differ by an order of magnitude in
value delivered. Say this out loud in any summary that includes a ranking.

**Low GitHub volume is a question, never a finding.** The work may live in
internal code review, on-call rotation, incident response, design documents,
customer escalations, interviewing, or mentoring. None of it appears here. When
someone's numbers are low, the output is a prompt for a conversation, not a
conclusion. Ask where their time went before forming a view.

**Signals that usually matter more than volume:**

- **Formal ownership** (`ownership_roles`) — someone else granted it, and the
  tier matters: a maintainer/approver group outranks a reviewer group in the
  same file.
- **Review load** (`review_to_authorship_ratio`) — a high ratio means the person
  spends their time unblocking others. Force multiplication, invisible in
  authored volume.
- **External upstream merges** — required convincing maintainers outside your
  org's control.
- **Merge rate** — a low rate can mean speculative work, or scope that keeps
  getting cut, or a reviewing bottleneck. Ask which.

**Comparability:** figures are comparable *within* one batch classified by one
`classifier_version`, and not across scans. If you fix a pattern, re-run
everyone. Mixing versions is a defect.

`repos_touched` is not portable across ecosystems. A project split into 100
small repos inflates it against a monorepo.

## Identity

A GitHub login is not a person. Resolve it before scanning and record how in
`identity_evidence`; the output marks a missing one `UNDOCUMENTED` and the
cohort index raises it as high severity.

Ranked strength of evidence:

1. An internal directory or doc that states the handle explicitly
2. The person confirming it directly
3. Profile name + company + activity overlap with their known team
4. Login pattern resemblance to an internal alias — **inference, not proof**

A profile with no name field, mapped by login pattern alone, must be confirmed
with the person before the file is used in a review. Never infer pronouns,
gender, or other personal attributes from a name or handle; use they/them if you
need a pronoun at all.

## Fairness

- Compare against level expectations, not against peers. A cohort spanning
  junior through senior will rank by seniority; that's not a finding.
- Partial final month looks like a decline. It isn't.
- Different surfaces aren't comparable: an engineer on one deep upstream project
  will show few repos and high depth; one on 60 small controllers shows the
  inverse. Neither is better.
- Someone on parental leave, a rotation, or a long incident will show a gap.
  Know the context before reading the cadence.
- Bring the same skepticism to high numbers as to low ones. The inflation bugs
  this skill guards against all made people look *better*, which is exactly why
  they survived unchallenged.
