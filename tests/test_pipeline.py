#!/usr/bin/env python3
"""Offline test suite. No network, no gh CLI, no pip installs.

Runs the whole pipeline against frozen fixtures and asserts the numbers. Written
after two real bugs shipped that fixtures would have caught instantly:

  * a shadowed variable (`ext` reused for a file extension, clobbering a list of
    PRs) -- caught here by test_no_variable_shadowing_regression
  * a helper returning raw text where a dict was expected -- caught here by
    test_gh_json_type_enforcement

Run:  python3 tests/test_pipeline.py
Exit code is non-zero if anything fails, so it works in CI as-is.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import audit          # noqa: E402
import comments       # noqa: E402
import insights       # noqa: E402
import build          # noqa: E402
import classify       # noqa: E402
import discover       # noqa: E402
import report         # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         "" if cond else "  <-- " + detail))


def eq(name, got, want):
    check(name, got == want, "got %r want %r" % (got, want))


# ===========================================================================
# classify.py -- the core correctness surface
# ===========================================================================
def test_classification():
    print("\n[classify] bucket assignment")
    d = tempfile.mkdtemp()
    pat, _ = classify.load_patterns(d)
    b = classify.build_bucket(pat)

    # Generated: things a tool wrote.
    for f in ("apis/v1/zz_generated.deepcopy.go", "pkg/api/types_generated.go",
              "proto/svc.pb.go", "gen/svc_pb2.py", "config/crd/bases/x.yaml",
              "helm/crds/y.yaml", "charts/app/crds/z.yaml",
              "pkg/generated/client.go", "web/versioned_docs/v1/intro.md",
              "src/__snapshots__/App.test.js.snap",
              "observability/grafana/dashboard.json", "mock_service.go"):
        eq("generated: %s" % f, b(f), "generated")

    # Vendored: third-party content copied in.
    for f in ("vendor/github.com/x/y.go", "third_party/lib.c", "go.sum",
              "package-lock.json", "yarn.lock", "ATTRIBUTION.md", "LICENSE",
              "docs/LICENSE-APACHE", "NOTICE"):
        eq("vendored: %s" % f, b(f), "vendored")

    # Hand-authored: the false-positive guard. Each of these looks like it
    # matches a generated/vendored rule but is real authored source. A rule that
    # eats these ERASES someone's work, which is worse than inflation because it
    # is invisible and unflattering.
    for f in ("pkg/resource/parser/parser.go",       # ACK-shaped path, generic repo
              "pkg/resource/loader.go",
              "src/crds/validate.go",                # /crds/ but Go, not YAML
              "pkg/crds/logic.go",
              "charts/README.md",                    # in charts/ but authored prose
              "grafana/provisioning/datasources.yaml",
              "internal/mocks/helper.go",            # mocks/ but not *mock*
              "website/docs/tutorial.md",            # docs but hand-written
              "docs/architecture.md",
              "LICENSES-explained.md",               # not a license file
              "api/openapi-spec/custom.yaml",        # only generated w/ profile
              "swagger.json",
              "src/app.ts", "main.py", "cmd/tool/main.go", "lib/models/user.rb"):
        eq("hand: %s" % f, b(f), "hand")

    print("\n[classify] profiles are OPT-IN, not default")
    d2 = tempfile.mkdtemp()
    pat2, _ = classify.load_patterns(d2, ["aws-ack", "kubernetes"])
    b2 = classify.build_bucket(pat2)
    eq("profile makes pkg/resource/*/sdk.go generated",
       b2("pkg/resource/table/sdk.go"), "generated")
    eq("profile keeps hooks.go hand-authored",
       b2("pkg/resource/table/hooks.go"), "hand")
    eq("profile marks SDK models vendored",
       b2("testdata/codegen/sdk-codegen/aws-models/svc.json"), "vendored")
    eq("profile marks apis/v1alpha1 generated",
       b2("apis/v1alpha1/types.go"), "generated")
    check("same path differs with/without profile -- proves opt-in",
          b("pkg/resource/table/sdk.go") == "hand"
          and b2("pkg/resource/table/sdk.go") == "generated")

    print("\n[classify] patterns.json is authoritative once written")
    p = os.path.join(d, "patterns.json")
    saved = json.load(open(p))
    saved["generated"].append({"re": r"^custom_gen/", "verified": "test rule"})
    saved["version"] = 99
    json.dump(saved, open(p, "w"))
    pat3, _ = classify.load_patterns(d, ["aws-ack"])   # profile must NOT re-apply
    b3 = classify.build_bucket(pat3)
    eq("user rule honoured", b3("custom_gen/x.go"), "generated")
    eq("version preserved", pat3["version"], 99)
    eq("profile not silently merged into existing file",
       b3("pkg/resource/table/sdk.go"), "hand")
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(d2, ignore_errors=True)


# ===========================================================================
# discover.py -- bot filtering
# ===========================================================================
def test_bot_filter():
    print("\n[discover] bot detection")
    # `<project>-bot` is the case that shipped broken: a release bot authored
    # 9 of 14 PRs in a real window and would have topped the roster.
    for lg in ("someproject-bot", "dependabot[bot]", "renovate[bot]",
               "github-actions[bot]", "k8s-ci-robot", "openshift-ci-robot",
               "my-bot", "bot-runner", "snyk-bot", "release_bot", "ci-bot2",
               "web-flow", "copilot"):
        check("bot: %s" % lg, bool(discover.BOT_RE.search(lg)),
              "not detected as a bot")
    # Human logins that contain bot-ish substrings. A false positive here
    # silently deletes a person from the roster.
    # Synthetic logins chosen to cover the tricky shapes: bot-ish substrings
    # inside ordinary names, a leading digit, and a hyphenated handle.
    for lg in ("talbot", "Abbott", "abbotsford", "Botond", "robotdreams",
               "sabotage-dev", "roberto", "Robertson", "probot-dev",
               "devuser", "a-person", "42dev", "mixedCaseDev"):
        check("human: %s" % lg, not discover.BOT_RE.search(lg),
              "WRONGLY filtered as a bot")


# ===========================================================================
# build.py -- helpers that broke in production
# ===========================================================================
def test_gh_json_type_enforcement():
    print("\n[build] gh_json returns None on unusable output (real bug)")
    real = build.subprocess.run
    try:
        class R:
            def __init__(self, rc, out):
                self.returncode, self.stdout = rc, out

        build.subprocess.run = lambda *a, **k: R(0, "not json at all")
        eq("garbage -> None", build.gh_json(["api", "x"]), None)

        build.subprocess.run = lambda *a, **k: R(0, '"a string"')
        eq("wrong JSON type -> None", build.gh_json(["api", "x"]), None)

        build.subprocess.run = lambda *a, **k: R(1, '{"fork":false}')
        eq("nonzero exit -> None", build.gh_json(["api", "x"]), None)

        build.subprocess.run = lambda *a, **k: R(0, '{"fork":false}')
        eq("valid dict passes", build.gh_json(["api", "x"]), {"fork": False})
    finally:
        build.subprocess.run = real


def test_repo_meta_degrades():
    print("\n[build] repo_meta always returns a dict")
    real = build.subprocess.run
    try:
        class R:
            returncode, stdout = 1, ""
        build.subprocess.run = lambda *a, **k: R()
        m = build.repo_meta("deleted/repo", {})
        check("dict on failure", isinstance(m, dict))
        check("flagged as failed", m.get("_lookup_failed") is True)
        check("stars key present", "stars" in m)
    finally:
        build.subprocess.run = real


def test_wrap_never_splits_words():
    print("\n[build] wrap() preserves words (YAML corruption guard)")
    out = []
    long_word = "supercalifragilisticexpialidocious_" * 3
    build.wrap(out.append, "short " + long_word + " tail", "  ", width=40)
    joined = " ".join(x.strip() for x in out)
    check("no word broken mid-token", long_word in joined,
          "long token was split across lines")
    eq("all content preserved", joined, "short " + long_word + " tail")
    check("indent applied", all(l.startswith("  ") for l in out))


def test_yq_escaping():
    print("\n[build] yq() escapes YAML-breaking characters")
    eq("single quote doubled", build.yq("it's"), "'it''s'")
    eq("none -> null", build.yq(None), "null")
    check("backslash escaped", "\\\\" in build.yq("a\\b"))
    check("colon safe inside quotes", build.yq("a: b") == "'a: b'")


# ===========================================================================
# report.py -- HTML safety
# ===========================================================================
def test_html_escaping():
    print("\n[report] HTML injection is escaped")
    evil = '<script>alert(1)</script>'
    esc = report.e(evil)
    check("script tag neutralised", "<script>" not in esc, esc)
    check("entity encoded", "&lt;script&gt;" in esc)
    eq("quote escaped", report.e('a"b'), "a&quot;b")
    eq("None -> empty", report.e(None), "")
    eq("thousands separator", report.num(1234567), "1,234,567")
    eq("non-numeric passthrough", report.num("n/a"), "n/a")
    eq("None num", report.num(None), "")


def test_bar_bounds():
    print("\n[report] bar widths stay in range")
    check("zero max does not divide by zero", "width:0%" in report.bar(5, 0))
    check("max value is 100%", "width:100%" in report.bar(10, 10))
    check("tiny value still visible", "width:1%" in report.bar(1, 1000))
    check("none value safe", "width:0%" in report.bar(None, 100)
          or "width:1%" in report.bar(None, 100))


# ===========================================================================
# Full pipeline over fixtures
# ===========================================================================
def make_fixture(d, with_extras=True):
    """A synthetic cohort that exercises every code path."""
    # 3 PRs: one mostly-generated, one genuinely authored, one open fork PR.
    prs = [
        {"number": 1, "title": "feat: add widget support", "state": "closed",
         "created_at": "2026-02-01T10:00:00Z", "merged_at": "2026-02-03T10:00:00Z",
         "url": "https://github.com/acme/svc/pull/1", "repo": "acme/svc",
         "labels": ["feature"], "comments": 3, "additions": 5200,
         "deletions": 40, "changed_files": 30, "review_comments": 4,
         "stats_ok": True},
        {"number": 2, "title": "fix: correct retry backoff", "state": "closed",
         "created_at": "2026-03-01T10:00:00Z", "merged_at": "2026-03-02T10:00:00Z",
         "url": "https://github.com/acme/svc/pull/2", "repo": "acme/svc",
         "labels": [], "comments": 1, "additions": 120, "deletions": 30,
         "changed_files": 4, "review_comments": 2, "stats_ok": True},
        {"number": 7, "title": "wip: experiment", "state": "open",
         "created_at": "2026-06-01T10:00:00Z", "merged_at": None,
         "url": "https://github.com/someone/svc-fork/pull/7",
         "repo": "someone/svc-fork", "labels": [], "comments": 0,
         "additions": 300, "deletions": 0, "changed_files": 3,
         "review_comments": 0, "stats_ok": True},
    ]
    bundle = {
        "person": {"github_login": "devone", "id": "devone", "name": "Dev One",
                   "team": "Platform", "level": "senior",
                   "identity_evidence": "Directory lists this handle."},
        "window": {"start": "2026-01-01", "end": "2026-07-01"},
        "visibility": "public",
        "prs": prs,
        "reviews": [{"number": 90, "title": "someone else's PR",
                     "url": "https://github.com/acme/svc/pull/90",
                     "repo": "acme/svc", "author": "devtwo",
                     "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}],
        "issues": [{"number": 5, "title": "Bug: crash on empty input",
                    "url": "https://github.com/acme/svc/issues/5",
                    "repo": "acme/svc", "state": "open",
                    "created_at": "2026-02-10T00:00:00Z", "comments": 2}],
        "comments_on_others": [
            {"number": 91, "title": "design discussion",
             "url": "https://github.com/acme/svc/pull/91", "repo": "acme/svc",
             "author": "devtwo", "is_pr": True, "state": "open",
             "updated_at": "2026-05-01T00:00:00Z"}],
        "reviews_true_total": 1, "prs_true_total": 3, "comments_true_total": 1,
        "trend": ({"split_date": "2026-04-01",
                   "early": {"window": ["2026-01-01", "2026-04-01"], "prs": 2,
                             "prs_merged": 2, "reviews": 1,
                             "comment_threads": 0, "repos": 1},
                   "late": {"window": ["2026-04-01", "2026-07-01"], "prs": 1,
                            "prs_merged": 0, "reviews": 4,
                            "comment_threads": 1, "repos": 1}}
                  if with_extras else None),
    }
    json.dump(bundle, open(os.path.join(d, "devone.json"), "w"))

    # PR 1 is 5,000 generated + 200 authored. A naive tool reports 5,200.
    filecache = {
        "acme/svc#1": ([[5000, "apis/v1/zz_generated.deepcopy.go"]]
                       + [[200, "pkg/widget/widget.go"]]),
        "acme/svc#2": [[90, "pkg/retry/backoff.go"], [30, "pkg/retry/backoff_test.go"]],
        "someone/svc-fork#7": [[300, "pkg/exp/exp.go"]],
        "acme/svc#90": [[10, "README.md"]],
    }
    json.dump(filecache, open(os.path.join(d, "filecache.json"), "w"))

    if with_extras:
        json.dump({"acme/svc#1": [["devone", "feat: add widget support"],
                                  ["devone", "test: widget coverage"],
                                  ["other", "chore: unrelated"]],
                   "acme/svc#2": [["devone", "fix: correct retry backoff"]],
                   "someone/svc-fork#7": [["devone", "wip: experiment"]],
                   "acme/svc#90": []},
                  open(os.path.join(d, "commitcache.json"), "w"))
        json.dump({"acme/svc#90": {
            "reviews": [["devone", "CHANGES_REQUESTED", 240],
                        ["devone", "APPROVED", 0]],
            "inline": [["devone", 80], ["devone", 120], ["other", 10]]}},
            open(os.path.join(d, "reviewcache.json"), "w"))
        json.dump({"generated_from": "test", "window_start": "2026-01-01",
                   "depth": 2, "subsystems_analyzed": 2,
                   "caveat": "test caveat",
                   "bus_factor_risk": [
                       {"repo": "acme/svc", "directory": "pkg/widget",
                        "top_author": "devone", "top_author_commits": 9,
                        "commits_in_window": 10, "bus_factor": 1,
                        "distinct_authors": 2, "top_author_share_pct": 90}],
                   "by_person": {"devone": [
                       {"repo": "acme/svc", "directory": "pkg/widget",
                        "share_pct": 90, "commits": 9, "of_total": 10,
                        "bus_factor": 1}]},
                   "subsystems": []},
                  open(os.path.join(d, "ownership.json"), "w"))

    json.dump({"window": {"start": "2026-01-01", "end": "2026-07-01"},
               "people": [{"github_login": "devone", "id": "devone",
                           "name": "Dev One", "team": "Platform",
                           "level": "senior",
                           "identity_evidence": "Directory lists this handle."}]},
              open(os.path.join(d, "roster.json"), "w"))
    return d


def run(mod_argv, mod):
    old = sys.argv
    sys.argv = mod_argv
    try:
        return mod.main()
    finally:
        sys.argv = old


def test_full_pipeline():
    print("\n[pipeline] classify -> audit -> build -> report over fixtures")
    d = make_fixture(tempfile.mkdtemp())

    run(["classify.py", "--outdir", d], classify)
    bundle = json.load(open(os.path.join(d, "devone.json")))
    by_num = {p["number"]: p for p in bundle["prs"]}

    # THE central assertion: a naive tool reports 5,200 for PR 1.
    eq("PR1 hand additions", by_num[1]["hand_additions"], 200)
    eq("PR1 generated excluded", by_num[1]["generated_additions"], 5000)
    eq("PR2 fully hand-authored", by_num[2]["hand_additions"], 120)

    run(["audit.py", "--outdir", d, "--check-forks"], audit)
    check("audit report written", os.path.exists(os.path.join(d, "audit-report.txt")))

    # build.py hits the network for repo metadata; stub it deterministically.
    real = build.subprocess.run

    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def fake(args, **kw):
        joined = " ".join(args)
        if "repos/someone/svc-fork" in joined and "contents" not in joined:
            return R(0, json.dumps({"fork": True, "parent": "acme/svc",
                                    "stars": 0, "owner": "someone"}))
        if "repos/acme/svc" in joined and "contents" not in joined:
            return R(0, json.dumps({"fork": False, "parent": None,
                                    "stars": 4200, "owner": "acme"}))
        if "contents" in joined:
            return R(1, "")          # no OWNERS files
        return R(1, "")

    try:
        build.subprocess.run = fake
        run(["build.py", "--outdir", d, "--roster",
             os.path.join(d, "roster.json"), "--scan-date", "2026-07-01",
             "--own-orgs", "acme"], build)
    finally:
        build.subprocess.run = real

    pj = json.load(open(os.path.join(d, "devone-evidence.json")))
    s = pj["summary"]
    eq("hand total", s["hand_additions_total"], 620)          # 200+120+300
    eq("generated excluded", s["generated_additions_excluded"], 5000)
    eq("raw total", s["raw_additions_total"], 5620)
    eq("canonical merged only", s["hand_additions_canonical_merged"], 320)
    eq("fork lines separated", s["hand_additions_in_forks"], 300)
    eq("prs merged", s["prs_merged"], 2)
    check("fork excluded from canonical",
          s["hand_additions_canonical_merged"] < s["hand_additions_total"])

    ids = [c["id"] for c in pj["metadata"]["caveats"]]
    for want in ("line_count_inflation", "volume_is_not_impact",
                 "fork_prs_may_double_count"):
        check("caveat present: %s" % want, want in ids, str(ids))

    rd = pj["review_depth"]
    check("review depth captured", isinstance(rd, dict) and rd)
    if isinstance(rd, dict) and rd:
        eq("inline comments (mine only)", rd["inline_comments"], 2)
        eq("changes requested", rd["changes_requested"], 1)
        eq("bare approvals", rd["bare_approvals"], 1)

    tj = pj["trajectory"]
    check("trajectory present", isinstance(tj, dict) and tj)
    if isinstance(tj, dict) and tj:
        eq("reviews trend up", tj["reviews_given"]["change"], "+300%")

    dfo = pj["de_facto_ownership"]["items"]
    eq("de-facto ownership carried", len(dfo), 1)

    acs = pj["authored_commit_subjects"]
    check("commit subjects present", isinstance(acs, dict) and acs)
    if isinstance(acs, dict) and acs:
        eq("only own commits attributed", acs["count"], 4)

    idx = json.load(open(os.path.join(d, "COHORT-INDEX.json")))
    eq("cohort size", len(idx["cohort"]), 1)
    check("rankings present", "by_hand_canonical_merged" in idx["rankings"])
    check("bus factor risk carried", len(idx["bus_factor_risk"]) == 1)

    run(["report.py", "--outdir", d], report)
    hp = os.path.join(d, "report.html")
    check("html written", os.path.exists(hp))
    h = open(hp, encoding="utf-8").read()
    check("html non-trivial", len(h) > 6000, "%d bytes" % len(h))
    check("self-contained (no external fetch)",
          "http-equiv" not in h and "<script src" not in h
          and "<link" not in h)
    check("shows the quotable number", "320" in h)
    check("shows raw struck through", "kpi never" in h)
    check("impact warning present", "not impact" in h.lower())
    check("low-numbers warning present", "question to ask" in h.lower())
    check("team-risk tab rendered", "Team risk" in h)
    check("person tab rendered", "Dev One" in h)
    check("caveats rendered as banners", h.count('class="banner') >= 4)

    # Field-name regressions between build.py's JSON mirror and report.py.
    # These shipped broken once: the login column was empty and the discussion
    # column showed a dash despite real data, because report.py read key names
    # the mirror does not use.
    check("login rendered in cohort table", "@devone" in h,
          "login column empty -- field-name mismatch")
    check("discussion count rendered (not a dash)",
          ">1</td>" in h or "data-v=\"1\"" in h)
    check("no double-escaped HTML entities", "&amp;mdash;" not in h,
          "an entity was passed through e() twice")
    check("identity confidence rendered", "documented" in h)
    check("inline-per-pr column rendered", "1.0" in h or "2.0" in h)
    # Guard the actual contract: every index key report.py reads for the cohort
    # table must exist in the mirror. Checking the source text for stale names is
    # too blunt -- the per-person JSON legitimately uses different key names for
    # the same concepts.
    idx_keys = set(idx["cohort"][0].keys())
    for used in ("login", "comment_threads", "roles", "external_prs",
                 "identity", "inline_per_pr", "hand_canonical_merged"):
        check("index mirror provides %s" % used, used in idx_keys,
              "report.py depends on it")

    shutil.rmtree(d, ignore_errors=True)


def test_stale_pipeline_guard():
    """Re-running fetch.py clears classification. build.py must say so, not die
    with a bare KeyError far from the cause."""
    print("\n[pipeline] stale-pipeline guard (real trap)")
    d = make_fixture(tempfile.mkdtemp())
    # Deliberately skip classify.py.
    err = None
    try:
        run(["build.py", "--outdir", d, "--roster",
             os.path.join(d, "roster.json")], build)
    except SystemExit as ex:
        err = str(ex)
    except KeyError as ex:
        err = "KeyError:" + repr(ex)
    check("guard fires", err is not None, "build.py accepted unclassified data")
    check("message is actionable, not a KeyError",
          err is not None and "classify.py" in err, repr(err))
    check("no bare KeyError", not (err or "").startswith("KeyError"), repr(err))
    shutil.rmtree(d, ignore_errors=True)


def test_review_depth_keys_match_report():
    """review_depth is written flat; report.py must read the flat names. This
    shipped broken: the HTML showed empty values for two KPIs."""
    print("\n[report] review_depth field names line up")
    src = open(os.path.join(SCRIPTS, "report.py")).read()
    check("report reads inline_per_pr", 'rdd.get("inline_per_pr")' in src)
    check("report reads flat changes_requested",
          'rdd.get("changes_requested")' in src)
    check("report does NOT read a nested verdicts dict",
          'rdd.get("verdicts")' not in src,
          "review_depth has no nested verdicts key in the JSON mirror")


def test_trend_windows_do_not_overlap():
    """GitHub date ranges are inclusive at both ends, so a naive split counts the
    split date twice and the halves sum to more than the whole."""
    print("\n[fetch] trend halves do not double-count the split date")
    sys.path.insert(0, SCRIPTS)
    import fetch                              # noqa: E402
    eq("day_before shifts back one day", fetch.day_before("2026-06-17"),
       "2026-06-16")
    mid = fetch.midpoint("2026-01-01", "2026-07-01")
    early_end = fetch.day_before(mid)
    check("early end is strictly before the split", early_end < mid,
          "%s !< %s" % (early_end, mid))
    check("no gap between halves",
          (__import__("datetime").date(*map(int, mid.split("-")))
           - __import__("datetime").date(*map(int, early_end.split("-")))).days == 1)


def test_ownership_timeout_is_bounded():
    """A hot directory in a large monorepo can paginate for minutes. An unbounded
    call makes the script look hung, so timeouts must be caught and reported."""
    print("\n[ownership] per-directory calls are bounded")
    sys.path.insert(0, SCRIPTS)
    import ownership                          # noqa: E402
    real = ownership.subprocess.run
    try:
        def boom(*a, **k):
            raise ownership.subprocess.TimeoutExpired(cmd="gh", timeout=1)
        ownership.subprocess.run = boom
        eq("timeout returns None (distinct from empty)",
           ownership.gh_lines(["api", "x"], timeout=1), None)
    finally:
        ownership.subprocess.run = real

    class R:
        returncode, stdout = 1, ""
    try:
        ownership.subprocess.run = lambda *a, **k: R()
        eq("api failure returns empty list, not None",
           ownership.gh_lines(["api", "x"]), [])
    finally:
        ownership.subprocess.run = real

    eq("dir_of depth 1", ownership.dir_of("pkg/api/types.go", 1), "pkg")
    eq("dir_of depth 2", ownership.dir_of("pkg/api/types.go", 2), "pkg/api")
    eq("dir_of top-level file", ownership.dir_of("main.go", 2), "(repo root)")
    check("ownership filters bots", bool(ownership.BOT_RE.search("ci-bot")))


def test_fetch_retries_transient_and_fails_loud():
    """The worst possible failure mode: a rate-limited search returning "" so
    total_count() reads a non-numeric string and records 0. That silently reports
    "this person did nothing" into someone's performance review.

    Observed live: a full 8-person scan tripped GitHub's secondary rate limit
    mid-run. Transient rejections are EXPECTED at this call volume, so they must
    be retried; everything else must fail loudly rather than degrade to zero."""
    print("\n[fetch] rate-limit retry + fail-loud (real incident)")
    sys.path.insert(0, SCRIPTS)
    import fetch                              # noqa: E402

    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    real_run, real_sleep = fetch.subprocess.run, fetch.time.sleep
    try:
        fetch.time.sleep = lambda _s: None    # keep the suite fast

        calls = []

        def flaky(a, **k):
            calls.append(1)
            if len(calls) < 3:
                return R(1, "", "API rate limit exceeded (HTTP 403)")
            return R(0, '{"ok":1}')
        fetch.subprocess.run = flaky
        got = fetch.gh(["api", "x"])
        check("transient 403 is retried, not swallowed", '{"ok":1}' in got, got)
        check("retried more than once", len(calls) == 3, str(len(calls)))

        for err in ("secondary rate limit", "429 Too Many Requests",
                    "abuse detection", "502 Bad Gateway", "timeout"):
            calls[:] = []

            def only_transient(a, _e=err, **k):
                calls.append(1)
                return (R(1, "", _e) if len(calls) < 2 else R(0, "ok"))
            fetch.subprocess.run = only_transient
            check("treated as transient: %s" % err,
                  fetch.gh(["api", "x"]) == "ok")

        # Permanent errors must raise, never return "".
        for err in ("Not Found (HTTP 404)", "Bad credentials (HTTP 401)"):
            fetch.subprocess.run = lambda a, _e=err, **k: R(1, "", _e)
            raised = False
            try:
                fetch.gh(["api", "x"])
            except RuntimeError:
                raised = True
            check("permanent error raises: %s" % err, raised,
                  "returned instead of raising -- would degrade to zero")

        # total_count must never coerce junk into a number.
        fetch.subprocess.run = lambda a, **k: R(0, "not-a-number")
        raised = False
        try:
            fetch.total_count("q")
        except RuntimeError:
            raised = True
        check("total_count refuses non-numeric output", raised,
              "a corrupted count silently becomes 'no activity'")

        fetch.subprocess.run = lambda a, **k: R(0, "42\n")
        eq("total_count parses a real number", fetch.total_count("q"), 42)
    finally:
        fetch.subprocess.run, fetch.time.sleep = real_run, real_sleep

    check("search calls are paced under the 30/min ceiling",
          fetch.SEARCH_PACE >= 2.0,
          "pace %s would exceed the limit" % fetch.SEARCH_PACE)


def test_insights_detectors():
    """Detectors must be deterministic and must never fabricate. Every number an
    insight cites has to trace back to detector output, which is why the LLM
    layer is separated from this one."""
    print("\n[insights] detectors run over fixtures")
    d = make_fixture(tempfile.mkdtemp())
    run(["classify.py", "--outdir", d], classify)
    real = build.subprocess.run

    class R:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout = rc, out

    def fake(args, **kw):
        j = " ".join(args)
        if "contents" in j:
            return R(1, "")
        if "repos/someone/svc-fork" in j:
            return R(0, json.dumps({"fork": True, "parent": "acme/svc",
                                    "stars": 0, "owner": "someone"}))
        if "repos/" in j:
            return R(0, json.dumps({"fork": False, "parent": None,
                                    "stars": 4200, "owner": "acme"}))
        return R(1, "")
    try:
        build.subprocess.run = fake
        run(["build.py", "--outdir", d, "--roster",
             os.path.join(d, "roster.json"), "--scan-date", "2026-07-01",
             "--own-orgs", "acme"], build)
    finally:
        build.subprocess.run = real

    rc = run(["insights.py", "--outdir", d], insights)
    eq("insights exits clean", rc, 0)
    data = json.load(open(os.path.join(d, "insights.json")))
    for k in ("release_toil", "theme_mix", "review_reciprocity",
              "knowledge_silos", "review_load_balance", "depth_vs_volume",
              "stalled_work", "trajectory_shifts", "external_visibility",
              "measurement_risk"):
        check("detector present: %s" % k, k in data["detectors"])
    check("no narrative until one is loaded", "narrative" not in data)

    # Determinism: same input, same output.
    run(["insights.py", "--outdir", d], insights)
    again = json.load(open(os.path.join(d, "insights.json")))
    eq("detectors are deterministic", json.dumps(data["detectors"], sort_keys=True),
       json.dumps(again["detectors"], sort_keys=True))

    # Bots must not be counted as collaborators -- a release bot can dominate
    # someone's thread count and make automation look like mentoring.
    for who in ("dependabot[bot]", "ack-bot", "k8s-ci-robot", "renovate[bot]"):
        check("bot excluded from reciprocity: %s" % who,
              bool(insights.BOT_RE.search(who)))
    for who in ("devuser", "talbot", "a-person"):
        check("human kept in reciprocity: %s" % who,
              not insights.BOT_RE.search(who))

    # --load must reject junk rather than shipping a broken section.
    bad = os.path.join(d, "bad.json")
    open(bad, "w").write("this is not json")
    raised = False
    try:
        run(["insights.py", "--outdir", d, "--load", bad], insights)
    except SystemExit:
        raised = True
    check("--load rejects non-JSON", raised)

    open(bad, "w").write('{"insights": []}')
    raised = False
    try:
        run(["insights.py", "--outdir", d, "--load", bad], insights)
    except SystemExit:
        raised = True
    check("--load rejects an empty insights array", raised)

    # A valid narrative attaches and survives a round trip.
    good = os.path.join(d, "good.json")
    json.dump({"insights": [{"title": "T", "audience": "team", "who": ["devone"],
                             "severity": "high", "confidence": "medium",
                             "finding": "F", "why_missed": "W",
                             "action": "A", "caveat": "C"}],
               "questions_for_1on1s": [{"who": "devone", "question": "Q?",
                                        "because": "B"}],
               "do_not_conclude": ["X"]}, open(good, "w"))
    eq("--load accepts valid narrative",
       run(["insights.py", "--outdir", d, "--load", good], insights), 0)
    loaded = json.load(open(os.path.join(d, "insights.json")))
    eq("narrative attached", len(loaded["narrative"]["insights"]), 1)
    check("detectors preserved alongside narrative", "detectors" in loaded)

    # Fenced code blocks are the normal shape of an LLM reply.
    open(bad, "w").write('```json\n{"insights":[{"title":"Z","finding":"f"}]}\n```')
    eq("--load tolerates ``` fences",
       run(["insights.py", "--outdir", d, "--load", bad], insights), 0)

    # Restore the full narrative -- the fence fixture above is intentionally
    # minimal, and the HTML assertions below need the complete shape.
    run(["insights.py", "--outdir", d, "--load", good], insights)

    # And it must reach the HTML.
    run(["report.py", "--outdir", d], report)
    h = open(os.path.join(d, "report.html"), encoding="utf-8").read()
    check("insights tab rendered", 'id="v-insights"' in h)
    check("insight card rendered", 'class="ins ' in h)
    check("caveat rendered as 'Unless'", "Unless" in h)
    check("1:1 question rendered", 'class="qa"' in h)
    check("do-not-conclude rendered", 'class="dnc"' in h)
    check("judgement-vs-measurement warning present",
          "numbers are measured" in h)
    shutil.rmtree(d, ignore_errors=True)


def test_report_without_insights_still_builds():
    """The insights section is optional. A report with no insights.json must
    still render every other tab."""
    print("\n[insights] report degrades gracefully with no narrative")
    d = make_fixture(tempfile.mkdtemp())
    run(["classify.py", "--outdir", d], classify)
    real = build.subprocess.run

    class R:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout = rc, out
    try:
        build.subprocess.run = lambda a, **k: (
            R(1, "") if "contents" in " ".join(a)
            else R(0, json.dumps({"fork": False, "parent": None, "stars": 10,
                                  "owner": "acme"})))
        run(["build.py", "--outdir", d, "--roster",
             os.path.join(d, "roster.json"), "--scan-date", "2026-07-01"], build)
    finally:
        build.subprocess.run = real
    run(["report.py", "--outdir", d], report)
    h = open(os.path.join(d, "report.html"), encoding="utf-8").read()
    check("no insights tab when absent", 'id="v-insights"' not in h)
    check("cohort tab still present", 'id="v-cohort"' in h)
    check("person tab still present", "Dev One" in h)
    shutil.rmtree(d, ignore_errors=True)


def test_comment_defect_classification():
    """Defect counting must attribute to the right side and must not let the
    author-side number read as a quality score."""
    print("\n[comments] defect attribution, both sides")
    d = make_fixture(tempfile.mkdtemp())
    run(["classify.py", "--outdir", d], classify)

    # Two threads on devone's PR: one real bug raised by a peer (acknowledged),
    # one style nit. Plus one thread where devone is the reviewer.
    json.dump({
        "acme/svc#1": [
            {"who": "devtwo", "id": "10", "reply_to": "0",
             "path": "pkg/widget/widget.go",
             "body": "this dereferences before the nil check"},
            {"who": "devone", "id": "11", "reply_to": "10", "path": "",
             "body": "good catch, fixed"},
            {"who": "devtwo", "id": "20", "reply_to": "0", "path": "",
             "body": "nit: rename to widgetID"},
        ],
        "acme/svc#90": [
            {"who": "devone", "id": "30", "reply_to": "0", "path": "",
             "body": "this design will not scale past one region"},
        ],
    }, open(os.path.join(d, "commentcache.json"), "w"))

    cache = json.load(open(os.path.join(d, "commentcache.json")))
    th = comments.threads(cache)
    eq("threads grouped by reply chain", len(th), 3)

    ans = os.path.join(d, "ans.json")
    json.dump({"classifications": [
        {"thread": "acme/svc#1::10", "kind": "bug", "severity": "serious",
         "author_acknowledged": True, "one_line": "nil deref"},
        {"thread": "acme/svc#1::20", "kind": "style_nit", "severity": "minor",
         "author_acknowledged": None, "one_line": "rename"},
        {"thread": "acme/svc#90::30", "kind": "design_flaw",
         "severity": "moderate", "author_acknowledged": None,
         "one_line": "single-region design"},
    ]}, open(ans, "w"))

    real = build.subprocess.run

    class R:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout = rc, out
    try:
        build.subprocess.run = lambda a, **k: (
            R(1, "") if "contents" in " ".join(a)
            else R(0, json.dumps({"fork": False, "parent": None, "stars": 10,
                                  "owner": "acme"})))
        run(["build.py", "--outdir", d, "--roster",
             os.path.join(d, "roster.json"), "--scan-date", "2026-07-01"], build)
    finally:
        build.subprocess.run = real

    eq("--load aggregates cleanly",
       run(["comments.py", "--outdir", d, "--load", ans], comments), 0)
    ca = json.load(open(os.path.join(d, "comment-analysis.json")))
    eq("every thread id matched", ca["unmatched_thread_ids"], 0)

    # devone REVIEWED #90 -> gets credit for the design flaw there.
    rev = ca["as_reviewer"].get("devone")
    check("reviewer side scored", rev is not None)
    if rev:
        eq("design flaw counted as a defect caught", rev["defects_caught"], 1)
        eq("nit not counted as a defect", rev["breakdown"].get("style_nit", 0), 0)

    # devone AUTHORED #1 -> the bug lands on their author side, not reviewer.
    au = ca["as_author"].get("devone")
    check("author side scored", au is not None)
    if au:
        eq("defect surfaced in their code", au["defects_found_in_their_code"], 1)
        eq("serious counted", au["serious_found"], 1)
        eq("their acknowledgement counted", au["they_acknowledged"], 1)
        eq("nit tracked separately", au["nits_received"], 1)
    check("a bug on your own PR is not credited to you as a catch",
          (rev or {}).get("defects_caught", 0) == 1,
          "self-credit leaked into the reviewer side")

    # The framing guard is not optional: this number invites misuse.
    hr = ca.get("how_to_read", {})
    check("author side carries a not-a-scorecard warning",
          "NOT a defect-rate scorecard" in hr.get("as_author", ""))
    check("explicit never-rank instruction present",
          "Never rank" in hr.get("never", ""))

    # Malformed input must be rejected, not half-loaded.
    bad = os.path.join(d, "bad.json")
    open(bad, "w").write("nope")
    raised = False
    try:
        run(["comments.py", "--outdir", d, "--load", bad], comments)
    except SystemExit:
        raised = True
    check("--load rejects non-JSON", raised)

    for who in ("dependabot[bot]", "ack-bot", "k8s-ci-robot"):
        check("bot comments excluded: %s" % who,
              bool(comments.BOT_RE.search(who)))
    check("human comment kept", not comments.BOT_RE.search("devtwo"))

    # And it must surface in the report.
    run(["report.py", "--outdir", d], report)
    h = open(os.path.join(d, "report.html"), encoding="utf-8").read()
    check("defect section rendered", "Defects caught in review" in h)
    check("author-side warning rendered", "not a quality score" in h)
    check("cohort table has a defects column", "Defects caught</th>" in h)
    shutil.rmtree(d, ignore_errors=True)


def test_no_variable_shadowing_regression():
    """A real bug: `ext` (a list of PRs) was reassigned to a file-extension
    string inside the language loop, so the external-contributions section
    crashed with 'string indices must be integers'."""
    print("\n[pipeline] no-shadowing regression (real bug)")
    d = make_fixture(tempfile.mkdtemp(), with_extras=False)
    run(["classify.py", "--outdir", d], classify)
    real = build.subprocess.run

    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def fake(args, **kw):
        j = " ".join(args)
        if "contents" in j:
            return R(1, "")
        if "repos/" in j:
            return R(0, json.dumps({"fork": False, "parent": None,
                                    "stars": 9000, "owner": "acme"}))
        return R(1, "")

    err = None
    try:
        build.subprocess.run = fake
        run(["build.py", "--outdir", d, "--roster",
             os.path.join(d, "roster.json"), "--scan-date", "2026-07-01"], build)
    except Exception as ex:                      # noqa: BLE001
        err = ex
    finally:
        build.subprocess.run = real
    check("build completes without extras (no shadowing crash)", err is None,
          repr(err))
    # Stars above the floor and org not in own_orgs -> external section populated,
    # which is exactly the code path the shadowed variable broke.
    if err is None:
        pj = json.load(open(os.path.join(d, "devone-evidence.json")))
        check("external upstream section built",
              isinstance(pj["external_upstream_contributions"]["count"], int))
    shutil.rmtree(d, ignore_errors=True)


def test_empty_and_degenerate_inputs():
    print("\n[pipeline] degenerate inputs degrade gracefully")
    d = tempfile.mkdtemp()
    json.dump({"person": {"github_login": "nobody", "id": "nobody"},
               "window": {"start": "2026-01-01", "end": "2026-07-01"},
               "prs": [], "reviews": [], "issues": [],
               "comments_on_others": []},
              open(os.path.join(d, "nobody.json"), "w"))
    json.dump({}, open(os.path.join(d, "filecache.json"), "w"))
    json.dump({"window": {"start": "2026-01-01", "end": "2026-07-01"},
               "people": [{"github_login": "nobody", "id": "nobody"}]},
              open(os.path.join(d, "roster.json"), "w"))
    run(["classify.py", "--outdir", d], classify)
    rc = run(["build.py", "--outdir", d, "--roster",
              os.path.join(d, "roster.json")], build)
    eq("zero-PR person exits non-zero with a message", rc, 1)
    check("no evidence file written for empty person",
          not os.path.exists(os.path.join(d, "nobody-evidence.json")))
    shutil.rmtree(d, ignore_errors=True)


def main():
    print("=" * 70)
    print("github-perf-evidence offline test suite")
    print("=" * 70)
    for t in (test_classification, test_bot_filter,
              test_stale_pipeline_guard, test_review_depth_keys_match_report,
              test_trend_windows_do_not_overlap, test_ownership_timeout_is_bounded,
              test_fetch_retries_transient_and_fails_loud,
              test_insights_detectors, test_report_without_insights_still_builds,
              test_comment_defect_classification,
              test_gh_json_type_enforcement, test_repo_meta_degrades,
              test_wrap_never_splits_words, test_yq_escaping,
              test_html_escaping, test_bar_bounds,
              test_full_pipeline, test_no_variable_shadowing_regression,
              test_empty_and_degenerate_inputs):
        try:
            t()
        except Exception as ex:                  # noqa: BLE001
            import traceback
            FAIL.append((t.__name__, repr(ex)))
            print("  FAIL %s raised %r" % (t.__name__, ex))
            traceback.print_exc()

    print("\n" + "=" * 70)
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("\nFAILURES:")
        for n, d in FAIL:
            print("  %s  %s" % (n, d))
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
