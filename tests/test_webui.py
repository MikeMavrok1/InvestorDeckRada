"""The Streamlit app, driven with streamlit.testing.v1. No test here touches the network."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="needs the web extra: uv sync --extra web")

from streamlit.testing.v1 import AppTest  # noqa: E402
from test_pipeline import HOME, ROOT, SLIDES, FakeSite  # noqa: E402

from irdecks import webui  # noqa: E402

SCRIPT = str(Path(webui.__file__))
REAL_CACHE = Path("output/cache")
LISTING = """<html><h1>Presentations</h1>
  <div class="row"><span>September 9, 2026</span> <a href="/files/corp.pdf">Corporate Presentation</a></div>
  <div class="row"><span>August 5, 2026</span> <a href="/files/q2.pdf">Q2 2026 Earnings Slides</a></div></html>"""


def _fake_site() -> FakeSite:
    return FakeSite(
        {ROOT: HOME, ROOT + "presentations": LISTING},
        files={
            ROOT + "files/corp.pdf": (SLIDES, "Wed, 09 Sep 2026 12:00:00 GMT"),
            ROOT + "files/q2.pdf": (SLIDES, "Wed, 05 Aug 2026 12:00:00 GMT"),
        },
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    """An AppTest whose Fetcher is replaced by a fake site and whose output goes to tmp_path."""
    monkeypatch.setenv("IRDECKS_OUT_DIR", str(tmp_path))
    monkeypatch.setattr("irdecks.fetch.Fetcher", lambda *a, **k: _fake_site())
    return AppTest.from_file(SCRIPT, default_timeout=60)


def _submit(at: AppTest, text: str) -> AppTest:
    at.run()
    at.text_input[0].set_value(text)
    next(b for b in at.button if b.label == "Run").click()
    return at.run()


def test_first_load_invites_a_choice(app):
    app.run()
    assert not app.exception
    assert [b.label for b in app.button if b.key and b.key.startswith("quick_")] == webui.COMPANIES
    assert "Offline (replay from cache)" in [t.label for t in app.toggle]
    assert app.info


def test_run_shows_pick_explanation_tables_pdf_and_trace(app, tmp_path):
    at = _submit(app, ROOT)
    assert not at.exception
    assert [s.value for s in at.subheader][0] == "Corporate Presentation"
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Document type"] == "corporate_overview"
    assert metrics["Date"] == "2026-09-09"
    assert metrics["Pages"] == "12" and metrics["Orientation"] == "Landscape"
    candidates = at.dataframe[0].value
    assert list(candidates["Role"])[0] == "Pick"
    assert "Q2 2026 Earnings Slides" in list(candidates["Title"])
    assert len(at.dataframe) == 2  # candidates + date evidence
    assert any("Full trace" in e.label for e in at.expander)
    assert (tmp_path / "ir_fake_test" / "2026-09-09_Corporate-Presentation.pdf").exists()


def test_quick_button_runs_that_company(app):
    """The fake site knows no 'Beam Therapeutics', so this ends in a message, under that name."""
    app.run()
    app.button(key="quick_Beam Therapeutics").click()
    app.run()
    assert not app.exception
    assert app.header[0].value == "Beam Therapeutics"
    assert app.warning or app.error or app.info


def test_nothing_found_is_a_message_not_a_stack_trace(app, monkeypatch):
    monkeypatch.setattr(
        "irdecks.fetch.Fetcher",
        lambda *a, **k: FakeSite({ROOT: HOME, ROOT + "presentations": "<html><h1>Presentations</h1></html>"}),
    )
    at = _submit(app, ROOT)
    assert not at.exception
    assert at.warning and "No eligible presentation" in at.warning[0].value
    assert any("Full trace" in e.label for e in at.expander)


def test_files_found_but_all_rejected_is_not_blamed_on_javascript(app, monkeypatch):
    """Seen on Bayer (5 Oct 2026): the static HTML had the deck, robots.txt refused the file, and
    the blocked browser that ran afterwards was reported as the cause."""
    from test_feeds import BlockedBrowser

    listing = """<html><h1>Presentations</h1>
      <div><span>September 9, 2026</span> <a href="/files/gone.pdf">Corporate Presentation</a></div></html>"""
    site = BlockedBrowser({ROOT: HOME, ROOT + "presentations": listing})
    monkeypatch.setattr("irdecks.fetch.Fetcher", lambda *a, **k: site)
    at = _submit(app, ROOT)
    assert not at.exception
    assert site.rendered_urls  # the browser did run and was blocked
    text = at.warning[0].value
    assert "none passed the file checks" in text and "HTTP 404" in text
    assert "JavaScript" not in text


def test_a_crash_inside_the_run_is_shown_as_an_error(app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("irdecks.pipeline.run_company", boom)
    at = _submit(app, ROOT)
    assert not at.exception
    assert at.error and "kaboom" in at.error[0].value


def test_offline_cache_miss_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("IRDECKS_OUT_DIR", str(tmp_path))  # an empty cache, real Fetcher, offline
    at = AppTest.from_file(SCRIPT, default_timeout=60)
    at.run()
    at.toggle[0].set_value(True)
    at.text_input[0].set_value("Nonexistent Biotech Holdings")
    next(b for b in at.button if b.label == "Run").click()
    at.run()
    assert not at.exception
    assert at.info and "Not in the cache" in at.info[-1].value


@pytest.mark.skipif(not REAL_CACHE.exists(), reason="no output/cache on this machine")
@pytest.mark.parametrize("name", webui.COMPANIES)
def test_the_four_test_companies_replay_from_the_real_cache(name, monkeypatch):
    monkeypatch.delenv("IRDECKS_OUT_DIR", raising=False)
    at = AppTest.from_file(SCRIPT, default_timeout=120)
    at.run()
    at.toggle[0].set_value(True)
    at.button(key=f"quick_{name}").click()
    at.run()
    assert not at.exception
    assert not at.error
    assert {m.label for m in at.metric} >= {"Document type", "Date", "Confidence", "Pages"}


def test_why_for_alternates_is_derived_from_the_selection(tmp_path):
    from irdecks.pipeline import Options, run_company
    from irdecks.trace import Trace
    from test_pipeline import TODAY

    result = run_company(ROOT, _fake_site(), Options(out_dir=tmp_path, use_llm=False, today=TODAY), Trace())
    rows = webui.candidate_rows(result.selection, TODAY)
    assert rows[0]["Role"] == "Pick" and "standing corporate deck" in rows[0]["Why"]
    assert "older than the pick" in rows[1]["Why"]
    evidence = webui.evidence_rows(result.selection.primary, TODAY)
    assert sum(r["Status"] == "winner" for r in evidence) == 1


def test_execute_streams_trace_events_in_order(tmp_path, monkeypatch):
    """The Trace subclass hands each event to the callback as it happens, after recording it."""
    monkeypatch.setenv("IRDECKS_OUT_DIR", str(tmp_path))
    monkeypatch.setattr("irdecks.fetch.Fetcher", lambda *a, **k: _fake_site())
    streamed = []
    run = webui.execute(ROOT, False, on_event=streamed.append)
    assert streamed and streamed == run.events
    stages = [e.stage for e in streamed]
    assert stages.index("discover") < stages.index("verify") < stages.index("select")
    assert streamed[-1].level == "ok" and streamed[-1].stage == "select"
