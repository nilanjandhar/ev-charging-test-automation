# AI usage

I used Claude (Opus, via Claude Code in VS Code) throughout. The raw log — every
prompt, the unedited output, and what I did with it — is in
[`notes/ai-journal.md`](notes/ai-journal.md), written as I went rather than
reconstructed afterwards. The three examples below are taken from it.

**How I worked, in one line:** I did the recon by hand and kept the strategy;
I delegated the typing. Every session started from a standing context block —
the role, the hard rules (never modify `service/`, no `sleep()`, no bare
status-code assertions), and a "facts established during recon" section that
existed specifically to stop the model re-deriving things I had already verified
against a running service.

The single most useful thing I did was **not** a prompt. Before opening a session
I stood the service up and curl'd every endpoint. That produced two facts that
contradicted my own starting assumptions, and every good decision in this repo
descends from them:

- The service is **not in-memory** — it is SQLAlchemy over SQLite/PostgreSQL, and
  state survives a restart.
- `POST /reports` is **append-only**, and each read endpoint independently
  recomputes "latest per station".

Had I asked a model to summarise the service instead, I would have got a fluent
description of a scoring API and would have spent the afternoon solving the wrong
isolation problem.

---

## Example 1 — Accepted as-is (and later deleted)

**What I asked for:** a step for the PR workflow that turns the JUnit XML into a
GitHub job summary, "so the result is visible without opening the log."

**What it produced** (committed verbatim, `.github/workflows/pr.yml`):

```yaml
      - name: Job summary
        if: always()
        run: |
          {
            echo "## PR gate"
            echo
            python - <<'PY'
          import xml.etree.ElementTree as ET, pathlib
          report = pathlib.Path("reports/junit.xml")
          if not report.exists():
              print("No JUnit report produced — the run failed before pytest started.")
              raise SystemExit
          suite = ET.parse(report).getroot().find("testsuite") or ET.parse(report).getroot()
          get = lambda key: int(suite.get(key, 0))
          total, failures, errors = get("tests"), get("failures"), get("errors")
          skipped, time_s = get("skipped"), float(suite.get("time", 0))
          print(f"| metric | value |\n| --- | --- |")
          print(f"| tests | {total} |")
          ...
          PY
          } >> "$GITHUB_STEP_SUMMARY"
```

**Why I accepted it unchanged.** Three reasons, in order:

1. **I verified it rather than trusting it.** I ran the exact script against a
   real `reports/junit.xml` produced by this suite (91 tests at the time) and
   checked the rendered table. I also checked that the heredoc terminator survives
   YAML block-scalar dedenting, which is the one thing that silently breaks this
   pattern.
2. **It requires no knowledge of this service.** JUnit XML is a fixed schema and
   `$GITHUB_STEP_SUMMARY` is a fixed contract. There is no domain judgment for me
   to add, which is exactly the kind of work worth delegating.
3. **It got the two details I would have had to fix otherwise:** `if: always()`
   (a summary you only get on green is useless at the moment you need it) and the
   missing-file branch, so a run that dies before pytest starts reports *why*
   instead of raising a confusing `FileNotFoundError` on top of the real failure.

**And it is no longer in the repo — check `pr.yml` and you will not find it.**
Later I did a pass specifically to cut over-engineering, and this went: thirty
lines of Python embedded in YAML to restate counts GitHub already renders, when by
then the HTML report shipped in the job artifact carried the real detail.

I have kept it as Example 1 rather than swapping in something still present,
because the two judgments are both true and neither cancels the other. Accepting
it was right: it was mechanical, verified in a minute, and wrong in ways I would
have noticed immediately. Deleting it was also right, and for a different reason —
"correct and cheap" is not the same test as "earns its place". The first question
is the one to ask of an AI output; the second is the one to ask of your own repo,
and it is easy to skip because the code already works.

---

## Example 2 — Modified

**The task:** the scaffolding prompt asked, before writing any code, for options
on how to guarantee a clean store between tests given that the service holds
state.

### Before (what the assistant proposed)

Three options, each with trade-offs:

> **Option A — unique station IDs per test (namespacing).** Each test generates
> `STATION-{uuid4}`. No reset needed, works against a live container,
> parallel-safe. Trade-off: aggregate endpoints (`/metrics/summary`) see every
> other test's rows, so you can never assert `total_stations == 3`.
>
> **Option B — restart the app instance per test.** A fixture that re-imports
> `app.main` against a fresh SQLite file. Perfect isolation, exact aggregate
> assertions. Trade-off: ~200 ms per test plus module re-import; cannot work
> against a live container.
>
> **Option C — truncate tables between tests.** A fixture that issues
> `DELETE FROM station_reports` after each test. Fast, exact aggregates.
> Trade-off: serial-only — two tests in parallel will delete each other's rows.

The analysis is correct as far as it goes, and A was implementable in ten minutes.

### After (what I committed, `tests/conftest.py`)

```python
@pytest.fixture
def api_client(isolated_engine: Engine) -> Iterator[TestClient]:
    session_factory = sessionmaker(bind=isolated_engine, autocommit=False, autoflush=False)

    def _override_get_db() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
```

### What was wrong with the original, and the judgment call

All three options shared a hidden premise: **that the tests must use the service's
own engine**. They do not. `get_db` is a FastAPI dependency
(`service/app/database.py:15`), so `app.dependency_overrides` lets each test bind
the app to *its own* engine for the duration of that test. That is Option D, and
it dominates all three — empty database, exact aggregate assertions, no
truncation, no module reload, parallel-safe, and zero changes to `service/`.

