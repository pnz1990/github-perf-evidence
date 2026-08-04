#!/usr/bin/env python3
"""Classify review comments to count real defects caught -- both sides of the review.

Answers two questions no count-based metric can:
  - As a REVIEWER, how often does this person catch something that mattered?
  - As an AUTHOR, is this person getting substantive review at all?

Why an LLM and not keywords: on real PRs, explicit acknowledgements ("good
catch", "fixed") appear in only 0-2 comments out of 8-30. A keyword scan
undercounts by an order of magnitude and cannot tell "this over-rejects on
non-branch-aware input" (a real bug) from "nit: move this to validation.go"
(style). Classification needs to read the comment.

Three steps, same split as insights.py -- detectors gather, the model judges:

  python3 comments.py --outdir OUT --fetch              # pull comment bodies
  python3 comments.py --outdir OUT --prompt > p.txt     # batched classify prompt
  python3 comments.py --outdir OUT --load answers.json  # attach + aggregate

READ THIS BEFORE USING THE AUTHOR-SIDE NUMBERS
----------------------------------------------
"Defects found in X's code" is a metric that punishes the wrong behaviour. It
goes UP when someone writes ambitious code, posts early for feedback, or has
thorough reviewers -- all things you want. It goes DOWN when someone writes
trivial code or gets rubber-stamped. So this tool reports the author side as
`review_rigor_received`: evidence the review process is working ON their
changes, not evidence they are careless. Never rank people on it.

The reviewer side (`defects_caught`) is a fairer signal, but it is still bounded
by what they were asked to review and by how hard the code was.

Comment bodies are far more sensitive than counts. commentcache.json contains
verbatim engineer-written text; treat it as confidential.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict

SKIP = ("filecache.json", "patterns.json", "commitcache.json",
        "reviewcache.json", "ownership.json", "insights.json",
        "commentcache.json", "comment-analysis.json", "COHORT-INDEX.json")

BOT_RE = re.compile(
    r"\[bot\]$|(^|[-_])(bot|robot)([-_]|$)|[-_](bot|robot)\d*$"
    r"|^(dependabot|renovate|copilot|github-actions|web-flow|ack-bot|"
    r"k8s-ci-robot|tide|codecov|sonarcloud)", re.I)

PACE = float(os.environ.get("PERF_COMMENT_PACE", "0.35"))
MAX_BODY = 700          # enough to judge; keeps prompts affordable


def gh(args, retries=4):
    delay = 8.0
    for attempt in range(retries):
        r = subprocess.run(["gh"] + args, capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        err = (r.stderr or "").lower()
        transient = any(t in err for t in ("rate limit", "429", "403",
                                           "secondary", "abuse", "timeout",
                                           "502", "503"))
        if not transient or attempt == retries - 1:
            return None          # caller records the gap rather than guessing
        sys.stderr.write("    rate-limited, retrying in %.0fs\n" % delay)
        time.sleep(delay)
        delay *= 2
    return None


def bundles(outdir):
    for f in sorted(glob.glob(os.path.join(outdir, "*.json"))):
        if os.path.basename(f) in SKIP:
            continue
        b = json.load(open(f))
        if "prs" in b:
            yield b


def fetch(outdir, max_prs, per_person):
    """Cache inline review comment bodies for PRs the cohort authored or reviewed."""
    path = os.path.join(outdir, "commentcache.json")
    cache = json.load(open(path)) if os.path.exists(path) else {}

    # Prefer PRs with known review activity: those are where defects surface.
    rpath = os.path.join(outdir, "reviewcache.json")
    rcache = json.load(open(rpath)) if os.path.exists(rpath) else {}

    def activity(key):
        e = rcache.get(key) or {}
        return len(e.get("inline") or [])

    targets, seen = [], set()
    for b in bundles(outdir):
        pid = b["person"].get("id") or b["person"]["github_login"]
        cand = []
        for p in b["prs"]:
            cand.append(("%s#%d" % (p["repo"], p["number"]), p["repo"],
                         p["number"], "authored", pid))
        for r in b.get("reviews", []):
            cand.append(("%s#%d" % (r["repo"], r["number"]), r["repo"],
                         r["number"], "reviewed", pid))
        cand.sort(key=lambda c: -activity(c[0]))
        for c in cand[:per_person]:
            if c[0] not in seen:
                seen.add(c[0])
                targets.append(c)

    targets.sort(key=lambda c: -activity(c[0]))
    todo = [t for t in targets if t[0] not in cache][:max_prs]
    print("PRs with review activity to scan: %d  (already cached %d)"
          % (len(todo), len(targets) - len(todo)), file=sys.stderr)
    if len(targets) > max_prs + (len(targets) - len(todo)):
        print("NOTE: capped at --max-prs %d, ordered by review activity so the "
              "densest PRs are covered first. Raise it for full coverage."
              % max_prs, file=sys.stderr)

    started = time.time()
    for i, (key, repo, num, _role, _pid) in enumerate(todo, 1):
        out = gh(["api", "--paginate",
                  "repos/%s/pulls/%d/comments" % (repo, num), "--jq",
                  '.[]|[(.user.login//"unknown"),(.id|tostring),'
                  '(.in_reply_to_id//0|tostring),(.path//""),'
                  '(.body|gsub("[\\n\\r\\t]";" "))]|@tsv'])
        if out is None:
            cache[key] = None                # unreachable, not empty
        else:
            rows = []
            for line in out.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 5:
                    who, cid, reply_to, fpath, body = parts[:5]
                    if BOT_RE.search(who):
                        continue
                    rows.append({"who": who, "id": cid,
                                 "reply_to": reply_to, "path": fpath,
                                 "body": body[:MAX_BODY]})
            cache[key] = rows
        if i % 25 == 0:
            json.dump(cache, open(path, "w"))
            rate = i / max(0.001, time.time() - started)
            print("  %d/%d  ~%d min left"
                  % (i, len(todo), (len(todo) - i) / max(0.001, rate) / 60),
                  file=sys.stderr)
        time.sleep(PACE)

    json.dump(cache, open(path, "w"))
    n = sum(len(v) for v in cache.values() if v)
    bad = sum(1 for v in cache.values() if v is None)
    print("\ncached %d comments across %d PRs (%d unreachable)"
          % (n, len(cache), bad))
    print("WARNING: %s contains verbatim engineer-written text. Confidential."
          % path)
    return 0


def threads(cache):
    """Group comments into threads so a reply can be matched to its trigger."""
    out = []
    for key, rows in sorted(cache.items()):
        if not rows:
            continue
        by_id = {r["id"]: r for r in rows}
        roots = defaultdict(list)
        for r in rows:
            root = r["reply_to"] if r["reply_to"] != "0" and r["reply_to"] in by_id \
                else r["id"]
            roots[root].append(r)
        for root, group in roots.items():
            group.sort(key=lambda r: int(r["id"]))
            out.append({"pr": key, "path": group[0].get("path", ""),
                        "comments": group})
    return out


PROMPT_HEAD = """Classify code-review threads. For each, decide what the FIRST
comment raised and whether the exchange confirms it was real.

