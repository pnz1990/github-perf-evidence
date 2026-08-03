#!/usr/bin/env python3
"""Flag implausible classifications so they get fixed before anyone quotes them.

Every heuristic here corresponds to a real bug found in practice:

  huge-single-file     a 22,567-line "hand-authored" vendored SDK schema where
                       the real change was 10 lines of logic
  low-file-count       636,236 lines across 18 files: downloaded API specs
  reformat-churn       additions ~= deletions: a formatter run, not new work
  repeated-basename    the same subtree committed under two path prefixes,
                       counted twice
  fork-target          a personal-fork PR duplicating an upstream PR
  suspicious-extension .json/.yaml/.lock dominating "authored" volume

Prints the specific files driving each flag so they can be checked directly:
  gh api repos/<owner>/<repo>/contents/<path> --jq .content | base64 -d | head -5
"""
import argparse
import glob
import json
import os
import subprocess
import sys

# Working files, not person bundles.
SKIP_FILES = ("filecache.json", "patterns.json", "commitcache.json",
              "reviewcache.json", "ownercache.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import build_bucket, load_patterns  # noqa: E402

DATAISH = (".json", ".yaml", ".yml", ".lock", ".sum", ".csv", ".svg", ".min.js")


def fork_status(repo, memo):
    """True if repo is a fork. Cached; None when the API call fails."""
    if repo in memo:
        return memo[repo]
    r = subprocess.run(["gh", "api", "repos/" + repo, "--jq",
                        '[(.fork|tostring),(.parent.full_name//"-")]|join("|")'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        memo[repo] = (None, None)
    else:
        parts = r.stdout.strip().split("|")
        memo[repo] = (parts[0] == "true", parts[1] if len(parts) > 1 else "-")
    return memo[repo]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--check-forks", action="store_true", default=True)
    a = ap.parse_args()

    pat, _ = load_patterns(a.outdir)
    bucket = build_bucket(pat)
    cache = json.load(open(os.path.join(a.outdir, "filecache.json")))
    memo = {}
    findings = []
    out = []

    def emit(s=""):
        out.append(s)
        print(s)

    for f in sorted(glob.glob(os.path.join(a.outdir, "*.json"))):
        if os.path.basename(f) in SKIP_FILES:
            continue
        b = json.load(open(f))
        if "prs" not in b:
            continue
        login = b["person"]["github_login"]
        hand_tot = sum(p["hand_additions"] for p in b["prs"])

        for p in b["prs"]:
            files = cache.get("%s#%d" % (p["repo"], p["number"])) or []
            hand = [(add, fn) for add, fn in files if bucket(fn) == "hand"]
            h = sum(add for add, _ in hand)
            if h < 500:
                continue
            flags = []

            biggest = max(hand, default=(0, ""))
            if biggest[0] > 0.5 * h and biggest[0] > 1000:
                flags.append(("huge-single-file",
                              "%d of %d lines in ONE file: %s"
                              % (biggest[0], h, biggest[1])))

            if len(hand) <= 20 and h > 5000:
                flags.append(("low-file-count",
                              "%d lines across only %d hand files"
                              % (h, len(hand))))

            dele = p.get("deletions", 0)
            if dele and 0.75 < p.get("additions", 0) / max(1, dele) < 1.35 \
                    and p.get("additions", 0) > 3000:
                flags.append(("reformat-churn",
                              "+%d/-%d: likely a formatter/rename, not new work"
                              % (p["additions"], dele)))

            seen, dup, egs = set(), 0, []
            for add, fn in hand:
                if add <= 20:
                    continue
                k = (fn.rsplit("/", 1)[-1], add)
                if k in seen:
                    dup += add
                    if len(egs) < 3:
                        egs.append(fn)
                else:
                    seen.add(k)
            if dup > 300:
                flags.append(("repeated-basename",
                              "%d lines duplicated within the PR (copied "
                              "subtree?) e.g. %s" % (dup, ", ".join(egs))))

            dataish = sum(add for add, fn in hand if fn.endswith(DATAISH))
            if dataish > 0.6 * h and h > 2000:
                flags.append(("suspicious-extension",
                              "%d of %d lines are data/config files, not code"
                              % (dataish, h)))

            if a.check_forks:
                isfork, parent = fork_status(p["repo"], memo)
                if isfork:
                    flags.append(("fork-target",
                                  "repo is a FORK of %s -- may duplicate an "
                                  "upstream PR" % parent))

            if flags:
                findings.append({
                    "login": login, "repo": p["repo"], "number": p["number"],
                    "title": p["title"], "state": p["state"],
                    "merged": bool(p.get("merged_at")),
                    "hand": h, "share_of_person": 100.0 * h / max(1, hand_tot),
                    "flags": flags,
                    "top_files": sorted(hand, reverse=True)[:6],
                })

    findings.sort(key=lambda x: -x["hand"])

    emit("=" * 78)
    emit("CLASSIFICATION AUDIT -- %d PRs flagged" % len(findings))
    emit("=" * 78)
    emit()
    emit("Work these top-down. For each, check whether the named files are")
    emit("really hand-authored:")
    emit("  gh api repos/<owner>/<repo>/contents/<path> --jq .content "
         "| base64 -d | head -5")
    emit("Look for 'Code generated', 'DO NOT EDIT', a generate.sh, or a")
    emit("vendoring README. Then add the pattern to patterns.json and re-run")
    emit("classify.py + audit.py FOR EVERYONE.")
    emit()

    for fd in findings[:a.top]:
        emit("-" * 78)
        emit("%s  %s#%d  [%s]" % (fd["login"], fd["repo"], fd["number"],
                                  "merged" if fd["merged"] else fd["state"]))
        emit("  %s" % fd["title"][:70])
        emit("  hand=%d  (%.0f%% of this person's hand total)"
             % (fd["hand"], fd["share_of_person"]))
        for name, detail in fd["flags"]:
            emit("  FLAG %-22s %s" % (name, detail))
        emit("  largest hand-classified files:")
        for add, fn in fd["top_files"]:
            emit("    %-8d %s" % (add, fn))

    if len(findings) > a.top:
        emit()
        emit("... %d more flagged PRs (raise --top to see them)"
             % (len(findings) - a.top))

    emit()
    emit("=" * 78)
    if not findings:
        emit("No flags. Classification looks plausible -- proceed to build.py.")
    else:
        emit("%d flagged. Do NOT build until you have checked the top ones."
             % len(findings))
        counts = {}
        for fd in findings:
            for name, _ in fd["flags"]:
                counts[name] = counts.get(name, 0) + 1
        emit("Flag counts: " + ", ".join(
            "%s=%d" % kv for kv in sorted(counts.items(), key=lambda x: -x[1])))

    rp = os.path.join(a.outdir, "audit-report.txt")
    open(rp, "w").write("\n".join(out) + "\n")
    print("\nreport: %s" % rp)


if __name__ == "__main__":
    sys.exit(main())
