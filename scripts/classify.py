#!/usr/bin/env python3
"""Split each PR's additions into hand-authored / generated / vendored.

Patterns live in <outdir>/patterns.json, seeded on first run with defaults that
hold across Go/Kubernetes/JS ecosystems. THE SEED IS A STARTING POINT, NOT AN
ANSWER -- run audit.py and extend it. Each rule carries a `verified` note
recording the evidence for it; that note ships in the output so a reader can
check the work.

Buckets:
  hand      -- authored by a person
  generated -- emitted by a tool into the repo (codegen, CRDs, doc snapshots)
  vendored  -- third-party content copied in (SDK models, charts, licenses)
"""
import argparse
import glob
import json
import os
import re
import sys

# Working files, not person bundles.
SKIP_FILES = ("filecache.json", "patterns.json", "commitcache.json")

# The seed is deliberately CONSERVATIVE. A rule that wrongly marks authored work
# as generated deletes someone's contribution from their review -- the one error
# mode worse than inflation, because it is invisible and unflattering. So rules
# here only fire on evidence that holds across ecosystems, and anything
# ecosystem-specific lives in PROFILES (opt in with --profile).
#
# Under-classifying is safe: audit.py flags the leftovers and you add rules with
# evidence. Over-classifying is not.
SEED = {
    "_comment": [
        "Patterns are regexes matched against the full repo-relative path via",
        "re.search (so anchor with ^ when you mean it).",
        "'verified' MUST say how you confirmed the rule -- codegen marker,",
        "generator script, vendoring README. 'assumed' is not acceptable;",
        "unverified rules are how a review gets embarrassed.",
        "This seed is intentionally narrow. Prefer adding a VERIFIED rule over",
        "loosening one: a rule that eats authored source silently understates a",
        "person, which is worse than a number audit.py will catch.",
        "Scope rules to file types where possible -- '/crds/' also matches",
        "src/crds/validate.go, which is hand-written Go.",
        "After editing, re-run classify.py + audit.py for EVERY person so the",
        "cohort stays comparable, and bump 'version'."
    ],
    "version": 1,
    "generated": [
        {"re": r"zz_generated", "verified": "deepcopy-gen convention"},
        {"re": r"_generated\.go$", "verified": "Go codegen convention"},
        {"re": r"\.generated\.[a-z]+$", "verified": "explicit generated marker"},
        {"re": r"\.pb\.go$", "verified": "protoc output"},
        {"re": r"_pb2(_grpc)?\.py$", "verified": "protoc python output"},
        {"re": r"\.pb\.cc$|\.pb\.h$", "verified": "protoc C++ output"},
        {"re": r"\.g\.dart$", "verified": "dart build_runner output"},
        {"re": r"\.freezed\.dart$", "verified": "dart freezed output"},
        # Scoped to YAML: CRD manifests are controller-gen output, but
        # src/crds/*.go is hand-written source.
        {"re": r"config/crd/.*\.ya?ml$", "verified": "kubebuilder CRD output dir"},
        {"re": r"helm/crds/.*\.ya?ml$", "verified": "chart CRD output"},
        {"re": r"/crds/.*\.ya?ml$",
         "verified": "CRD YAML is controller-gen output; scoped to .yaml so it "
                     "cannot swallow hand-written code in a crds/ package"},
        {"re": r"^pkg/generated/", "verified": "explicit generated dir"},
        {"re": r"^generated/", "verified": "explicit generated dir"},
        {"re": r"/generated/", "verified": "explicit generated dir"},
        {"re": r"^mocks/.*_?mock.*", "verified": "mockgen output naming"},
        {"re": r"mock_[^/]+\.go$", "verified": "mockgen default naming"},
        {"re": r"__snapshots__/", "verified": "jest snapshot output"},
        {"re": r"versioned_docs/", "verified": "docusaurus version snapshot"},
        {"re": r"versioned_sidebars/", "verified": "docusaurus version snapshot"},
        {"re": r"\.docusaurus/", "verified": "docusaurus build cache"},
        {"re": r"grafana/(dashboards?|.*dashboard)[^/]*\.json$",
         "verified": "Grafana dashboard JSON is a UI export; scoped to "
                     "dashboard files so it cannot eat hand-written "
                     "grafana/provisioning config"}
    ],
    "vendored": [
        {"re": r"^vendor/", "verified": "Go vendor dir"},
        {"re": r"^third_party/", "verified": "conventional vendoring dir"},
        {"re": r"^node_modules/", "verified": "npm deps"},
        {"re": r"go\.sum$", "verified": "dependency checksum lockfile"},
        {"re": r"(^|/)[^/]*\.lock$", "verified": "lockfile"},
        {"re": r"package-lock\.json$", "verified": "npm lockfile"},
        {"re": r"pnpm-lock\.ya?ml$", "verified": "pnpm lockfile"},
        {"re": r"ATTRIBUTION\.md$",
         "verified": "generated dependency-license manifest"},
        {"re": r"(^|/)LICEN[SC]E([-.][\w.]+)?$",
         "verified": "third-party license text; anchored so it cannot match a "
                     "hand-written doc like LICENSES-explained.md"},
        {"re": r"(^|/)NOTICE(\.[\w]+)?$", "verified": "third-party notice text"}
    ],
    "generated_unless": []
}

