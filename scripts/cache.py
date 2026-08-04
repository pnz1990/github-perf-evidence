#!/usr/bin/env python3
"""Cache the per-PR file list for every PR in every bundle.

Exists so classification can be iterated without refetching. Classification
rules are wrong on the first pass essentially always; without this cache each
correction costs another full crawl and you stop correcting.

Writes filecache.json: {"repo#num": [[additions, filename], ...]}
A null value means the API call failed (deleted fork, lost access) -- that is
distinguishable from a genuinely empty PR and is surfaced downstream.

With --commits, also writes commitcache.json: {"repo#num": [[author, subject]]}.
Commit subjects are what let a reader see WHAT someone built rather than only
how much; line counts alone cannot distinguish a refactor from a feature. Costs
one extra API call per PR, hence opt-in.

With --review-depth, writes reviewcache.json:
  {"repo#num": {"reviews": [[login, state, body_chars]],
                "inline": [[login, body_chars]]}}

This exists because review COUNT is a bad metric on its own. Measured on real
PRs, one reviewer had 52 review events and 11 inline comments while another had
9 events and 50 inline comments. A count-only report ranks the first person 5.8x
higher; they are doing different work, and only the second was engaging deeply.
Costs 2 extra calls per reviewed PR.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

# Flush cadence. Smaller means less lost work if the run is interrupted; the
# write is cheap relative to an API call.
FLUSH_EVERY = 25

# Working files, not person bundles.
SKIP_FILES = ("filecache.json", "patterns.json", "commitcache.json",
              "reviewcache.json", "ownercache.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--refetch", action="store_true",
                    help="discard existing cache entries and refetch all")
    ap.add_argument("--commits", action="store_true",
                    help="also cache commit subjects per PR (1 extra call/PR). "
                         "Lets the report say what was built, not just how much.")
    ap.add_argument("--review-depth", action="store_true",
                    help="also cache review verdicts + inline comment counts "
                         "(2 extra calls/PR). Distinguishes a substantive review "
                         "from a rubber stamp.")
    ap.add_argument("--all", action="store_true",
                    help="shorthand for --commits --review-depth")
    a = ap.parse_args()
    if a.all:
        a.commits = a.review_depth = True

    path = os.path.join(a.outdir, "filecache.json")
    cache = {} if a.refetch else (
        json.load(open(path)) if os.path.exists(path) else {})
    cpath = os.path.join(a.outdir, "commitcache.json")
    ccache = {} if a.refetch else (
        json.load(open(cpath)) if os.path.exists(cpath) else {})
    rpath = os.path.join(a.outdir, "reviewcache.json")
    rcache = {} if a.refetch else (
        json.load(open(rpath)) if os.path.exists(rpath) else {})

    keys = []
    for f in sorted(glob.glob(os.path.join(a.outdir, "*.json"))):
        if os.path.basename(f) in SKIP_FILES:
            continue
        b = json.load(open(f))
        if "prs" not in b:
            continue
        for p in b["prs"]:
            keys.append((p["repo"], p["number"]))
        if a.review_depth:
            # Reviewed PRs are usually NOT in anyone's authored list, so they
            # must be added explicitly or review depth would only cover
            # self-reviewed work.
            for r in b.get("reviews", []):
                keys.append((r["repo"], r["number"]))

    keys = list(dict.fromkeys(keys))       # dedupe: people co-author PRs
    todo = [k for k in keys
            if "%s#%d" % k not in cache
            or (a.commits and "%s#%d" % k not in ccache)
            or (a.review_depth and "%s#%d" % k not in rcache)]
    per_pr = 1 + (1 if a.commits else 0) + (2 if a.review_depth else 0)
    print("unique PRs=%d  cached=%d  todo=%d  (~%d API calls at %d/PR)"
          % (len(keys), len(keys) - len(todo), len(todo),
             len(todo) * per_pr, per_pr))
    if len(todo) > 300:
        print("This is a long run. It is fully RESUMABLE: progress is written")
        print("every %d PRs, so you can stop any time and re-run the same"
              % FLUSH_EVERY)
        print("command to continue. Nothing already cached is re-fetched.")
    started = time.time()

    for i, (repo, num) in enumerate(todo, 1):
        key = "%s#%d" % (repo, num)
        if key not in cache:
            r = subprocess.run(
                ["gh", "api", "--paginate", "repos/%s/pulls/%d/files" % (repo, num),
                 "--jq", '.[]|[.additions,.filename]|@tsv'],
                capture_output=True, text=True)
            files = []
            for line in r.stdout.strip().splitlines():
                if not line.strip():
                    continue
                try:
                    add, fn = line.split("\t", 1)
                    files.append([int(add), fn])
                except ValueError:
                    pass
            cache[key] = files if (files or r.returncode == 0) else None

        if a.review_depth and key not in rcache:
            rv = subprocess.run(
                ["gh", "api", "--paginate",
                 "repos/%s/pulls/%d/reviews" % (repo, num), "--jq",
                 '.[]|[(.user.login//"unknown"),.state,(.body|length)]|@tsv'],
                capture_output=True, text=True)
            reviews = []
            for line in rv.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    try:
                        reviews.append([parts[0], parts[1], int(parts[2])])
                    except ValueError:
                        pass
            ic = subprocess.run(
                ["gh", "api", "--paginate",
                 "repos/%s/pulls/%d/comments" % (repo, num), "--jq",
                 '.[]|[(.user.login//"unknown"),(.body|length)]|@tsv'],
                capture_output=True, text=True)
            inline = []
            for line in ic.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) == 2:
                    try:
                        inline.append([parts[0], int(parts[1])])
                    except ValueError:
                        pass
            ok = rv.returncode == 0 and ic.returncode == 0
            rcache[key] = ({"reviews": reviews, "inline": inline}
                           if (reviews or inline or ok) else None)

        if a.commits and key not in ccache:
            r2 = subprocess.run(
                ["gh", "api", "--paginate",
                 "repos/%s/pulls/%d/commits" % (repo, num), "--jq",
                 '.[]|[(.author.login//"unknown"),'
                 '(.commit.message|split("\n")[0])]|@tsv'],
                capture_output=True, text=True)
            subs = []
            for line in r2.stdout.strip().splitlines():
                if "\t" in line:
                    au, msg = line.split("\t", 1)
                    subs.append([au, msg[:160]])
            ccache[key] = subs if (subs or r2.returncode == 0) else None

        if i % FLUSH_EVERY == 0:
            json.dump(cache, open(path, "w"))
            if a.commits:
                json.dump(ccache, open(cpath, "w"))
            if a.review_depth:
                json.dump(rcache, open(rpath, "w"))
            done = time.time() - started
            rate = i / max(0.001, done)
            left = (len(todo) - i) / max(0.001, rate)
            print("  %d/%d  (%.0f%%)  ~%d min remaining"
                  % (i, len(todo), 100.0 * i / len(todo), left / 60))

    json.dump(cache, open(path, "w"))
    if a.commits:
        json.dump(ccache, open(cpath, "w"))
        print("commit subjects cached: %d PRs" % len(ccache))
    if a.review_depth:
        json.dump(rcache, open(rpath, "w"))
        print("review depth cached: %d PRs" % len(rcache))
    failed = [k for k, v in cache.items() if v is None]
    print("done. entries=%d failed=%d" % (len(cache), len(failed)))
    for f in failed[:20]:
        print("  UNAVAILABLE:", f)


if __name__ == "__main__":
    sys.exit(main())
