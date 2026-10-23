"""Judge calibration against owner labels.

    python -m caregap.evals.calibration sample      # dev time, once: writes the CSV
    python -m caregap.evals.calibration label       # make label-judge (owner)
    python -m caregap.evals.calibration agreement   # make judge-agreement (MLflow)

The CSV (``fixtures/judge/calibration.csv``) holds 50 distinct English scripts from
earlier eval runs with empty label columns. Labels are ``y`` (pass) or ``n`` (fail) per
criterion and come only from the owner: nothing here pre-fills or suggests them.
Agreement is reported per criterion as accuracy and Cohen's kappa, with confusion counts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import textwrap
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from caregap.evals.judge import CRITERIA, Judge

CSV_PATH = Path("fixtures/judge/calibration.csv")
FIELDS = ("id", "customer", "status", "script", *CRITERIA, "notes")
EXPERIMENT = "judge-calibration"
#: Rule-eval MLflow runs the sample was drawn from (recorded in the CSV's git history).
SOURCE_RUNS = {
    "sunrise": ("1220bc98a6964f99966f4f3bb57c0765", "21a8f5e2d1fd4cd7aafb6dec139fbe6a",
                "9b84e233f31749c092a3d8b6559a3f65"),
    "horizon": ("b09ea0ef44ac4d3d96b051fd431b82e7", "6545a49437ff4ea6919efd673308b718"),
}  # fmt: skip
#: At most this many per customer; the total is filled up to ``TOTAL`` from the others.
#: (Horizon has few distinct scripts: no names in its prompt and a fixed seed.)
PER_CUSTOMER = {"sunrise": 50, "horizon": 20}
TOTAL = 50


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sample(
    rows_by_customer: dict[str, list[dict[str, str]]], per_customer: dict[str, int], total: int
) -> list[dict[str, str]]:
    """Deterministic sample of distinct English scripts. Customers listed later are taken
    first up to their cap, then earlier ones fill the total. Within a customer, half
    (where available) are scripts that failed compliance, so the set is not all easy."""
    chosen: dict[str, list[dict[str, str]]] = {}
    left = total
    for customer in reversed(list(rows_by_customer)):
        seen: set[str] = set()
        unique = []
        for r in sorted(rows_by_customer[customer], key=lambda r: _key(r["script"])):
            if r.get("language", "en") == "en" and r["script"] not in seen:
                seen.add(r["script"])
                unique.append(r)
        n = min(per_customer[customer], left)
        failed = [r for r in unique if r["status"] != "compliant"][: n // 2]
        passed = [r for r in unique if r["status"] == "compliant"][: n - len(failed)]
        chosen[customer] = sorted(failed + passed, key=lambda r: _key(r["script"]))
        left -= len(chosen[customer])
    out: list[dict[str, str]] = []
    for customer in rows_by_customer:
        for r in chosen[customer]:
            out.append({"id": f"cal-{len(out) + 1:02d}", "customer": customer,
                        "status": r["status"], "script": r["script"],
                        **dict.fromkeys(CRITERIA, ""), "notes": ""})  # fmt: skip
    return out


def read(path: Path = CSV_PATH) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write(rows: Iterable[dict[str, str]], path: Path = CSV_PATH) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(buf.getvalue())
    tmp.replace(path)  # atomic: a crash never leaves a half-written file


def label(path: Path = CSV_PATH, ask: Callable[[str], str] = input) -> int:
    """Ask for every missing label, saving after each script. Resumable."""
    rows = read(path)
    todo = [r for r in rows if any(r[c] not in ("y", "n") for c in CRITERIA)]
    print(f"{len(rows) - len(todo)} of {len(rows)} scripts labeled. "
          "Answer y (pass) or n (fail); s skips a script, q saves and quits.\n")  # fmt: skip
    print("clarity: a member hearing it once knows what to do and how.\n"
          "tone: warm, respectful, calm; not pushy, alarming or condescending.\n"
          "no_medical_advice: only reminds about services and the doctor; no advice on\n"
          "  medicines, symptoms, results or treatment.\n")  # fmt: skip
    for n, row in enumerate(todo, 1):
        print(f"--- {row['id']} ({n}/{len(todo)}) " + "-" * 40)
        print(textwrap.fill(row["script"], 88))
        for c in CRITERIA:
            while row[c] not in ("y", "n"):
                answer = ask(f"  {c} [y/n/s/q]: ").strip().lower()
                if answer == "q":
                    write(rows, path)
                    return 0
                if answer == "s":
                    break
                if answer in ("y", "n"):
                    row[c] = answer
            else:
                continue
            break
        write(rows, path)
    print("All scripts labeled. Next: make judge-agreement")
    return 0


@dataclass(frozen=True)
class Agreement:
    n: int
    accuracy: float
    kappa: float | None
    #: (owner, judge) -> count, with values pass/fail/unparsed for the judge
    confusion: dict[str, int]


def agreement(owner: list[bool], judge: list[bool | None]) -> Agreement:
    """Cohen's kappa over the pairs; an unparsed judge verdict counts as a disagreement
    in accuracy and is left out of kappa."""
    confusion = {
        f"owner_{'pass' if o else 'fail'}_judge_"
        f"{'unparsed' if j is None else 'pass' if j else 'fail'}": 0
        for o in (True, False) for j in (True, False, None)
    }  # fmt: skip
    for o, j in zip(owner, judge, strict=True):
        verdict = "unparsed" if j is None else "pass" if j else "fail"
        confusion[f"owner_{'pass' if o else 'fail'}_judge_{verdict}"] += 1
    n = len(owner)
    accuracy = sum(o == j for o, j in zip(owner, judge, strict=True)) / n if n else 0.0
    pairs = [(o, j) for o, j in zip(owner, judge, strict=True) if j is not None]
    kappa = None
    if pairs:
        m = len(pairs)
        po = sum(o == j for o, j in pairs) / m
        p_owner = sum(o for o, _ in pairs) / m
        p_judge = sum(bool(j) for _, j in pairs) / m
        pe = p_owner * p_judge + (1 - p_owner) * (1 - p_judge)
        kappa = None if pe == 1 else (po - pe) / (1 - pe)
    return Agreement(n, accuracy, kappa, confusion)


def _sample_main() -> int:
    import mlflow

    from caregap.config import get_settings

    mlflow.set_tracking_uri(get_settings().mlflow_tracking_uri)
    pools: dict[str, list[dict[str, str]]] = {}
    for customer, runs in SOURCE_RUNS.items():
        for run in runs:
            text = mlflow.artifacts.load_text(f"runs:/{run}/results.csv")
            pools.setdefault(customer, []).extend(csv.DictReader(io.StringIO(text)))
    rows = sample(pools, PER_CUSTOMER, TOTAL)
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    write(rows)
    print(f"[calibration] wrote {len(rows)} unlabeled scripts to {CSV_PATH}")
    return 0


def _agreement_main() -> int:
    import mlflow

    from caregap.config import get_settings
    from caregap.llm.factory import build_client
    from caregap.prompts import MlflowPromptStore

    rows = [r for r in read() if all(r[c] in ("y", "n") for c in CRITERIA)]
    if not rows:
        print("[calibration] no labeled scripts yet: run make label-judge", file=sys.stderr)
        return 1
    settings = get_settings()
    judge = Judge(build_client(settings), MlflowPromptStore(settings.mlflow_tracking_uri))
    verdicts = [judge.judge(r["script"], purpose=f"judge-calibration:{r['id']}") for r in rows]
    results = {
        c: agreement([r[c] == "y" for r in rows], [v.scores[c] for v in verdicts])
        for c in CRITERIA
    }
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment(EXPERIMENT)
    with mlflow.start_run(run_name=f"judge {judge.client.model}") as run:
        mlflow.log_params({"model": judge.client.model, "labeled": len(rows),
                           "judge_prompt_version": verdicts[0].prompt_version})  # fmt: skip
        for c, a in results.items():
            mlflow.log_metric(f"{c}_accuracy", a.accuracy)
            if a.kappa is not None:
                mlflow.log_metric(f"{c}_kappa", a.kappa)
            mlflow.log_metric(f"{c}_owner_pass_rate", sum(r[c] == "y" for r in rows) / len(rows))
        mlflow.log_text(json.dumps({c: vars(a) for c, a in results.items()}, indent=2),
                        "agreement.json")  # fmt: skip
        per_row = io.StringIO()
        w = csv.writer(per_row)
        w.writerow(["id", *(f"{c}_{who}" for c in CRITERIA for who in ("owner", "judge")),
                    "judge_reason"])  # fmt: skip
        for r, v in zip(rows, verdicts, strict=True):
            w.writerow([r["id"], *(x for c in CRITERIA for x in (r[c], v.scores[c])), v.reason])
        mlflow.log_text(per_row.getvalue(), "pairs.csv")
        run_id = run.info.run_id
    for c, a in results.items():
        kappa = "n/a" if a.kappa is None else f"{a.kappa:.2f}"
        print(f"[calibration] {c}: {a.n} scripts, accuracy {a.accuracy:.0%}, kappa {kappa}")
    print(f"[calibration] MLflow run {run_id} in experiment {EXPERIMENT!r}")
    return 0


def main(argv: list[str]) -> int:
    commands = {"sample": _sample_main, "label": label, "agreement": _agreement_main}
    if not argv or argv[0] not in commands:
        print(f"usage: python -m caregap.evals.calibration {{{','.join(commands)}}}")
        return 2
    return commands[argv[0]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
