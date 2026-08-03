#!/usr/bin/env python3
"""Render per-person evidence YAML plus a cohort index.

Every number that can mislead ships with a caveat naming the specific way it
can mislead. Ownership roles are discovered from OWNERS/OWNERS_ALIASES/CODEOWNERS
in the repos the person actually touched, so they are externally granted
evidence rather than self-report.
"""
import argparse
import datetime
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

# Working files, not person bundles.
SKIP_FILES = ("filecache.json", "patterns.json", "commitcache.json")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify import build_bucket, load_patterns  # noqa: E402


def yq(s):
    if s is None:
        return "null"
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def wrap(w, text, indent, width=86):
    words, line = str(text).split(), ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            w(indent + line)
            line = word
        else:
            line = (line + " " + word) if line else word
    if line:
        w(indent + line)


def gh_json(args, want=dict):
    """Run gh and return parsed JSON of the expected type, else None.

    Returning raw text on a parse failure would let a string leak into code that
    expects a dict and fail far from the cause -- so the type is enforced here.
    """
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, want) else None


def repo_meta(repo, memo):
    """fork / parent / stars for a repo. Always returns a dict.

    Repos can be deleted, renamed, or made private between the PR being opened
    and the scan running, so a failed lookup must degrade to "unknown" rather
    than crash the whole build.
    """
    if repo in memo:
        return memo[repo]
    d = gh_json(["api", "repos/" + repo, "--jq",
                 "{fork:.fork,parent:.parent.full_name,stars:.stargazers_count,"
                 "owner:.owner.login}"])
    if not isinstance(d, dict):
        d = {"fork": None, "parent": None, "stars": None, "owner": None,
             "_lookup_failed": True}
    memo[repo] = d
    return memo[repo]


def ownership(login, repos, memo):
    """Find login in OWNERS / OWNERS_ALIASES / CODEOWNERS of the given repos."""
    roles = []
    for repo in repos:
        if repo in memo:
            blobs = memo[repo]
        else:
            blobs = {}
            for path in ("OWNERS_ALIASES", "OWNERS", "CODEOWNERS",
                         ".github/CODEOWNERS"):
                r = subprocess.run(
                    ["gh", "api", "repos/%s/contents/%s" % (repo, path),
                     "--jq", ".content"], capture_output=True, text=True)
                if r.returncode == 0 and r.stdout.strip():
                    import base64
                    try:
                        blobs[path] = base64.b64decode(r.stdout.strip()).decode(
                            "utf-8", "replace")
                    except Exception:
                        pass
            memo[repo] = blobs
        for path, text in blobs.items():
            if not re.search(r"(^|[\s:@-])" + re.escape(login) + r"\s*$",
                             text, re.M):
                continue
            group = None
            if path == "OWNERS_ALIASES":
                cur = None
                for line in text.splitlines():
                    m = re.match(r"^\s{2}([\w-]+):\s*$", line)
                    if m:
                        cur = m.group(1)
                    elif re.match(r"^\s*-\s*" + re.escape(login) + r"\s*$", line):
                        group = cur
                        break
            roles.append({"repo": repo, "file": path, "group": group})
    return roles


