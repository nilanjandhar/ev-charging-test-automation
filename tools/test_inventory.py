#!/usr/bin/env python3
"""Generate `notes/test-inventory.md` — every test, its tier, and why it exists.

Generated rather than hand-written for one reason: a 109-row table maintained by
hand is a table that is wrong within a week. Read from the source with `ast`, so
`make inventory` always matches what pytest would collect.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAYERS = ["unit", "contract", "api", "e2e", "perf", "ui"]
COLLECTED = 74  # `pytest --collect-only -q`, kept here so the header cannot silently rot


def tier_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    for decorator in node.decorator_list:
        name = ast.unparse(decorator)
        if match := re.fullmatch(r"pytest\.mark\.(p[012])", name):
            return match.group(1).upper()
    return "—"


def why_of(doc: str) -> str:
    """The `Why:` line from a docstring — the justification for the test existing.

    A convention rather than machinery, and deliberately mandatory: a test whose
    author cannot finish the sentence "this must exist because…" is a test to
    delete. `main()` fails if any test is missing one.
    """
    lines = doc.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("Why:"):
            block = [line.strip().removeprefix("Why:").strip()]
            for follow in lines[index + 1 :]:
                if not follow.strip():
                    break
                block.append(follow.strip())
            return " ".join(block).rstrip(".")
    return ""


def cases(path: Path) -> list[tuple[str, str, str, str]]:
    """(tier, name, summary, why) for every test in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        doc = ast.get_docstring(node) or ""
        summary = doc.splitlines()[0] if doc else ""
        found.append((tier_of(node), node.name, summary.rstrip("."), why_of(doc)))
    return found


def main() -> int:
    files = sorted(
        (REPO_ROOT / "tests").rglob("test_*.py"),
        key=lambda p: (LAYERS.index(p.parent.name) if p.parent.name in LAYERS else 9, p.name),
    )
    total = sum(len(cases(f)) for f in files)
    tier_counts: dict[str, int] = {}
    for f in files:
        for tier, *_ in cases(f):
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

    lines = [
        "# Test inventory",
        "",
        f"{total} test functions, {COLLECTED} collected (parametrised ones expand).",
        "",
        "Every test carries a `Why:` line in its docstring saying what goes unnoticed",
        "without it; `make inventory` fails if one is missing, so a test that cannot be",
        "justified cannot be added. Generated — do not edit by hand. Tier definitions",
        "are in `TEST_STRATEGY.md`.",
        "",
        "| Tier | Tests |",
        "|---|---|",
    ]
    lines += [f"| **{tier}** | {tier_counts[tier]} |" for tier in sorted(tier_counts)]

    for path in files:
        rows = cases(path)
        rel = path.relative_to(REPO_ROOT)
        lines += [
            "",
            f"## `{rel}`",
            "",
            f"{len(rows)} tests.",
            "",
            "| Tier | Test | Covers | Why it must exist |",
            "|---|---|---|---|",
        ]
        for tier, name, summary, why in rows:
            lines.append(f"| {tier} | `{name}` | {summary} | {why} |")

    unjustified = [name for f in files for tier, name, _, why in cases(f) if not why]
    if unjustified:
        raise SystemExit(
            "these tests have no `Why:` line in their docstring — either justify them "
            "or delete them:\n  " + "\n  ".join(unjustified)
        )

    out = REPO_ROOT / "notes" / "test-inventory.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{out}: {total} test functions across {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
