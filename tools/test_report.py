#!/usr/bin/env python3
"""Render pytest's JUnit XML as one self-contained HTML page.

CI already writes JUnit XML, so this needs no extra plugin in the merge gate. It
exists to say two things a generic report does not:

* **Which failures are P0.** Tiers are stamped into the XML by `tests/conftest.py`,
  so the report reads them rather than re-deriving them.
* **Known defects are findings, not skips.** The suite's `xfail`s each name a real
  service defect; a generic report files them under "skipped".

Usage:
    python tools/test_report.py                          # reads reports/*.xml
    python tools/test_report.py --junit reports/junit.xml -o out.html
"""

from __future__ import annotations

import argparse
import html
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

TIERS = ["p0", "p1", "p2", "untiered"]
TIER_BLURB = {
    "p0": "The service is doing its core job wrong. Read these first.",
    "p1": "A real defect with a narrower blast radius, or a specific edge.",
    "p2": "Worth having, not worth blocking on.",
    "untiered": "No tier declared — tests/conftest.py normally rejects these.",
}
LABELS = {"passed": "Passed", "failed": "Failed", "known": "Known defect", "skipped": "Skipped"}


@dataclass(frozen=True)
class Case:
    module: str
    name: str
    outcome: str  # passed | failed | known | skipped
    tier: str
    duration_s: float
    message: str
    detail: str

    @property
    def title(self) -> str:
        return self.name.removeprefix("test_").replace("_", " ")

    @property
    def node_id(self) -> str:
        return f"{self.module.replace('.', '/')}.py::{self.name}"


def parse(paths: list[Path]) -> tuple[list[Case], float]:
    cases: list[Case] = []
    seconds = 0.0
    for path in paths:
        root = ET.parse(path).getroot()
        for suite in root.findall("testsuite") or [root]:
            seconds += float(suite.get("time") or 0)
            for element in suite.iter("testcase"):
                outcome, message, detail = "passed", "", ""
                if (bad := element.find("failure")) is not None or (
                    bad := element.find("error")
                ) is not None:
                    outcome = "failed"
                    message, detail = bad.get("message") or "", bad.text or ""
                elif (skipped := element.find("skipped")) is not None:
                    # An xfail is a documented defect, not an un-run test.
                    outcome = "known" if "xfail" in (skipped.get("type") or "") else "skipped"
                    message = skipped.get("message") or ""
                cases.append(
                    Case(
                        module=element.get("classname") or "",
                        name=element.get("name") or "",
                        outcome=outcome,
                        tier=next(
                            (
                                prop.get("value") or "untiered"
                                for prop in element.iter("property")
                                if prop.get("name") == "priority"
                            ),
                            "untiered",
                        ),
                        duration_s=float(element.get("time") or 0),
                        message=message.strip(),
                        detail=detail.strip(),
                    )
                )
    return cases, seconds


