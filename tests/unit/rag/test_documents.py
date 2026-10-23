from __future__ import annotations

import re
from pathlib import Path

import pytest

from caregap.rag.documents import build_pdf, load_sections, read_pdf, sections

DIR = Path("benefits")


@pytest.mark.parametrize("customer", ["sunrise", "horizon"])
def test_committed_pdfs_match_their_markdown(customer: str, tmp_path: Path) -> None:
    out = tmp_path / f"{customer}.pdf"
    build_pdf(DIR / f"{customer}.md", out)
    assert out.read_bytes() == (DIR / f"{customer}.pdf").read_bytes(), "run make benefits-pdf"


@pytest.mark.parametrize("customer", ["sunrise", "horizon"])
def test_sections_survive_the_pdf_round_trip(customer: str) -> None:
    md = (DIR / f"{customer}.md").read_text()
    headings = re.findall(r"^## (\d+)\. (.+)$", md, re.MULTILINE)
    found = load_sections(customer)
    assert [(str(s.number), s.title) for s in found] == headings
    # Every dollar amount and percentage in the source is in the extracted text.
    amounts = set(re.findall(r"\$[\d,]+|\d+%", md))
    extracted = " ".join(s.text for s in found)
    assert amounts == set(re.findall(r"\$[\d,]+|\d+%", extracted))


def test_items_keep_their_own_lines() -> None:
    eye = load_sections("sunrise")[5]
    assert eye.id == "S6"
    assert eye.text.splitlines()[2] == (
        "Eyewear allowance: up to $150 every year toward glasses or contact lenses."
    )


def test_sections_must_be_numbered_in_order() -> None:
    with pytest.raises(ValueError, match=r"1\.\.n"):
        sections("1. A\ntext\n3. C\ntext")
    assert read_pdf(DIR / "horizon.pdf").startswith("Horizon ACA Marketplace")
