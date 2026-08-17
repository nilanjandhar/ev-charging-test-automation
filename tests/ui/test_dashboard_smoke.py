"""Dashboard smoke test — exactly one, on purpose.

The scope decision matters more than the code here. `static/index.html` is a
single file: no build step, no framework, no router, no state beyond a
30-second `setInterval`. Page objects, cross-browser matrices and visual diffs
would cost more to maintain than the rest of the suite combined, to catch a class
of bug (a JavaScript typo, a renamed field) that one smoke test already catches.
So: one test, nightly, never in the PR gate. The reasoning is in the risk register
under "the three I would not spend automation budget on".

What this covers that the API layer cannot (**R14**): the dashboard reads six
fields across three endpoints and renders them. A backwards-compatible-looking API
change — `flagged` becoming a string, `average_latency_ms` losing its null case —
leaves every API test green and silently blanks a panel an operator is watching.

Selectors are roles and text, never CSS classes: `.card.danger .value` is a
styling decision that a designer may change tomorrow, while "the page shows this
station as Flagged" is the behaviour we actually care about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.builders import at, report, station_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx
    from playwright.sync_api import Page

pytestmark = [pytest.mark.ui, pytest.mark.slow]


def test_dashboard_renders_an_ingested_station(page: Page, live_client: httpx.Client) -> None:
    """R14: a station ingested via the API appears on the dashboard with its real status.

    Seeds through the API rather than through fixtures or the database, so the test
    exercises the same path an actual station does: POST a report, then assert the
    operator's view reflects it. That is the full loop the dashboard exists to
    close.

    The station is deliberately flagged (offline, slow, erroring) so the test
    covers both tables and the flagged tile — the healthy path renders an empty
    worklist, which would pass against a dashboard that had stopped rendering the
    worklist at all.

    No `wait_for_timeout`: Playwright's assertions retry until their own deadline,
    so the page's asynchronous `loadData()` is handled by waiting on the *content*
    rather than on the clock.
    """
    from playwright.sync_api import expect

    sid = station_id("UI")
    assert_status(
        live_client.post(
            "/reports",
            json=report(
                station_id_=sid,
                timestamp=at(),
                connectivity_status="offline",
                latency_ms=600.0,
                error_count=8,
            ),
        ),
        201,
    )

    page.goto(str(live_client.base_url), wait_until="domcontentloaded")

    expect(page.get_by_role("heading", name="NOC Station Health Dashboard")).to_be_visible()

    # The station appears in the all-stations table with its computed score...
    station_row = page.get_by_role("row").filter(has_text=sid).first
    expect(station_row).to_be_visible()
    expect(station_row).to_contain_text("10.0", timeout=10_000)
    expect(station_row).to_contain_text("offline")
    expect(station_row).to_contain_text("Flagged")

    # ...and on the poor-hygiene worklist below it, which is a separate fetch.
    worklist = page.get_by_role("table").last
    expect(worklist.get_by_role("row").filter(has_text=sid)).to_be_visible()

    # The metric tiles are populated from /metrics/summary, not left as placeholders.
    expect(page.get_by_text("Total Stations")).to_be_visible()
    for tile_id in ("total", "online", "offline", "flagged"):
        value = page.locator(f"#{tile_id}")
        expect(value).not_to_have_text("—", timeout=10_000)
        assert value.inner_text().strip().isdigit(), (
            f"the {tile_id} tile should render a count, got {value.inner_text()!r}"
        )

    # And no error banner: the dashboard swallows fetch failures into a small red
    # line (index.html:135-138) while leaving stale numbers on screen, so an
    # invisible banner is the only evidence that all three fetches actually worked.
    expect(page.locator("#error-msg")).to_be_hidden()