CSS = """
:root {
  --ground: #f2f5f6; --surface: #fff; --sunk: #e9eef0; --line: #d3dcdf;
  --ink: #0e1719; --muted: #5f7176; --accent: #0b6e77;
  --pass: #1b7a4b; --fail: #ae2318; --known: #8a5300;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #0b1215; --surface: #131e22; --sunk: #0e181b; --line: #27373d;
    --ink: #e7eef0; --muted: #8ba1a6; --accent: #48c6cf;
    --pass: #4fcb86; --fail: #ff8073; --known: #e5a84b;
  }
}
:root[data-theme="dark"] {
  --ground: #0b1215; --surface: #131e22; --sunk: #0e181b; --line: #27373d;
  --ink: #e7eef0; --muted: #8ba1a6; --accent: #48c6cf;
  --pass: #4fcb86; --fail: #ff8073; --known: #e5a84b;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--ground); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
.wrap {
  max-width: 62rem; margin: 0 auto; padding: 2rem 1.25rem 4rem;
  display: flex; flex-direction: column; gap: 1.75rem;
}
.path, .reason, pre, .chip, .eyebrow, td.num, code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.eyebrow {
  margin: 0; font-size: 0.78rem; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--muted);
}
h1 { margin: 0.35rem 0 0; font-size: 2rem; letter-spacing: -0.02em; text-wrap: balance; }
h2 { margin: 0 0 0.6rem; font-size: 1.05rem; letter-spacing: -0.01em; }
.meta { margin: 0.5rem 0 0; font-size: 0.78rem; color: var(--muted); }
.bar {
  display: flex; height: 0.55rem; margin-top: 1rem; border-radius: 2px;
  overflow: hidden; background: var(--sunk); border: 1px solid var(--line);
}
.bar i { display: block; }
.seg-passed { background: var(--pass); }
.seg-failed { background: var(--fail); }
.seg-known { background: var(--known); }
.seg-skipped { background: var(--muted); opacity: 0.4; }
.alert {
  padding: 0.85rem 1rem; border: 1px solid var(--fail); border-left-width: 4px;
  border-radius: 3px; background: var(--surface);
}
.alert p { margin: 0; }
.alert ul { margin: 0.5rem 0 0; padding-left: 1.1rem; font-size: 0.85rem; }
.alert strong { color: var(--fail); }
.lede { margin: 0 0 0.8rem; max-width: 62ch; color: var(--muted); }
.rows { display: flex; flex-direction: column; gap: 0.5rem; }
.row {
  background: var(--surface); border: 1px solid var(--line);
  border-left: 3px solid var(--stripe, var(--line)); border-radius: 3px;
  padding: 0.7rem 0.9rem;
}
.row.failed { --stripe: var(--fail); }
.row.known { --stripe: var(--known); }
.row.passed { --stripe: var(--pass); }
.row.skipped { --stripe: var(--muted); }
.row header { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: baseline; }
.row h3 { margin: 0; font-size: 0.95rem; font-weight: 600; flex: 1 1 20rem; }
.path { margin: 0.25rem 0 0; font-size: 0.78rem; color: var(--muted); word-break: break-all; }
.chip {
  font-size: 0.7rem; font-weight: 700; letter-spacing: 0.04em;
  padding: 0.15rem 0.4rem; border-radius: 2px; border: 1px solid currentColor;
}
.chip.p0 { color: var(--fail); }
.chip.p1, .chip.p2, .chip.untiered { color: var(--muted); }
.reason, pre {
  margin: 0.6rem 0 0; padding: 0.6rem 0.7rem; background: var(--sunk); border-radius: 2px;
  font-size: 0.8rem; line-height: 1.5; white-space: pre-wrap; overflow-x: auto;
}
summary { cursor: pointer; font-size: 0.8rem; color: var(--accent); margin-top: 0.6rem; }
summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tablewrap {
  overflow-x: auto; border: 1px solid var(--line); border-radius: 3px; background: var(--surface);
}
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th, td { text-align: left; padding: 0.55rem 0.85rem; border-top: 1px solid var(--line); }
thead th {
  border-top: none; background: var(--sunk); font-size: 0.72rem;
  letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted);
}
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td .blurb { color: var(--muted); font-size: 0.78rem; }
.group > summary { color: var(--ink); margin: 0; padding: 0.4rem 0; }
footer {
  border-top: 1px solid var(--line); padding-top: 1rem; font-size: 0.78rem; color: var(--muted);
}
"""


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def bar(counts: Counter[str], total: int) -> str:
    segments = "".join(
        f'<i class="seg-{state}" style="width:{counts[state] / total:.4%}"></i>'
        for state in ("passed", "known", "skipped", "failed")
        if total and counts[state]
    )
    return f'<div class="bar">{segments}</div>'


def row(case: Case, *, detailed: bool) -> str:
    reason = f'<p class="reason">{esc(case.message)}</p>' if detailed and case.message else ""
    output = (
        f"<details><summary>Full output</summary><pre>{esc(case.detail)}</pre></details>"
        if detailed and case.detail and case.detail != case.message
        else ""
    )
    return (
        f'<div class="row {case.outcome}"><header>'
        f"<h3>{esc(case.title)}</h3>"
        f'<span class="chip {case.tier}">{case.tier.upper()}</span>'
        f'<span class="path">{LABELS[case.outcome]} · {case.duration_s * 1000:.0f} ms</span>'
        f'</header><p class="path">{esc(case.node_id)}</p>{reason}{output}</div>'
    )


