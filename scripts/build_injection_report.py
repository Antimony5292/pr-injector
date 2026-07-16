"""Build a self-contained static HTML report from an injection_results.jsonl.

Usage:
    python scripts/build_injection_report.py experiments/ado_anapa
"""

from __future__ import annotations

import json
import sys
from html import escape
from pathlib import Path


def classify(rec: dict) -> str:
    if not rec.get("success"):
        fr = rec.get("failure_reason") or ""
        return {
            "healthy_check_failed": "A_baseline_unstable",
            "build_failed_on_clean_head": "B_build_failed_clean",
            "no_test_projects_detected": "C_no_tests",
        }.get(fr, "X_other")
    v = rec.get("verification") or {}
    if v.get("pass_to_fail"):
        return "P2F_OK"
    if (v.get("buggy_total") or 0) == 0:
        return "D_buggy_execution_invalid"
    if (v.get("fail_count_increase") or 0) <= 0 and not v.get("new_failed_tests"):
        return "E_no_delta_failure"
    return "F_other_p2f_miss"


CATEGORY_META = {
    "P2F_OK": ("Pass-to-fail confirmed", "#2ea043"),
    "A_baseline_unstable": ("Baseline unstable", "#d29922"),
    "B_build_failed_clean": ("Build failed on HEAD", "#bf6700"),
    "C_no_tests": ("No test projects", "#8957e5"),
    "D_buggy_execution_invalid": ("Buggy run invalid", "#cf222e"),
    "E_no_delta_failure": ("No delta failure", "#6e7781"),
    "F_other_p2f_miss": ("Other P2F miss", "#a40e26"),
    "X_other": ("Other", "#57606a"),
}


