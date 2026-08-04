#!/usr/bin/env python3
"""Fetch GitHub activity for each person in a roster. One JSON bundle per person.

Four searches per person: PRs authored, PRs reviewed, issues opened, and
discussion threads on other people's work. Then a per-PR call for diff stats.
Records the search API's reported total_count separately from what pagination
actually captured, so truncation is visible instead of silently understating
someone.

Visibility: defaults to PUBLIC repos only, which is the safe default for a tool
whose output gets shared. --visibility all includes private repos your token can
see (needs the `repo` scope). Private titles and repo names then land in the
output files -- treat them as confidential and never commit them.

Windows: --compare-window splits the range in half (or takes an explicit split
date) and records both halves, so build.py can report trajectory. Growth matters
more than a single snapshot when the question is "is this person expanding
scope?".
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

# Search API allows 30 req/min. Pace under that so retries stay rare.
SEARCH_PACE = float(os.environ.get("PERF_SEARCH_PACE", "2.2"))

PR_JQ = ('.items[]|{number,title,state,created_at,'
         'merged_at:.pull_request.merged_at,url:.html_url,'
         'repo:(.repository_url|split("/")|.[-2:]|join("/")),'
         'labels:[.labels[].name],comments}')
REV_JQ = ('.items[]|{number,title,url:.html_url,'
          'repo:(.repository_url|split("/")|.[-2:]|join("/")),'
          'author:.user.login,state,merged_at:.pull_request.merged_at}')
ISS_JQ = ('.items[]|{number,title,url:.html_url,'
          'repo:(.repository_url|split("/")|.[-2:]|join("/")),'
          'state,created_at,comments}')
# Discussion participation. Distinct from `reviewed-by:`: catches design debate,
# triage, and helping on other people's issues -- often someone's largest
# collaboration signal and invisible in PR/review counts alone.
CMT_JQ = ('.items[]|{number,title,url:.html_url,'
          'repo:(.repository_url|split("/")|.[-2:]|join("/")),'
          'author:.user.login,is_pr:(.pull_request!=null),state,updated_at}')

# GitHub search caps at 1000 results (10 pages x 100) regardless of total_count.
MAX_PAGES = 10


def gh(args, retries=5):
    """Run a gh call, retrying through search rate-limit rejections.

    The search API allows only 30 requests/minute and a full scan makes far more
    than that, so transient 403/429 rejections are EXPECTED, not exceptional.
    Returning "" on failure (the previous behaviour) made those rejections
    indistinguishable from a genuinely empty result: total_count() saw a
    non-numeric string and recorded 0, so a rate-limited sub-window silently
    reported "this person did nothing" instead of failing loudly.
    """
    delay = 8.0
    for attempt in range(retries):
        r = subprocess.run(["gh"] + args, capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        err = (r.stderr or "").lower()
        transient = ("rate limit" in err or "429" in err or "403" in err
                     or "secondary" in err or "abuse" in err
                     or "timeout" in err or "502" in err or "503" in err)
        if not transient or attempt == retries - 1:
            raise RuntimeError(
                "gh call failed (attempt %d/%d): %s\n  args: %s"
                % (attempt + 1, retries, (r.stderr or "").strip()[:400],
                   " ".join(args[:6])))
        sys.stderr.write("    rate-limited, retrying in %.0fs\n" % delay)
        time.sleep(delay)
        delay *= 2
    return ""


def jlines(s):
    out = []
    for line in s.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def total_count(q):
    s = gh(["api", "-X", "GET", "search/issues", "-f", "q=" + q,
            "-f", "per_page=1", "--jq", ".total_count"]).strip()
    if not s.isdigit():
        # Do NOT fall back to 0/None here: a corrupted count that looks like a
        # real number is worse than a crash, because it lands in someone's
        # review as "no activity".
        raise RuntimeError("total_count returned non-numeric %r for q=%s"
                           % (s[:120], q))
    time.sleep(SEARCH_PACE)
    return int(s)


def search(q, jq, pages=MAX_PAGES):
    out = []
    for p in range(1, pages + 1):
        got = jlines(gh(["api", "-X", "GET", "search/issues", "-f", "q=" + q,
                         "-f", "per_page=100", "-f", "page=%d" % p, "--jq", jq]))
        out += got
        time.sleep(SEARCH_PACE)
        if len(got) < 100:
            break
    return out


def midpoint(start, end):
    d1 = datetime.date(*map(int, start.split("-")))
    d2 = datetime.date(*map(int, end.split("-")))
    return str(d1 + (d2 - d1) / 2)


def day_before(d):
    """GitHub's `created:A..B` is INCLUSIVE at both ends, so naively splitting a
    window at date S puts S in both halves and the halves sum to more than the
    whole. The early half must end the day before the split."""
    return str(datetime.date(*map(int, d.split("-")))
               - datetime.timedelta(days=1))


def collect_window(login, win, vis):
    """All four searches for one login over one window."""
    scope = "" if vis == "all" else " is:public"
    q_pr = "author:%s type:pr created:%s..%s%s" % (login, win[0], win[1], scope)
    q_rev = ("reviewed-by:%s -author:%s type:pr updated:%s..%s%s"
             % (login, login, win[0], win[1], scope))
    q_iss = "author:%s type:issue created:%s..%s%s" % (login, win[0], win[1], scope)
    q_cmt = ("commenter:%s -author:%s updated:%s..%s%s"
             % (login, login, win[0], win[1], scope))

    prs = search(q_pr, PR_JQ)
    revs = search(q_rev, REV_JQ)
    iss = search(q_iss, ISS_JQ)
    cmts = search(q_cmt, CMT_JQ)

    seen, ded = set(), []
    for r in revs:
        k = (r["repo"], r["number"])
        if k not in seen:
            seen.add(k)
            ded.append(r)

    return {
        "prs": prs, "reviews": ded, "issues": iss, "comments_on_others": cmts,
        "reviews_true_total": total_count(q_rev) or len(ded),
        "prs_true_total": total_count(q_pr) or len(prs),
        "comments_true_total": total_count(q_cmt) or len(cmts),
        "queries": {"prs": q_pr, "reviews": q_rev, "issues": q_iss,
                    "comments": q_cmt},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--only", nargs="*", help="limit to these github logins")
    ap.add_argument("--visibility", choices=["public", "all"], default="public",
                    help="'public' (default, safe to share) or 'all' to include "
                         "private repos your token can read. Private data in the "
                         "output is CONFIDENTIAL -- do not commit it.")
    ap.add_argument("--compare-window", nargs="?", const="auto", default=None,
                    metavar="SPLIT_DATE",
                    help="also scan two sub-windows to show trajectory. Pass a "
                         "YYYY-MM-DD split, or omit the value to split in half.")
    ap.add_argument("--dry-run", action="store_true",
                    help="estimate API calls and exit without fetching")
    a = ap.parse_args()

    roster = json.load(open(a.roster))
    win = (roster["window"]["start"], roster["window"]["end"])
    os.makedirs(a.outdir, exist_ok=True)

    people = [p for p in roster["people"]
              if not a.only or p["github_login"] in a.only]

    if a.dry_run:
        n = len(people)
        mult = 3 if a.compare_window else 1
        searches = n * 8 * mult      # 4 searches + 4 total_count probes
        # Search pacing dominates: SEARCH_PACE between calls, plus one
        # diff-stat call per authored PR. Measured on a real 8-person,
        # 7-month scan: ~1,700 PRs and a bit over an hour for this step
        # alone, so the honest estimate has to include the per-PR cost.
        search_min = searches * SEARCH_PACE / 60.0
        print("DRY RUN -- estimate only")
        print("  people: %d   windows each: %d   visibility: %s"
              % (n, mult, a.visibility))
        print("  search calls: ~%d  ->  ~%d min just for searches"
              % (searches, max(1, round(search_min))))
        print()
        print("  PLUS one diff-stat call per authored PR, which usually")
        print("  dominates and cannot be known until the searches run.")
        print("  Rough guide from a real run: ~200 PRs/person over 7 months")
        print("  => ~%d PR calls => roughly %d-%d min more."
              % (n * 200 * mult, n * 200 * mult // 60, n * 200 * mult // 25))
        print()
        print("  Then cache.py costs 1-4 calls per UNIQUE PR (see its own")
        print("  output for a live ETA). For a large cohort budget HOURS,")
        print("  not minutes, and run it in the background.")
        print()
        print("  Every step is resumable -- stop and re-run any time.")
        print("Re-run without --dry-run to fetch.")
        return 0

    if a.visibility == "all":
        print("WARNING: including PRIVATE repos. Output files will contain "
              "private repo names and PR titles.")
        print("         Treat %s as confidential. Do not commit it.\n" % a.outdir)

    for person in people:
        login = person["github_login"]
        pid = person.get("id") or login

        main_w = collect_window(login, win, a.visibility)
        prs = main_w["prs"]
        ded = main_w["reviews"]
        iss = main_w["issues"]
        cmts = main_w["comments_on_others"]
        rev_true = main_w["reviews_true_total"]
        pr_true = main_w["prs_true_total"]
        cmt_true = main_w["comments_true_total"]

        trend = None
        if a.compare_window:
            split = (midpoint(win[0], win[1]) if a.compare_window == "auto"
                     else a.compare_window)
            early_end = day_before(split)
            early = collect_window(login, (win[0], early_end), a.visibility)
            late = collect_window(login, (split, win[1]), a.visibility)
            trend = {
                "split_date": split,
                "early": {"window": [win[0], early_end],
                          "prs": len(early["prs"]),
                          "prs_merged": sum(1 for p in early["prs"]
                                            if p.get("merged_at")),
                          "reviews": early["reviews_true_total"],
                          "comment_threads": early["comments_true_total"],
                          "repos": len({p["repo"] for p in early["prs"]})},
                "late": {"window": [split, win[1]],
                         "prs": len(late["prs"]),
                         "prs_merged": sum(1 for p in late["prs"]
                                           if p.get("merged_at")),
                         "reviews": late["reviews_true_total"],
                         "comment_threads": late["comments_true_total"],
                         "repos": len({p["repo"] for p in late["prs"]})},
            }

        for i, p in enumerate(prs, 1):
            s = gh(["api", "repos/%s/pulls/%d" % (p["repo"], p["number"]),
                    "--jq", '[.additions,.deletions,.changed_files,'
                            '.review_comments]|@tsv']).strip()
            if s:
                add, dele, cf, rc = s.split("\t")
                p.update(additions=int(add), deletions=int(dele),
                         changed_files=int(cf), review_comments=int(rc),
                         stats_ok=True)
            else:
                # transient failure or inaccessible repo -- flag, do not zero silently
                p.update(additions=0, deletions=0, changed_files=0,
                         review_comments=0, stats_ok=False)
            if i % 50 == 0:
                print("    %s: %d/%d diff stats" % (login, i, len(prs)))

        missing = [p for p in prs if not p.get("stats_ok")]
        bundle = {
            "person": person,
            "window": {"start": win[0], "end": win[1]},
            "visibility": a.visibility,
            "prs": prs,
            "reviews": ded,
            "issues": iss,
            "comments_on_others": cmts,
            "reviews_true_total": rev_true if rev_true is not None else len(ded),
            "prs_true_total": pr_true if pr_true is not None else len(prs),
            "comments_true_total": (cmt_true if cmt_true is not None
                                    else len(cmts)),
            "trend": trend,
        }
        json.dump(bundle, open("%s/%s.json" % (a.outdir, pid), "w"))

        flag = ""
        if rev_true and rev_true > len(ded):
            flag += "  REVIEWS TRUNCATED (%d captured of %d)" % (len(ded), rev_true)
        if pr_true and pr_true > len(prs):
            flag += "  PRS TRUNCATED (%d of %d)" % (len(prs), pr_true)
        if missing:
            flag += "  %d PRs missing diff stats -- rerun" % len(missing)
        tr = ""
        if trend:
            tr = "  trend prs %d->%d reviews %d->%d" % (
                trend["early"]["prs"], trend["late"]["prs"],
                trend["early"]["reviews"], trend["late"]["reviews"])
        print("%-20s prs=%-4d reviews=%-4d issues=%-3d comments=%-4d%s%s"
              % (login, len(prs), len(ded), len(iss), len(cmts), tr, flag))


if __name__ == "__main__":
    sys.exit(main())
