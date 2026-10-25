"""Prompts: YAML files in ``prompts/`` are the source of truth, MLflow is the registry.

``register_all`` registers a new MLflow prompt version only when the text changed and
points the ``production`` alias at it (or a ``candidate`` alias, for evaluating a change
before ``promote``). The service loads prompts by alias, so a version
can be rolled back in MLflow without a deploy. ``FilePromptStore`` reads the YAML files
directly (tests, and running without MLflow).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from caregap.llm.base import Message, Role

PROMPTS_DIR = Path("prompts")
PRODUCTION_ALIAS = "production"
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str  # MLflow version number, or "file" when read from disk
    messages: tuple[tuple[Role, str], ...]

    @property
    def variables(self) -> set[str]:
        return {v for _, text in self.messages for v in _VAR.findall(text)}

    def render(self, **values: Any) -> list[Message]:
        missing = self.variables - values.keys()
        if missing:
            raise KeyError(f"prompt {self.name} is missing variables: {sorted(missing)}")
        return [
            Message(role, _VAR.sub(lambda m: str(values[m.group(1)]), text))
            for role, text in self.messages
        ]


def _from_template(name: str, version: str, template: list[dict[str, str]]) -> Prompt:
    return Prompt(name, version, tuple((m["role"], m["content"]) for m in template))  # type: ignore[misc]


def read_file(path: Path) -> tuple[str, str, list[dict[str, str]]]:
    data = yaml.safe_load(path.read_text())
    return data["name"], data.get("description", ""), data["messages"]


class PromptStore(Protocol):
    def get(self, name: str) -> Prompt: ...


class FilePromptStore:
    def __init__(self, directory: Path = PROMPTS_DIR) -> None:
        self.directory = directory

    def get(self, name: str) -> Prompt:
        file_name, _, template = read_file(self.directory / f"{name}.yaml")
        if file_name != name:
            raise ValueError(f"{name}.yaml declares name {file_name!r}")
        return _from_template(name, "file", template)


class MlflowPromptStore:
    def __init__(self, tracking_uri: str, alias: str = PRODUCTION_ALIAS) -> None:
        import mlflow

        mlflow.set_tracking_uri(tracking_uri)
        self.alias = alias
        self._cache: dict[str, Prompt] = {}

    def get(self, name: str) -> Prompt:
        if name not in self._cache:
            import mlflow

            pv = mlflow.genai.load_prompt(f"prompts:/{name}@{self.alias}")
            self._cache[name] = _from_template(name, str(pv.version), pv.template)
        return self._cache[name]


def register_all(
    tracking_uri: str, directory: Path = PROMPTS_DIR, alias: str = PRODUCTION_ALIAS
) -> list[str]:
    """Register changed prompts and point ``alias`` at them. Returns a log line each.

    With ``alias="candidate"`` a prompt whose file matches production gets the production
    version under the candidate alias (no new version); only changed prompts get a new
    version. ``promote`` then moves production to the candidate versions."""
    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    log = []
    for path in sorted(directory.glob("*.yaml")):
        name, description, template = read_file(path)
        production = mlflow.genai.load_prompt(
            f"prompts:/{name}@{PRODUCTION_ALIAS}", allow_missing=True
        )
        if alias != PRODUCTION_ALIAS and production is not None and production.template == template:
            mlflow.genai.set_prompt_alias(name, alias, production.version)
            log.append(f"{name}: same as production (version {production.version})")
            continue
        current = mlflow.genai.load_prompt(f"prompts:/{name}@{alias}", allow_missing=True)
        if current is not None and current.template == template:
            log.append(f"{name}: unchanged (version {current.version})")
            continue
        pv = mlflow.genai.register_prompt(
            name=name,
            template=template,
            commit_message=f"from {path.as_posix()}",
            tags={"description": description, "source": path.as_posix()},
        )
        mlflow.genai.set_prompt_alias(name, alias, pv.version)
        log.append(f"{name}: registered version {pv.version} -> {alias}")
    return log


def promote(tracking_uri: str, alias: str, directory: Path = PROMPTS_DIR) -> list[str]:
    """Point production at every prompt's ``alias`` version."""
    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    log = []
    for path in sorted(directory.glob("*.yaml")):
        name = read_file(path)[0]
        pv = mlflow.genai.load_prompt(f"prompts:/{name}@{alias}", allow_missing=True)
        if pv is None:
            continue
        old = mlflow.genai.load_prompt(f"prompts:/{name}@{PRODUCTION_ALIAS}", allow_missing=True)
        mlflow.genai.set_prompt_alias(name, PRODUCTION_ALIAS, pv.version)
        if old is None or str(old.version) != str(pv.version):
            log.append(f"{name}: production -> version {pv.version}")
    return log


def versions(tracking_uri: str, alias: str, names: list[str]) -> dict[str, str]:
    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    return {n: str(mlflow.genai.load_prompt(f"prompts:/{n}@{alias}").version) for n in names}


if __name__ == "__main__":
    import argparse

    from caregap.config import get_settings

    ap = argparse.ArgumentParser(prog="python -m caregap.prompts")
    ap.add_argument("command", nargs="?", default="register", choices=["register", "promote"])
    ap.add_argument("--alias", default=PRODUCTION_ALIAS)
    args = ap.parse_args()
    uri = get_settings().mlflow_tracking_uri
    lines = (
        register_all(uri, alias=args.alias)
        if args.command == "register"
        else promote(uri, args.alias)
    )
    for line in lines:
        print(f"[prompts] {line}")
