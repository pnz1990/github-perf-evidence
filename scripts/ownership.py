#!/usr/bin/env python3
"""Map de-facto code ownership and bus-factor risk per subsystem.

Two distinct uses from the same data:

  For the PERSON -- de-facto ownership of a subsystem is seniority evidence even
  with no OWNERS entry. "35 of 61 commits to the graph engine" is concrete.

  For the TEAM -- which subsystems have exactly one person who understands them.
  This is arguably the more valuable output, and it reframes the tool from
  evaluating individuals to understanding risk.

Directories are derived from the hand-authored files the cohort actually touched
(so it reflects real work, not repo layout), then each is attributed by commit
history. Writes ownership.json for build.py and report.py.

  python3 ownership.py --outdir OUT --depth 2
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import build_bucket, load_patterns  # noqa: E402

SKIP_FILES = ("filecache.json", "patterns.json", "commitcache.json",
              "reviewcache.json", "ownercache.json")

BOT_RE = re.compile(
    r"\[bot\]$|(^|[-_])(bot|robot)([-_]|$)|[-_](bot|robot)\d*$"
    r"|^(dependabot|renovate|copilot|github-actions|web-flow)", re.I)


def gh_lines(args):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    return r.stdout.strip().splitlines() if r.returncode == 0 else []


def dir_of(path, depth):
    parts = path.split("/")
    if len(parts) <= 1:
        return "(repo root)"
    return "/".join(parts[:min(depth, len(parts) - 1)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--depth", type=int, default=2,
                    help="directory depth to treat as a subsystem (default 2)")
    ap.add_argument("--min-lines", type=int, default=200,
                    help="only analyze subsystems with at least this many "
                         "hand-authored lines from the cohort (default 200)")
    ap.add_argument("--max-dirs", type=int, default=40,
                    help="cap subsystems analyzed; largest kept (default 40)")
    ap.add_argument("--since", help="YYYY-MM-DD for commit history (defaults to "
                                    "the roster window start)")
    a = ap.parse_args()

    pat, _ = load_patterns(a.outdir)
    bucket = build_bucket(pat)
    cache = json.load(open(os.path.join(a.outdir, "filecache.json")))

    # Which (repo, dir) did the cohort actually write hand-authored code in?
    dir_lines = Counter()
    cohort = set()
    window_start = a.since
    for f in sorted(glob.glob(os.path.join(a.outdir, "*.json"))):
        if os.path.basename(f) in SKIP_FILES:
            continue
        b = json.load(open(f))
        if "prs" not in b:
            continue
        cohort.add(b["person"]["github_login"])
        if not window_start:
            window_start = b.get("window", {}).get("start")
        for p in b["prs"]:
            for add, fn in (cache.get("%s#%d" % (p["repo"], p["number"])) or []):
                if bucket(fn) == "hand":
                    dir_lines[(p["repo"], dir_of(fn, a.depth))] += add

    targets = [(k, n) for k, n in dir_lines.most_common() if n >= a.min_lines]
    if len(targets) > a.max_dirs:
        print("NOTE: %d subsystems qualify; analyzing the %d largest. Raise "
              "--max-dirs for full coverage."
              % (len(targets), a.max_dirs), file=sys.stderr)
        targets = targets[:a.max_dirs]

    print("analyzing %d subsystems (commit history since %s)"
          % (len(targets), window_start or "repo start"), file=sys.stderr)

    subsystems = []
    for i, ((repo, d), lines) in enumerate(targets, 1):
        args = ["api", "--paginate",
                "repos/%s/commits?per_page=100&path=%s" % (repo, d)]
        if window_start:
            args[-1] += "&since=%sT00:00:00Z" % window_start
        authors = Counter()
        for line in gh_lines(args + ["--jq", '.[]|.author.login//"unknown"']):
            if line and not BOT_RE.search(line):
                authors[line] += 1
        if not authors:
            continue
        total = sum(authors.values())
        top, top_n = authors.most_common(1)[0]
        # Bus factor: how many people cover >=50% of commits.
        run, bus = 0, 0
        for _, n in authors.most_common():
            run += n
            bus += 1
            if run >= 0.5 * total:
                break
        subsystems.append({
            "repo": repo,
            "directory": d,
            "cohort_hand_lines": lines,
            "commits_in_window": total,
            "distinct_authors": len(authors),
            "top_author": top,
            "top_author_commits": top_n,
            "top_author_share_pct": round(100.0 * top_n / total),
            "bus_factor": bus,
            "authors": [{"login": u, "commits": n} for u, n in
                        authors.most_common(8)],
            "top_author_in_cohort": top in cohort,
        })
        if i % 10 == 0:
            print("  %d/%d" % (i, len(targets)), file=sys.stderr)

    # Per-person: subsystems where they are the dominant author.
    per_person = defaultdict(list)
    for s in subsystems:
        if s["top_author_share_pct"] >= 40 and s["commits_in_window"] >= 5:
            per_person[s["top_author"]].append({
                "repo": s["repo"], "directory": s["directory"],
                "share_pct": s["top_author_share_pct"],
                "commits": s["top_author_commits"],
                "of_total": s["commits_in_window"],
                "bus_factor": s["bus_factor"],
            })

    risk = sorted([s for s in subsystems
                   if s["bus_factor"] == 1 and s["commits_in_window"] >= 5],
                  key=lambda x: -x["commits_in_window"])

    out = {
        "generated_from": "commit history per directory",
        "window_start": window_start,
        "depth": a.depth,
        "subsystems_analyzed": len(subsystems),
        "caveat": (
            "Commit counts attribute by volume, not by difficulty or design "
            "influence. A reviewer who shaped a subsystem without committing to "
            "it will not appear. Directory boundaries are a proxy for "
            "subsystem boundaries and are sometimes wrong. Treat as a "
            "conversation starter, not an ownership ruling."),
        "bus_factor_risk": risk,
        "by_person": dict(per_person),
        "subsystems": subsystems,
    }
    path = os.path.join(a.outdir, "ownership.json")
    json.dump(out, open(path, "w"), indent=1)

    print("\nwrote %s" % path)
    if risk:
        print("\nBUS-FACTOR RISK -- one person is >=50% of commits:")
        for s in risk[:12]:
            print("  %-46s %-18s %d/%d commits"
                  % ("%s/%s" % (s["repo"].split("/")[-1], s["directory"]),
                     s["top_author"], s["top_author_commits"],
                     s["commits_in_window"]))
        print("\nThis is a team-risk finding, not a performance finding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
