#!/usr/bin/env python3
"""Turn pytest's JUnit XML into a single self-contained HTML report.

Why a generator rather than `pytest-html`: every job in this pipeline already
writes JUnit XML, so this needs no new plugin in the merge gate's install path,
and — more importantly — it can say things a generic report cannot.

This suite's most important signal is its **8 xfails**. Each one asserts the
behaviour the service *should* have and names a documented defect by risk ID
(see TEST_STRATEGY.md). A generic report files those under "skipped" and moves on,
which is exactly backwards: they are the findings. Here they get their own
section, and every test is cross-referenced against the risk IDs in its docstring
so a reader can ask "what does this test protect?" and get an answer.

Usage:
    python tools/test_report.py                              # reads reports/*.xml
    python tools/test_report.py --junit reports/junit.xml
    python tools/test_report.py --junit a.xml b.xml -o out/report.html
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import html
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RISK_PATTERN = re.compile(r"\bR\d{1,2}\b")

#: Layer names come from the directory a test lives in, which is also how the
#: markers are organised. Order is the order they run in the pipeline.
LAYER_ORDER = ["unit", "contract", "api", "e2e", "perf", "ui", "other"]
LAYER_BLURB = {
    "unit": "Scoring boundaries and Hypothesis properties. No I/O.",
    "contract": "Responses validated against the service's own /openapi.json.",
    "api": "In-process integration against a per-test isolated database.",
    "e2e": "Live HTTP against a running deployment.",
    "perf": "Latency budget and concurrent-write invariants. Never gates a merge.",
    "ui": "One Playwright dashboard smoke test.",
    "other": "Uncategorised.",
}

Outcome = str  # "passed" | "failed" | "error" | "known" | "skipped"


@dataclass
class Case:
    module: str
    name: str
    layer: str
    outcome: Outcome
    duration_s: float
    message: str = ""
    detail: str = ""
    risks: list[str] = field(default_factory=list)

    @property
    def node_id(self) -> str:
        return f"{self.module.replace('.', '/')}.py::{self.name}"

    @property
    def display_name(self) -> str:
        """`test_a_dead_station_is_flagged` -> `a dead station is flagged`."""
        return self.name.removeprefix("test_").replace("_", " ")


def collect_docstring_risks() -> dict[tuple[str, str], list[str]]:
    """Map (module, test name) -> the risk IDs that test is actually about.

    JUnit XML carries no docstrings, so this reads them from the source. It is what
    lets the report answer "which failure mode does this test protect against?".

    A test's *own* docstring wins outright; the module docstring is only a fallback
    for tests that cite nothing themselves. Merging both was the first thing I
    tried and it was worse: most modules name four or five risks in their header,
    so every test in the file inherited them, three chips appeared on rows that
    genuinely covered one, and filtering for "R2" matched 51 of 91 tests. Precise
    attribution is the whole value of showing the chip.
    """
    found: dict[tuple[str, str], list[str]] = {}
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - report must never crash
            continue
        module = ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
        module_risks = list(dict.fromkeys(RISK_PATTERN.findall(ast.get_docstring(tree) or "")))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test_"
            ):
                own = list(dict.fromkeys(RISK_PATTERN.findall(ast.get_docstring(node) or "")))
                found[(module, node.name)] = own or module_risks
    return found


def layer_of(module: str) -> str:
    parts = module.split(".")
    for part in parts:
        if part in LAYER_ORDER:
            return part
    return "other"


def parse_junit(paths: list[Path]) -> tuple[list[Case], dict[str, str]]:
    """Read one or more JUnit files into a flat list of cases, plus run metadata."""
    docstring_risks = collect_docstring_risks()
    cases: list[Case] = []
    meta: dict[str, str] = {}
    total_time = 0.0

    for path in paths:
        root = ET.parse(path).getroot()
        suites = root.findall("testsuite") or ([root] if root.tag == "testsuite" else [])
        for suite in suites:
            total_time += float(suite.get("time") or 0.0)
            meta.setdefault("timestamp", suite.get("timestamp") or "")
            meta.setdefault("hostname", suite.get("hostname") or "")
            for element in suite.iter("testcase"):
                module = element.get("classname") or ""
                name = element.get("name") or ""
                # pytest parametrisation lands in the name: keep it, strip it for lookup.
                base_name = name.split("[")[0]
                outcome: Outcome = "passed"
                message = detail = ""

                if (failure := element.find("failure")) is not None:
                    outcome = "failed"
                    message, detail = failure.get("message") or "", failure.text or ""
                elif (error := element.find("error")) is not None:
                    outcome = "error"
                    message, detail = error.get("message") or "", error.text or ""
                elif (skipped := element.find("skipped")) is not None:
                    # This is the distinction a generic report throws away: an xfail
                    # is a documented defect, not an un-run test.
                    is_xfail = (skipped.get("type") or "").endswith("xfail")
                    outcome = "known" if is_xfail else "skipped"
                    message, detail = skipped.get("message") or "", skipped.text or ""

                risks = RISK_PATTERN.findall(message)
                risks += [r for r in docstring_risks.get((module, base_name), []) if r not in risks]

                cases.append(
                    Case(
                        module=module,
                        name=name,
                        layer=layer_of(module),
                        outcome=outcome,
                        duration_s=float(element.get("time") or 0.0),
                        message=message.strip(),
                        detail=detail.strip(),
                        risks=risks,
                    )
                )

    meta["duration_s"] = f"{total_time:.2f}"
    meta["sources"] = ", ".join(p.name for p in paths)
    return cases, meta


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
STATUS_LABEL = {
    "passed": "Passed",
    "failed": "Failed",
    "error": "Error",
    "known": "Known defect",
    "skipped": "Skipped",
}

CSS = """
/* Tokens. The bare :root is the complete light palette; the two blocks below
   redefine only tokens, so every component resolves in all three viewer states
   (explicit light, explicit dark, and un-stamped "system"). */
