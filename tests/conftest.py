"""Shared pytest configuration.

Two rules enforced here:

1. `pytest` with no arguments NEVER touches the network. Layer-2 tests carry
   the `live` marker and are deselected unless explicitly requested, so the
   default suite stays deterministic and safe to run anywhere.
2. Layer-2 tests are skipped (not failed) when credentials are absent - a
   laptop without tenant access should report "not verified", never a red
   suite that hides real failures.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    """Deselect `live` tests unless the run explicitly asked for them via
    `-m live`. Without this, `pytest` in CI would start calling a production
    SAP tenant the moment someone adds credentials to the environment."""
    marker_expr = config.getoption("-m", default="")
    if "live" in marker_expr:
        return
    skip_live = pytest.mark.skip(
        reason="live tenant test - run explicitly with: pytest -m live"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def sap_client():
    """A real SAPClient against the configured tenant, or skip.

    Skips rather than fails when credentials are unavailable, so the live
    matrix honestly reports "not verified" instead of a false red.
    """
    from app.config import settings
    from app.mcp.credentials import CredentialError, resolve_sap_credentials
    from app.mcp.client import SAPClient
    from app.mcp.errors import SAPRequestError

    try:
        credentials = resolve_sap_credentials(settings)
    except CredentialError as exc:
        pytest.skip(f"SAP credentials unavailable: {exc}")

    try:
        client = SAPClient(
            base_url="",
            username="",
            password="",
            auth_method=settings.sap_auth_method,
            timeout_seconds=settings.sap_http_timeout_seconds,
            credentials=credentials,
        )
    except SAPRequestError as exc:
        pytest.skip(f"SAP client could not be built: {exc}")

    yield client
    client.close()


@pytest.fixture(scope="session")
def live_results():
    """Collects one row per live tool check so the run can print a dated
    verification matrix at the end - the artifact this layer exists to
    produce. Written by tests, rendered by the terminal summary hook."""
    return []


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Render the live verification matrix, when a live run happened."""
    rows = getattr(config, "_live_matrix", None)
    if not rows:
        return
    from datetime import datetime, timezone

    terminalreporter.write_sep("=", "LIVE TENANT VERIFICATION MATRIX")
    terminalreporter.write_line(
        f"tenant: {rows[0].get('tenant', '-')}    "
        f"run: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    terminalreporter.write_line("")
    terminalreporter.write_line(f"{'CHECK':<38} {'RESULT':<8} {'ms':>7}  DETAIL")
    for row in rows:
        terminalreporter.write_line(
            f"{row['check']:<38} {row['result']:<8} {row['ms']:>7}  {row.get('detail', '')}"
        )
    passed = sum(1 for r in rows if r["result"] == "PASS")
    terminalreporter.write_line("")
    terminalreporter.write_line(f"{passed}/{len(rows)} live checks passed")
