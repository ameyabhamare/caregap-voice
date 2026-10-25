"""Prompt registration against the running MLflow server (needs `make up`)."""

import os
import uuid
from pathlib import Path

import httpx
import pytest

from caregap.config import get_settings
from caregap.prompts import MlflowPromptStore, register_all

pytestmark = pytest.mark.integration


@pytest.fixture
def tracking_uri() -> str:
    uri = get_settings().mlflow_tracking_uri
    try:
        httpx.get(f"{uri}/health", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        if os.environ.get("CAREGAP_REQUIRE_SERVICES") == "1":
            raise
        pytest.skip(f"MLflow not reachable ({exc})")
    return uri


def test_register_is_idempotent_and_new_text_gets_a_new_version(
    tracking_uri: str, tmp_path: Path
) -> None:
    name = f"test-prompt-{uuid.uuid4().hex[:8]}"
    path = tmp_path / f"{name}.yaml"
    path.write_text(f"name: {name}\nmessages: [{{role: user, content: 'Hi {{{{who}}}}'}}]\n")

    assert register_all(tracking_uri, tmp_path) == [f"{name}: registered version 1 -> production"]
    assert register_all(tracking_uri, tmp_path) == [f"{name}: unchanged (version 1)"]

    path.write_text(f"name: {name}\nmessages: [{{role: user, content: 'Hello {{{{who}}}}'}}]\n")
    assert register_all(tracking_uri, tmp_path) == [f"{name}: registered version 2 -> production"]

    prompt = MlflowPromptStore(tracking_uri).get(name)
    assert prompt.version == "2"
    assert prompt.render(who="Ava")[0].content == "Hello Ava"


def test_candidate_alias_then_promote(tracking_uri: str, tmp_path: Path) -> None:
    from caregap.prompts import promote

    name = f"test-prompt-{uuid.uuid4().hex[:8]}"
    path = tmp_path / f"{name}.yaml"
    path.write_text(f"name: {name}\nmessages: [{{role: user, content: 'v1'}}]\n")
    register_all(tracking_uri, tmp_path)
    # Same text as production: the candidate alias reuses the production version.
    assert register_all(tracking_uri, tmp_path, alias="candidate") == [
        f"{name}: same as production (version 1)"
    ]
    path.write_text(f"name: {name}\nmessages: [{{role: user, content: 'v2'}}]\n")
    assert register_all(tracking_uri, tmp_path, alias="candidate") == [
        f"{name}: registered version 2 -> candidate"
    ]
    assert MlflowPromptStore(tracking_uri).get(name).version == "1"  # production untouched
    assert MlflowPromptStore(tracking_uri, alias="candidate").get(name).version == "2"
    assert promote(tracking_uri, "candidate", tmp_path) == [f"{name}: production -> version 2"]
    assert MlflowPromptStore(tracking_uri).get(name).version == "2"