:root {
  --ground:      #f2f5f6;
  --surface:     #ffffff;
  --surface-sunk:#e9eef0;
  --line:        #d3dcdf;
  --line-soft:   #e4eaec;
  --ink:         #0e1719;
  --ink-soft:    #3d4f54;
  --muted:       #5f7176;
  --accent:      #0b6e77;
  --accent-soft: #d7ecee;
  --pass:        #1b7a4b;
  --pass-soft:   #dcf0e5;
  --fail:        #ae2318;
  --fail-soft:   #fbe3e0;
  --known:       #8a5300;
  --known-soft:  #fbeed6;
  --skip:        #64757a;
  --skip-soft:   #e6ebec;
  --shadow:      0 1px 2px rgb(14 23 25 / 6%), 0 8px 24px -16px rgb(14 23 25 / 24%);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:      #0b1215;
    --surface:     #131e22;
    --surface-sunk:#0e181b;
    --line:        #27373d;
    --line-soft:   #1d2a2e;
    --ink:         #e7eef0;
    --ink-soft:    #bccace;
    --muted:       #8ba1a6;
    --accent:      #48c6cf;
    --accent-soft: #12333a;
    --pass:        #4fcb86;
    --pass-soft:   #12301f;
    --fail:        #ff8073;
    --fail-soft:   #3a1512;
    --known:       #e5a84b;
    --known-soft:  #33230a;
    --skip:        #8ba1a6;
    --skip-soft:   #1b262a;
    --shadow:      0 1px 2px rgb(0 0 0 / 40%), 0 8px 24px -16px rgb(0 0 0 / 60%);
  }
}
:root[data-theme="dark"] {
  --ground:      #0b1215;
  --surface:     #131e22;
  --surface-sunk:#0e181b;
  --line:        #27373d;
  --line-soft:   #1d2a2e;
  --ink:         #e7eef0;
  --ink-soft:    #bccace;
  --muted:       #8ba1a6;
  --accent:      #48c6cf;
  --accent-soft: #12333a;
  --pass:        #4fcb86;
  --pass-soft:   #12301f;
  --fail:        #ff8073;
  --fail-soft:   #3a1512;
  --known:       #e5a84b;
  --known-soft:  #33230a;
  --skip:        #8ba1a6;
  --skip-soft:   #1b262a;
  --shadow:      0 1px 2px rgb(0 0 0 / 40%), 0 8px 24px -16px rgb(0 0 0 / 60%);
}

