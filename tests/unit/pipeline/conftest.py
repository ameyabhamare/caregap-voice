from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("pyspark", reason="pipeline group not installed (uv sync --group pipeline)")

from pyspark.sql import SparkSession

from caregap.data.edge_cases import export_resources


@pytest.fixture(scope="session")
def spark() -> Iterator[SparkSession]:
    if shutil.which("java") is None:
        pytest.skip("PySpark tests need Java 17+ on PATH")
    from caregap.pipeline.spark import local_session

    session = local_session("caregap-tests", cores="2")
    session.conf.set("spark.sql.shuffle.partitions", "2")
    yield session
    session.stop()


@pytest.fixture(scope="session")
def edge_export(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Edge-case members written as a HAPI-style NDJSON bulk export."""
    out = tmp_path_factory.mktemp("edge_export")
    for rtype, resources in export_resources().items():
        (out / f"{rtype}.ndjson").write_text("".join(json.dumps(r) + "\n" for r in resources))
    return out
