#!/usr/bin/env python3
"""Render the evidence data into ONE self-contained interactive HTML file.

Built for a manager reading on a laptop before a calibration meeting: no server,
no build step, no network. Open the file and click.

Design choices that are deliberate, not cosmetic:
  - Caveats are rendered as visible banners, not collapsed footnotes. A number
    whose warning is one click away gets quoted without the warning.
  - Raw additions are shown struck through. Seeing the number you must not cite,
    crossed out, next to the one you should, teaches the distinction faster than
    prose does.
  - Every ranking header states what it does NOT measure.
  - Bars are relative to the cohort max, and the axis always starts at zero.

Reads the JSON mirrors that build.py writes (COHORT-INDEX.json and
<id>-evidence.json), NOT the YAML. A hand-rolled YAML reader would be a
reliability liability for zero benefit, and this keeps report.py dependency-free.

  python3 report.py --outdir OUT -o report.html
"""
import argparse
import glob
import html
import json
import os
import subprocess
import sys

SKIP_FILES = ("filecache.json", "patterns.json", "commitcache.json",
              "reviewcache.json", "ownercache.json", "ownership.json")


# ---------------------------------------------------------------------------
def e(x):
    return html.escape(str(x if x is not None else ""), quote=True)


def num(x):
    try:
        return "{:,}".format(int(x))
    except (TypeError, ValueError):
        return e(x)


CSS = """
:root{
 --bg:#0f1115; --panel:#171a21; --panel2:#1d212a; --line:#2a2f3a;
 --fg:#e6e8ee; --dim:#9aa3b2; --faint:#6b7280;
 --accent:#6ea8fe; --good:#4ade80; --warn:#fbbf24; --bad:#f87171;
 --gen:#8b5cf6; --ven:#f59e0b;
}
@media(prefers-color-scheme:light){:root{
 --bg:#f6f7f9;--panel:#fff;--panel2:#f0f2f5;--line:#dfe3ea;
 --fg:#111827;--dim:#4b5563;--faint:#6b7280;--accent:#2563eb;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:26px 20px 90px}
h1{font-size:25px;margin:0 0 4px}h2{font-size:19px;margin:30px 0 12px}
h3{font-size:16px;margin:20px 0 8px}
.sub{color:var(--dim);font-size:13px;margin-bottom:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;
 padding:16px 18px;margin:12px 0}
.banner{border-left:4px solid var(--warn);background:var(--panel2);
 border-radius:8px;padding:11px 14px;margin:9px 0;font-size:13.5px}
.banner.high{border-left-color:var(--bad)}
.banner.medium{border-left-color:var(--warn)}
.banner.low{border-left-color:var(--faint)}
.banner b{display:block;margin-bottom:3px;font-size:12px;letter-spacing:.03em;
 text-transform:uppercase;color:var(--dim)}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:18px 0 6px;
 border-bottom:1px solid var(--line);padding-bottom:10px}
.tab{padding:7px 13px;border-radius:8px;cursor:pointer;font-size:14px;
 background:var(--panel);border:1px solid var(--line);color:var(--dim)}
.tab:hover{color:var(--fg)}
.tab.on{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.view{display:none}.view.on{display:block}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:8px 9px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;
 letter-spacing:.03em;cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--fg)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:hover{background:var(--panel2)}
.bar{height:7px;border-radius:4px;background:var(--accent);min-width:2px;display:block}
.bar.g{background:var(--good)}.bar.p{background:var(--gen)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:11px}
.kpi{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:12px 13px}
.kpi .v{font-size:23px;font-weight:650;font-variant-numeric:tabular-nums}
.kpi .l{font-size:11.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.03em}
.kpi .h{font-size:11.5px;color:var(--faint);margin-top:3px}
.kpi.quote{border-color:var(--good)}.kpi.quote .v{color:var(--good)}
.kpi.never{opacity:.62}.kpi.never .v{text-decoration:line-through;color:var(--bad)}
.pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11.5px;
 background:var(--panel2);border:1px solid var(--line);color:var(--dim);margin:0 4px 4px 0}
.pill.ok{border-color:var(--good);color:var(--good)}
.pill.warn{border-color:var(--warn);color:var(--warn)}
.pill.bad{border-color:var(--bad);color:var(--bad)}
.stack{display:flex;height:20px;border-radius:5px;overflow:hidden;margin:7px 0 3px;
 border:1px solid var(--line)}
.stack i{display:block;height:100%}
.legend{font-size:11.5px;color:var(--dim)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 4px 0 11px}
details{margin:7px 0}summary{cursor:pointer;color:var(--dim);font-size:13.5px;padding:3px 0}
summary:hover{color:var(--fg)}
code{background:var(--panel2);padding:1px 5px;border-radius:4px;font-size:12.5px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.searchbox{width:100%;padding:9px 12px;border-radius:8px;border:1px solid var(--line);
 background:var(--panel);color:var(--fg);font-size:14px;margin:8px 0 4px}
.note{color:var(--faint);font-size:12.5px;margin:5px 0}
.up{color:var(--good)}.down{color:var(--bad)}.flat{color:var(--faint)}
.risk{border-left:4px solid var(--bad)}
.hdr{display:flex;justify-content:space-between;align-items:baseline;gap:14px;flex-wrap:wrap}
.chip{font-size:11.5px;color:var(--dim);background:var(--panel2);padding:3px 9px;
 border-radius:6px;border:1px solid var(--line)}
"""