:root {
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI",
          system-ui, Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
          "Liberation Mono", monospace;
  --step--1: 0.78rem;
  --step-0:  0.9375rem;
  --step-1:  1.0625rem;
  --step-2:  1.375rem;
  --step-3:  2.125rem;
}

*, *::before, *::after { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font: 400 var(--step-0)/1.55 var(--sans);
  -webkit-font-smoothing: antialiased;
}

.wrap {
  max-width: 68rem;
  margin: 0 auto;
  padding: clamp(1.25rem, 3vw, 2.75rem) clamp(1rem, 3vw, 2rem) 4rem;
  display: flex;
  flex-direction: column;
  gap: 2rem;
}

/* --- masthead ---------------------------------------------------------- */
.masthead { display: flex; flex-direction: column; gap: 1rem; }
.eyebrow {
  font: 600 var(--step--1)/1 var(--mono);
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--muted);
}
h1 {
  margin: 0;
  font-size: var(--step-3);
  font-weight: 680;
  letter-spacing: -0.022em;
  text-wrap: balance;
  line-height: 1.1;
}
.verdict-row { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem; }
.verdict {
  display: inline-flex; align-items: center; gap: 0.5rem;
  padding: 0.3rem 0.7rem;
  border-radius: 2px;
  font: 650 var(--step--1)/1 var(--mono);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  border: 1px solid;
}
.verdict.is-pass  { color: var(--pass); background: var(--pass-soft); border-color: var(--pass); }
.verdict.is-fail  { color: var(--fail); background: var(--fail-soft); border-color: var(--fail); }
.verdict .dot { width: 0.5rem; height: 0.5rem; border-radius: 50%; background: currentColor; }
.runmeta {
  margin: 0;
  font-family: var(--mono);
  font-size: var(--step--1);
  color: var(--muted);
}
.runmeta span + span::before { content: "·"; margin: 0 0.5rem; color: var(--line); }
.provenance {
  margin: 0;
  padding: 0.6rem 0.75rem;
  background: var(--accent-soft);
  border-left: 3px solid var(--accent);
  border-radius: 2px;
  font-size: var(--step--1);
  color: var(--ink-soft);
}
.provenance strong {
  font-family: var(--mono);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
  margin-right: 0.5rem;
}

/* --- proportion bar: widths are the real proportions ------------------- */
.bar {
  display: flex;
  height: 0.6rem;
  border-radius: 2px;
  overflow: hidden;
  background: var(--surface-sunk);
  border: 1px solid var(--line-soft);
}
.bar > i { display: block; }
.bar .seg-passed  { background: var(--pass); }
.bar .seg-failed  { background: var(--fail); }
.bar .seg-error   { background: var(--fail); opacity: 0.7; }
.bar .seg-known   { background: var(--known); }
.bar .seg-skipped { background: var(--skip); opacity: 0.45; }

/* --- counter tiles ----------------------------------------------------- */
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr));
  gap: 0.75rem;
}
.tile {
  background: var(--surface);
  border: 1px solid var(--line);
  border-top: 3px solid var(--edge, var(--line));
  border-radius: 3px;
  padding: 0.9rem 1rem;
  box-shadow: var(--shadow);
  display: flex; flex-direction: column; gap: 0.15rem;
}
.tile.is-passed { --edge: var(--pass); }
.tile.is-failed { --edge: var(--fail); }
.tile.is-known  { --edge: var(--known); }
.tile.is-skipped{ --edge: var(--skip); }
.tile.is-total  { --edge: var(--accent); }
.tile dt {
  font: 600 var(--step--1)/1 var(--mono);
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted);
}
.tile dd {
  margin: 0;
  font: 650 var(--step-2)/1.1 var(--sans);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}
.tile .sub { font: 400 var(--step--1)/1.3 var(--mono); color: var(--muted); }

