"""Summary of Benefits documents: markdown source -> PDF (dev time) -> sections (ingest).

The markdown in ``benefits/`` is the source we write; ``make benefits-pdf`` renders it to
the PDFs that are committed next to it. Ingestion reads the PDFs, as it would a plan's
real documents, and splits them on numbered section headings ("6. Eye care").

    python -m caregap.rag.documents build     # make benefits-pdf
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DIR = Path("benefits")
_HEADING = re.compile(r"^(\d{1,2})\.\s+(\S.*)$")


@dataclass(frozen=True)
class Section:
    number: int
    title: str
    text: str

    @property
    def id(self) -> str:
        return f"S{self.number}"


def build_pdf(markdown: Path, out: Path) -> None:
    """Render a benefits markdown file to PDF. Byte-identical for the same input."""
    from fpdf import FPDF  # dev-only dependency

    pdf = FPDF(format="Letter")
    pdf.set_creation_date(datetime(2025, 1, 1, tzinfo=UTC))
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            pdf.set_font("Helvetica", size=10.5)
            pdf.multi_cell(0, 5.5, " ".join(paragraph), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.5)
            paragraph.clear()

    for line in markdown.read_text().splitlines():
        if line.startswith("# "):
            pdf.set_title(line[2:])
            pdf.set_font("Helvetica", style="B", size=15)
            pdf.multi_cell(0, 7, line[2:], new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        elif line.startswith("## "):
            flush()
            pdf.ln(2)
            pdf.set_font("Helvetica", style="B", size=12)
            pdf.multi_cell(0, 6, line[3:], new_x="LMARGIN", new_y="NEXT")
        elif not line.strip():
            flush()
        elif line.endswith(":") or re.match(r"^[A-Z][^.]*: ", line):
            # One benefit per line: keep line breaks so each reads as its own item.
            flush()
            pdf.set_font("Helvetica", size=10.5)
            pdf.multi_cell(0, 5.5, line, new_x="LMARGIN", new_y="NEXT")
        else:
            paragraph.append(line)
    flush()
    pdf.output(str(out))


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def sections(text: str) -> list[Section]:
    """Split extracted text on numbered headings. Text before the first heading (title,
    disclaimer) is dropped. Line breaks inside a sentence are joined."""
    found: list[Section] = []
    number, title = 0, ""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _HEADING.match(line)
        if m:
            if number:
                found.append(Section(number, title, _join(lines)))
            number, title, lines = int(m.group(1)), m.group(2), []
        elif number and line:
            lines.append(line)
    if number:
        found.append(Section(number, title, _join(lines)))
    numbers = [s.number for s in found]
    if numbers != list(range(1, len(found) + 1)):
        raise ValueError(f"section numbers are not 1..n: {numbers}")
    return found


def _join(lines: list[str]) -> str:
    """Join PDF lines: a line that ends a sentence or an item starts a new line."""
    out: list[str] = []
    for line in lines:
        if out and not re.search(r"[.:]$", out[-1]):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
    return "\n".join(out)


def load_sections(customer: str, directory: Path = DEFAULT_DIR) -> list[Section]:
    return sections(read_pdf(directory / f"{customer}.pdf"))


def main(argv: list[str]) -> int:
    if argv[:1] != ["build"]:
        print("usage: python -m caregap.rag.documents build [DIR]", file=sys.stderr)
        return 2
    directory = Path(argv[1]) if len(argv) > 1 else DEFAULT_DIR
    for md in sorted(directory.glob("*.md")):
        out = md.with_suffix(".pdf")
        build_pdf(md, out)
        print(f"[benefits] {md} -> {out} ({len(sections(read_pdf(out)))} sections)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
