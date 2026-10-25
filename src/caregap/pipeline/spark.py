"""Local Spark session for development, tests and `make pipeline`."""

from __future__ import annotations

import shutil

from pyspark.sql import SparkSession


class JavaNotFoundError(RuntimeError):
    pass


def local_session(app_name: str = "caregap", cores: str = "*") -> SparkSession:
    if shutil.which("java") is None:
        raise JavaNotFoundError(
            "PySpark needs Java 17 or newer on PATH. Install a JDK (e.g. Temurin 21) and retry."
        )
    return (
        SparkSession.builder.master(f"local[{cores}]")
        .appName(app_name)
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", "8")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.ansi.enabled", "true")
        .getOrCreate()
    )
