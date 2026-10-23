from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from caregap.evals.calibration import CSV_PATH, agreement, label, read, sample, write
from caregap.evals.judge import CRITERIA


def test_committed_csv_has_50_scripts_and_only_owner_labels() -> None:
    rows = read(CSV_PATH)
    assert len(rows) == 50
    assert len({r["script"] for r in rows}) == 50
    for r in rows:
        assert all(r[c] in ("", "y", "n") for c in CRITERIA)


def test_sample_leaves_every_label_empty_and_is_deterministic() -> None:
    pool = {
        "a": [{"script": f"a{i}", "status": "compliant" if i % 2 else "needs_review"}
              for i in range(20)],
        "b": [{"script": "b1", "status": "compliant"}, {"script": "b1", "status": "compliant"},
              {"script": "hola", "status": "compliant", "language": "es"}],
    }  # fmt: skip
    rows = sample(pool, {"a": 50, "b": 20}, 10)
    assert rows == sample(pool, {"a": 50, "b": 20}, 10)
    assert [r["customer"] for r in rows].count("b") == 1  # distinct English scripts only
    assert len(rows) == 10
    assert sum(r["status"] == "needs_review" for r in rows if r["customer"] == "a") == 4
    assert all(r[c] == "" for r in rows for c in CRITERIA)


def answers(*values: str) -> Iterator[str]:
    yield from values


def test_labeling_saves_as_it_goes_and_resumes(tmp_path: Path) -> None:
    path = tmp_path / "cal.csv"
    rows = sample({"a": [{"script": f"s{i}", "status": "compliant"} for i in range(3)]},
                  {"a": 3}, 3)  # fmt: skip
    write(rows, path)
    first = answers("y", "n", "y", "y", "q")  # first script, then quit mid-second
    label(path, ask=lambda _: next(first))
    saved = read(path)
    assert [saved[0][c] for c in CRITERIA] == ["y", "n", "y"]
    assert saved[1]["clarity"] == "y" and saved[1]["tone"] == ""
    second = answers("x", "n", "y", "s", "y", "y", "y")  # bad input re-asks; s skips a script
    label(path, ask=lambda _: next(second))
    saved = read(path)
    assert [saved[1][c] for c in CRITERIA] == ["y", "n", "y"]
    assert [saved[2][c] for c in CRITERIA] == ["", "", ""]


def test_agreement_and_kappa() -> None:
    perfect = agreement([True, False, True], [True, False, True])
    assert (perfect.accuracy, perfect.kappa) == (1.0, 1.0)
    # Textbook case: po = 0.7, pe = 0.5 -> kappa 0.4
    owner = [True] * 5 + [False] * 5
    judge: list[bool | None] = [True] * 4 + [False] + [True] * 2 + [False] * 3
    a = agreement(owner, judge)
    assert a.accuracy == pytest.approx(0.7) and a.kappa == pytest.approx(0.4)
    assert a.confusion["owner_pass_judge_pass"] == 4
    unparsed = agreement([True, False], [None, False])
    assert unparsed.accuracy == 0.5 and unparsed.confusion["owner_pass_judge_unparsed"] == 1
    assert agreement([True, True], [True, True]).kappa is None  # undefined: one class
