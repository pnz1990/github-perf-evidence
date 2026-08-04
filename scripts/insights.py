#!/usr/bin/env python3
"""Compute cohort signals, then have an LLM turn them into manager actions.

Two layers, deliberately separated:

  1. DETECTORS (this file, deterministic)  -- find patterns in the data. Every
     number an insight cites comes from here, so it is reproducible and
     checkable. Detectors never speculate.

  2. NARRATION (the calling agent, an LLM) -- read the detector output and write
     what a manager should DO about it. Judgement, framing, and the awkward
     conversations belong here.

Splitting them matters: an LLM asked to both find and explain patterns will
invent numbers that sound right. Here it can only interpret figures the
detectors produced.

Usage:
  python3 insights.py --outdir OUT                  # detector output as JSON
  python3 insights.py --outdir OUT --prompt         # ready-to-send LLM prompt
  python3 insights.py --outdir OUT --load notes.md  # attach LLM prose, feed report.py

The agent workflow is: run --prompt, answer it, save the answer, then --load it.
report.py renders whatever is loaded into an Insights section.
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

SKIP = ("filecache.json", "patterns.json", "commitcache.json",
        "reviewcache.json", "ownership.json", "insights.json",
        "COHORT-INDEX.json")

BOT_RE = re.compile(
    r"\[bot\]$|(^|[-_])(bot|robot)([-_]|$)|[-_](bot|robot)\d*$"
    r"|^(dependabot|renovate|copilot|github-actions|web-flow|ack-bot|"
    r"k8s-ci-robot|tide)", re.I)

THEMES = [
    ("release_toil", r"\b(release artifact|cut release|bump|update .* to v|"
                     r"release v\d|version bump|changelog)"),
    ("ci_infra", r"\b(ci|prow|workflow|pipeline|bootstrap|flux|helm chart|"
                 r"terraform|deploy)"),
    ("tests", r"\b(test|e2e|coverage|fixture)"),
    ("docs", r"\b(doc|readme|changelog|comment)"),
    ("bugfix", r"\b(fix|bug|regress|patch|revert|hotfix)"),
    ("feature", r"\b(feat|add support|implement|introduce|new )"),
    ("refactor", r"\b(refactor|cleanup|rename|dead code|simplify|migrate)"),
]


def load(outdir):
    idx_path = os.path.join(outdir, "COHORT-INDEX.json")
    if not os.path.exists(idx_path):
        raise SystemExit("no COHORT-INDEX.json -- run build.py first")
    idx = json.load(open(idx_path))
    people = {}
    for r in idx.get("cohort", []):
        f = os.path.join(outdir, str(r.get("json_file") or ""))
        if os.path.exists(f):
            people[r["id"]] = json.load(open(f))
    own_path = os.path.join(outdir, "ownership.json")
    owners = json.load(open(own_path)) if os.path.exists(own_path) else None
    return idx, people, owners


def median(v):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


# ---------------------------------------------------------------------------
# Detectors. Each returns a list of findings; each finding carries the numbers
# that produced it so an LLM cannot round, drift, or invent.
# ---------------------------------------------------------------------------
def d_release_toil(people):
    """Repeated near-identical commits = mechanical work a person is absorbing.

    Managers rarely see this: each commit looks like legitimate output, and only
    the ratio reveals that a quarter of someone's commits were the same chore.
    """
    out = []
    for pid, p in people.items():
        items = (p.get("authored_commit_subjects") or {}).get("items") or []
        if len(items) < 15:
            continue
        subs = [i["subject"] for i in items]
        norm = [re.sub(r"\d+", "N", s.lower())[:45] for s in subs]
        c = Counter(norm)
        dupes = sum(v for v in c.values() if v > 1)
        pct = round(100.0 * dupes / len(subs))
        if pct >= 20:
            top, n = c.most_common(1)[0]
            out.append({
                "id": pid, "repetitive_pct": pct, "commits": len(subs),
                "most_repeated_pattern": top, "times": n,
                "why_it_matters": (
                    "Mechanical work is invisible in a review because every "
                    "commit looks like output. If this is unautomated release "
                    "chore, it is a tooling gap the team is paying for with a "
                    "person's time."),
            })
    return sorted(out, key=lambda x: -x["repetitive_pct"])


def d_theme_mix(people):
    """What kind of work each person actually does, from commit subjects."""
    out = []
    for pid, p in people.items():
        items = (p.get("authored_commit_subjects") or {}).get("items") or []
        if len(items) < 10:
            continue
        subs = [i["subject"].lower() for i in items]
        b = Counter()
        for s in subs:
            for name, pat in THEMES:
                if re.search(pat, s):
                    b[name] += 1
        tot = len(subs)
        out.append({
            "id": pid, "commits_classified": tot,
            "mix_pct": {k: round(100.0 * v / tot) for k, v in b.most_common(5)},
        })
    return out


def d_review_reciprocity(people):
    """Who unblocks whom. Reveals mentoring pairs, and one-way relationships.

    Bots are excluded: a release bot can dominate someone's thread count and
    make automation look like collaboration.
    """
    helped = defaultdict(Counter)
    for pid, p in people.items():
        for m in (p.get("collaboration") or {}).get("most_helped") or []:
            if not BOT_RE.search(m["author"]):
                helped[pid][m["author"]] += m["threads"]
    cohort_logins = {p["metadata"]["github_login"]: pid
                     for pid, p in people.items()}
    out = []
    for pid, targets in helped.items():
        inside = {a: n for a, n in targets.items() if a in cohort_logins}
        outside = sum(n for a, n in targets.items() if a not in cohort_logins)
        pairs = []
        for a, n in sorted(inside.items(), key=lambda x: -x[1])[:4]:
            other = cohort_logins[a]
            back = helped.get(other, Counter()).get(
                people[pid]["metadata"]["github_login"], 0)
            pairs.append({"peer": other, "threads_given": n,
                          "threads_received_back": back,
                          "one_way": back == 0 and n >= 5})
        out.append({"id": pid, "in_cohort": pairs,
                    "threads_to_people_outside_cohort": outside})
    return out


def d_silos(people):
    """Repos only one person touches. Knowledge concentration a manager owns."""
    reach = defaultdict(set)
    vol = Counter()
    for pid, p in people.items():
        for br in p.get("by_repo") or []:
            if br["prs_total"] >= 3 and not br.get("is_fork"):
                reach[br["repo"]].add(pid)
                vol[br["repo"]] += br["hand_additions"]
    solo = [{"repo": r, "sole_contributor": list(v)[0],
             "cohort_hand_lines": vol[r]}
            for r, v in reach.items() if len(v) == 1 and vol[r] >= 500]
    shared = [{"repo": r, "people": sorted(v)}
              for r, v in reach.items() if len(v) >= 3]
    return {"solo_repos": sorted(solo, key=lambda x: -x["cohort_hand_lines"])[:12],
            "shared_repos": sorted(shared, key=lambda x: -len(x["people"]))[:8]}


def d_review_imbalance(people, idx):
    """Who carries the review load vs who consumes it.

    A person authoring far more than they review is drawing on the team's
    attention; the reverse is subsidising it. Neither is automatically wrong,
    both are worth naming.
    """
    rows = idx.get("cohort", [])
    med = median([r.get("review_ratio") for r in rows])
    out = []
    for r in rows:
        ratio = r.get("review_ratio")
        if ratio is None:
            continue
        out.append({
            "id": r["id"], "reviews_given": r.get("reviews_given"),
            "prs_authored": r.get("prs_authored"),
            "review_to_authorship_ratio": ratio,
            "cohort_median_ratio": med,
            "posture": ("net reviewer -- subsidises others" if ratio >= 2
                        else "net author -- consumes review capacity"
                        if ratio < 0.5 else "balanced"),
        })
    return sorted(out, key=lambda x: -(x["review_to_authorship_ratio"] or 0))


def d_depth_vs_volume(people, idx):
    """Review COUNT and review DEPTH disagree often. Where they disagree most is
    where a count-only reading of the team is most wrong."""
    out = []
    for r in idx.get("cohort", []):
        ipp, rv = r.get("inline_per_pr"), r.get("reviews_given")
        if ipp is None or not rv:
            continue
        out.append({"id": r["id"], "reviews_given": rv,
                    "inline_per_reviewed_pr": ipp,
                    "inline_comments": r.get("inline_comments"),
                    "bare_approvals": r.get("bare_approvals")})
    by_count = [x["id"] for x in sorted(out, key=lambda x: -x["reviews_given"])]
    by_depth = [x["id"] for x in
                sorted(out, key=lambda x: -(x["inline_per_reviewed_pr"] or 0))]
    disagree = [{"id": i, "rank_by_count": by_count.index(i) + 1,
                 "rank_by_depth": by_depth.index(i) + 1,
                 "rank_gap": abs(by_count.index(i) - by_depth.index(i))}
                for i in by_count]
    return {"per_person": out,
            "ranking_disagreement": sorted(disagree,
                                           key=lambda x: -x["rank_gap"])[:6]}


def d_stalled_work(people):
    """Long-open PRs. Often a review bottleneck the author cannot fix alone."""
    out = []
    for pid, p in people.items():
        ow = p.get("open_and_wip") or {}
        old = [i for i in (ow.get("items") or []) if i.get("age_days", 0) >= 45]
        if not old:
            continue
        out.append({
            "id": pid, "open_prs": ow.get("count"),
            "hand_lines_parked": ow.get("hand_additions_parked"),
            "stalled_45d_plus": len(old),
            "oldest": sorted(old, key=lambda x: -x["age_days"])[:3],
        })
    return sorted(out, key=lambda x: -x["stalled_45d_plus"])


def d_trajectory_shifts(people):
    """Big half-over-half swings. A prompt for a conversation, never a verdict --
    leave, oncall, and single large projects all produce these."""
    out = []
    for pid, p in people.items():
        tj = p.get("trajectory")
        if not isinstance(tj, dict) or not tj:
            continue
        moves = []
        for k in ("prs_opened", "prs_merged", "reviews_given",
                  "discussion_threads", "repos_touched"):
            d = tj.get(k)
            if not isinstance(d, dict):
                continue
            e, l = d.get("early") or 0, d.get("late") or 0
            if max(e, l) >= 8 and (e == 0 or l == 0 or
                                   abs(l - e) / max(1, e) >= 0.6):
                moves.append({"metric": k, "early": e, "late": l,
                              "change": d.get("change")})
        if moves:
            out.append({"id": pid, "split_date": tj.get("split_date"),
                        "shifts": moves})
    return out


def d_external_visibility(people):
    """Merged PRs to projects the org does not control."""
    out = []
    for pid, p in people.items():
        ext = p.get("external_upstream_contributions") or {}
        merged = [i for i in (ext.get("items") or [])
                  if i.get("status") == "MERGED"]
        if merged:
            out.append({
                "id": pid, "merged_external_prs": len(merged),
                "top": sorted(merged,
                              key=lambda x: -(x.get("upstream_stars") or 0))[:3],
            })
    return sorted(out, key=lambda x: -x["merged_external_prs"])


def d_defect_catching(outdir, people):
    """Who catches real defects, and whose code is actually getting reviewed.

    Two findings a manager cannot get any other way:
      - a reviewer whose threads are mostly real defects vs mostly nits
      - an author whose complex work is passing review with nothing raised,
        which usually means nobody read it properly
    """
    path = os.path.join(outdir, "comment-analysis.json")
    if not os.path.exists(path):
        return None
    ca = json.load(open(path))
    rev = ca.get("as_reviewer") or {}
    auth = ca.get("as_author") or {}
    out = {"note": ("LLM-classified from real review comment text. Sample is "
                    "capped, so all counts are FLOORS, not totals."),
           "reviewers": [], "authors": [], "unreviewed_risk": []}
    for pid, v in sorted(rev.items(), key=lambda x: -x[1]["defects_caught"]):
        out["reviewers"].append(dict(v, id=pid))
    for pid, v in auth.items():
        out["authors"].append(dict(v, id=pid))
        # Substantive volume, nothing raised -> likely unreviewed, not flawless.
        p = people.get(pid)
        shipped = (p or {}).get("summary", {}).get(
            "hand_additions_canonical_merged") or 0
        if (v["threads_on_their_prs"] >= 2
                and v["defects_found_in_their_code"] == 0 and shipped >= 3000):
            out["unreviewed_risk"].append({
                "id": pid, "shipped_lines": shipped,
                "threads_on_their_prs": v["threads_on_their_prs"],
                "defects_surfaced": 0,
                "why_it_matters": (
                    "Substantial merged volume with no defect raised in review. "
                    "The likely explanation is thin review, not flawless code. "
                    "Check who is reviewing them.")})
    return out


def d_measurement_risk(people, idx):
    """Where the DATA is weakest. An insight built on a truncated count is worse
    than no insight, so this is surfaced alongside the findings."""
    risks = []
    for r in idx.get("cohort", []):
        if r.get("identity") != "documented":
            risks.append({"id": r["id"], "risk": "identity undocumented",
                          "severity": "high"})
        if r.get("truncated"):
            risks.append({"id": r["id"], "risk": "search ceiling hit -- counts "
                                                "are floors", "severity": "high"})
    for pid, p in people.items():
        s = p["summary"]
        raw = s.get("raw_additions_total") or 0
        hand = s.get("hand_additions_total") or 0
        if raw and hand / raw < 0.25:
            risks.append({
                "id": pid, "severity": "medium",
                "risk": "only %d%% of raw additions are hand-authored; volume "
                        "comparisons against people in less codegen-heavy repos "
                        "are not meaningful" % round(100.0 * hand / raw)})
        for c in p["metadata"].get("caveats") or []:
            if c.get("severity") == "high" and c.get("id") not in (
                    "line_count_inflation", "volume_is_not_impact"):
                risks.append({"id": pid, "risk": c["id"], "severity": "high"})
    return risks


def compute(outdir):
    idx, people, owners = load(outdir)
    if not people:
        raise SystemExit("no per-person JSON found -- re-run build.py")
    return {
        "cohort_size": len(people),
        "window": [idx["scan"].get("window_start"), idx["scan"].get("window_end")],
        "detectors": {
            "release_toil": d_release_toil(people),
            "theme_mix": d_theme_mix(people),
            "review_reciprocity": d_review_reciprocity(people),
            "knowledge_silos": d_silos(people),
            "review_load_balance": d_review_imbalance(people, idx),
            "depth_vs_volume": d_depth_vs_volume(people, idx),
            "stalled_work": d_stalled_work(people),
            "trajectory_shifts": d_trajectory_shifts(people),
            "external_visibility": d_external_visibility(people),
            "bus_factor": (owners or {}).get("bus_factor_risk", [])[:10],
            "defect_catching": d_defect_catching(outdir, people),
            "measurement_risk": d_measurement_risk(people, idx),
        },
    }


PROMPT = """You are helping an engineering manager read their team's contribution
data before performance conversations. Below is the output of deterministic
detectors run over a real cohort. Every number in it is measured.

