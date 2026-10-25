from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from caregap.customers import (
    KNOWN_MEASURES,
    ConfigError,
    CustomerConfig,
    customer_for,
    load,
    load_all,
)

SUNRISE = Path("customers/sunrise.yaml")


def raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(SUNRISE.read_text())
    return data


def invalid(match: str, **changes: Any) -> None:
    data = raw()
    for dotted, value in changes.items():
        node = data
        *parents, leaf = dotted.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    with pytest.raises(ValueError, match=match):
        CustomerConfig.model_validate(data)


def test_every_config_in_the_repo_is_valid() -> None:
    configs = load_all()
    assert "sunrise" in configs


def test_measures_match_the_pipeline() -> None:
    from caregap.pipeline.gold import MEASURES

    assert MEASURES == KNOWN_MEASURES


@pytest.mark.parametrize(
    ("match", "changes"),
    [
        ("unknown measures", {"measures": ["awv", "colonoscopy"]}),
        ("unknown member fields", {"allowed_member_fields": ["ssn"]}),
        ("default_language", {"default_language": "fr"}),
        ("unsupported language", {"languages__fr": raw()["languages"]["en"]}),
        ("not a Kokoro voice", {"languages__en__voice": "af_nobody"}),
        ("does not speak", {"languages__en__voice": "ef_dora"}),
        ("measure_descriptions", {"languages__en__measure_descriptions": {"awv": "x"}}),
        ("name the plan", {"languages__en__disclaimer": "For information only."}),
        ("press 9", {"languages__en__opt_out": "To stop these calls, say stop."}),
        ("press 0 for a person", {"languages__en__qa_greeting": "Hello, ask your question."}),
        ("must differ", {"escalation__callback_digit": "9"}),
        ("min_age", {"members": {"min_age": 70, "max_age": 60}}),
        ("before", {"campaign__call_window_end": "08:00"}),
        ("Extra inputs", {"surprise": True}),
    ],
)
def test_invalid_configs_are_rejected(match: str, changes: dict[str, Any]) -> None:
    invalid(match, **changes)


def write(tmp: Path, name: str, data: dict[str, Any]) -> None:
    (tmp / f"{name}.yaml").write_text(yaml.safe_dump(data))


def test_cross_config_checks(tmp_path: Path) -> None:
    write(tmp_path, "sunrise", raw())
    write(tmp_path, "other", {**raw(), "id": "other"})
    with pytest.raises(ConfigError, match="age ranges overlap"):
        load_all(tmp_path)

    (tmp_path / "other.yaml").write_text(
        yaml.safe_dump({**raw(), "id": "other", "members": {"max_age": 64}})
    )
    assert set(load_all(tmp_path)) == {"sunrise", "other"}

    write(tmp_path, "wrongname", {**raw(), "id": "x", "members": {"max_age": 1}})
    with pytest.raises(ConfigError, match="must match the file name"):
        load_all(tmp_path)


def test_prompts_may_only_use_allowed_member_fields(tmp_path: Path) -> None:
    write(tmp_path, "sunrise", {**raw(), "allowed_member_fields": []})
    with pytest.raises(ConfigError, match=r"uses \['given_name'\]"):
        load_all(tmp_path)
    data = raw()
    data["languages"]["en"]["script_prompt"] = "no-such-prompt"
    write(tmp_path, "sunrise", data)
    with pytest.raises(ConfigError, match="not in prompts"):
        load_all(tmp_path)


def test_plan_names_need_a_pronunciation(tmp_path: Path) -> None:
    data = raw()
    data["plan_name"] = "Zyx Health"
    for lang in data["languages"].values():
        lang["disclaimer"] = "This message is from Zyx Health."
    write(tmp_path, "sunrise", data)
    with pytest.raises(ConfigError, match="pronunciation"):
        load_all(tmp_path)


def test_profile_view_and_membership() -> None:
    c = load(SUNRISE)
    p = c.profile("en")
    assert (p.language, p.script_prompt, p.plan_name) == ("en", "outreach-script", c.plan_name)
    assert list(p.measure_descriptions) == list(c.measures)
    assert c.language_for("en") == "en"
    assert c.language_for("es") == "es"
    assert c.profile("es").script_prompt == "outreach-script-es"
    assert c.language_for("zh") == "en"
    assert c.language_for(None) == "en"
    # Age on Dec 31 of the measurement year.
    assert c.members.includes(date(1960, 12, 31), 2025)
    assert not c.members.includes(date(1961, 1, 1), 2025)
    assert customer_for({"sunrise": c}, date(1990, 1, 1), 2025) is None


@pytest.mark.models
def test_kokoro_voice_list_matches_the_model_file() -> None:
    import numpy as np

    from caregap.customers import KOKORO_VOICES

    voices = np.load("data/cache/kokoro/voices-v1.0.bin")
    english_or_spanish = {v for v in voices.files if v[:2] in {"af", "am", "bf", "bm", "ef", "em"}}
    assert english_or_spanish == KOKORO_VOICES
