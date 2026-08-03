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
"""
import argparse
import glob
import json
import os
import subprocess
import sys

# Working files, not person bundles.
SKIP_FILES = ("filecache.json", "patterns.json", "commitcache.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--refetch", action="store_true",
                    help="discard existing cache entries and refetch all")
    ap.add_argument("--commits", action="store_true",
                    help="also cache commit subjects per PR (1 extra call/PR). "
                         "Lets the report say what was built, not just how much.")
    a = ap.parse_args()

    path = os.path.join(a.outdir, "filecache.json")
    cache = {} if a.refetch else (
        json.load(open(path)) if os.path.exists(path) else {})
    cpath = os.path.join(a.outdir, "commitcache.json")
    ccache = {} if a.refetch else (
        json.load(open(cpath)) if os.path.exists(cpath) else {})

    keys = []
    for f in sorted(glob.glob(os.path.join(a.outdir, "*.json"))):
        if os.path.basename(f) in SKIP_FILES:
            continue
        b = json.load(open(f))
        if "prs" not in b:
            continue
        for p in b["prs"]:
            keys.append((p["repo"], p["number"]))

    keys = list(dict.fromkeys(keys))       # dedupe: people co-author PRs
    todo = [k for k in keys if "%s#%d" % k not in cache
            or (a.commits and "%s#%d" % k not in ccache)]
    print("unique PRs=%d cached=%d todo=%d"
          % (len(keys), len(keys) - len(todo), len(todo)))

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

        if i % 50 == 0:
            json.dump(cache, open(path, "w"))
            if a.commits:
                json.dump(ccache, open(cpath, "w"))
            print("  %d/%d" % (i, len(todo)))

    json.dump(cache, open(path, "w"))
    if a.commits:
        json.dump(ccache, open(cpath, "w"))
        print("commit subjects cached: %d PRs" % len(ccache))
    failed = [k for k, v in cache.items() if v is None]
    print("done. entries=%d failed=%d" % (len(cache), len(failed)))
    for f in failed[:20]:
        print("  UNAVAILABLE:", f)


if __name__ == "__main__":
    sys.exit(main())
