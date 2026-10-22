"""Bronze: raw FHIR NDJSON lines, unparsed, with ingestion metadata."""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F


def build_bronze(spark: SparkSession, source_dir: str) -> DataFrame:
    """One row per resource across every ``<Type>.ndjson`` file in ``source_dir``.

    Columns: resource_type, resource_id, raw (the JSON line), source_file, ingested_at.
    """
    # A filter instead of a "*.ndjson" glob: Spark logs a spurious stack trace for globs.
    lines = spark.read.option("pathGlobFilter", "*.ndjson").text(source_dir)
    return lines.where(F.length(F.trim("value")) > 0).select(
        F.get_json_object("value", "$.resourceType").alias("resource_type"),
        F.get_json_object("value", "$.id").alias("resource_id"),
        F.col("value").alias("raw"),
        F.col("_metadata.file_name").alias("source_file"),
        F.current_timestamp().alias("ingested_at"),
    )
