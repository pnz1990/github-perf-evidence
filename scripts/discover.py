#!/usr/bin/env python3
"""Build a roster by discovering who was active in an org or a set of repos.

Removes the hand-written-roster barrier: point this at an org and it finds the
contributors, then you confirm the result. Bots are filtered. Identity evidence
is left DELIBERATELY BLANK -- discovery proves a login was active, not who the
person is, and the pipeline reports a blank as UNDOCUMENTED rather than guessing.

  python3 discover.py --org myorg --days 90 -o roster.json
  python3 discover.py --org myorg --start 2026-01-01 --end 2026-06-30 \
      --per-repo --include-reviewers --members-only -o roster.json
  python3 discover.py --repo owner/a --repo owner/b --start ... --end ...

WHY --per-repo EXISTS
GitHub search returns at most 1,000 results per query. A single
`org:X type:pr created:A..B` query against a busy org silently truncates at
1,000 PRs, and whoever happens to fall outside that page window is simply
absent from the roster -- an invisible, unrecoverable omission. --per-repo
enumerates the org's repositories and searches each one separately, which
raises the ceiling to 1,000 PRs *per repo* and reports any repo that still
truncates. It costs one search per repo, so it is opt-in for --repo runs and
the default for --org runs.

WHY --include-reviewers EXISTS
Author-only discovery misses the person whose contribution IS review. A real
measured case: 2 PRs and 6 hand-authored lines, alongside 8 reviews and 13
discussion threads on other people's work. Author-only discovery drops that
person from the roster entirely, which is the exact failure this whole tool
exists to prevent.
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
import time
from collections import Counter

# Bot detection. The trailing/leading -bot and bot- forms matter: project-specific
# service accounts (release bots, CI bots) are named like `<project>-bot` and are
# NOT covered by the `[bot]` suffix convention. Verified against a real org where
# a `<project>-bot` release account authored 9 of 14 PRs in the window and would
# otherwise have been reported as the most productive "person" on the team.
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

# Search is rate-limited at 30 requests/minute. Discovery can make one search
# per repo, so it has to pace itself or it trips the secondary limit and the
# run dies partway through with a half-built roster.
PACE = float(2.2)
MAX_PAGES = 10          # GitHub's hard ceiling: 10 pages x 100 = 1,000 results


def gh(args, retries=4):
    """Run a gh call, retrying through rate-limit rejections.

    Returns "" only after exhausting retries. Callers must treat "" as
    "unknown", never as "empty" -- a rate-limited repo that reported no
    contributors would silently shrink the roster.
    """
    for attempt in range(retries + 1):
        r = subprocess.run(["gh"] + args, capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        err = (r.stderr or "").lower()
        transient = ("rate limit" in err or "secondary" in err
                     or "abuse" in err or "403" in err or "429" in err
                     or "502" in err or "503" in err)
        if not transient or attempt == retries:
            return ""
        wait = min(60, 5 * (2 ** attempt))
        print("  rate-limited, waiting %ds..." % wait, file=sys.stderr)
        time.sleep(wait)
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


def search_logins(q, field_jq, pages=MAX_PAGES):
    """Collect logins matching a search query.

    Returns (Counter, truncated). `truncated` is True when the query hit the
    1,000-result ceiling, which means the roster from this query is incomplete
    and the caller must say so out loud.
    """
    logins = Counter()
    pages_used = 0
    for p in range(1, pages + 1):
        got = jlines(gh(["api", "-X", "GET", "search/issues", "-f", "q=" + q,
                         "-f", "per_page=100", "-f", "page=%d" % p,
                         "--jq", field_jq]))
        pages_used = p
        for it in got:
            lg = it.get("login")
            if lg and not BOT_RE.search(lg):
                logins[lg] += 1
        time.sleep(PACE)
        if len(got) < 100:
            break
    return logins, pages_used >= pages


def comment_authors(repo, start, end, max_pages=10):
    """Find people who commented on PRs in one repo, via the REST endpoints.

    Search CANNOT do this. `search/issues` returns PRs and its `.user.login` is
    the PR AUTHOR, so no search query enumerates reviewers for a repo without
    already knowing whose name to put in `reviewed-by:`. The repo-scoped comment
    endpoints return the commenter directly, which is the only way to discover
    review-only contributors from scratch.

    Both endpoints accept `since` but not `until`, so the upper bound is filtered
    client-side. Returns (Counter, hit_page_cap).
    """
    found = Counter()
    capped = False
    for path in ("pulls/comments", "issues/comments"):
        for p in range(1, max_pages + 1):
            got = jlines(gh([
                "api", "-X", "GET", "repos/%s/%s" % (repo, path),
                "-f", "since=%sT00:00:00Z" % start, "-f", "per_page=100",
                "-f", "page=%d" % p, "-f", "sort=created", "-f", "direction=desc",
                "--jq", ".[]|{login:.user.login,created:.created_at}"]))
            for it in got:
                lg, created = it.get("login"), (it.get("created") or "")
                # `since` bounds the low end; enforce the high end here.
                if not lg or BOT_RE.search(lg) or created[:10] > end:
                    continue
                found[lg] += 1
            time.sleep(0.4)          # REST limit is far looser than search
            if len(got) < 100:
                break
            if p == max_pages:
                capped = True
    return found, capped


def list_org_repos(org, pushed_since=None, include_forks=False,
                   include_archived=False):
    """Enumerate an org's repos, cheaply pre-filtered by last push.

    pushed_since prunes repos that cannot possibly have activity in the window
    before we spend a search call on each one. On a large org this is the
    difference between a few dozen searches and several hundred.
    """
    raw = gh(["api", "orgs/%s/repos" % org, "--paginate", "--jq",
              ".[]|{name:.name,full_name:.full_name,fork:.fork,"
              "archived:.archived,pushed_at:.pushed_at}"])
    if not raw:
        # Fall back to the user endpoint: --org may name a user account.
        raw = gh(["api", "users/%s/repos" % org, "--paginate", "--jq",
                  ".[]|{name:.name,full_name:.full_name,fork:.fork,"
                  "archived:.archived,pushed_at:.pushed_at}"])
    repos, skipped = [], Counter()
    for r in jlines(raw):
        if r.get("fork") and not include_forks:
            skipped["fork"] += 1
            continue
        if r.get("archived") and not include_archived:
            skipped["archived"] += 1
            continue
        if pushed_since and (r.get("pushed_at") or "") < pushed_since:
            skipped["no_push_in_window"] += 1
            continue
        repos.append((r.get("pushed_at") or "", r["full_name"]))
    # Most recently pushed first. This ordering is load-bearing: --max-repos
    # truncates this list, and the caller tells the user it kept the most
    # recently active repos. Returning API order would make that claim false
    # and would silently drop whichever repos happened to sort late.
    repos.sort(reverse=True)
    return [full for _, full in repos], skipped


def org_members(org):
    """Org membership, used to prune outside contributors automatically.

    Returns None if unavailable (no read:org scope, or not a member), which is
    different from an empty set and must not be treated as "nobody is a member".
    """
    raw = gh(["api", "orgs/%s/members" % org, "--paginate",
              "--jq", ".[].login"])
    if not raw.strip():
        return None
    return {l.strip() for l in raw.splitlines() if l.strip()}


def main():
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--org", help="discover contributors across an org or user")
    g.add_argument("--repo", action="append", default=[],
                   help="specific repo(s), repeatable: owner/name")
    ap.add_argument("--days", type=int,
                    help="look back N days from today (or use --start/--end)")
    ap.add_argument("--start", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD")
    ap.add_argument("--per-repo", dest="per_repo", action="store_true",
                    default=None,
                    help="search each repo separately to avoid the 1,000-result "
                         "cap. Default ON for --org, OFF for --repo.")
    ap.add_argument("--no-per-repo", dest="per_repo", action="store_false",
                    help="force one org-wide query (faster, may truncate)")
    ap.add_argument("--include-reviewers", action="store_true",
                    help="also find people who reviewed or commented but never "
                         "authored a PR. Costs 2 extra searches per repo.")
    ap.add_argument("--members-only", action="store_true",
                    help="keep only confirmed members of --org, dropping outside "
                         "contributors automatically")
    ap.add_argument("--include-forks", action="store_true",
                    help="include the org's forked repos (off by default: their "
                         "contributors are usually upstream, not your team)")
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--min-prs", type=int, default=1,
                    help="omit logins with fewer authored PRs (default 1)")
    ap.add_argument("--min-activity", type=int, default=1,
                    help="omit logins with fewer total events (authored + "
                         "reviewed + commented). Default 1.")
    ap.add_argument("--max-people", type=int, default=60,
                    help="cap roster size; most active kept (default 60)")
    ap.add_argument("--max-repos", type=int, default=200,
                    help="cap repos searched in --per-repo mode (default 200)")
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

    if a.per_repo is None:
        a.per_repo = bool(a.org)
    if a.members_only and not a.org:
        raise SystemExit("--members-only needs --org (membership is per-org)")

    # ---- decide what to search -------------------------------------------
    repo_skips = Counter()
    if a.per_repo:
        if a.org:
            print("enumerating repos in %s..." % a.org, file=sys.stderr)
            targets, repo_skips = list_org_repos(
                a.org, pushed_since=start + "T00:00:00Z",
                include_forks=a.include_forks,
                include_archived=a.include_archived)
            if not targets:
                print("No repos found in %s with a push since %s. Check the "
                      "name and `gh auth status`, or pass --include-archived."
                      % (a.org, start), file=sys.stderr)
                return 1
        else:
            targets = list(a.repo)
        if len(targets) > a.max_repos:
            print("NOTE: %d repos in scope; searching the %d most recently "
                  "pushed. Raise --max-repos to cover all of them."
                  % (len(targets), a.max_repos), file=sys.stderr)
            repo_skips["over_max_repos"] = len(targets) - a.max_repos
            targets = targets[:a.max_repos]
        scopes = ["repo:%s" % r for r in targets]
    else:
        targets = [a.org] if a.org else list(a.repo)
        scopes = (["org:%s" % a.org] if a.org
                  else [" ".join("repo:%s" % r for r in a.repo)])

    if a.include_reviewers and not a.per_repo:
        # Reviewer discovery is repo-scoped by construction (see
        # comment_authors). An org-wide single query has no repo to ask.
        print("NOTE: --include-reviewers needs per-repo mode; enabling it.",
              file=sys.stderr)
        raise SystemExit("re-run with --per-repo (or drop --include-reviewers)")

    total_searches = len(scopes)
    print("searching %d scope(s) = ~%d searches, ~%d min%s"
          % (len(scopes), total_searches,
             max(1, round(total_searches * PACE / 60.0)),
             " + comment scan per repo" if a.include_reviewers else ""),
          file=sys.stderr)

    # ---- run the searches -------------------------------------------------
    authored, commented = Counter(), Counter()
    truncated_scopes = []
    for i, sc in enumerate(scopes, 1):
        if a.per_repo:
            print("  [%d/%d] %s" % (i, len(scopes), sc), file=sys.stderr)
        lg, trunc = search_logins(
            "%s type:pr created:%s..%s" % (sc, start, end),
            ".items[]|{login:.user.login}")
        authored += lg
        if trunc:
            truncated_scopes.append(sc + " (authored)")
        if a.include_reviewers and sc.startswith("repo:"):
            got, capped = comment_authors(sc[5:], start, end)
            commented += got
            if capped:
                truncated_scopes.append(sc + " (comments)")

    if not authored and not commented:
        print("No contributors found. Check the org/repo name and that "
              "`gh auth status` has access.", file=sys.stderr)
        return 1

    # ---- membership pruning ----------------------------------------------
    members = org_members(a.org) if a.org else None
    if a.members_only:
        if members is None:
            print("WARNING: could not read members of %s (needs read:org scope "
                  "or membership). Keeping everyone -- prune the roster by "
                  "hand." % a.org, file=sys.stderr)
        else:
            before = len(authored)
            authored = Counter({k: v for k, v in authored.items()
                                if k in members})
            commented = Counter({k: v for k, v in commented.items()
                                 if k in members})
            print("members-only: kept %d of %d logins"
                  % (len(authored), before), file=sys.stderr)

    # ---- assemble ---------------------------------------------------------
    everyone = set(authored) | set(commented)
    scored = []
    for lg in everyone:
        n_pr = authored.get(lg, 0)
        n_other = commented.get(lg, 0)
        # Two independent floors. --min-prs filters authors; --min-activity
        # lets a review-only contributor qualify on review volume alone, which
        # is the entire point of --include-reviewers. Someone with 0 PRs and 20
        # review comments passes; someone with 0 of both never appears.
        if n_pr + n_other < a.min_activity:
            continue
        if n_pr and n_pr < a.min_prs and not n_other:
            continue
        scored.append((lg, n_pr, n_other))
    scored.sort(key=lambda t: (-(t[1] + t[2]), t[0]))

    if len(scored) > a.max_people:
        print("NOTE: %d contributors found; keeping the %d most active. Raise "
              "--max-people to include everyone."
              % (len(scored), a.max_people), file=sys.stderr)
        scored = scored[:a.max_people]

    people = []
    for lg, n_pr, n_other in scored:
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
        # Hint fields, all underscore-prefixed. They are DISCOVERY NOTES, not
        # evidence: a GitHub profile is self-reported and an org can contain
        # contractors, alumni and bots-with-human-names. They exist so a human
        # can confirm the roster in one pass instead of researching each login.
        if company:
            person["_github_company"] = company
        if members is not None:
            person["_org_member"] = lg in members
        person["_authored_prs_in_window"] = n_pr
        if n_other:
            person["_other_activity_in_window"] = n_other
        # Left blank ON PURPOSE: see module docstring. Auto-filling this would
        # turn an unverified guess into "documented" downstream and destroy the
        # one safety property that stops a login being mistaken for a person.
        person["identity_evidence"] = ""
        people.append(person)

    notes = [
        "GENERATED STARTER ROSTER -- confirm before use.",
        "1. Remove anyone who is not on your team. Discovery finds everyone "
        "who was active, including outside contributors.",
        "2. Fill in identity_evidence for each person: how you know this "
        "login is this human. A blank value is reported as UNDOCUMENTED.",
        "3. Add team/level/title/manager if you want them in the output.",
        "4. Delete the _-prefixed hint fields; they are discovery notes, "
        "not evidence.",
        "Bots were filtered automatically.",
    ]
    if a.per_repo:
        notes.append("Searched %d repo(s) individually to avoid the "
                     "1,000-result search cap." % len(scopes))
    for k, v in sorted(repo_skips.items()):
        notes.append("Skipped %d repo(s): %s." % (v, k.replace("_", " ")))
    if truncated_scopes:
        notes.append("INCOMPLETE: these scopes hit the 1,000-result cap, so "
                     "contributors may be missing: %s. Narrow the window and "
                     "re-run." % ", ".join(truncated_scopes[:10]))
    if a.members_only and members is None:
        notes.append("--members-only was requested but membership was "
                     "unreadable; NO pruning happened.")

    roster = {
        "_comment": notes,
        "window": {"start": start, "end": end},
        "discovery": {
            "mode": "per-repo" if a.per_repo else "single-query",
            "scopes_searched": len(scopes),
            "repos": targets if a.per_repo else None,
            "truncated_scopes": truncated_scopes,
            "skipped_repos": dict(repo_skips),
            "members_pruned": bool(a.members_only and members is not None),
            "included_reviewers": a.include_reviewers,
        },
        "people": people,
    }
    json.dump(roster, open(a.output, "w"), indent=2)
    print("wrote %s with %d people" % (a.output, len(people)), file=sys.stderr)
    if truncated_scopes:
        print("WARNING: %d scope(s) truncated at 1,000 results -- roster may "
              "be incomplete." % len(truncated_scopes), file=sys.stderr)
    print("CONFIRM IT before running fetch.py -- especially identity_evidence.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
