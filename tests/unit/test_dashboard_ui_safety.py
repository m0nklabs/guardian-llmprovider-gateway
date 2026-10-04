"""Safety contracts for the dashboard UI that have no JavaScript test runner.

The dashboard is a single hand-written HTML file. Upstream model ids that reach
it originate from external ``/v1/models`` catalogs, so anything that flows from
a rendered payload into executable markup is an injection boundary. These tests
read the source and pin the invariants that a browser session would otherwise
have to re-discover by hand.
"""

from pathlib import Path

UI_SOURCE = (
    Path(__file__).resolve().parents[2] / "app" / "ui" / "index.html"
).read_text(encoding="utf-8")


def test_no_dynamic_value_reaches_an_inline_event_handler():
    """An inline ``onclick`` attribute is parsed as HTML first and as
    JavaScript second: ``escapeHtml`` cannot protect a JS-string context,
    because the HTML parser decodes entities in the attribute value *before*
    the JS engine sees it, so a decoded quote still terminates the string and
    executes the remainder as script. A model id such as ``x');alert(1);//``
    therefore executed in the legacy address pill.

    Static wiring (``showKeyModal()``, the key-toast helpers) carries no
    external data and may keep inline handlers; interpolated values may not.
    """
    offenders = [
        line.strip()
        for line in UI_SOURCE.splitlines()
        if "onclick=" in line and "${" in line
    ]
    assert offenders == [], offenders


def test_legacy_address_pill_copies_through_the_delegated_handler():
    """The legacy pill must carry its id in a ``data-model-copy`` attribute and
    delegate the click to the same handler the catalog grid uses — the one copy
    path, and no value interpolated into executable markup."""
    assert 'data-model-copy="${escapeHtml(entry.id)}"' in UI_SOURCE
    assert "legacyCards.addEventListener('click', handleModelCatalogClick)" in UI_SOURCE