def build(cases: list[Case], seconds: float, sources: str) -> str:
    counts = Counter(c.outcome for c in cases)
    tiers = Counter(c.tier for c in cases)
    failing = sorted(
        (c for c in cases if c.outcome == "failed"), key=lambda c: (TIERS.index(c.tier), c.name)
    )
    p0 = [c for c in failing if c.tier == "p0"]
    known = [c for c in cases if c.outcome == "known"]

    alert = ""
    if p0:
        alert = (
            f'<div class="alert"><p><strong>{len(p0)} P0 test'
            f"{'s' if len(p0) != 1 else ''} failing.</strong> P0 means the service is doing its "
            "core job wrong — the score, the flag decision, which report counts as latest, or "
            "the endpoints disagreeing about one station. Start here.</p><ul>"
            + "".join(f"<li>{esc(c.title)}</li>" for c in p0)
            + "</ul></div>"
        )

    tier_rows = "".join(
        f"<tr><td><strong>{tier.upper()}</strong><br>"
        f'<span class="blurb">{esc(TIER_BLURB[tier])}</span></td>'
        f'<td class="num">{tiers[tier]}</td>'
        f'<td class="num">{sum(1 for c in cases if c.tier == tier and c.outcome == "failed")}</td>'
        f'<td class="num">{sum(1 for c in cases if c.tier == tier and c.outcome == "known")}</td>'
        "</tr>"
        for tier in TIERS
        if tiers[tier]
    )

    sections = ""
    if failing:
        sections += (
            f"<section><h2>Failures ({len(failing)})</h2>"
            f'<div class="rows">{"".join(row(c, detailed=True) for c in failing)}</div></section>'
        )
    if known:
        sections += (
            f"<section><h2>Known service defects ({len(known)})</h2>"
            '<p class="lede">Expected failures, not gaps. Each asserts the behaviour the service '
            "<em>should</em> have and is marked <code>xfail(strict=True)</code>, so fixing the "
            "service turns the run red and forces TEST_STRATEGY.md to be updated.</p>"
            f'<div class="rows">{"".join(row(c, detailed=True) for c in known)}</div></section>'
        )

    groups = ""
    for module in sorted({c.module for c in cases}):
        in_module = sorted((c for c in cases if c.module == module), key=lambda c: c.name)
        tally = ", ".join(
            f"{n} {LABELS[state].lower()}"
            for state, n in Counter(c.outcome for c in in_module).items()
        )
        rows = "".join(row(c, detailed=False) for c in in_module)
        groups += (
            f'<details class="group"><summary>{esc(module)} — {tally}</summary>'
            f'<div class="rows">{rows}</div></details>'
        )

    return f"""<title>Station Health Suite Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CSS}</style>
<div class="wrap">
  <header>
    <p class="eyebrow">NOC Station Health API · test automation suite</p>
    <h1>{len(cases)} tests, {counts["passed"]} passed, {counts["failed"]} failed</h1>
    <p class="meta">{counts["known"]} known defects · {counts["skipped"]} skipped ·
      {seconds:.2f}s · {esc(sources)}</p>
    {bar(counts, len(cases))}
  </header>
  {alert}
  <section>
    <h2>By priority</h2>
    <p class="lede">Tiers come from the risk register and answer &ldquo;which red test do I read
      first?&rdquo;. Every test declares exactly one; <code>tests/conftest.py</code> refuses to
      collect one that declares none.</p>
    <div class="tablewrap"><table>
      <thead><tr><th>Tier</th><th class="num">Tests</th><th class="num">Failed</th>
        <th class="num">Known</th></tr></thead>
      <tbody>{tier_rows}</tbody>
    </table></div>
  </section>
  {sections}
  <section><h2>Every test</h2>{groups}</section>
  <footer>Generated by <code>tools/test_report.py</code>. Risk definitions:
    <code>notes/risk-register.md</code>.</footer>
</div>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", nargs="+", type=Path, help="JUnit XML (default: reports/*.xml)")
    parser.add_argument("-o", "--out", type=Path, default=REPO_ROOT / "reports/test-report.html")
    args = parser.parse_args()

    paths = [p for p in (args.junit or sorted((REPO_ROOT / "reports").glob("*.xml"))) if p.exists()]
    if not paths:
        raise SystemExit("no JUnit XML found — run `make test-report`, or pass --junit <file>")

    cases, seconds = parse(paths)
    if not cases:
        raise SystemExit(f"no test cases in {', '.join(str(p) for p in paths)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(cases, seconds, ", ".join(p.name for p in paths)), encoding="utf-8")

    counts = Counter(c.outcome for c in cases)
    p0_failed = sum(1 for c in cases if c.tier == "p0" and c.outcome == "failed")
    print(
        f"{args.out}: {len(cases)} tests — {counts['passed']} passed, "
        f"{counts['failed']} failed, {counts['known']} known defects"
        + (f"  ** {p0_failed} P0 FAILING **" if p0_failed else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