The model did not surface it because it was answering the question I asked
("how do I reset the store?") rather than the question underneath it ("how do I
give each test its own store?"). That reframing is the part I would not delegate.

**Why it mattered, concretely.** Option A would have meant no test could assert
`total_stations == 2`. Three of the eight defects I found — R1 (duplicate reports
double-count), R5 and R18 (one bad report destroys the network average) — are
*only* visible in network-wide aggregates. Taking the ten-minute option would have
cost me half the findings in this repo.

**What survived from the original.** Option A is genuinely right for the one layer
where dependency overrides are impossible — e2e over real HTTP against a container
— so the suite runs **D in-process and A over the wire**, with every e2e
assertion written as a delta (`after == before + 1`) rather than an absolute. The
three rejected options are documented in the fixture's docstring so the next
person does not re-litigate them.

### Example 2b — the same pattern, sharper: a test that was green and meaningless

Worth including because it is the failure mode I most distrust in generated tests.
The schemathesis fuzz suite passed on the first run. I ran the whole suite three
times in a row and got **three different xfail counts** — 5, 6, then 7.

The cause: against an empty database the collection endpoints return `[]`, and an
empty array validates against any item schema. Whether the fuzzer found anything
depended on whether some earlier test had happened to leave rows behind. It was
reporting coverage it did not have.

```diff
+@pytest.fixture(scope="module", autouse=True)
+def _seed_one_station() -> None:
+    """Make sure the read endpoints have something to serialise."""
+    with TestClient(app) as client:
+        client.post("/reports", json={... "station_id": SEED_STATION_ID ...})
+
 def test_operation_conforms_to_its_published_schema(case: Case) -> None:
+    if case.path == _R9_OPERATION:
+        # A randomly generated ID 404s, and a 404 body has no timestamp to
+        # validate, so this operation's verdict flipped between runs.
+        case.path_parameters = {"station_id": SEED_STATION_ID}
```

With those two changes the result is deterministic — and the suite immediately and
repeatably reports R16 on all three station endpoints. A vacuously-passing test is
worse than a missing one, because it is counted.

---

## Example 3 — Rejected

**The suggestion:** while writing the cross-endpoint consistency tests, the
obvious assertion for the poor-hygiene worklist was list equality against the
order the endpoint returns:

```python
# rejected
worklist = api_client.get("/stations/poor-hygiene").json()
assert [s["station_id"] for s in worklist] == [dead_station, borderline_station]
```

It is the natural thing to write, it is easier to read than the set-based version,
and it passes locally every single time.

**Why it is wrong for *this* service specifically** — not generically:

`/stations/poor-hygiene` has **no `ORDER BY`** (`service/app/routers/stations.py:82-91`).
Its sibling `/stations` does (`stations.py:31`), which is what makes the omission
easy to miss. With no ordering clause the result order is whatever the engine
returns, and this service runs on **two different engines**: SQLite locally,
PostgreSQL in the container (`docker-compose.yml:26`). SQLite happens to return
rows in rowid order, which for this suite means insertion order, which is exactly
what a locally-written assertion expects. PostgreSQL makes no such promise and
reorders freely once a plan changes.

So this assertion would have passed on my machine and in `make test`, and failed
in the `main` workflow's e2e job against the container — the single most expensive
kind of test to own, because it fails after merge, on someone else's PR, for a
reason that has nothing to do with their change.

**What I did instead:**

```python
flagged_in_worklist = set(station_ids(worklist))
flagged_in_listing = {s["station_id"] for s in listing if s["flagged"]}

assert flagged_in_worklist == {dead_ish}
assert flagged_in_worklist == flagged_in_listing
assert metrics["flagged_count"] == len(flagged_in_worklist) == 1
```

Set semantics, plus the missing `ORDER BY` filed as risk **R10** — a defect in the
service (an operator's worklist that reshuffles between 30-second refreshes has no
stable "top of the list") rather than a fact to pin in a test. Pinning today's
accident would have locked in behaviour the service never promised.

The general principle I applied: **assert the guarantee, not the observation.**
The service guarantees *which* stations are flagged. It does not guarantee their
order. A test that cannot tell the difference between those two things will
eventually fail for a reason its author cannot explain.

---

## Where AI was most and least useful

**Most useful:** scaffolding and boilerplate (workflow YAML, the Makefile, the
`_measure` helper), and as a fast second reader — "which of these invariants
actually hold?" was a good question to argue with a model about, and the argument
sharpened the property tests even where the model was wrong.

**Least useful, and actively risky:** anything requiring a fact about *this*
service. Its first-draft risk register ranked "negative `latency_ms` is accepted"
as a top-five risk. It is not a risk at all — `schemas.py:11` has `ge=0` and
returns a 422, which I already knew because I had curl'd it. It also asserted that
duplicate reports were harmless because "the join picks the latest"; they are not,
and that mistake is now R1, the fourth-highest risk in the register. Both errors
are the same error: confident inference from reading code, without running it.

**What the tools found that I did not:** three of the eight defects came from
Hypothesis and schemathesis rather than from me or from the model — R15 (rounding
decides the flag at the boundary), R16 (the service violates its own published
`date-time` format), and R17 (`error_count: 2**63` → unhandled 500). My
contribution there was choosing tools whose defaults disagree with each other and
then taking the disagreement seriously, rather than tuning it away to get a green
run.