Return STRICT JSON only:

{"classifications":[
  {"thread": "<thread id exactly as given>",
   "kind": "bug" | "design_flaw" | "correctness_risk" | "test_gap" |
           "style_nit" | "question" | "praise" | "logistics" | "other",
   "severity": "serious" | "moderate" | "minor" | "none",
   "author_acknowledged": true | false | null,
   "one_line": "<=90 chars, what was raised"}
]}

Definitions -- be strict, err toward the lower category:

  bug               a concrete defect in the proposed change: wrong logic, bad
                    edge case, nil/overflow/race, wrong error handling. Would
                    misbehave at runtime if merged.
  design_flaw       the approach itself is wrong or will not scale: wrong
                    abstraction, wrong layer, API that will need breaking
                    change, missing failure mode in the design. NOT a local
                    coding mistake.
  correctness_risk  plausible defect the reviewer is unsure about ("is there a
                    gap here for number types?"). Real concern, unconfirmed.
  test_gap          missing or insufficient test coverage.
  style_nit         naming, formatting, file placement, "nit:" prefixed.
  question          asking to understand, no defect implied.
  praise            positive only.
  logistics         rebase, CI, merge, ping, changelog, version bumps.

severity: "serious" only if merging it would likely cause an outage, data
problem, security issue, or a breaking API change. Most bugs are "moderate".

author_acknowledged: true if a LATER comment in the thread from a DIFFERENT
person than the raiser confirms it ("good catch", "fixed", "you're right",
"done"). false if they pushed back or explained why it is fine. null if the
thread has no reply.

Judge only what the text supports. If a thread is ambiguous, use "other" with
severity "none" -- an inflated count is worse than a missing one, because these
numbers land in someone's performance review.

THREADS:
"""


def emit_prompt(outdir, limit):
    cpath = os.path.join(outdir, "commentcache.json")
    if not os.path.exists(cpath):
        raise SystemExit("no commentcache.json -- run --fetch first")
    th = threads(json.load(open(cpath)))
    # Densest threads first: multi-comment exchanges are where defects live.
    th.sort(key=lambda t: -len(t["comments"]))
    th = th[:limit]
    print(PROMPT_HEAD)
    for i, t in enumerate(th):
        tid = "%s::%s" % (t["pr"], t["comments"][0]["id"])
        print("--- thread %s  (file: %s)" % (tid, t["path"] or "n/a"))
        for c in t["comments"]:
            print("  [%s] %s" % (c["who"], c["body"]))
    print("\n%d threads above. Classify every one." % len(th))
    return 0


def aggregate(outdir, classifications):
    """Attribute each classified thread to a reviewer and to an author."""
    cpath = os.path.join(outdir, "commentcache.json")
    cache = json.load(open(cpath))
    th = {("%s::%s" % (t["pr"], t["comments"][0]["id"])): t
          for t in threads(cache)}

    # PR -> author, from the bundles.
    pr_author, cohort = {}, {}
    for b in bundles(outdir):
        pid = b["person"].get("id") or b["person"]["github_login"]
        login = b["person"]["github_login"]
        cohort[login] = pid
        for p in b["prs"]:
            pr_author["%s#%d" % (p["repo"], p["number"])] = login

    DEFECT = ("bug", "design_flaw", "correctness_risk")
    rev = defaultdict(lambda: Counter())
    auth = defaultdict(lambda: Counter())
    examples = defaultdict(list)
    unmatched = 0

    for c in classifications:
        t = th.get(c.get("thread"))
        if not t:
            unmatched += 1
            continue
        raiser = t["comments"][0]["who"]
        kind = c.get("kind", "other")
        sev = c.get("severity", "none")
        ack = c.get("author_acknowledged")

        rpid = cohort.get(raiser)
        if rpid:
            r = rev[rpid]
            r["threads_raised"] += 1
            r[kind] += 1
            if kind in DEFECT:
                r["defects_caught"] += 1
                if sev == "serious":
                    r["serious_caught"] += 1
                if ack is True:
                    r["confirmed_by_author"] += 1
            if len(examples[rpid]) < 6 and kind in DEFECT:
                examples[rpid].append({
                    "pr": t["pr"], "kind": kind, "severity": sev,
                    "confirmed": ack is True,
                    "summary": str(c.get("one_line", ""))[:110]})

        apid = cohort.get(pr_author.get(t["pr"], ""))
        if apid and apid != rpid:
            aa = auth[apid]
            aa["threads_on_their_prs"] += 1
            if kind in DEFECT:
                aa["defects_found_in_their_code"] += 1
                if sev == "serious":
                    aa["serious_found"] += 1
                if ack is True:
                    aa["they_acknowledged"] += 1
            if kind == "style_nit":
                aa["nits_received"] += 1

    out = {"classified_threads": len(classifications),
           "unmatched_thread_ids": unmatched,
           "as_reviewer": {}, "as_author": {}}

    for pid, c in rev.items():
        raised = c["threads_raised"]
        out["as_reviewer"][pid] = {
            "threads_raised": raised,
            "defects_caught": c["defects_caught"],
            "serious_caught": c["serious_caught"],
            "confirmed_by_author": c["confirmed_by_author"],
            "signal_rate_pct": round(100.0 * c["defects_caught"] / max(1, raised)),
            "breakdown": {k: c[k] for k in
                          ("bug", "design_flaw", "correctness_risk", "test_gap",
                           "style_nit", "question", "praise", "logistics")
                          if c[k]},
            "examples": examples.get(pid, []),
        }
    for pid, c in auth.items():
        n = c["threads_on_their_prs"]
        out["as_author"][pid] = {
            "threads_on_their_prs": n,
            "defects_found_in_their_code": c["defects_found_in_their_code"],
            "serious_found": c["serious_found"],
            "they_acknowledged": c["they_acknowledged"],
            "nits_received": c["nits_received"],
            "review_rigor_received_pct": round(
                100.0 * c["defects_found_in_their_code"] / max(1, n)),
        }

    out["how_to_read"] = {
        "as_reviewer": (
            "defects_caught is a fair positive signal, bounded by what this "
            "person was asked to review and how hard that code was. "
            "signal_rate_pct is the share of their review threads that raised a "
            "real defect rather than a nit -- a reviewer with a low rate may "
            "simply be doing careful style review, which is also useful."),
        "as_author": (
            "This is NOT a defect-rate scorecard. The count goes UP when "
            "someone writes ambitious code, posts early for feedback, or has "
            "thorough reviewers -- all behaviours you want. It goes DOWN when "
            "someone writes trivial code or gets rubber-stamped. Read it as "
            "evidence the review process is working on their changes. A LOW "
            "number on complex work is the finding worth investigating, because "
            "it usually means nobody reviewed it properly."),
        "never": (
            "Never rank people by defects_found_in_their_code. Never present it "
            "as a quality score. Never use either number without the sample "
            "size next to it."),
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--fetch", action="store_true",
                    help="cache inline review comment bodies (CONFIDENTIAL)")
    ap.add_argument("--max-prs", type=int, default=400,
                    help="cap PRs fetched, densest review activity first")
    ap.add_argument("--per-person", type=int, default=120,
                    help="cap PRs contributed per person before global cap")
    ap.add_argument("--prompt", action="store_true",
                    help="print a classification prompt for an LLM")
    ap.add_argument("--limit", type=int, default=250,
                    help="threads per prompt (default 250)")
    ap.add_argument("--load", metavar="FILE",
                    help="attach LLM classifications and aggregate")
    a = ap.parse_args()

    if a.fetch:
        return fetch(a.outdir, a.max_prs, a.per_person)
    if a.prompt:
        return emit_prompt(a.outdir, a.limit)
    if a.load:
        raw = open(a.load, encoding="utf-8").read()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise SystemExit("no JSON object in %s" % a.load)
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError as ex:
            raise SystemExit("invalid JSON in %s: %s" % (a.load, ex))
        cl = payload.get("classifications")
        if not cl:
            raise SystemExit("%s has no 'classifications' array" % a.load)
        out = aggregate(a.outdir, cl)
        path = os.path.join(a.outdir, "comment-analysis.json")
        json.dump(out, open(path, "w"), indent=1, default=str)
        print("classified %d threads (%d ids did not match a known thread)"
              % (out["classified_threads"], out["unmatched_thread_ids"]))
        print("reviewers scored: %d   authors scored: %d"
              % (len(out["as_reviewer"]), len(out["as_author"])))
        print("-> %s" % path)
        print("\nRe-run insights.py and report.py to surface it.")
        return 0

    cpath = os.path.join(a.outdir, "commentcache.json")
    if os.path.exists(cpath):
        cache = json.load(open(cpath))
        th = threads(cache)
        print("commentcache: %d PRs, %d comments, %d threads"
              % (len(cache), sum(len(v) for v in cache.values() if v), len(th)))
        print("next: --prompt, answer it, then --load")
    else:
        print("no commentcache.json yet -- start with --fetch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