Your job: write the insights the manager would probably MISS -- either because
they have no time to cross-reference eight people, or because the pattern only
shows up in aggregate.

Return STRICT JSON, no prose outside it:

{
  "insights": [
    {
      "title": "short, specific, not a metric name",
      "audience": "team" | "individual",
      "who": ["<person id>", ...],
      "severity": "high" | "medium" | "low",
      "confidence": "high" | "medium" | "low",
      "finding": "what the data shows, citing the actual numbers",
      "why_missed": "why a busy manager would not already know this",
      "action": "one concrete thing to do this week, with an owner",
      "caveat": "what would make this reading wrong"
    }
  ],
  "questions_for_1on1s": [
    {"who": "<person id>", "question": "...", "because": "..."}
  ],
  "do_not_conclude": ["readings of this data that would be unfair or wrong"]
}

Rules, all of them load-bearing:

- Cite real numbers from the detector output. Never invent or round-trip a
  figure that is not there.
- Prefer insights that cross two or more detectors. Single-metric observations
  are already visible in the report tables.
- Volume is NOT impact. Never rank people by lines or PR count, and never imply
  someone is underperforming from low counts alone.
- Low activity is a QUESTION, not a finding. The work is usually somewhere
  GitHub cannot see: internal review, oncall, incident response, design,
  mentoring, interviewing.