def build_html(records: list[dict], repo_label: str) -> str:
    rows = []
    counts: dict[str, int] = {}
    for r in records:
        cat = classify(r)
        counts[cat] = counts.get(cat, 0) + 1
        v = r.get("verification") or {}
        rows.append({
            "pr": r.get("pr_number"),
            "title": r.get("title", ""),
            "category": cat,
            "level": r.get("injection_level") or "-",
            "failure_reason": r.get("failure_reason") or "",
            "healthy_passed": v.get("healthy_passed", 0),
            "healthy_failed": v.get("healthy_failed", 0),
            "healthy_total": v.get("healthy_total", 0),
            "buggy_passed": v.get("buggy_passed", 0),
            "buggy_failed": v.get("buggy_failed", 0),
            "buggy_total": v.get("buggy_total", 0),
            "delta": v.get("fail_count_increase", 0),
            "new_failed_tests": v.get("new_failed_tests") or [],
            "healthy_failed_tests": v.get("healthy_failed_tests") or [],
            "buggy_failed_tests": v.get("buggy_failed_tests") or [],
            "duration": v.get("duration_seconds"),
            "p2f": bool(v.get("pass_to_fail")),
            "diff": r.get("injected_diff") or "",
            "original_patch": r.get("_original_patch") or "",
            "source_files": r.get("source_files") or [],
            "test_files": r.get("test_files") or [],
            "merge_commit_sha": r.get("merge_commit_sha", ""),
            "base_sha": r.get("base_sha", ""),
            "html_url": r.get("_html_url", ""),
            "summary": r.get("_summary"),
        })

    data_json = json.dumps(rows, ensure_ascii=False)
    counts_items = sorted(counts.items(), key=lambda kv: -kv[1])
    summary_html = " ".join(
        f'<span class="chip" style="--c:{CATEGORY_META.get(c, ("?", "#999"))[1]}" '
        f'data-cat="{c}">{c}: {n}</span>'
        for c, n in counts_items
    )

    cat_options = "".join(
        f'<option value="{c}">{c} ({CATEGORY_META.get(c, (c,))[0]})</option>'
        for c in CATEGORY_META if c in counts
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Injection Report - {escape(repo_label)}</title>
<style>
  :root {{
    --bg: #0d1117;
    --panel: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --muted: #8b949e;
    --accent: #2f81f7;
    --add: #033a16;
    --del: #67060c;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    flex-direction: column;
  }}
  header {{
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    gap: 12px;
    align-items: center;
    flex-wrap: wrap;
  }}
  header h1 {{ font-size: 16px; margin: 0; font-weight: 600; }}
  header input, header select {{
    background: var(--panel);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 13px;
  }}
  .chip {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 12px;
    background: color-mix(in srgb, var(--c) 25%, var(--panel));
    border: 1px solid var(--c);
    color: #fff;
    cursor: pointer;
    user-select: none;
  }}
  .chip.muted {{ opacity: 0.35; }}
  main {{
    display: flex;
    flex: 1;
    min-height: 0;
  }}
  #list {{
    width: 360px;
    border-right: 1px solid var(--border);
    overflow-y: auto;
    background: var(--panel);
  }}
  .item {{
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    font-size: 13px;
  }}
  .item:hover {{ background: #1f2630; }}
  .item.active {{ background: #1f2937; border-left: 3px solid var(--accent); }}
  .item .pr {{ color: var(--muted); font-size: 11px; }}
  .item .title {{ margin: 2px 0; }}
  .item .meta {{ font-size: 11px; color: var(--muted); }}
  .cat-dot {{
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: middle;
  }}
  #detail {{ flex: 1; overflow-y: auto; padding: 18px 24px; }}
  #detail h2 {{ font-size: 18px; margin: 0 0 8px; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
    margin: 14px 0;
  }}
  .stat {{
    background: var(--panel);
    border: 1px solid var(--border);
    padding: 10px 12px;
    border-radius: 6px;
  }}
  .stat .label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; }}
  .stat .value {{ font-size: 18px; font-weight: 600; margin-top: 4px; }}
  .stat.ok .value {{ color: #3fb950; }}
  .stat.bad .value {{ color: #f85149; }}
  .stat.warn .value {{ color: #d29922; }}
  section {{ margin-top: 18px; }}
  section h3 {{
    font-size: 13px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin: 0 0 8px;
  }}
  pre.diff {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
    font-family: ui-monospace, Consolas, monospace;
    font-size: 12px;
    line-height: 1.5;
    overflow-x: auto;
    white-space: pre;
    margin: 0;
  }}
  pre.diff .add {{ background: var(--add); display: block; }}
  pre.diff .del {{ background: var(--del); display: block; }}
  pre.diff .hunk {{ color: #79c0ff; display: block; }}
  pre.diff .meta {{ color: var(--muted); display: block; }}
  .diff-grid {{
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 12px;
  }}
  .diff-grid > div {{ min-width: 0; }}
  .diff-grid pre.diff {{ max-height: 520px; overflow: auto; }}
  @media (max-width: 1100px) {{
    .diff-grid {{ grid-template-columns: minmax(0, 1fr); }}
  }}
  .diff-summary {{
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 10px;
    align-items: center;
  }}
  .diff-summary .pill {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 2px 10px;
  }}
  .diff-summary .pill.match {{ color: #3fb950; border-color: #2ea043; }}
  .diff-summary .pill.extra {{ color: #3fb950; border-color: #2ea043; }}
  .diff-summary .pill.missing {{ color: #f85149; border-color: #cf222e; }}
  .diff-summary label {{ cursor: pointer; user-select: none; color: var(--text); }}
  details.raw-diff {{ margin-top: 16px; }}
  details.raw-diff > summary {{
    cursor: pointer;
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 8px;
  }}
  pre.delta {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
    font-family: ui-monospace, Consolas, monospace;
    font-size: 12px;
    line-height: 1.5;
    overflow-x: auto;
    white-space: pre;
    margin: 0;
    max-height: 520px;
  }}
  pre.delta .extra {{ background: var(--add); display: block; }}
  pre.delta .missing {{ background: var(--del); display: block; }}
  pre.delta .file {{ color: #79c0ff; display: block; padding-top: 4px; }}
  .diff-header {{
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .ai-summary {{
    background: linear-gradient(180deg, #1c2535 0%, #161b22 100%);
    border: 1px solid #2f81f7;
    border-radius: 8px;
    padding: 14px 18px;
    margin: 14px 0;
  }}
  .ai-summary .verdict {{
    font-size: 15px;
    font-weight: 600;
    color: #79c0ff;
    margin-bottom: 8px;
  }}
  .ai-summary dl {{
    display: grid;
    grid-template-columns: 130px 1fr;
    row-gap: 4px;
    column-gap: 12px;
    margin: 0;
    font-size: 12.5px;
  }}
  .ai-summary dt {{
    color: var(--muted);
    text-transform: uppercase;
    font-size: 10.5px;
    letter-spacing: 0.5px;
    padding-top: 2px;
  }}
  .ai-summary dd {{ margin: 0; color: var(--text); }}
  .ai-analysis {{
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px dashed var(--border);
  }}
  .ai-analysis-label {{
    font-size: 10.5px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 6px;
  }}
  .ai-analysis-body p {{
    margin: 0 0 8px;
    font-size: 12.5px;
    line-height: 1.55;
    color: var(--text);
  }}
  .ai-analysis-body p:last-child {{ margin-bottom: 0; }}
  .ai-tag {{
    display: inline-block;
    font-size: 10px;
    background: #2f81f7;
    color: white;
    padding: 2px 6px;
    border-radius: 4px;
    vertical-align: middle;
    margin-right: 8px;
    letter-spacing: 0.5px;
  }}
  ul.tests {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin: 0;
    padding: 10px 16px 10px 28px;
    font-family: ui-monospace, Consolas, monospace;
    font-size: 12px;
    max-height: 200px;
    overflow-y: auto;
  }}
  ul.tests li {{ color: #f85149; }}
  ul.tests.healthy li {{ color: #d29922; }}
  .files {{ font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: var(--muted); }}
  .files div {{ padding: 2px 0; }}
  .empty {{ color: var(--muted); font-style: italic; }}
  .badge {{
    display: inline-block;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    background: var(--panel);
    border: 1px solid var(--border);
    color: var(--muted);
    margin-left: 6px;
  }}
</style>
</head>
<body>
<header>
  <h1>Injection Report — {escape(repo_label)}</h1>
  <span class="badge" id="totalCount"></span>
  <input id="search" type="text" placeholder="search title or PR…" style="flex:1;min-width:200px">
  <select id="catFilter">
    <option value="">All categories</option>
    {cat_options}
  </select>
  <span style="flex-basis:100%;margin-top:6px">{summary_html}</span>
</header>
<main>
  <div id="list"></div>
  <div id="detail"><p class="empty">Select a PR on the left.</p></div>
</main>
<script id="data" type="application/json">{data_json}</script>
<script>
const META = {json.dumps(CATEGORY_META)};
const RECORDS = JSON.parse(document.getElementById('data').textContent);
const listEl = document.getElementById('list');
const detailEl = document.getElementById('detail');
const search = document.getElementById('search');
const catFilter = document.getElementById('catFilter');
const totalCount = document.getElementById('totalCount');
let activePr = null;
let chipFilter = null;

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }})[c]);
}}

function renderDiff(diff) {{
  if (!diff) return '<p class="empty">No diff available.</p>';
  const lines = diff.split('\\n').map(l => {{
    const safe = escapeHtml(l);
    if (l.startsWith('+++') || l.startsWith('---') || l.startsWith('diff ') || l.startsWith('index '))
      return `<span class="meta">${{safe}}</span>`;
    if (l.startsWith('@@'))
      return `<span class="hunk">${{safe}}</span>`;
    if (l.startsWith('+'))
      return `<span class="add">${{safe}}</span>`;
    if (l.startsWith('-'))
      return `<span class="del">${{safe}}</span>`;
    return safe;
  }});
  return `<pre class="diff">${{lines.join('\\n')}}</pre>`;
}}

function parseDiffByFile(diff) {{
  if (!diff) return {{}};
  const files = {{}};
  let current = '__pre__';
  files[current] = {{ added: [], removed: [] }};
  for (const raw of diff.split('\\n')) {{
    const m = raw.match(/^diff --git a\\/(\\S+) b\\/(\\S+)/);
    if (m) {{
      current = m[2];
      if (!files[current]) files[current] = {{ added: [], removed: [] }};
      continue;
    }}
    if (raw.startsWith('+++') || raw.startsWith('---') || raw.startsWith('@@') || raw.startsWith('index ')) continue;
    if (raw.startsWith('+')) files[current].added.push(raw.slice(1));
    else if (raw.startsWith('-')) files[current].removed.push(raw.slice(1));
  }}
  return files;
}}

function normalizeLine(s) {{
  return s.replace(/\\s+/g, '').replace(/\\r/g, '');
}}

function computeDelta(injected, original) {{
  // Mirror: reverse injected so + becomes - and vice versa, then compare per file.
  const injFiles = parseDiffByFile(injected);
  const origFiles = parseDiffByFile(original);
  const allFiles = new Set([...Object.keys(injFiles), ...Object.keys(origFiles)]);
  let matched = 0, extra = 0, missing = 0;
  const perFile = [];
  for (const f of [...allFiles].sort()) {{
    if (f === '__pre__') continue;
    const inj = injFiles[f] || {{ added: [], removed: [] }};
    const orig = origFiles[f] || {{ added: [], removed: [] }};
    // Reverse-injected: added in inj corresponds to removed in orig, vice versa.
    const tally = (injSide, origSide) => {{
      const origMap = new Map();
      for (const l of origSide) {{
        const k = normalizeLine(l);
        if (!k) continue;
        origMap.set(k, (origMap.get(k) || 0) + 1);
      }}
      const extras = [];
      for (const l of injSide) {{
        const k = normalizeLine(l);
        if (!k) continue;
        if ((origMap.get(k) || 0) > 0) origMap.set(k, origMap.get(k) - 1);
        else extras.push(l);
      }}
      const missings = [];
      for (const [k, cnt] of origMap) {{
        if (cnt <= 0) continue;
        for (const l of origSide) {{
          if (normalizeLine(l) === k) {{
            missings.push(l);
            break;
          }}
        }}
      }}
      return {{ extras, missings, matchedCount: injSide.filter(l => normalizeLine(l)).length - extras.length }};
    }};
    const addSide = tally(inj.added, orig.removed);     // injected '+' should mirror orig '-'
    const delSide = tally(inj.removed, orig.added);     // injected '-' should mirror orig '+'
    const fileExtra = addSide.extras.length + delSide.extras.length;
    const fileMissing = addSide.missings.length + delSide.missings.length;
    const fileMatched = addSide.matchedCount + delSide.matchedCount;
    if (fileExtra === 0 && fileMissing === 0 && fileMatched === 0) continue;
    matched += fileMatched;
    extra += fileExtra;
    missing += fileMissing;
    perFile.push({{
      file: f,
      extraAdds: addSide.extras,
      extraDels: delSide.extras,
      missingDels: addSide.missings,
      missingAdds: delSide.missings,
    }});
  }}
  const total = matched + extra + missing;
  const matchRate = total > 0 ? Math.round((matched / total) * 100) : 0;
  return {{ matched, extra, missing, matchRate, perFile }};
}}

function renderDeltaSection(r) {{
  const delta = computeDelta(r.diff || '', r.original_patch || '');
  const summary = `<div class="diff-summary">
    <span class="pill match">match ${{delta.matchRate}}%</span>
    <span class="pill match">${{delta.matched}} matched lines</span>
    <span class="pill extra">+${{delta.extra}} extra in injection</span>
    <span class="pill missing">-${{delta.missing}} missing vs original</span>
  </div>`;
  return summary + renderDelta(delta);
}}

function renderDelta(delta) {{
  if (!delta || delta.perFile.length === 0) {{
    if (delta && delta.matched > 0) {{
      return `<p class="empty">Injection mirrors the original PR exactly (whitespace ignored). ${{delta.matched}} lines matched.</p>`;
    }}
    return '<p class="empty">No comparable diff content.</p>';
  }}
  const blocks = delta.perFile.map(f => {{
    const lines = [`<span class="file">--- ${{escapeHtml(f.file)}} ---</span>`];
    for (const l of f.extraAdds) lines.push(`<span class="extra">+ ${{escapeHtml(l)}}</span>`);
    for (const l of f.extraDels) lines.push(`<span class="missing">- ${{escapeHtml(l)}}</span>`);
    for (const l of f.missingDels) lines.push(`<span class="missing">(orig -) ${{escapeHtml(l)}}</span>`);
    for (const l of f.missingAdds) lines.push(`<span class="extra">(orig +) ${{escapeHtml(l)}}</span>`);
    return lines.join('\\n');
  }});
  return `<pre class="delta">${{blocks.join('\\n\\n')}}</pre>`;
}}

function renderList() {{
  const q = search.value.trim().toLowerCase();
  const cf = chipFilter || catFilter.value;
  const items = RECORDS.filter(r => {{
    if (cf && r.category !== cf) return false;
    if (q && !(`${{r.pr}}`.includes(q) || r.title.toLowerCase().includes(q))) return false;
    return true;
  }});
  totalCount.textContent = `${{items.length}} / ${{RECORDS.length}} PRs`;
  listEl.innerHTML = items.map(r => {{
    const color = (META[r.category] || ['', '#999'])[1];
    const active = r.pr === activePr ? ' active' : '';
    return `<div class="item${{active}}" data-pr="${{r.pr}}">
      <div class="pr">PR #${{r.pr}} · ${{r.level}}</div>
      <div class="title">${{escapeHtml(r.title)}}</div>
      <div class="meta">
        <span class="cat-dot" style="background:${{color}}"></span>${{r.category}}
      </div>
    </div>`;
  }}).join('');
  for (const el of listEl.querySelectorAll('.item')) {{
    el.addEventListener('click', () => {{
      activePr = parseInt(el.dataset.pr, 10);
      renderList();
      renderDetail();
    }});
  }}
}}

function statBox(label, value, cls = '') {{
  return `<div class="stat ${{cls}}"><div class="label">${{label}}</div><div class="value">${{value}}</div></div>`;
}}

function renderSummary(s) {{
  if (!s || (!s.verdict && !s.analysis)) return '';
  const row = (label, val) => val ? `<dt>${{escapeHtml(label)}}</dt><dd>${{escapeHtml(val)}}</dd>` : '';
  const analysisHtml = s.analysis
    ? `<div class="ai-analysis"><div class="ai-analysis-label">Detailed analysis</div><div class="ai-analysis-body">${{escapeHtml(s.analysis).replace(/\\n\\n/g, '</p><p>').replace(/\\n/g, '<br>').replace(/^/, '<p>').replace(/$/, '</p>')}}</div></div>`
    : '';
  const verdictHtml = s.verdict
    ? `<div class="verdict"><span class="ai-tag">AI</span>${{escapeHtml(s.verdict)}}</div>`
    : '';
  return `<div class="ai-summary">
    ${{verdictHtml}}
    <dl>
      ${{row('PR intent', s.pr_intent)}}
      ${{row('Injection quality', s.injection_quality)}}
      ${{row('Root cause', s.root_cause)}}
      ${{row('Next action', s.next_action)}}
    </dl>
    ${{analysisHtml}}
  </div>`;
}}

function renderDetail() {{
  const r = RECORDS.find(x => x.pr === activePr);
  if (!r) {{
    detailEl.innerHTML = '<p class="empty">Select a PR on the left.</p>';
    return;
  }}
  const color = (META[r.category] || ['', '#999'])[1];
  const verdict = r.p2f ? 'PASS-TO-FAIL ✓' : (r.failure_reason ? `SKIPPED · ${{escapeHtml(r.failure_reason)}}` : 'P2F NOT CONFIRMED');
  const verdictCls = r.p2f ? 'ok' : 'bad';
  const deltaCls = r.delta > 0 ? 'ok' : (r.delta < 0 ? 'warn' : '');

  const newFailedHtml = r.new_failed_tests.length
    ? `<ul class="tests">${{r.new_failed_tests.map(t => `<li>${{escapeHtml(t)}}</li>`).join('')}}</ul>`
    : '<p class="empty">None.</p>';
  const healthyFailedHtml = r.healthy_failed_tests.length
    ? `<ul class="tests healthy">${{r.healthy_failed_tests.map(t => `<li>${{escapeHtml(t)}}</li>`).join('')}}</ul>`
    : '<p class="empty">Clean baseline.</p>';

  detailEl.innerHTML = `
    <h2>PR #${{r.pr}} <span class="badge" style="border-color:${{color}};color:${{color}}">${{r.category}}</span> <span class="badge">${{r.level}}</span></h2>
    <div style="color:var(--muted)">${{escapeHtml(r.title)}}</div>
    ${{renderSummary(r.summary)}}
    <div class="grid">
      ${{statBox('Verdict', verdict, verdictCls)}}
      ${{statBox('Healthy', `${{r.healthy_passed}}/${{r.healthy_total}}`, r.healthy_failed === 0 ? 'ok' : 'warn')}}
      ${{statBox('Buggy', `${{r.buggy_passed}}/${{r.buggy_total}}`, r.buggy_total === 0 ? 'bad' : '')}}
      ${{statBox('Δ failures', (r.delta > 0 ? '+' : '') + r.delta, deltaCls)}}
    </div>
    <section>
      <h3>New failed tests after injection</h3>
      ${{newFailedHtml}}
    </section>
    <section>
      <h3>Baseline failed tests on HEAD</h3>
      ${{healthyFailedHtml}}
    </section>
    <section>
      <h3>Source files</h3>
      <div class="files">${{r.source_files.map(f => `<div>${{escapeHtml(f)}}</div>`).join('') || '<span class="empty">—</span>'}}</div>
    </section>
    <section>
      <h3>Test files</h3>
      <div class="files">${{r.test_files.map(f => `<div>${{escapeHtml(f)}}</div>`).join('') || '<span class="empty">—</span>'}}</div>
    </section>
    <section>
      <h3>Diff comparison</h3>
      <div class="diff-grid">
        <div>
          <div class="diff-header">Injected diff (after revert)</div>
          ${{renderDiff(r.diff)}}
        </div>
        <div>
          <div class="diff-header">Original PR diff (the fix)</div>
          ${{renderDiff(r.original_patch)}}
        </div>
      </div>
      <details class="raw-diff" open>
        <summary>Show delta (injection vs original, whitespace-ignored)</summary>
        ${{renderDeltaSection(r)}}
      </details>
    </section>
    <section>
      <h3>Commits</h3>
      <div class="files">
        <div>merge_commit: ${{escapeHtml(r.merge_commit_sha)}}</div>
        <div>base: ${{escapeHtml(r.base_sha)}}</div>
        ${{r.duration ? `<div>duration: ${{r.duration}}s</div>` : ''}}
      </div>
    </section>
  `;
}}

search.addEventListener('input', renderList);
catFilter.addEventListener('change', () => {{ chipFilter = null; updateChips(); renderList(); }});

document.querySelectorAll('.chip').forEach(chip => {{
  chip.addEventListener('click', () => {{
    const c = chip.dataset.cat;
    chipFilter = chipFilter === c ? null : c;
    catFilter.value = '';
    updateChips();
    renderList();
  }});
}});

function updateChips() {{
  document.querySelectorAll('.chip').forEach(chip => {{
    chip.classList.toggle('muted', chipFilter && chip.dataset.cat !== chipFilter);
  }});
}}

renderList();
</script>
</body>
</html>
"""


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: build_injection_report.py <experiments/<repo>>", file=sys.stderr)
        sys.exit(2)
    exp_dir = Path(sys.argv[1])
    src = exp_dir / "injection_results.jsonl"
    if not src.exists():
        print(f"file not found: {src}", file=sys.stderr)
        sys.exit(1)

    records: list[dict] = []
    with open(src, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))

    # Load original PR patches from sampled.jsonl and merge
    sampled = exp_dir / "sampled.jsonl"
    patches: dict[int, dict] = {}
    if sampled.exists():
        with open(sampled, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                s = json.loads(line)
                patches[s["pr_number"]] = {
                    "patch": s.get("patch", ""),
                    "html_url": s.get("html_url", ""),
                }
    for r in records:
        meta = patches.get(r.get("pr_number"))
        if meta:
            patch = meta["patch"] or ""
            # Truncate very large patches to keep the HTML manageable
            if len(patch) > 80000:
                patch = patch[:80000] + "\n... (truncated, original patch too large)"
            r["_original_patch"] = patch
            r["_html_url"] = meta["html_url"]
        # Also cap injected_diff defensively
        diff = r.get("injected_diff") or ""
        if len(diff) > 80000:
            r["injected_diff"] = diff[:80000] + "\n... (truncated)"

    # Load AI-generated PR summaries if present
    summary_file = exp_dir / "pr_summaries.json"
    if summary_file.exists():
        try:
            summaries = json.loads(summary_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summaries = {}
        for r in records:
            s = summaries.get(str(r.get("pr_number")))
            if s:
                r["_summary"] = s

    out = exp_dir / "report.html"
    out.write_text(build_html(records, exp_dir.name), encoding="utf-8")
    print(f"wrote {out}  ({len(records)} records, {sum(1 for r in records if r.get('_original_patch'))} with original patch)")


if __name__ == "__main__":
    main()