JS = """
function show(id,el){
 document.querySelectorAll('.view').forEach(v=>v.classList.remove('on'));
 document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
 document.getElementById(id).classList.add('on');
 el.classList.add('on');
 location.hash=id;
 window.scrollTo({top:0,behavior:'instant'});
}
function sortTable(th){
 const tb=th.closest('table'),bd=tb.tBodies[0],i=[...th.parentNode.children].indexOf(th);
 const asc=!(th.dataset.asc==='1');
 [...th.parentNode.children].forEach(x=>{if(x!==th)delete x.dataset.asc});
 th.dataset.asc=asc?'1':'0';
 const key=r=>{
  const c=r.children[i], v=c.dataset.v!==undefined?c.dataset.v:c.textContent;
  const f=parseFloat(String(v).replace(/[,%$]/g,''));
  return isNaN(f)?String(v).toLowerCase():f;
 };
 [...bd.rows].sort((a,b)=>{
  const x=key(a),y=key(b);
  if(typeof x==='number'&&typeof y==='number')return asc?x-y:y-x;
  return asc?String(x).localeCompare(y):String(y).localeCompare(x);
 }).forEach(r=>bd.appendChild(r));
}
function filt(inp,tid){
 const q=inp.value.toLowerCase();
 document.querySelectorAll('#'+tid+' tbody tr').forEach(r=>{
  r.style.display=r.textContent.toLowerCase().includes(q)?'':'none';
 });
}
window.addEventListener('DOMContentLoaded',()=>{
 const h=location.hash.slice(1);
 if(h&&document.getElementById(h)){
  const t=document.querySelector('.tab[data-v="'+h+'"]');
  if(t)show(h,t);
 }
});
"""


def bar(v, mx, cls=""):
    pctw = 0 if not mx else max(1, round(100.0 * (v or 0) / mx))
    return ('<span class="bar %s" style="width:%d%%"></span>' % (cls, pctw))


def caveat_html(c):
    sev = str(c.get("severity", "low"))
    return ('<div class="banner %s"><b>%s &middot; %s</b>%s</div>'
            % (e(sev), e(c.get("id", "caveat")), e(sev), e(c.get("detail", ""))))


def trend_cell(d):
    if not isinstance(d, dict):
        return "&mdash;"
    ch = str(d.get("change", ""))
    cls = "up" if ch.startswith("+") else ("down" if ch.startswith("-") else "flat")
    return ('%s &rarr; %s <span class="%s">%s</span>'
            % (num(d.get("early")), num(d.get("late")), cls, e(ch)))


