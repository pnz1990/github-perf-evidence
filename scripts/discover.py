#!/usr/bin/env python3
"""Build a starter roster by discovering who was active in an org or repo list.

Removes the hand-written-roster barrier: point this at an org and it finds the
contributors, then you edit the result. Bots are filtered. Identity evidence is
left DELIBERATELY BLANK -- discovery proves a login was active, not who the
person is, and the pipeline flags a blank as UNDOCUMENTED rather than guessing.

  python3 discover.py --org myorg --days 90 -o roster.json
  python3 discover.py --repo owner/a --repo owner/b --start 2026-01-01 --end 2026-06-30
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
from collections import Counter

# Bot detection. The trailing/leading -bot and bot- forms matter: project-specific
# service accounts (release bots, CI bots) are named like `<project>-bot` and are
# NOT covered by the `[bot]` suffix convention. Verified against a real org where
# `eksctl-bot` authored 9 of 14 PRs in the window and would otherwise have been
# reported as the most productive "person" on the team.
BOT_RE = re.compile(
    r"\[bot\]$"
    r"|(^|[-_])bot([-_]|$)"
    r"|[-_]bot\d*$"
    r"|(^|[-_])robot([-_]|$)|[-_]robot\d*$"
    r"|(^|[-_])(ci|cd)[-_]?(bot|robot)([-_]|$)"
    r"|^(dependabot|renovate|copilot|github-actions|codecov|greenkeeper|"
    r"snyk|imgbot|allcontributors|semantic-release|mergify|kodiak|"
    r"pre-commit-ci|netlify|vercel|sonarcloud|stale|codeclimate|"
    r"web-flow|ghost)(\[bot\])?$",
    re.I)


def gh(args):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


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


def search_authors(q, pages=10):
    """Collect PR authors matching a search query."""
    authors = Counter()
    for p in range(1, pages + 1):
        got = jlines(gh(["api", "-X", "GET", "search/issues", "-f", "q=" + q,
                         "-f", "per_page=100", "-f", "page=%d" % p,
                         "--jq", ".items[]|{login:.user.login}"]))
        for it in got:
            lg = it.get("login")
            if lg and not BOT_RE.search(lg):
                authors[lg] += 1
        if len(got) < 100:
            break
    return authors


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--org", help="discover contributors across an org")
    g.add_argument("--repo", action="append", default=[],
                   help="specific repo(s), repeatable: owner/name")
    ap.add_argument("--days", type=int,
                    help="look back N days from today (or use --start/--end)")
    ap.add_argument("--start", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD")
    ap.add_argument("--min-prs", type=int, default=1,
                    help="omit logins with fewer authored PRs (default 1)")
    ap.add_argument("--max-people", type=int, default=60,
                    help="cap roster size; most active kept (default 60)")
    ap.add_argument("--output", "-o", required=True)
    a = ap.parse_args()

    if a.start and a.end:
        start, end = a.start, a.end
    elif a.days:
        today = datetime.date.today()
        start = str(today - datetime.timedelta(days=a.days))
        end = str(today)
    else:
        raise SystemExit("give --days, or both --start and --end")

    scope = ("org:%s" % a.org) if a.org else " ".join(
        "repo:%s" % r for r in a.repo)
    if a.repo and len(a.repo) > 1:
        # search OR-joins repo: qualifiers automatically
        scope = " ".join("repo:%s" % r for r in a.repo)

    print("discovering PR authors in %s, %s..%s" % (scope, start, end),
          file=sys.stderr)
    authors = search_authors("%s type:pr created:%s..%s" % (scope, start, end))

    if not authors:
        print("No PR authors found. Check the org/repo name and that `gh auth "
              "status` has access.", file=sys.stderr)
        return 1

    kept = [(lg, n) for lg, n in authors.most_common() if n >= a.min_prs]
    dropped_bots = "filtered by bot pattern"
    if len(kept) > a.max_people:
        print("NOTE: %d contributors found; keeping the %d most active. Raise "
              "--max-people to include everyone."
              % (len(kept), a.max_people), file=sys.stderr)
        kept = kept[:a.max_people]

    people = []
    for lg, n in kept:
        prof = gh(["api", "users/" + lg, "--jq",
                   "{name:.name,company:.company}"]).strip()
        name = company = None
        if prof:
            try:
                d = json.loads(prof)
                name, company = d.get("name"), d.get("company")
            except json.JSONDecodeError:
                pass
        person = {"github_login": lg, "id": lg}
        if name:
            person["name"] = name
        if company:
            person["_github_company"] = company
        person["_authored_prs_in_window"] = n
        # Left blank on purpose: see module docstring.
        person["identity_evidence"] = ""
        people.append(person)

    roster = {
        "_comment": [
            "GENERATED STARTER ROSTER -- edit before use.",
            "1. Remove anyone who is not on your team. Discovery finds everyone "
            "who opened a PR, including outside contributors.",
            "2. Fill in identity_evidence for each person: how you know this "
            "login is this human. A blank value is reported as UNDOCUMENTED.",
            "3. Add team/level/title/manager if you want them in the output.",
            "4. Delete the _-prefixed hint fields; they are discovery notes, "
            "not evidence.",
            "Bots were filtered automatically (%s)." % dropped_bots,
        ],
        "window": {"start": start, "end": end},
        "people": people,
    }
    json.dump(roster, open(a.output, "w"), indent=2)
    print("wrote %s with %d people" % (a.output, len(people)), file=sys.stderr)
    print("EDIT IT before running fetch.py -- especially identity_evidence.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