def build_person(path, cfg, bucket, cache, rmeta, ometa, ccache=None):
    b = json.load(open(path))
    P, PRS = b["person"], b["prs"]
    REVIEWS, ISSUES = b["reviews"], b["issues"]
    login = P["github_login"]
    pid = P.get("id") or login
    win = (b["window"]["start"], b["window"]["end"])
    scan = cfg["scan_date"]
    scan_d = datetime.date(*map(int, scan.split("-")))

    if not PRS:
        print("SKIP %s -- no PRs in window" % login)
        return None

    for p in PRS:
        m = repo_meta(p["repo"], rmeta)
        p["_fork"] = bool(m.get("fork"))
        p["_parent"] = m.get("parent")
        p["_stars"] = m.get("stars") or 0
        p["_meta_failed"] = bool(m.get("_lookup_failed"))

    merged = [p for p in PRS if p.get("merged_at")]
    open_prs = [p for p in PRS if p["state"] == "open"]
    closed_un = [p for p in PRS
                 if p["state"] == "closed" and not p.get("merged_at")]
    forks = [p for p in PRS if p["_fork"]]
    canon = [p for p in PRS if not p["_fork"]]

    hand = sum(p["hand_additions"] for p in PRS)
    gen = sum(p["generated_additions"] for p in PRS)
    ven = sum(p["vendored_additions"] for p in PRS)
    raw = hand + gen + ven
    hand_m = sum(p["hand_additions"] for p in merged)
    hand_o = sum(p["hand_additions"] for p in open_prs)
    hand_fork = sum(p["hand_additions"] for p in forks)
    hand_cm = sum(p["hand_additions"] for p in canon if p.get("merged_at"))
    dele = sum(p.get("deletions", 0) for p in PRS)

    dup_total, dup_worst = 0, None
    for p in PRS:
        files = cache.get("%s#%d" % (p["repo"], p["number"])) or []
        seen, d = set(), 0
        for add, fn in files:
            if add <= 20 or bucket(fn) != "hand":
                continue
            k = (fn.rsplit("/", 1)[-1], add)
            if k in seen:
                d += add
            else:
                seen.add(k)
        if d > 300:
            dup_total += d
            if not dup_worst or d > dup_worst[1]:
                dup_worst = ("%s#%d" % (p["repo"], p["number"]), d)

    by_repo = defaultdict(lambda: dict(total=0, merged=0, hand=0, gen=0,
                                       ven=0, dele=0, fork=False))
    for p in PRS:
        r = by_repo[p["repo"]]
        r["total"] += 1
        r["merged"] += 1 if p.get("merged_at") else 0
        r["hand"] += p["hand_additions"]
        r["gen"] += p["generated_additions"]
        r["ven"] += p["vendored_additions"]
        r["dele"] += p.get("deletions", 0)
        r["fork"] = p["_fork"]

    top_repos = [r for r, _ in Counter(
        p["repo"] for p in canon).most_common(cfg["ownership_repo_limit"])]
    roles = ownership(login, top_repos, ometa)

    ext = [p for p in canon
           if p["repo"].split("/")[0].lower() not in cfg["own_orgs"]
           and (p["_stars"] or 0) >= cfg["external_star_floor"]]

    # Cycle time: open -> merge. Reads as delivery speed but is confounded by
    # review latency, which is not the author's to control.
    def days(a, z):
        d1 = datetime.datetime.strptime(a, "%Y-%m-%dT%H:%M:%SZ")
        d2 = datetime.datetime.strptime(z, "%Y-%m-%dT%H:%M:%SZ")
        return (d2 - d1).total_seconds() / 86400.0

    cycles = sorted(days(p["created_at"], p["merged_at"]) for p in merged
                    if p.get("merged_at"))

    def pct(vals, q):
        if not vals:
            return None
        i = min(len(vals) - 1, int(round(q * (len(vals) - 1))))
        return vals[i]

    CMTS = b.get("comments_on_others", [])
    cmt_true = b.get("comments_true_total", len(CMTS))
    cmt_repos = Counter(c["repo"] for c in CMTS)
    cmt_authors = Counter(c["author"] for c in CMTS)
    cmt_pr = sum(1 for c in CMTS if c.get("is_pr"))

    # Language mix from the hand-authored files actually touched.
    EXT_LANG = {
        ".go": "Go", ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
        ".js": "JavaScript", ".jsx": "JavaScript", ".java": "Java",
        ".rs": "Rust", ".rb": "Ruby", ".c": "C", ".h": "C/C++ header",
        ".cc": "C++", ".cpp": "C++", ".cs": "C#", ".kt": "Kotlin",
        ".swift": "Swift", ".scala": "Scala", ".sh": "Shell",
        ".bash": "Shell", ".ya?ml": "YAML", ".yaml": "YAML", ".yml": "YAML",
        ".json": "JSON", ".md": "Markdown", ".sql": "SQL", ".tf": "Terraform",
        ".proto": "Protobuf", ".php": "PHP", ".ex": "Elixir", ".dart": "Dart",
        ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".lua": "Lua",
        ".gradle": "Gradle", ".mk": "Make", ".dockerfile": "Docker",
    }
    lang = Counter()
    test_lines = impl_lines = 0
    for p in PRS:
        for add, fn in (cache.get("%s#%d" % (p["repo"], p["number"])) or []):
            if bucket(fn) != "hand":
                continue
            base = fn.rsplit("/", 1)[-1].lower()
            suffix = "." + base.rsplit(".", 1)[-1] if "." in base else ""
            if base in ("dockerfile", "makefile"):
                lang["Docker" if base == "dockerfile" else "Make"] += add
            else:
                lang[EXT_LANG.get(suffix, suffix or "(no extension)")] += add
            if re.search(r"(^|[/_.])test|_test\.|/tests?/|\.spec\.|__tests__/",
                         fn, re.I):
                test_lines += add
            else:
                impl_lines += add

    rev_true = b.get("reviews_true_total", len(REVIEWS))
    rev_trunc = rev_true > len(REVIEWS)
    pr_true = b.get("prs_true_total", len(PRS))
    pr_trunc = pr_true > len(PRS)
    unavail = [p for p in PRS if p.get("classification") == "UNAVAILABLE"]

    monthly = Counter(p["merged_at"][:7] for p in merged)
    monthly_o = Counter(p["created_at"][:7] for p in PRS)
    rev_repo = Counter(r["repo"] for r in REVIEWS)
    rev_auth = Counter(r["author"] for r in REVIEWS)

    L = []
    w = L.append
    w("# GitHub Performance Evidence -- scan %s" % scan)
    w("# Subject: %s (GitHub %s)" % (P.get("name") or pid, login))
    w("# Window: %s to %s" % win)
    w("# %d PRs authored, %d reviews given, %d issues opened"
      % (len(PRS), rev_true, len(ISSUES)))
    w("#")
    w("# READ metadata.caveats BEFORE QUOTING ANY NUMBER.")
    w("# Best volume figure: summary.hand_additions_canonical_merged")
    w("---")
    w("metadata:")
    w("  scan_date: %s" % yq(scan))
    w("  source: github")
    w("  github_login: %s" % yq(login))
    w("  id: %s" % yq(pid))
    for k in ("name", "team", "level", "title", "manager"):
        if P.get(k):
            w("  %s: %s" % (k, yq(P[k])))
    w("  window_start: %s" % yq(win[0]))
    w("  window_end: %s" % yq(win[1]))
    w("  classifier_version: %s" % b.get("classifier_version", 1))
    w("  purpose: 'Performance review evidence'")
    w("  scope: >-")
    wrap(w, cfg["scope_note"], "    ")
    w("  identity_resolution: >-")
    wrap(w, P.get("identity_evidence")
         or ("NOT DOCUMENTED. The roster gave no identity evidence for this "
             "login. Confirm the login belongs to this person before use."),
         "    ")
    w("  identity_confidence: %s" % (
        "documented" if P.get("identity_evidence") else "UNDOCUMENTED"))
    w("  caveats:")

    w("    - id: line_count_inflation")
    w("      severity: high")
    w("      detail: >-")
    wrap(w, ("Raw GitHub additions total %d, but only %d (%.0f%%) are "
             "HAND-AUTHORED. %d lines are machine-GENERATED and %d are VENDORED "
             "third-party content. Cite hand_additions_*, never raw additions."
             % (raw, hand, 100.0 * hand / max(1, raw), gen, ven)), "        ")
    w("      classification_method: >-")
    wrap(w, ("Per-file path regex over every PR file list; rules and their "
             "verification evidence are in patterns.json (classifier_version "
             "%s). Heuristic: treat hand_additions as a good-faith lower bound."
             % b.get("classifier_version", 1)), "        ")
    w("      comparability: >-")
    wrap(w, ("All people in this scan batch were classified with the identical "
             "classifier_version, so hand_additions IS comparable within the "
             "cohort. It is NOT comparable to any earlier scan."), "        ")

    w("    - id: volume_is_not_impact")
    w("      severity: high")
    w("      detail: >-")
    wrap(w, ("Line counts measure activity. A small change to a code generator, "
             "shared library, or CI pipeline can outweigh thousands of lines of "
             "leaf-level work. Weight ownership_roles, "
             "external_upstream_contributions and reviews_given at least as "
             "heavily as volume, and never rank people on lines alone."),
         "        ")

    if forks:
        w("    - id: fork_prs_may_double_count")
        w("      severity: high")
        w("      detail: >-")
        wrap(w, ("%d of %d PRs target a FORK rather than the canonical upstream "
                 "repo, holding %d hand-authored lines. Fork PRs are often "
                 "pre-flight or staging duplicates of an upstream PR; counting "
                 "both double-counts the same work. Canonical merged "
                 "hand-authored volume is %d."
                 % (len(forks), len(PRS), hand_fork, hand_cm)), "        ")
    if hand_o > 0.25 * max(1, hand):
        w("    - id: large_open_share")
        w("      severity: medium")
        w("      detail: >-")
        wrap(w, ("%d of %d PRs are still OPEN, holding %d of %d hand-authored "
                 "lines (%.0f%%). Merged hand-authored volume is %d. Do not "
                 "present unmerged work as shipped."
                 % (len(open_prs), len(PRS), hand_o, hand,
                    100.0 * hand_o / max(1, hand), hand_m)), "        ")
    if dup_total > 300:
        w("    - id: intra_pr_duplicate_subtree")
        w("      severity: %s" % ("high" if dup_total > 0.08 * max(1, hand)
                                  else "medium"))
        w("      detail: >-")
        wrap(w, ("%d hand-classified lines (%.0f%% of hand_additions_total) are "
                 "duplicates WITHIN a single PR: same filename at same line "
                 "count under two path prefixes, i.e. a copied subtree. Worst: "
                 "%s (%d lines). See hand_additions_dedup_estimate."
                 % (dup_total, 100.0 * dup_total / max(1, hand),
                    dup_worst[0], dup_worst[1])), "        ")
    if rev_trunc:
        w("    - id: review_detail_truncated")
        w("      severity: high")
        w("      detail: >-")
        wrap(w, ("GitHub search reports %d reviews in window but pagination "
                 "captured only %d. reviews.total (%d) is the correct headline; "
                 "reviews.by_repo and top_authors_reviewed describe the "
                 "%d-review SAMPLE only."
                 % (rev_true, len(REVIEWS), rev_true, len(REVIEWS))), "        ")
    if pr_trunc:
        w("    - id: pr_list_truncated")
        w("      severity: high")
        w("      detail: >-")
        wrap(w, ("Search reports %d authored PRs but only %d were captured "
                 "(1000-result search ceiling). ALL volume figures understate. "
                 "Split the window into shorter ranges and re-scan."
                 % (pr_true, len(PRS))), "        ")
    if cmt_true > len(CMTS):
        w("    - id: comment_detail_truncated")
        w("      severity: medium")
        w("      detail: >-")
        wrap(w, ("Search reports %d comment threads but only %d were captured. "
                 "collaboration.comment_threads_total (%d) is the correct "
                 "headline; top_repos and most_helped are a sample."
                 % (cmt_true, len(CMTS), cmt_true)), "        ")
    if unavail:
        w("    - id: file_lists_unavailable")
        w("      severity: medium")
        w("      detail: >-")
        wrap(w, ("%d PRs had unreachable file lists (deleted fork or lost "
                 "access) and contribute 0 to all line counts, understating "
                 "volume: %s" % (len(unavail), ", ".join(
                     "%s#%d" % (p["repo"], p["number"]) for p in unavail[:8]))),
             "        ")
    if monthly:
        last = max(monthly)
        if last[:7] == win[1][:7]:
            w("    - id: partial_final_month")
            w("      severity: low")
            w("      detail: >-")
            wrap(w, ("%s shows %d merged PRs but is a partial month: the window "
                     "ends %s. Do not read it as a trend."
                     % (last, monthly[last], win[1])), "        ")
    w("")

    w("summary:")
    w("  recommended_metric: hand_additions_canonical_merged")
    w("  prs_authored: %d" % len(PRS))
    w("  prs_merged: %d" % len(merged))
    w("  prs_open: %d" % len(open_prs))
    w("  prs_closed_unmerged: %d" % len(closed_un))
    w("  merge_rate_pct: %.1f" % (100.0 * len(merged) / len(PRS)))
    w("  repos_touched: %d" % len(by_repo))
    w("  reviews_given: %d" % rev_true)
    w("  reviews_detail_captured: %d" % len(REVIEWS))
    w("  review_to_authorship_ratio: %.2f" % (rev_true / len(PRS)))
    w("  distinct_authors_reviewed: %d  # within captured sample" % len(rev_auth))
    w("  comment_threads_on_others_work: %d" % cmt_true)
    w("  issues_opened: %d" % len(ISSUES))
    if cycles:
        w("  cycle_time_days_median: %.1f" % pct(cycles, 0.5))
        w("  cycle_time_days_p90: %.1f" % pct(cycles, 0.9))
    w("  test_line_share_pct: %.0f  # of hand-authored lines" % (
        100.0 * test_lines / max(1, test_lines + impl_lines)))
    w("  hand_additions_canonical_merged: %d  # <- QUOTE THIS" % hand_cm)
    w("  hand_additions_total: %d" % hand)
    w("  hand_additions_merged: %d" % hand_m)
    w("  hand_additions_open: %d" % hand_o)
    w("  hand_additions_in_forks: %d" % hand_fork)
    w("  hand_additions_dedup_estimate: %d" % (hand - dup_total))
    w("  intra_pr_duplicate_lines: %d" % dup_total)
    w("  generated_additions_excluded: %d" % gen)
    w("  vendored_additions_excluded: %d" % ven)
    w("  raw_additions_total: %d  # DO NOT CITE" % raw)
    w("  deletions_total: %d" % dele)
    w("")

    w("# Discovered by reading OWNERS / OWNERS_ALIASES / CODEOWNERS in the repos")
    w("# this person touched. Externally granted, independently verifiable.")
    w("ownership_roles:")
    if not roles:
        w("  []  # none found in the %d most-touched repos" % len(top_repos))
    for r in roles:
        w("  - repo: %s" % yq(r["repo"]))
        w("    file: %s" % yq(r["file"]))
        w("    group: %s" % yq(r["group"]))
        w("    confidence: confirmed")
        w("    note: >-")
        wrap(w, ("Listed in %s. Note the tier: a maintainer/approver group "
                 "outranks a reviewer group in the same file. Check the file "
                 "to see which." % r["file"]), "      ")
    w("")

    w("# PRs to repos outside the org's own namespaces, weighted by stars.")
    w("# Externally visible influence: required convincing outside maintainers.")
    w("external_upstream_contributions:")
    w("  count: %d" % len(ext))
    if not ext:
        w("  items: []")
    else:
        w("  items:")
        for p in sorted(ext, key=lambda x: -(x["_stars"] or 0)):
            w("    - repo: %s" % yq(p["repo"]))
            w("      upstream_stars: %s" % (p["_stars"] or "null"))
            w("      number: %d" % p["number"])
            w("      title: %s" % yq(p["title"]))
            w("      url: %s" % yq(p["url"]))
            w("      status: %s" % ("MERGED" if p.get("merged_at")
                                    else p["state"].upper()))
            w("      hand_additions: %d" % p["hand_additions"])
    w("")

    w("fork_activity:  # likely duplicates of upstream work -- do not double-count")
    w("  count: %d" % len(forks))
    w("  hand_additions: %d" % hand_fork)
    if not forks:
        w("  items: []")
    else:
        w("  items:")
        for p in sorted(forks, key=lambda x: x["created_at"]):
            w("    - repo: %s" % yq(p["repo"]))
            w("      forks_from: %s" % yq(p["_parent"]))
            w("      number: %d" % p["number"])
            w("      title: %s" % yq(p["title"]))
            w("      status: %s" % ("MERGED" if p.get("merged_at")
                                    else p["state"].upper()))
            w("      hand_additions: %d" % p["hand_additions"])
    w("")

    w("# Discussion on OTHER people's PRs and issues. Distinct from formal")
    w("# reviews: design debate, triage, unblocking. For some people this is")
    w("# their largest collaboration signal and it is invisible in PR counts.")
    w("collaboration:")
    w("  comment_threads_total: %d" % cmt_true)
    w("  detail_captured: %d" % len(CMTS))
    w("  truncated: %s" % ("true" if cmt_true > len(CMTS) else "false"))
    w("  on_pull_requests: %d" % cmt_pr)
    w("  on_issues: %d" % (len(CMTS) - cmt_pr))
    w("  distinct_people_helped: %d" % len(cmt_authors))
    w("  ratio_to_own_prs: %.2f" % (cmt_true / len(PRS)))
    w("  caveat: >-")
    wrap(w, ("Thread counts, not comment counts: one thread with 20 substantive "
             "comments counts once, same as a thread with a single '+1'. This "
             "measures reach, NOT depth or quality. Read a sample of the actual "
             "threads before drawing any conclusion about mentoring or "
             "technical leadership."), "    ")
    w("  top_repos:")
    for r, c in cmt_repos.most_common(15):
        w("    - {repo: %s, threads: %d}" % (yq(r), c))
    w("  most_helped:")
    for au, c in cmt_authors.most_common(15):
        w("    - {author: %s, threads: %d}" % (yq(au), c))
    w("")

    w("# Language mix of HAND-AUTHORED lines only. Indicates technical surface,")
    w("# not skill. A polyglot count is not automatically better than depth.")
    w("language_mix:")
    tot_lang = sum(lang.values())
    for lg, n in lang.most_common(12):
        w("  - {language: %s, hand_lines: %d, pct: %.0f}"
          % (yq(lg), n, 100.0 * n / max(1, tot_lang)))
    w("")

    w("delivery:")
    if cycles:
        w("  cycle_time_days:")
        w("    median: %.1f" % pct(cycles, 0.5))
        w("    p90: %.1f" % pct(cycles, 0.9))
        w("    max: %.1f" % cycles[-1])
        w("    n_merged: %d" % len(cycles))
    else:
        w("  cycle_time_days: null  # nothing merged in window")
    w("  test_lines: %d" % test_lines)
    w("  impl_lines: %d" % impl_lines)
    w("  caveat: >-")
    wrap(w, ("Cycle time is open-to-merge and is NOT a productivity measure: it "
             "is dominated by how fast reviewers respond, which the author does "
             "not control. A long median can indicate a reviewing bottleneck on "
             "the team, which is the manager's problem, not the author's. Test "
             "share is a rough signal only -- some work legitimately has no "
             "tests, and high test volume is not automatically rigor."), "    ")
    w("")

    if ccache:
        subs = []
        for p in PRS:
            for au, msg in (ccache.get("%s#%d" % (p["repo"], p["number"])) or []):
                if au == login:
                    subs.append((p["repo"], p["number"], msg))
        if subs:
            w("# Commit subjects this person authored. THE MOST USEFUL SECTION for")
            w("# writing a review: line counts say how much, these say WHAT. Read")
            w("# them before quoting any number.")
            w("authored_commit_subjects:")
            w("  count: %d" % len(subs))
            w("  note: >-")
            wrap(w, ("Filtered to commits whose GitHub author is this person, so "
                     "co-authored PRs do not misattribute. Conventional-commit "
                     "prefixes (feat/fix/refactor/test/docs/chore) are a rough "
                     "guide to the KIND of work, if the project uses them."),
                 "    ")
            kinds = Counter()
            for _, _, m in subs:
                mm = re.match(r"^(\w+)(\([^)]*\))?!?:", m)
                kinds[mm.group(1).lower() if mm else "(unprefixed)"] += 1
            w("  by_conventional_prefix:")
            for k, n in kinds.most_common():
                w("    - {prefix: %s, commits: %d}" % (yq(k), n))
            w("  items:")
            for repo, num, m in subs[:200]:
                w("    - {repo: %s, pr: %d, subject: %s}"
                  % (yq(repo), num, yq(m)))
            if len(subs) > 200:
                w("    # ... %d more not listed" % (len(subs) - 200))
            w("")

    w("cadence:")
    w("  merged_by_month:")
    for m in sorted(monthly):
        w("    %s: %d" % (m, monthly[m]))
    w("  opened_by_month:")
    for m in sorted(monthly_o):
        w("    %s: %d" % (m, monthly_o[m]))
    w("  months_active: %d" % len(monthly_o))
    w("")

    w("by_repo:")
    for r, s in sorted(by_repo.items(), key=lambda x: (-x[1]["total"], x[0])):
        w("  - repo: %s" % yq(r))
        w("    prs_total: %d" % s["total"])
        w("    prs_merged: %d" % s["merged"])
        w("    hand_additions: %d" % s["hand"])
        w("    generated_additions: %d" % s["gen"])
        w("    vendored_additions: %d" % s["ven"])
        w("    deletions: %d" % s["dele"])
        w("    is_fork: %s" % ("true" if s["fork"] else "false"))
    w("")

    w("reviews:")
    w("  total: %d" % rev_true)
    w("  detail_captured: %d" % len(REVIEWS))
    w("  truncated: %s" % ("true" if rev_trunc else "false"))
    w("  ratio_to_own_prs: %.2f" % (rev_true / len(PRS)))
    w("  interpretation: >-")
    wrap(w, ("A high ratio means this person spends much of their time "
             "unblocking others. That is force multiplication and is easy to "
             "miss when reading authored volume alone."), "    ")
    w("  by_repo:%s" % ("  # SAMPLE ONLY -- truncated" if rev_trunc else ""))
    for r, c in rev_repo.most_common(30):
        w("    - {repo: %s, reviews: %d}" % (yq(r), c))
    w("  top_authors_reviewed:")
    for au, c in rev_auth.most_common(20):
        w("    - {author: %s, reviews: %d}" % (yq(au), c))
    w("")

    top = sorted([p for p in canon], key=lambda x: -x["hand_additions"])[:10]
    w("# Largest hand-authored canonical-repo PRs. Candidate citations --")
    w("# read the title and check the diff; size alone is not significance.")
    w("largest_hand_authored:")
    for p in top:
        w("  - repo: %s" % yq(p["repo"]))
        w("    number: %d" % p["number"])
        w("    title: %s" % yq(p["title"]))
        w("    url: %s" % yq(p["url"]))
        w("    status: %s" % ("MERGED" if p.get("merged_at")
                              else p["state"].upper()))
        w("    merged_at: %s" % yq(p["merged_at"][:10] if p.get("merged_at")
                                   else None))
        w("    hand_additions: %d" % p["hand_additions"])
        w("    generated_additions: %d" % p["generated_additions"])
        w("    vendored_additions: %d" % p["vendored_additions"])
        w("    changed_files: %d" % p.get("changed_files", 0))
        w("    review_comments: %d" % p.get("review_comments", 0))
    w("")

    w("open_and_wip:")
    w("  count: %d" % len(open_prs))
    w("  hand_additions_parked: %d" % hand_o)
    w("  probe: >-")
    wrap(w, ("Ask about the long-lived ones: blocked on upstream review, "
             "deprioritized, or abandoned? Large parked PRs can indicate an "
             "unblocking problem rather than an output problem."), "    ")
    w("  items:")
    if not open_prs:
        w("    []")
    for p in sorted(open_prs, key=lambda x: x["created_at"]):
        w("    - repo: %s" % yq(p["repo"]))
        w("      number: %d" % p["number"])
        w("      title: %s" % yq(p["title"]))
        w("      url: %s" % yq(p["url"]))
        w("      opened: %s" % yq(p["created_at"][:10]))
        w("      age_days: %d" % (scan_d - datetime.date(
            *map(int, p["created_at"][:10].split("-")))).days)
        w("      hand_additions: %d" % p["hand_additions"])
    w("")

    w("issues_opened:")
    w("  count: %d" % len(ISSUES))
    w("  items:")
    if not ISSUES:
        w("    []")
    for i in sorted(ISSUES, key=lambda x: x["created_at"]):
        t = i["title"]
        kind = ("version-bump" if re.match(r"(?i)^(update|bump)\b.*\bto v?\d", t)
                else "membership-request" if "membership" in t.lower()
                else "feature-or-bug-report")
        w("    - repo: %s" % yq(i["repo"]))
        w("      number: %d" % i["number"])
        w("      title: %s" % yq(t))
        w("      url: %s" % yq(i["url"]))
        w("      opened: %s" % yq(i["created_at"][:10]))
        w("      state: %s" % i["state"])
        w("      comments: %d" % i["comments"])
        w("      kind: %s  # routine bumps are not evidence of design work" % kind)
    w("")

    w("# Every authored PR. Primary machine-readable record.")
    w("entries:")
    for n, p in enumerate(sorted(PRS, key=lambda x: x["created_at"]), 1):
        st = "MERGED" if p.get("merged_at") else p["state"].upper()
        w("  - id: github-%s-%s-%03d" % (pid, scan, n))
        w("    scan_date: %s" % yq(scan))
        w("    source: github")
        w("    id_ref: %s" % yq(pid))
        w("    date: %s" % yq((p.get("merged_at") or p["created_at"])[:10]))
        w("    summary: %s" % yq("%s %s#%d: %s"
                                 % (st.capitalize(), p["repo"], p["number"],
                                    p["title"])))
        w("    evidence_links: [%s]" % yq(p["url"]))
        w("    raw:")
        w("      repo: %s" % yq(p["repo"]))
        w("      pr_number: %d" % p["number"])
        w("      title: %s" % yq(p["title"]))
        w("      role: author")
        w("      status: %s" % st)
        w("      created_at: %s" % yq(p["created_at"][:10]))
        w("      merged_at: %s" % yq(p["merged_at"][:10]
                                     if p.get("merged_at") else None))
        w("      hand_additions: %d" % p["hand_additions"])
        w("      generated_additions: %d" % p["generated_additions"])
        w("      vendored_additions: %d" % p["vendored_additions"])
        w("      deletions: %d" % p.get("deletions", 0))
        w("      changed_files: %d" % p.get("changed_files", 0))
        w("      review_comments: %d" % p.get("review_comments", 0))
        w("      is_fork: %s" % ("true" if p["_fork"] else "false"))
        if p.get("labels"):
            w("      labels: [%s]" % ", ".join(yq(x) for x in p["labels"]))

    out = os.path.join(cfg["outdir"], "%s-evidence.yaml" % pid)
    open(out, "w").write("\n".join(L) + "\n")
    print("%-20s -> %-32s hand_canonical_merged=%-8d roles=%d"
          % (login, os.path.basename(out), hand_cm, len(roles)))

    return {
        "id": pid, "login": login, "file": os.path.basename(out),
        "name": P.get("name"), "team": P.get("team"), "level": P.get("level"),
        "manager": P.get("manager"),
        "prs_authored": len(PRS), "prs_merged": len(merged),
        "merge_rate_pct": round(100.0 * len(merged) / len(PRS), 1),
        "repos_touched": len(by_repo), "reviews_given": rev_true,
        "review_ratio": round(rev_true / len(PRS), 2),
        "hand_canonical_merged": hand_cm, "hand_total": hand,
        "generated_excluded": gen, "vendored_excluded": ven,
        "roles": len(roles), "external_prs": len(ext), "fork_prs": len(forks),
        "comment_threads": cmt_true,
        "cycle_median": round(pct(cycles, 0.5), 1) if cycles else None,
        "test_share": round(100.0 * test_lines / max(1, test_lines + impl_lines)),
        "top_language": (lang.most_common(1)[0][0] if lang else None),
        "identity": "documented" if P.get("identity_evidence") else "UNDOCUMENTED",
        "truncated": rev_trunc or pr_trunc,
    }