/* --- sections ---------------------------------------------------------- */
section { display: flex; flex-direction: column; gap: 0.9rem; }
h2 {
  margin: 0;
  font-size: var(--step-1);
  font-weight: 650;
  letter-spacing: -0.012em;
  display: flex; align-items: baseline; gap: 0.6rem;
}
h2 .count {
  font: 500 var(--step--1)/1 var(--mono);
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.lede { margin: 0; max-width: 62ch; color: var(--ink-soft); }

/* --- result rows ------------------------------------------------------- */
.rows { display: flex; flex-direction: column; gap: 0.5rem; }
.row {
  background: var(--surface);
  border: 1px solid var(--line);
  border-left: 3px solid var(--stripe, var(--line));
  border-radius: 3px;
  box-shadow: var(--shadow);
}
.row.is-failed, .row.is-error { --stripe: var(--fail); }
.row.is-known   { --stripe: var(--known); }
.row.is-passed  { --stripe: var(--pass); }
.row.is-skipped { --stripe: var(--skip); }
.row > summary {
  cursor: pointer;
  list-style: none;
  padding: 0.7rem 0.9rem;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.3rem 0.75rem;
  align-items: start;
}
.row > summary::-webkit-details-marker { display: none; }
.row > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.row[open] > summary { border-bottom: 1px solid var(--line-soft); }
.title { font-weight: 550; letter-spacing: -0.005em; }
.path {
  grid-column: 1 / -1;
  font: 400 var(--step--1)/1.4 var(--mono);
  color: var(--muted);
  word-break: break-all;
}
.meta { display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; justify-content: flex-end; }
.dur {
  font: 400 var(--step--1)/1 var(--mono);
  font-variant-numeric: tabular-nums;
  color: var(--muted);
}
.chip {
  font: 600 var(--step--1)/1 var(--mono);
  letter-spacing: 0.04em;
  padding: 0.2rem 0.4rem;
  border-radius: 2px;
  border: 1px solid transparent;
  white-space: nowrap;
}
.chip.risk { color: var(--accent); background: var(--accent-soft); border-color: var(--accent); }
.chip.state-failed, .chip.state-error { color: var(--fail); background: var(--fail-soft); border-color: var(--fail); }
.chip.state-known { color: var(--known); background: var(--known-soft); border-color: var(--known); }
.chip.state-passed { color: var(--pass); background: var(--pass-soft); border-color: var(--pass); }
.chip.state-skipped { color: var(--skip); background: var(--skip-soft); border-color: var(--skip); }
.body { padding: 0.8rem 0.9rem 1rem; display: flex; flex-direction: column; gap: 0.7rem; }
.reason {
  margin: 0;
  font-family: var(--mono);
  font-size: var(--step--1);
  line-height: 1.5;
  color: var(--ink);
  background: var(--surface-sunk);
  border-left: 2px solid var(--stripe, var(--line));
  padding: 0.6rem 0.75rem;
  white-space: pre-wrap;
  overflow-x: auto;
}
pre.trace {
  margin: 0;
  font-family: var(--mono);
  font-size: var(--step--1);
  line-height: 1.5;
  background: var(--surface-sunk);
  border: 1px solid var(--line-soft);
  border-radius: 2px;
  padding: 0.7rem 0.8rem;
  overflow-x: auto;
  color: var(--ink-soft);
}
.trace-toggle {
  font: 600 var(--step--1)/1 var(--mono);
  letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--accent); cursor: pointer; width: fit-content;
}
.trace-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

/* --- layer table ------------------------------------------------------- */
.tablewrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 3px; background: var(--surface); box-shadow: var(--shadow); }
table { width: 100%; border-collapse: collapse; font-size: var(--step-0); }
caption { text-align: left; padding: 0.75rem 0.9rem; color: var(--muted); font-size: var(--step--1); font-family: var(--mono); }
th, td { text-align: left; padding: 0.6rem 0.9rem; border-top: 1px solid var(--line-soft); vertical-align: middle; }
thead th {
  border-top: none;
  font: 600 var(--step--1)/1 var(--mono);
  letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted);
  background: var(--surface-sunk);
  white-space: nowrap;
}
td.num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
td .layername { font-weight: 600; }
td .layerblurb { color: var(--muted); font-size: var(--step--1); }
td .bar { height: 0.4rem; min-width: 6rem; }

