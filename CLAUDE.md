# Standing context for AI sessions in this repo

ROLE
You are pairing with a Staff-level QA engineer building a test automation suite
for a FastAPI microservice. Treat test code as production code.

PROJECT
Service under test: an EV charging station health service. Ingests station
health reports, computes a "network hygiene score", exposes REST endpoints for
station status, poor-hygiene stations, and network metrics. Source is in
service/. It's already built — we do NOT modify it.

Deliverables: pytest suite, TEST_STRATEGY.md, AI_USAGE.md, README.md,
.github/workflows/.

HARD RULES
- Never modify anything under service/. If a test can only pass by changing the
  service, the service has a bug — say so instead.
- Every test must map to a named failure mode. If you can't say what regression
  it catches, don't write it.
- No sleep()-based synchronisation. No test that depends on execution order.
- No bare `assert response.status_code == 200` as the only assertion — assert on
  the response body's shape and values too.
- Deterministic: no reliance on wall-clock now(), no random data without a
  seeded/Hypothesis-managed source, no external network calls.
- Prefer few strong tests over many shallow ones. Three meaningful tests beat
  twenty trivial ones.
- Type-hint fixtures and helpers. Docstring each test with the risk it covers.

WORKING STYLE
- When you're unsure about intended service behaviour, stop and ask me. Do not
  invent a spec.
- When you spot behaviour that looks like a service bug, flag it separately —
  do not write a test that locks in the buggy behaviour as correct.
- Show me a plan before writing more than ~50 lines of code.

## Facts established during recon — do not re-derive, do not contradict

- The service is **not in-memory**. It is SQLAlchemy over SQLite (local, `noc.db`
  file) or PostgreSQL (Docker). State survives a restart. See
  `notes/behaviour-inventory.md` §3.
- `POST /reports` is **append-only**. Nothing is ever updated or deduplicated.
- Risk IDs (R1…R14) are defined in `notes/risk-register.md`. Every test docstring
  cites one.
- Isolation in-process is done with `app.dependency_overrides[get_db]`, never by
  truncating tables or re-importing the app. Over the wire (e2e) it is done with
  unique station IDs and delta assertions.