def build_html(idx, people, owners, title):
    scan = idx.get("scan", {}) or {}
    rows = idx.get("cohort", []) or []
    H = []
    a = H.append

    a("<!doctype html><meta charset=utf-8>")
    a('<meta name=viewport content="width=device-width,initial-scale=1">')
    a("<title>%s</title>" % e(title))
    a("<style>%s</style>" % CSS)
    a('<div class="wrap">')

    a('<div class="hdr"><div>')
    a("<h1>%s</h1>" % e(title))
    a('<div class="sub">Window <b>%s</b> to <b>%s</b> &middot; scanned %s '
      '&middot; %d people &middot; classifier v%s</div>'
      % (e(scan.get("window_start")), e(scan.get("window_end")),
         e(scan.get("scan_date")), len(rows), e(scan.get("classifier_version"))))
    a("</div><div>")
    if any((p.get("metadata") or {}).get("visibility") == "all" for p in people):
        a('<span class="chip" style="border-color:var(--bad);color:var(--bad)">'
          'contains private repo data</span>')
    else:
        a('<span class="chip">public GitHub only</span>')
    a("</div></div>")

    a('<div class="banner high"><b>read this first</b>'
      'These numbers measure <b>activity on public GitHub</b>, not impact. A '
      'ten-line fix in a shared library can outweigh a three-thousand-line '
      'feature. Low numbers are a <b>question to ask the person</b>, never a '
      'finding about them &mdash; most engineering work (internal review, '
      'on-call, incident response, design, mentoring) is invisible here. '
      'Never rank people by line count.</div>')

    # ---- tabs
    a('<div class="tabs">')
    a('<div class="tab on" data-v="v-cohort" onclick="show(\'v-cohort\',this)">Cohort</div>')
    a('<div class="tab" data-v="v-rank" onclick="show(\'v-rank\',this)">Rankings</div>')
    risk_rows = (owners or {}).get("bus_factor_risk") or idx.get("bus_factor_risk") or []
    if risk_rows:
        a('<div class="tab" data-v="v-risk" onclick="show(\'v-risk\',this)">'
          'Team risk <span class="pill bad">%d</span></div>' % len(risk_rows))
    for p in people:
        m = p.get("metadata", {}) or {}
        pid = str(m.get("id") or m.get("github_login"))
        label = m.get("name") or m.get("github_login")
        a('<div class="tab" data-v="v-%s" onclick="show(\'v-%s\',this)">%s</div>'
          % (e(pid), e(pid), e(label)))
    a("</div>")

    # ================= COHORT =================
    a('<div class="view on" id="v-cohort">')
    hu = idx.get("how_to_use", {}) or {}
    a('<div class="card"><h3 style="margin-top:0">Which number to quote</h3>')
    a('<p><code>%s</code> &mdash; %s</p>'
      % (e(hu.get("best_volume_metric", "hand_additions_canonical_merged")),
         e(hu.get("why", ""))))
    a('<p class="note"><b>Do not use:</b> %s</p>' % e(hu.get("do_not_use", "")))
    a("</div>")

    mx = {k: max([r.get(k) or 0 for r in rows] + [1]) for k in
          ("hand_canonical_merged", "reviews_given", "prs_merged",
           "comment_threads", "inline_comments")}
    a('<input class="searchbox" placeholder="Filter people, teams, languages..." '
      'oninput="filt(this,\'t-cohort\')">')
    a('<div class="card" style="overflow-x:auto"><table id="t-cohort"><thead><tr>')
    cols = ["Person", "Team", "Level", "Shipped lines", "PRs merged",
            "Merge %", "Reviews", "Inline/PR", "Discussion", "Roles",
            "Ext upstream", "Identity"]
    for c in cols:
        cl = ' class="n"' if c not in ("Person", "Team", "Level", "Identity") else ""
        a("<th%s onclick=\"sortTable(this)\">%s</th>" % (cl, e(c)))
    a("</tr></thead><tbody>")
    for r in rows:
        pid = e(r.get("id"))
        a("<tr>")
        a('<td><a href="#v-%s" onclick="show(\'v-%s\',document.querySelector(\'.tab[data-v=&quot;v-%s&quot;]\'))">%s</a>'
          '<div class="note mono">@%s</div></td>'
          % (pid, pid, pid, e(r.get("name") or r.get("id")), e(r.get("login") or r.get("github_login"))))
        a("<td>%s</td><td>%s</td>" % (e(r.get("team")) or "&mdash;",
                                      e(r.get("level")) or "&mdash;"))
        v = r.get("hand_canonical_merged") or 0
        a('<td class="n" data-v="%d">%s%s</td>'
          % (v, num(v), bar(v, mx["hand_canonical_merged"], "g")))
        a('<td class="n" data-v="%d">%s</td>' % (r.get("prs_merged") or 0,
                                                 num(r.get("prs_merged"))))
        a('<td class="n">%s</td>' % e(r.get("merge_rate_pct")))
        rv = r.get("reviews_given") or 0
        a('<td class="n" data-v="%d">%s%s</td>' % (rv, num(rv),
                                                   bar(rv, mx["reviews_given"])))
        ipp = r.get("inline_per_pr")
        a('<td class="n" data-v="%s">%s</td>'
          % (ipp if ipp is not None else 0,
             e(ipp) if ipp is not None else "&mdash;"))
        a('<td class="n" data-v="%d">%s</td>'
          % (r.get("comment_threads") or 0,
             num(r.get("comment_threads"))))
        nr = r.get("roles") or 0
        a('<td class="n">%s</td>'
          % ('<span class="pill ok">%d</span>' % nr if nr else "&mdash;"))
        ex = r.get("external_prs") or 0
        a('<td class="n">%s</td>'
          % ('<span class="pill ok">%d</span>' % ex if ex else "&mdash;"))
        ic = str(r.get("identity", ""))
        cls = "ok" if ic == "documented" else "bad"
        a('<td><span class="pill %s">%s</span></td>' % (cls, e(ic)))
        a("</tr>")
    a("</tbody></table></div>")
    a('<p class="note">Click any column header to sort. Bars are relative to the '
      'cohort maximum and start at zero.</p>')

    for n in idx.get("cross_cohort_notes", []) or []:
        a(caveat_html(n))
    a("</div>")

    # ================= RANKINGS =================
    a('<div class="view" id="v-rank">')
    a('<div class="banner high"><b>none of these measures impact</b>'
      'Each list orders one kind of activity. A person can top one and sit last '
      'on another while contributing more than everyone. Read several, then go '
      'read the actual PRs.</div>')
    WHAT = {
        "by_hand_canonical_merged": ("Shipped authored volume",
                                     "Does NOT measure difficulty, design quality, or leverage."),
        "by_reviews_given": ("Review load",
                             "Counts PRs reviewed. Does NOT measure review depth &mdash; see Inline/PR."),
        "by_inline_comments": ("Review depth",
                              "Line-level engagement. The best available proxy for substantive review."),
        "by_comment_threads": ("Discussion reach",
                               "Threads, not comments. Does NOT measure whether the input was useful."),
        "by_prs_merged": ("Merged PR count",
                          "A PR can be one line or ten thousand."),
        "by_repos_touched": ("Breadth",
                             "Not portable: many small repos inflate this against a monorepo."),
        "by_roles": ("Formal ownership",
                     "Granted by others in OWNERS/CODEOWNERS. Hardest to game."),
    }
    for key, items in (idx.get("rankings", {}) or {}).items():
        if not isinstance(items, list) or not items:
            continue
        label, dis = WHAT.get(key, (key.replace("by_", "").replace("_", " "), ""))
        a('<div class="card"><h3 style="margin-top:0">%s</h3>' % e(label))
        if dis:
            a('<p class="note">%s</p>' % dis)
        top = max([(i.get("value") or 0) for i in items] + [1])
        a("<table><tbody>")
        for i in items:
            a('<tr><td style="width:170px">%s</td><td class="n" '
              'style="width:90px">%s</td><td>%s</td></tr>'
              % (e(i.get("id")), num(i.get("value")),
                 bar(i.get("value") or 0, top,
                     "p" if key == "by_inline_comments" else "")))
        a("</tbody></table></div>")
    a("</div>")

    # ================= TEAM RISK =================
    if risk_rows:
        a('<div class="view" id="v-risk">')
        a('<div class="banner high"><b>this is a staffing finding, not a '
          'performance finding</b>Each row is a subsystem where one person wrote '
          'at least half the commits. That is a risk you own as a manager: '
          'pair someone in, or spread the next project. It is not a credit to '
          'award or a problem to raise with the person.</div>')
        a('<div class="card risk" style="overflow-x:auto"><table id="t-risk"><thead><tr>')
        for c, cl in (("Repo", ""), ("Subsystem", ""), ("Sole author", ""),
                      ("Their commits", ' class="n"'), ("Total", ' class="n"')):
            a("<th%s onclick=\"sortTable(this)\">%s</th>" % (cl, e(c)))
        a("</tr></thead><tbody>")
        for s in risk_rows:
            a("<tr><td class=mono>%s</td><td class=mono>%s</td>"
              "<td>@%s</td><td class=n>%s</td><td class=n>%s</td></tr>"
              % (e(str(s.get("repo", "")).split("/")[-1]), e(s.get("directory")),
                 e(s.get("top_author")), num(s.get("top_author_commits")),
                 num(s.get("commits_in_window"))))
        a("</tbody></table></div>")
        a('<div class="banner low"><b>caveat</b>%s</div>'
          % e((owners or {}).get("caveat") or idx.get("ownership_caveat", "")))
        a("</div>")

    # ================= PER PERSON =================
    for p in people:
        m = p.get("metadata", {}) or {}
        s = p.get("summary", {}) or {}
        pid = e(m.get("id") or m.get("github_login"))
        a('<div class="view" id="v-%s">' % pid)
        a('<div class="hdr"><div><h2 style="margin-top:0">%s</h2>'
          '<div class="sub mono">@%s &middot; %s &middot; %s</div></div><div>'
          % (e(m.get("name") or m.get("github_login")), e(m.get("github_login")),
             e(m.get("team") or "no team"), e(m.get("level") or "no level")))
        ic = str(m.get("identity_confidence", ""))
        a('<span class="pill %s">identity: %s</span>'
          % ("ok" if ic == "documented" else "bad", e(ic)))
        a("</div></div>")

        a('<div class="banner %s"><b>identity resolution</b>%s</div>'
          % ("low" if ic == "documented" else "high",
             e(m.get("identity_resolution", ""))))

        # KPIs
        a('<div class="kpis">')
        a('<div class="kpi quote"><div class="l">Shipped authored lines</div>'
          '<div class="v">%s</div><div class="h">hand-authored, merged, canonical '
          'repo &mdash; QUOTE THIS</div></div>'
          % num(s.get("hand_additions_canonical_merged")))
        a('<div class="kpi never"><div class="l">Raw additions</div>'
          '<div class="v">%s</div><div class="h">what GitHub reports &mdash; '
          'NEVER cite</div></div>' % num(s.get("raw_additions_total")))
        for lab, val, hint in (
                ("PRs merged", "%s / %s" % (num(s.get("prs_merged")),
                                            num(s.get("prs_authored"))),
                 "%s%% merge rate" % e(s.get("merge_rate_pct"))),
                ("Reviews given", num(s.get("reviews_given")),
                 "ratio %s to own PRs" % e(s.get("review_to_authorship_ratio"))),
                ("Discussion threads", num(s.get("comment_threads_on_others_work")),
                 "on other people's work"),
                ("Repos touched", num(s.get("repos_touched")), "breadth")):
            a('<div class="kpi"><div class="l">%s</div><div class="v">%s</div>'
              '<div class="h">%s</div></div>' % (e(lab), val, hint))
        a("</div>")

        # composition
        h = s.get("hand_additions_total") or 0
        g = s.get("generated_additions_excluded") or 0
        v_ = s.get("vendored_additions_excluded") or 0
        tot = h + g + v_
        if tot:
            a('<div class="card"><h3 style="margin-top:0">What the diff actually '
              'contained</h3><div class="stack">')
            for val, col in ((h, "var(--good)"), (g, "var(--gen)"), (v_, "var(--ven)")):
                if val:
                    a('<i style="width:%.4f%%;background:%s"></i>'
                      % (100.0 * val / tot, col))
            a("</div>")
            a('<div class="legend"><i style="background:var(--good)"></i>'
              'hand-authored %s (%d%%)<i style="background:var(--gen)"></i>'
              'generated %s<i style="background:var(--ven)"></i>vendored %s</div>'
              % (num(h), round(100.0 * h / tot), num(g), num(v_)))
            a("</div>")

        # review depth
        rdd = p.get("review_depth")
        if isinstance(rdd, dict) and rdd:
            a('<div class="card"><h3 style="margin-top:0">Review depth</h3>')
            a('<div class="kpis">')
            for lab, val, hint in (
                    ("Inline comments", num(rdd.get("inline_comments")),
                     "line-level engagement"),
                    ("Per reviewed PR", e(rdd.get("inline_per_pr")),
                     "best single depth signal"),
                    ("Changes requested", num(rdd.get("changes_requested")),
                     "blocking bad changes is senior behaviour"),
                    ("Bare approvals", num(rdd.get("bare_approvals")),
                     "APPROVED with empty body")):
                a('<div class="kpi"><div class="l">%s</div><div class="v">%s</div>'
                  '<div class="h">%s</div></div>' % (e(lab), val, hint))
            a("</div>")
            a('<p class="note">inline_comments_per_reviewed_pr is the best '
              'single depth signal: it counts line-level engagement with the '
              'code. A high changes_requested share means this person blocks bad '
              'changes, which is a senior behaviour that costs social capital. '
              'Many bare approvals is not automatically bad &mdash; trivial and '
              'automated PRs deserve fast approvals &mdash; but if they '
              'dominate, the review count is not evidence of depth.</p>')
            a('<p class="note">Captured detail for %s of %s reviewed PRs; '
              '%s%% were substantive.</p>'
              % (num(rdd.get("prs_with_detail")), num(s.get("reviews_given")),
                 e(rdd.get("substantive_pct"))))
            a("</div>")

        # trajectory
        tj = p.get("trajectory")
        if isinstance(tj, dict) and tj:
            a('<div class="card"><h3 style="margin-top:0">Trajectory</h3>')
            a('<p class="note">%s &rarr; %s, split at <b>%s</b></p>'
              % (e(tj.get("early_window")), e(tj.get("late_window")),
                 e(tj.get("split_date"))))
            a("<table><tbody>")
            for k in ("prs_opened", "prs_merged", "reviews_given",
                      "discussion_threads", "repos_touched"):
                if k in tj:
                    a("<tr><td>%s</td><td class=n>%s</td></tr>"
                      % (e(k.replace("_", " ")), trend_cell(tj[k])))
            a("</tbody></table>")
            a('<p class="note">%s</p>' % e(tj.get("caveat", "")))
            a("</div>")

        # ownership
        for key, head, note in (
                ("ownership_roles", "Formal ownership roles",
                 "Read out of OWNERS / CODEOWNERS. Granted by others, so this is "
                 "the hardest signal to game."),
                ("de_facto_ownership", "De-facto ownership",
                 "From commit history, not formal grants.")):
            blk = p.get(key)
            items = blk.get("items") if isinstance(blk, dict) else blk
            if isinstance(blk, dict) and key == "de_facto_ownership":
                items = blk.get("items")
            if items:
                a('<div class="card"><h3 style="margin-top:0">%s</h3>' % e(head))
                a('<p class="note">%s</p>' % e(note))
                a("<table><tbody>")
                for it in (items if isinstance(items, list) else []):
                    if not isinstance(it, dict):
                        continue
                    if key == "ownership_roles":
                        a("<tr><td class=mono>%s</td><td>%s</td>"
                          "<td class=mono>%s</td></tr>"
                          % (e(it.get("repo")),
                             '<span class="pill ok">%s</span>' % e(it.get("group"))
                             if it.get("group") else "&mdash;",
                             e(it.get("file"))))
                    else:
                        a("<tr><td class=mono>%s</td><td class=mono>%s</td>"
                          "<td class=n>%s%% (%s of %s)</td>"
                          "<td class=n>bus factor %s</td></tr>"
                          % (e(it.get("repo")), e(it.get("directory")),
                             e(it.get("share_pct")), num(it.get("commits")),
                             num(it.get("of_subsystem_total")),
                             e(it.get("bus_factor"))))
                a("</tbody></table></div>")

        # external upstream
        ext = p.get("external_upstream_contributions") or {}
        if isinstance(ext, dict) and (ext.get("count") or 0):
            a('<div class="card"><h3 style="margin-top:0">External upstream '
              'contributions <span class="pill ok">%s</span></h3>' % e(ext.get("count")))
            a('<p class="note">Merged into projects outside your org. Required '
              'convincing maintainers you do not employ.</p>')
            a("<table><tbody>")
            for it in (ext.get("items") or [])[:20]:
                if not isinstance(it, dict):
                    continue
                a('<tr><td class=mono>%s</td><td>%s</td><td class=n>%s&#9733;</td>'
                  '<td><a href="%s" target=_blank rel=noopener>%s</a></td></tr>'
                  % (e(it.get("repo")), e(it.get("status")),
                     num(it.get("upstream_stars")), e(it.get("url")),
                     e(str(it.get("title", ""))[:70])))
            a("</tbody></table></div>")

        # commit subjects
        acs = p.get("authored_commit_subjects") or {}
        if isinstance(acs, dict) and acs.get("items"):
            a('<div class="card"><h3 style="margin-top:0">What they built '
              '<span class="pill">%s commits</span></h3>' % e(acs.get("count")))
            a('<p class="note">Line counts say how much. These say what. Read '
              'these before quoting any number.</p>')
            a('<input class="searchbox" placeholder="Search commit subjects..." '
              'oninput="filt(this,\'t-c-%s\')">' % pid)
            a('<table id="t-c-%s"><tbody>' % pid)
            for it in acs["items"]:
                if not isinstance(it, dict):
                    continue
                a("<tr><td class=mono style='width:230px'>%s#%s</td><td>%s</td></tr>"
                  % (e(str(it.get("repo", "")).split("/")[-1]), e(it.get("pr")),
                     e(it.get("subject"))))
            a("</tbody></table></div>")

        # largest PRs
        lg = p.get("largest_hand_authored")
        if isinstance(lg, list) and lg:
            a('<div class="card"><h3 style="margin-top:0">Largest hand-authored '
              'PRs</h3><p class="note">Candidate citations. Size alone is not '
              'significance &mdash; open them.</p><table><thead><tr>')
            for c in ("Repo", "#", "Title", "Status", "Hand", "Generated", "Files"):
                a("<th%s onclick=\"sortTable(this)\">%s</th>"
                  % (' class="n"' if c in ("Hand", "Generated", "Files", "#") else "", c))
            a("</tr></thead><tbody>")
            for it in lg:
                if not isinstance(it, dict):
                    continue
                a("<tr><td class=mono>%s</td><td class=n>%s</td>"
                  '<td><a href="%s" target=_blank rel=noopener>%s</a></td>'
                  "<td>%s</td><td class=n>%s</td><td class=n>%s</td>"
                  "<td class=n>%s</td></tr>"
                  % (e(str(it.get("repo", "")).split("/")[-1]), e(it.get("number")),
                     e(it.get("url")), e(str(it.get("title", ""))[:80]),
                     e(it.get("status")), num(it.get("hand_additions")),
                     num(it.get("generated_additions")), num(it.get("changed_files"))))
            a("</tbody></table></div>")

        # open work
        ow = p.get("open_and_wip") or {}
        if isinstance(ow, dict) and (ow.get("count") or 0):
            a('<details><summary>Open / in-progress work (%s PRs, %s parked lines)'
              "</summary>" % (e(ow.get("count")), num(ow.get("hand_additions_parked"))))
            a('<div class="card"><p class="note">%s</p><table><tbody>'
              % e(ow.get("probe", "")))
            for it in (ow.get("items") or []):
                if not isinstance(it, dict):
                    continue
                a("<tr><td class=mono>%s#%s</td>"
                  '<td><a href="%s" target=_blank rel=noopener>%s</a></td>'
                  "<td class=n>%s days</td><td class=n>%s lines</td></tr>"
                  % (e(str(it.get("repo", "")).split("/")[-1]), e(it.get("number")),
                     e(it.get("url")), e(str(it.get("title", ""))[:70]),
                     e(it.get("age_days")), num(it.get("hand_additions"))))
            a("</tbody></table></div></details>")

        # caveats last, but expanded
        cavs = (m.get("caveats") or [])
        if cavs:
            a("<h3>Caveats for these numbers</h3>")
            for c in cavs:
                if isinstance(c, dict):
                    a(caveat_html(c))

        a('<div class="banner low"><b>scope</b>%s</div>' % e(m.get("scope", "")))
        a("</div>")

    a('<p class="note" style="margin-top:40px">Generated by '
      '<a href="https://github.com/pnz1990/github-perf-evidence" target=_blank '
      'rel=noopener>github-perf-evidence</a>. Self-contained: no network calls, '
      'no tracking. Contains people data &mdash; do not post it anywhere '
      'shared.</p>')
    a("</div><script>%s</script>" % JS)
    return "\n".join(H)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--output", "-o", default=None)
    ap.add_argument("--title", default="Contribution Evidence")
    ap.add_argument("--open", action="store_true", help="open in browser after writing")
    a = ap.parse_args()

    ipath = os.path.join(a.outdir, "COHORT-INDEX.json")
    if not os.path.exists(ipath):
        raise SystemExit(
            "no COHORT-INDEX.json in %s -- run build.py first" % a.outdir)
    idx = json.load(open(ipath, encoding="utf-8"))

    people = []
    for r in (idx.get("cohort") or []):
        f = os.path.join(a.outdir, str(r.get("json_file") or ""))
        if r.get("json_file") and os.path.exists(f):
            people.append(json.load(open(f, encoding="utf-8")))
    if not people:
        for f in sorted(glob.glob(os.path.join(a.outdir, "*-evidence.json"))):
            people.append(json.load(open(f, encoding="utf-8")))

    opath = os.path.join(a.outdir, "ownership.json")
    owners = json.load(open(opath)) if os.path.exists(opath) else None

    out = a.output or os.path.join(a.outdir, "report.html")
    open(out, "w", encoding="utf-8").write(build_html(idx, people, owners, a.title))
    print("wrote %s  (%d people, %.0f KB)"
          % (out, len(people), os.path.getsize(out) / 1024.0))
    print("open it:  open %s" % out)
    if a.open:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", out])
    return 0


if __name__ == "__main__":
    sys.exit(main())