# Ecosystem-specific rule sets. These are NOT applied unless requested, because
# each assumes a repo layout that means something different elsewhere.
PROFILES = {
    "aws-ack": {
        "_about": "AWS Controllers for Kubernetes. Codegen-heavy: a small "
                  "generator.yaml edit emits thousands of lines.",
        "generated": [
            {"re": r"^apis/v1alpha1/.*\.go$",
             "verified": "ACK: apis/v1alpha1 Go types are ack-generate output"},
            {"re": r"ack-generate-metadata\.yaml$",
             "verified": "ACK codegen metadata"},
            {"re": r"^prow/jobs/.*\.ya?ml$",
             "verified": "ACK test-infra: prow job config is regenerated"}
        ],
        "vendored": [
            {"re": r"aws-models/.*\.json$",
             "verified": "AWS SDK Smithy service models ({'smithy':'2.0'}), "
                         "copied from aws-sdk-go"},
            {"re": r"testdata/models/apis/.*/[^/]+\.json$",
             "verified": "AWS SDK service models used as codegen fixtures"}
        ],
        "generated_unless": [
            {"dir_re": r"(^|/)pkg/resource/[^/]+/", "except_basename": ["hooks.go"],
             "verified": "ACK: every file in pkg/resource/<r>/ carries 'Code "
                         "generated by ack-generate. DO NOT EDIT.' except "
                         "hooks.go, the hand-authored extension point. VERIFY "
                         "this holds in your repo -- pkg/resource/ is an "
                         "ordinary package name in most Go projects."}
        ]
    },
    "kubernetes": {
        "_about": "Kubernetes / controller-runtime projects.",
        "generated": [
            {"re": r"^api/openapi-spec/",
             "verified": "k8s openapi-gen output"},
            {"re": r"(^|/)swagger\.json$",
             "verified": "OpenAPI spec, generated or vendored upstream"},
            {"re": r"^staging/src/.*/generated",
             "verified": "k8s staging generated clients"},
            {"re": r"clientset/versioned/", "verified": "client-gen output"},
            {"re": r"informers/externalversions/", "verified": "informer-gen output"},
            {"re": r"listers/", "verified": "lister-gen output"}
        ],
        "vendored": [
            {"re": r"^charts/",
             "verified": "vendored Helm charts -- CONFIRM via charts/README.md; "
                         "some repos author charts by hand"}
        ],
        "generated_unless": []
    },
    "web": {
        "_about": "JS/TS front-end and Node projects.",
        "generated": [
            {"re": r"^dist/", "verified": "build output"},
            {"re": r"^build/", "verified": "build output"},
            {"re": r"\.d\.ts$", "verified": "emitted type declarations"},
            {"re": r"^\.next/", "verified": "next.js build output"},
            {"re": r"^storybook-static/", "verified": "storybook build output"}
        ],
        "vendored": [
            {"re": r"\.min\.(js|css)$", "verified": "minified third-party bundle"}
        ],
        "generated_unless": []
    }
}