def build_index(rows, cfg):
    L = []
    w = L.append
    w("# COHORT INDEX -- GitHub performance evidence, scan %s" % cfg["scan_date"])
    w("# Window: %s to %s" % (cfg["window"]["start"], cfg["window"]["end"]))
    w("#")
    w("# START HERE, then open the per-person file for detail.")
    w("# All people scanned in one batch with the same classifier, so figures")
    w("# ARE comparable within this cohort (and only within it).")
    w("---")
    w("scan:")
    w("  scan_date: %s" % yq(cfg["scan_date"]))
    w("  window_start: %s" % yq(cfg["window"]["start"]))
    w("  window_end: %s" % yq(cfg["window"]["end"]))
    w("  classifier_version: %s" % cfg["classifier_version"])
    w("  people: %d" % len(rows))
    w("  scope: >-")
    wrap(w, cfg["scope_note"], "    ")
    w("")
    w("how_to_use:")
    w("  best_volume_metric: hand_canonical_merged")
    w("  why: >-")
    wrap(w, ("Hand-authored, merged, in the canonical upstream repo. Excludes "
             "generated code, vendored content, unmerged work and fork "
             "duplicates. Most conservative defensible figure."), "    ")
    w("  do_not_use: raw additions from the GitHub UI or API")
    w("  volume_is_not_impact: >-")
    wrap(w, ("Rankings below order activity, not value. A small change to a "
             "generator or shared library can outweigh thousands of leaf-level "
             "lines. Read ownership_roles, external_upstream_contributions and "
             "reviews_given before forming a view."), "    ")
    w("  signals_beyond_volume: >-")
    wrap(w, ("reviews_given, comment_threads_on_others_work, "
             "ownership_role_count and external_upstream_prs all capture "
             "contribution that authored volume cannot see. Someone with modest "
             "line counts and high review + discussion load is holding the team "
             "up, not underperforming."), "    ")
    w("  cycle_time_warning: >-")
    wrap(w, ("cycle_time_days_median measures open-to-merge latency, which is "
             "mostly a function of reviewer responsiveness. Do not read it as "
             "individual speed; a high team-wide median is a process finding."),
         "    ")
    w("  low_volume_is_a_question: >-")
    wrap(w, ("Low GitHub volume is never a finding on its own. The work may "
             "live in internal code review, on-call, incident response, design, "
             "or mentoring, none of which appear here. Ask before concluding."),
         "    ")
    w("")
    w("cohort:")
    for r in rows:
        w("  - id: %s" % r["id"])
        w("    github_login: %s" % r["login"])
        w("    file: %s" % r["file"])
        for k in ("name", "team", "level", "manager"):
            if r.get(k):
                w("    %s: %s" % (k, yq(r[k])))
        w("    prs_authored: %d" % r["prs_authored"])
        w("    prs_merged: %d" % r["prs_merged"])
        w("    merge_rate_pct: %s" % r["merge_rate_pct"])
        w("    repos_touched: %d" % r["repos_touched"])
        w("    reviews_given: %d" % r["reviews_given"])
        w("    comment_threads_on_others_work: %d" % r["comment_threads"])
        w("    review_to_authorship_ratio: %s" % r["review_ratio"])
        w("    hand_canonical_merged: %d" % r["hand_canonical_merged"])
        w("    hand_total: %d" % r["hand_total"])
        w("    generated_excluded: %d" % r["generated_excluded"])
        w("    vendored_excluded: %d" % r["vendored_excluded"])
        w("    cycle_time_days_median: %s" % (r["cycle_median"]
                                               if r["cycle_median"] is not None
                                               else "null"))
        w("    test_line_share_pct: %d" % r["test_share"])
        w("    primary_language: %s" % yq(r["top_language"]))
        w("    ownership_role_count: %d" % r["roles"])
        w("    external_upstream_prs: %d" % r["external_prs"])
        w("    fork_prs: %d" % r["fork_prs"])
        w("    identity_confidence: %s" % r["identity"])
        w("    has_truncation: %s" % ("true" if r["truncated"] else "false"))
    w("")
    w("# Each ranking measures something DIFFERENT. None measures impact.")
    w("rankings:")
    for key, label in (("hand_canonical_merged", "shipped authored volume"),
                       ("reviews_given", "review load / force multiplication"),
                       ("comment_threads", "discussion reach on others' work"),
                       ("prs_merged", "merged PR count"),
                       ("repos_touched", "breadth"),
                       ("roles", "formal ownership granted by others")):
        w("  by_%s:  # %s" % (key, label))
        for r in sorted(rows, key=lambda x: -x[key]):
            w("    - {id: %s, value: %d}" % (r["id"], r[key]))
    w("")
    w("cross_cohort_notes:")
    und = [r["id"] for r in rows if r["identity"] == "UNDOCUMENTED"]
    if und:
        w("  - id: undocumented_identity")
        w("    severity: high")
        w("    detail: >-")
        wrap(w, ("No identity evidence recorded for: %s. A GitHub login is not "
                 "a person. Confirm before using those files in a review."
                 % ", ".join(und)), "      ")
    tr = [r["id"] for r in rows if r["truncated"]]
    if tr:
        w("  - id: truncated_data")
        w("    severity: high")
        w("    detail: >-")
        wrap(w, ("Hit a search-API ceiling for: %s. Headline totals are correct "
                 "but per-repo breakdowns are samples. See each file's caveats."
                 % ", ".join(tr)), "      ")
    levels = {r.get("level") for r in rows if r.get("level")}
    if len(levels) > 1:
        w("  - id: mixed_levels")
        w("    severity: medium")
        w("    detail: >-")
        wrap(w, ("This cohort spans %s. Compare each person against their level "
                 "expectations, not against each other."
                 % ", ".join(sorted(levels))), "      ")
    w("  - id: repo_count_not_portable")
    w("    severity: low")
    w("    detail: >-")
    wrap(w, ("repos_touched is not comparable across ecosystems. A project split "
             "into 100 small repos inflates it relative to a monorepo. Check "
             "each person's by_repo before reading breadth into it."), "      ")
    w("  - id: public_github_only")
    w("    severity: high")
    w("    detail: >-")
    wrap(w, cfg["scope_note"], "      ")

    out = os.path.join(cfg["outdir"], "COHORT-INDEX.yaml")
    open(out, "w").write("\n".join(L) + "\n")
    print("\ncohort index -> %s" % out)