- Team-level findings (silos, bus factor, review imbalance, unautomated toil)
  are the manager's problems to fix, not the individual's failings. Frame them
  that way.
- If a person's data carries a measurement risk, either say so in the caveat or
  leave them out.
- The defect_catching detector (if present) is LLM-classified from real review
  comment text and its counts are FLOORS from a capped sample. On the reviewer
  side, defects_caught is a fair positive signal. On the author side,
  defects_found_in_their_code is NOT a quality score: it rises with ambitious
  code and thorough reviewers and falls with trivial code and rubber-stamping.
  A LOW number on high shipped volume means thin review, and that is the
  manager's problem to fix. Never rank anyone on the author-side number.
- 5 to 9 insights. Fewer, sharper beats more.
- Use they/them for everyone. Never infer gender or any personal attribute.
- Write plainly. No corporate filler, no praise sandwiches.

DETECTOR OUTPUT:
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--prompt", action="store_true",
                    help="print an LLM-ready prompt with the detector output")
    ap.add_argument("--load", metavar="FILE",
                    help="attach LLM-written insights (JSON) so report.py "
                         "renders them")
    a = ap.parse_args()

    data = compute(a.outdir)
    path = os.path.join(a.outdir, "insights.json")

    # A bare re-run recomputes the detectors and rewrites insights.json. That
    # MUST NOT destroy a narrative that was already attached: writing the
    # narrative costs an LLM pass over the whole cohort, and losing it is
    # silent -- report.py simply omits the Insights tab and the user is left
    # wondering where their section went. So carry the old narrative forward.
    prior = {}
    if os.path.exists(path):
        try:
            prior = json.load(open(path, encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            prior = {}
    kept = prior.get("narrative")

    if a.load:
        raw = open(a.load, encoding="utf-8").read().strip()
        m = re.search(r"\{.*\}", raw, re.S)      # tolerate ``` fences
        if not m:
            raise SystemExit("no JSON object found in %s" % a.load)
        try:
            narr = json.loads(m.group(0))
        except json.JSONDecodeError as ex:
            raise SystemExit("invalid JSON in %s: %s" % (a.load, ex))
        n = len(narr.get("insights") or [])
        if not n:
            raise SystemExit("%s has no 'insights' array" % a.load)
        data["narrative"] = narr
        json.dump(data, open(path, "w"), indent=1, default=str)
        print("attached %d insights, %d 1:1 questions -> %s"
              % (n, len(narr.get("questions_for_1on1s") or []), path))
        print("now re-run report.py to render them")
        return 0

    if kept and (kept.get("insights") or []):
        data["narrative"] = kept

    json.dump(data, open(path, "w"), indent=1, default=str)

    if a.prompt:
        print(PROMPT)
        print(json.dumps(data["detectors"], indent=1, default=str))
        return 0

    d = data["detectors"]
    print("wrote %s" % path)
    if kept and (kept.get("insights") or []):
        print("kept the %d existing narrative insight(s); detectors were "
              "recomputed, so re-run --prompt/--load if the underlying data "
              "changed" % len(kept["insights"]))
    print("\ndetector summary:")
    for k, v in d.items():
        if v is None:
            print("  %-22s (not run -- see comments.py)" % k)
            continue
        n = len(v) if isinstance(v, list) else sum(
            len(x) for x in v.values() if isinstance(x, list))
        print("  %-22s %d finding(s)" % (k, n))
    print("\nNext: python3 insights.py --outdir %s --prompt" % a.outdir)
    print("      answer it with an LLM, save the JSON, then --load it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
