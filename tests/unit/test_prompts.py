from pathlib import Path

import pytest

from caregap.prompts import FilePromptStore, Prompt
from tests.helpers import P


def test_outreach_prompt_renders_all_variables() -> None:
    prompt = FilePromptStore().get("outreach-script")
    assert prompt.variables == {
        "plan_name", "max_words", "disclaimer", "opt_out", "given_name", "services",
    }  # fmt: skip
    messages = prompt.render(
        plan_name=P.plan_name, max_words=120, disclaimer=P.disclaimer,
        opt_out=P.opt_out, given_name="Ava", services="- a yearly eye exam",
    )  # fmt: skip
    assert [m.role for m in messages] == ["system", "user"]
    assert P.disclaimer in messages[0].content
    assert "{{" not in messages[0].content + messages[1].content


def test_render_rejects_missing_variables() -> None:
    prompt = Prompt("p", "file", (("user", "Hi {{name}} from {{plan}}"),))
    with pytest.raises(KeyError, match="plan"):
        prompt.render(name="Ava")


def test_file_store_checks_declared_name(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("name: b\nmessages: [{role: user, content: hi}]\n")
    with pytest.raises(ValueError, match="declares name"):
        FilePromptStore(tmp_path).get("a")