DEFAULT_SCOPE = (
    "PUBLIC GITHUB ONLY. Excludes private repos, internal code review systems, "
    "ticketing, on-call load, incident response, design documents, and mentoring. "
    "For many organizations this is a MINORITY of an engineer's total output. "
    "Merge with internal sources before drawing conclusions.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--roster", required=True)
    ap.add_argument("--scan-date", default=str(datetime.date.today()))
    ap.add_argument("--own-orgs", nargs="*", default=[],
                    help="orgs owned by your company; PRs elsewhere count as "
                         "external upstream (default: infer from repo owners)")
    ap.add_argument("--external-star-floor", type=int, default=100)
    ap.add_argument("--ownership-repo-limit", type=int, default=8)
    ap.add_argument("--scope-note", default=DEFAULT_SCOPE)
    a = ap.parse_args()

    roster = json.load(open(a.roster))
    pat, _ = load_patterns(a.outdir)
    bucket = build_bucket(pat)
    cache = json.load(open(os.path.join(a.outdir, "filecache.json")))
    cpath = os.path.join(a.outdir, "commitcache.json")
    ccache = json.load(open(cpath)) if os.path.exists(cpath) else {}
    if ccache:
        print("commit subjects available for %d PRs" % len(ccache))

    own = {o.lower() for o in a.own_orgs}
    if not own:
        c = Counter()
        for f in glob.glob(os.path.join(a.outdir, "*.json")):
            if os.path.basename(f) in SKIP_FILES:
                continue
            b = json.load(open(f))
            for p in b.get("prs", []):
                c[p["repo"].split("/")[0].lower()] += 1
        own = {o for o, _ in c.most_common(3)}
        print("inferred own_orgs=%s (override with --own-orgs)" % sorted(own))

    cfg = {
        "scan_date": a.scan_date, "outdir": a.outdir,
        "window": roster["window"], "own_orgs": own,
        "external_star_floor": a.external_star_floor,
        "ownership_repo_limit": a.ownership_repo_limit,
        "scope_note": a.scope_note,
        "classifier_version": pat.get("version", 1),
    }

    ids = [(p.get("id") or p["github_login"]) for p in roster["people"]]
    rmeta, ometa, rows = {}, {}, []
    for pid in ids:
        path = os.path.join(a.outdir, "%s.json" % pid)
        if not os.path.exists(path):
            print("SKIP %s -- no bundle (run fetch.py)" % pid)
            continue
        r = build_person(path, cfg, bucket, cache, rmeta, ometa, ccache)
        if r:
            rows.append(r)

    if not rows:
        print("\nNothing built: no person had any PRs in the window.")
        print("Check the window dates, the logins, and `gh auth status`.")
        print("Note that a person with no public PRs may still be doing")
        print("substantial work that GitHub cannot see.")
        return 1
    build_index(rows, cfg)
    print("\nValidate: python3 -c \"import yaml,glob;[yaml.safe_load(open(f)) "
          "for f in glob.glob('%s/*.yaml')];print('ok')\"" % a.outdir)


if __name__ == "__main__":
    sys.exit(main())
