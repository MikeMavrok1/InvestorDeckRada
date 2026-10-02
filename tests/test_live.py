"""End-to-end against the real sites. Run with: uv run pytest --live"""

from datetime import date
from pathlib import Path

import pytest

from irdecks.fetch import Fetcher
from irdecks.pipeline import Options, run_company
from irdecks.trace import Trace

pytestmark = pytest.mark.live

ROOT = Path(__file__).parent.parent


@pytest.mark.parametrize("name", ["Moderna", "BioNTech", "CRISPR Therapeutics", "Beam Therapeutics"])
def test_finds_a_recent_verified_deck(name, tmp_path):
    fetcher = Fetcher(tmp_path / "cache")
    opts = Options(out_dir=tmp_path, overrides=ROOT / "companies.csv", use_llm=False)
    result = run_company(name, fetcher, opts, Trace())

    deck = result.selection.primary
    assert deck is not None, result.error
    assert deck.pdf and deck.pdf.pages >= 5 and deck.pdf.landscape_ratio >= 0.6
    assert Path(deck.pdf.path).read_bytes().startswith(b"%PDF")
    assert (date.today() - deck.best_date).days < 200  # these companies publish every quarter