/* --- controls ---------------------------------------------------------- */
.controls { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.controls input[type="search"] {
  flex: 1 1 14rem;
  font: 400 var(--step-0)/1.4 var(--mono);
  padding: 0.45rem 0.6rem;
  color: var(--ink);
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 2px;
}
.controls input[type="search"]:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.filter {
  font: 600 var(--step--1)/1 var(--mono);
  letter-spacing: 0.06em; text-transform: uppercase;
  padding: 0.4rem 0.6rem;
  color: var(--ink-soft);
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 2px;
  cursor: pointer;
}
.filter[aria-pressed="true"] { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
.filter:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.empty { margin: 0; color: var(--muted); font-family: var(--mono); font-size: var(--step--1); }
.group > summary {
  cursor: pointer; list-style: none;
  font: 600 var(--step--1)/1.4 var(--mono);
  letter-spacing: 0.04em;
  color: var(--ink-soft);
  padding: 0.5rem 0;
  display: flex; gap: 0.6rem; align-items: center;
}
.group > summary::-webkit-details-marker { display: none; }
.group > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.group > summary .tally { color: var(--muted); font-weight: 400; }
.group { border-top: 1px solid var(--line-soft); }
.group .rows { padding: 0.25rem 0 0.9rem; }

footer {
  border-top: 1px solid var(--line);
  padding-top: 1rem;
  color: var(--muted);
  font-size: var(--step--1);
  font-family: var(--mono);
}
footer a { color: var(--accent); }

@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
"""

JS = """
const search = document.getElementById('q');
const filters = Array.from(document.querySelectorAll('.filter'));
const rows = Array.from(document.querySelectorAll('#all .row'));

function apply() {
  const needle = (search.value || '').toLowerCase().trim();
  const active = new Set(filters.filter(f => f.getAttribute('aria-pressed') === 'true')
                                .map(f => f.dataset.state));
  rows.forEach(row => {
    const matchesText = !needle || row.dataset.search.includes(needle);
    const matchesState = active.size === 0 || active.has(row.dataset.state);
    row.hidden = !(matchesText && matchesState);
  });
  document.querySelectorAll('#all .group').forEach(group => {
    const visible = Array.from(group.querySelectorAll('.row')).filter(r => !r.hidden).length;
    group.hidden = visible === 0;
    const tally = group.querySelector('.tally');
    if (tally) { tally.textContent = visible + ' shown'; }
    if (needle && visible > 0) { group.open = true; }
  });
}

search.addEventListener('input', apply);
filters.forEach(f => f.addEventListener('click', () => {
  f.setAttribute('aria-pressed', f.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
  apply();
}));
apply();
"""


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def render_row(case: Case, *, open_by_default: bool = False) -> str:
    chips = "".join(f'<span class="chip risk">{esc(risk)}</span>' for risk in case.risks[:3])
    state_chip = f'<span class="chip state-{case.outcome}">{STATUS_LABEL[case.outcome]}</span>'
    reason = (
        f'<p class="reason">{esc(case.message)}</p>'
        if case.message
        else '<p class="empty">No message recorded.</p>'
    )
    trace = ""
    if case.detail and case.detail != case.message:
        trace = (
            '<details class="tracebox"><summary class="trace-toggle">Full output</summary>'
            f'<pre class="trace">{esc(case.detail)}</pre></details>'
        )
    haystack = f"{case.node_id} {case.display_name} {case.message} {' '.join(case.risks)}".lower()
    return f"""<details class="row is-{case.outcome}" data-state="{case.outcome}"
         data-search="{esc(haystack)}"{" open" if open_by_default else ""}>
  <summary>
    <span class="title">{esc(case.display_name)}</span>
    <span class="meta">{chips}{state_chip}<span class="dur">{case.duration_s * 1000:.0f} ms</span></span>
    <span class="path">{esc(case.node_id)}</span>
  </summary>
  <div class="body">{reason}{trace}</div>
</details>"""


def render_bar(counts: Counter[str], total: int) -> str:
    if total == 0:
        return '<div class="bar"></div>'
    segments = "".join(
        f'<i class="seg-{state}" style="width:{counts[state] / total * 100:.4f}%"></i>'
        for state in ("passed", "known", "skipped", "failed", "error")
        if counts[state]
    )
    return f'<div class="bar" role="img" aria-label="{esc(bar_label(counts))}">{segments}</div>'


def bar_label(counts: Counter[str]) -> str:
    return ", ".join(
        f"{counts[state]} {STATUS_LABEL[state].lower()}"
        for state in ("passed", "failed", "error", "known", "skipped")
        if counts[state]
    )


def build_html(cases: list[Case], meta: dict[str, str], label: str = "") -> str:
    counts: Counter[str] = Counter(case.outcome for case in cases)
    total = len(cases)
    broken = counts["failed"] + counts["error"]
    verdict_class = "is-fail" if broken else "is-pass"
    verdict_text = f"{broken} failing" if broken else "All green"

    when = meta.get("timestamp") or ""
    with contextlib.suppress(ValueError):
        when = datetime.fromisoformat(when).strftime("%d %b %Y, %H:%M %Z").strip()

    tiles = "".join(
        f"""<div class="tile is-{key}">
      <dt>{label}</dt>
      <dd>{counts[key] if key != "total" else total}</dd>
      <span class="sub">{sub}</span>
    </div>"""
        for key, label, sub in [
            ("total", "Tests", f"{meta.get('duration_s', '0')} s wall clock"),
            ("passed", "Passed", f"{(counts['passed'] / total * 100) if total else 0:.0f}% of run"),
            ("failed", "Failed", "regressions to fix" if broken else "nothing to fix"),
            ("known", "Known defects", "documented service bugs"),
            ("skipped", "Skipped", "no service reachable"),
        ]
    )

    failing = [c for c in cases if c.outcome in ("failed", "error")]
    known = sorted((c for c in cases if c.outcome == "known"), key=lambda c: c.risks or ["zz"])

    failing_section = (
        f"""<section id="failures">
    <h2>Failures <span class="count">{len(failing)}</span></h2>
    <p class="lede">Every entry is a regression: the assertion, then the full pytest output.
      The risk chip names the failure mode the test was written to catch.</p>
    <div class="rows">{"".join(render_row(c, open_by_default=True) for c in failing)}</div>
  </section>"""
        if failing
        else ""
    )

    known_section = (
        f"""<section id="known">
    <h2>Known service defects <span class="count">{len(known)}</span></h2>
    <p class="lede">These are <em>expected</em> failures, not gaps. Each asserts the behaviour the
      service <em>should</em> have and is marked <code>xfail(strict=True)</code>, so the day the
      service is fixed the run turns red and forces this list to be updated. Full write-ups live in
      TEST_STRATEGY.md under &ldquo;Known service issues&rdquo;.</p>
    <div class="rows">{"".join(render_row(c, open_by_default=True) for c in known)}</div>
  </section>"""
        if known
        else ""
    )

    layer_rows = ""
    for layer in LAYER_ORDER:
        in_layer = [c for c in cases if c.layer == layer]
        if not in_layer:
            continue
        layer_counts: Counter[str] = Counter(c.outcome for c in in_layer)
        seconds = sum(c.duration_s for c in in_layer)
        layer_rows += f"""<tr>
      <td><span class="layername">{esc(layer)}</span><br><span class="layerblurb">{esc(LAYER_BLURB[layer])}</span></td>
      <td>{render_bar(layer_counts, len(in_layer))}</td>
      <td class="num">{len(in_layer)}</td>
      <td class="num">{layer_counts["passed"]}</td>
      <td class="num">{layer_counts["failed"] + layer_counts["error"]}</td>
      <td class="num">{layer_counts["known"]}</td>
      <td class="num">{seconds:.2f}s</td>
    </tr>"""

    groups = ""
    for module in sorted({c.module for c in cases}):
        in_module = [c for c in cases if c.module == module]
        module_counts: Counter[str] = Counter(c.outcome for c in in_module)
        groups += f"""<details class="group">
      <summary>{esc(module)} <span class="tally">{len(in_module)} shown</span>
        <span class="dur">{bar_label(module_counts)}</span></summary>
      <div class="rows">{"".join(render_row(c) for c in sorted(in_module, key=lambda c: c.name))}</div>
    </details>"""

    filters = "".join(
        f'<button type="button" class="filter" data-state="{state}" aria-pressed="false">'
        f"{STATUS_LABEL[state]}</button>"
        for state in ("failed", "known", "passed", "skipped")
        if counts[state]
    )

    label_block = (
        f'<p class="provenance"><strong>Run context</strong> {esc(label)}</p>' if label else ""
    )

    return f"""<title>Station Health Suite Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CSS}</style>
<div class="wrap">
  <header class="masthead">
    <p class="eyebrow">NOC Station Health API &middot; test automation suite</p>
    <h1>{total} tests, {counts["passed"]} passed, {broken} failed</h1>
    <div class="verdict-row">
      <span class="verdict {verdict_class}"><i class="dot"></i>{esc(verdict_text)}</span>
      <p class="runmeta"><span>{esc(when or "unknown time")}</span><span>{esc(meta.get("duration_s", "0"))} s</span><span>{esc(meta.get("hostname", "unknown host"))}</span><span>{esc(meta.get("sources", ""))}</span></p>
    </div>
    {render_bar(counts, total)}
    {label_block}
  </header>

  <dl class="tiles">{tiles}</dl>

  {failing_section}
  {known_section}

  <section id="layers">
    <h2>By layer</h2>
    <p class="lede">Layer weighting follows the risk register rather than a test pyramid: the
      integration layer is the heaviest because this service's defects are cross-endpoint, not
      arithmetic.</p>
    <div class="tablewrap">
      <table>
        <caption>Counts and wall-clock time per layer</caption>
        <thead><tr>
          <th scope="col">Layer</th><th scope="col">Mix</th><th scope="col">Tests</th>
          <th scope="col">Pass</th><th scope="col">Fail</th><th scope="col">Known</th>
          <th scope="col">Time</th>
        </tr></thead>
        <tbody>{layer_rows}</tbody>
      </table>
    </div>
  </section>

  <section id="all">
    <h2>Every test <span class="count">{total}</span></h2>
    <div class="controls">
      <input type="search" id="q" placeholder="filter by name, path, risk ID or message"
             aria-label="Filter tests">
      {filters}
    </div>
    {groups}
  </section>

  <footer>
    Generated by <code>tools/test_report.py</code> from JUnit XML
    ({esc(meta.get("sources", ""))}). Risk IDs are cross-referenced from each test's docstring;
    definitions are in <code>notes/risk-register.md</code>.
  </footer>
</div>
<script>{JS}</script>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--junit",
        nargs="+",
        type=Path,
        help="JUnit XML file(s). Defaults to every *.xml under reports/.",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=REPO_ROOT / "reports" / "test-report.html",
        help="Where to write the HTML (default: reports/test-report.html).",
    )
    parser.add_argument(
        "--label",
        default="",
        help="Provenance note shown in the masthead (branch, commit, job, or caveat).",
    )
    args = parser.parse_args()

    paths = args.junit or sorted((REPO_ROOT / "reports").glob("*.xml"))
    paths = [p for p in paths if p.exists()]
    if not paths:
        raise SystemExit("no JUnit XML found. Run `make coverage` first, or pass --junit <file>.")

    cases, meta = parse_junit(paths)
    if not cases:
        raise SystemExit(f"no test cases in {', '.join(str(p) for p in paths)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_html(cases, meta, args.label), encoding="utf-8")

    counts: Counter[str] = Counter(c.outcome for c in cases)
    print(
        f"{args.out}: {len(cases)} tests — "
        f"{counts['passed']} passed, {counts['failed'] + counts['error']} failed, "
        f"{counts['known']} known defects, {counts['skipped']} skipped"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