def load_patterns(outdir, profiles=()):
    """Load patterns.json, seeding it on first run.

    profiles merge extra ecosystem rules into the seed. They are only applied at
    creation time; after that patterns.json is the single source of truth so
    edits are never silently overwritten.
    """
    path = os.path.join(outdir, "patterns.json")
    if not os.path.exists(path):
        pat = json.loads(json.dumps(SEED))     # deep copy
        applied = []
        for name in profiles:
            if name not in PROFILES:
                raise SystemExit("unknown profile %r; available: %s"
                                 % (name, ", ".join(sorted(PROFILES))))
            prof = PROFILES[name]
            for key in ("generated", "vendored", "generated_unless"):
                pat.setdefault(key, []).extend(prof.get(key, []))
            applied.append(name)
        pat["profiles_applied"] = applied
        json.dump(pat, open(path, "w"), indent=2)
        print("seeded %s%s" % (path,
              " with profiles: %s" % ", ".join(applied) if applied else ""))
        print("  The seed is deliberately narrow. Run audit.py and add VERIFIED")
        print("  rules; do not loosen rules to make numbers look tidy.")
    return json.load(open(path)), path


def build_bucket(pat):
    gen = [re.compile(r["re"]) for r in pat.get("generated", [])]
    ven = [re.compile(r["re"]) for r in pat.get("vendored", [])]
    unless = [(re.compile(r["dir_re"]), set(r.get("except_basename", [])))
              for r in pat.get("generated_unless", [])]

    def bucket(fn):
        for rx in ven:
            if rx.search(fn):
                return "vendored"
        for rx in gen:
            if rx.search(fn):
                return "generated"
        for rx, keep in unless:
            if rx.search(fn) and fn.rsplit("/", 1)[-1] not in keep:
                return "generated"
        return "hand"

    return bucket


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir")
    ap.add_argument("--profile", nargs="*", default=[],
                    metavar="NAME",
                    help="ecosystem rule sets to seed on first run: %s. Each "
                         "assumes a repo layout -- read its _about and confirm "
                         "it matches your repos."
                         % ", ".join(sorted(PROFILES)))
    ap.add_argument("--list-profiles", action="store_true")
    a = ap.parse_args()

    if a.list_profiles:
        for name in sorted(PROFILES):
            p = PROFILES[name]
            n = sum(len(p.get(k, [])) for k in
                    ("generated", "vendored", "generated_unless"))
            print("%-12s %d rules -- %s" % (name, n, p["_about"]))
        return 0

    if not a.outdir:
        raise SystemExit("--outdir is required (or use --list-profiles)")

    pat, ppath = load_patterns(a.outdir, a.profile)
    bucket = build_bucket(pat)
    version = pat.get("version", 1)
    cache = json.load(open(os.path.join(a.outdir, "filecache.json")))

    for f in sorted(glob.glob(os.path.join(a.outdir, "*.json"))):
        if os.path.basename(f) in SKIP_FILES:
            continue
        b = json.load(open(f))
        if "prs" not in b:
            continue
        unavailable = 0
        for p in b["prs"]:
            files = cache.get("%s#%d" % (p["repo"], p["number"]))
            if files is None:
                p.update(hand_additions=0, generated_additions=0,
                         vendored_additions=0, hand_files=0,
                         classification="UNAVAILABLE")
                unavailable += 1
                continue
            tot = {"hand": 0, "generated": 0, "vendored": 0}
            nf = 0
            for add, fn in files:
                k = bucket(fn)
                tot[k] += add
                if k == "hand":
                    nf += 1
            p.update(hand_additions=tot["hand"],
                     generated_additions=tot["generated"],
                     vendored_additions=tot["vendored"],
                     hand_files=nf, classification="ok")
        b["classifier_version"] = version
        json.dump(b, open(f, "w"))
        h = sum(p["hand_additions"] for p in b["prs"])
        g = sum(p["generated_additions"] for p in b["prs"])
        v = sum(p["vendored_additions"] for p in b["prs"])
        raw = h + g + v
        print("%-20s hand=%-8d gen=%-8d vendored=%-8d  hand_share=%3.0f%%  "
              "prs=%-4d unavailable=%d"
              % (b["person"]["github_login"], h, g, v,
                 100.0 * h / max(1, raw), len(b["prs"]), unavailable))

    print("\npatterns: %s (version %s)" % (ppath, version))
    print("NEXT: run audit.py and work its findings before building.")


if __name__ == "__main__":
    sys.exit(main())
