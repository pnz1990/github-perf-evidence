#!/usr/bin/env python3
"""Fetch GitHub activity for each person in a roster. One JSON bundle per person.

Three searches per person: PRs authored, PRs reviewed, issues opened. Then a
per-PR call for diff stats. Records the search API's reported total_count
separately from what pagination actually captured, so truncation is visible
instead of silently understating someone.
"""
import argparse
import json
import os
import subprocess
import sys

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


def gh(args):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    return r.stdout


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
    return int(s) if s.isdigit() else None


def search(q, jq, pages=MAX_PAGES):
    out = []
    for p in range(1, pages + 1):
        got = jlines(gh(["api", "-X", "GET", "search/issues", "-f", "q=" + q,
                         "-f", "per_page=100", "-f", "page=%d" % p, "--jq", jq]))
        out += got
        if len(got) < 100:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--only", nargs="*", help="limit to these github logins")
    a = ap.parse_args()

    roster = json.load(open(a.roster))
    win = (roster["window"]["start"], roster["window"]["end"])
    os.makedirs(a.outdir, exist_ok=True)

    for person in roster["people"]:
        login = person["github_login"]
        if a.only and login not in a.only:
            continue
        pid = person.get("id") or login

        q_pr = "author:%s type:pr created:%s..%s" % (login, win[0], win[1])
        q_rev = ("reviewed-by:%s -author:%s type:pr updated:%s..%s"
                 % (login, login, win[0], win[1]))
        q_iss = "author:%s type:issue created:%s..%s" % (login, win[0], win[1])
        q_cmt = ("commenter:%s -author:%s updated:%s..%s"
                 % (login, login, win[0], win[1]))

        prs = search(q_pr, PR_JQ)
        revs = search(q_rev, REV_JQ)
        iss = search(q_iss, ISS_JQ)
        cmts = search(q_cmt, CMT_JQ)

        # dedupe reviews: one PR reviewed N times is one PR reviewed
        seen, ded = set(), []
        for r in revs:
            k = (r["repo"], r["number"])
            if k not in seen:
                seen.add(k)
                ded.append(r)

        rev_true = total_count(q_rev)
        pr_true = total_count(q_pr)
        cmt_true = total_count(q_cmt)

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
            "prs": prs,
            "reviews": ded,
            "issues": iss,
            "comments_on_others": cmts,
            "reviews_true_total": rev_true if rev_true is not None else len(ded),
            "prs_true_total": pr_true if pr_true is not None else len(prs),
            "comments_true_total": (cmt_true if cmt_true is not None
                                    else len(cmts)),
        }
        json.dump(bundle, open("%s/%s.json" % (a.outdir, pid), "w"))

        flag = ""
        if rev_true and rev_true > len(ded):
            flag += "  REVIEWS TRUNCATED (%d captured of %d)" % (len(ded), rev_true)
        if pr_true and pr_true > len(prs):
            flag += "  PRS TRUNCATED (%d of %d)" % (len(prs), pr_true)
        if missing:
            flag += "  %d PRs missing diff stats -- rerun" % len(missing)
        print("%-20s prs=%-4d reviews=%-4d issues=%-3d comments=%-4d%s"
              % (login, len(prs), len(ded), len(iss), len(cmts), flag))


if __name__ == "__main__":
    sys.exit(main())
