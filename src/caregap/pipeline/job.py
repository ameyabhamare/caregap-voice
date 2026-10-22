"""Pipeline entry point, shared by `make pipeline` (local) and the Databricks job.

    python -m caregap.pipeline.job --layer all --source data/export --lake data/lake

Layers: bronze, silver, gold, snapshot, or all (in that order). ``snapshot`` writes the
gold tables as JSON lines for `make sync-gaps` to load into Postgres.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from pyspark.sql import SparkSession

from caregap.pipeline import gold, silver
from caregap.pipeline.bronze import build_bronze
from caregap.pipeline.store import ParquetStore, Store, UnityCatalogStore

LAYERS = ("bronze", "silver", "gold", "snapshot")
BRONZE_TABLE = "bronze_fhir_resources"
GOLD_GAPS = "gold_member_gaps"
GOLD_MEMBERS = "gold_members"
SNAPSHOT_TABLES = {"member_gaps": GOLD_GAPS, "members": GOLD_MEMBERS}


def run_bronze(spark: SparkSession, store: Store, source: str) -> None:
    store.write(build_bronze(spark, source), BRONZE_TABLE)


def run_silver(spark: SparkSession, store: Store) -> None:
    bronze = store.read(spark, BRONZE_TABLE)
    for table, build in silver.SILVER_TABLES.items():
        store.write(build(bronze), table)


def run_gold(spark: SparkSession, store: Store, year: int) -> None:
    tables = {t: store.read(spark, t) for t in silver.SILVER_TABLES}
    store.write(gold.member_gaps(spark, tables, year), GOLD_GAPS)
    store.write(gold.gold_members(tables["silver_members"]), GOLD_MEMBERS)


def run_snapshot(spark: SparkSession, store: Store, snapshot_dir: str) -> None:
    for name, table in SNAPSHOT_TABLES.items():
        df = store.read(spark, table)
        df = df.orderBy(*[c for c in ("member_id", "measure") if c in df.columns])
        df.coalesce(1).write.mode("overwrite").json(f"{snapshot_dir.rstrip('/')}/{name}")


def main(argv: Sequence[str] | None = None, spark: SparkSession | None = None) -> None:
    parser = argparse.ArgumentParser(prog="caregap-pipeline")
    parser.add_argument("--layer", choices=[*LAYERS, "all"], default="all")
    parser.add_argument("--source", help="directory with <Type>.ndjson files (bronze)")
    parser.add_argument("--year", type=int, default=2025, help="measurement year")
    parser.add_argument("--lake", help="local Parquet root (local runs)")
    parser.add_argument("--catalog", help="Unity Catalog catalog (Databricks runs)")
    parser.add_argument("--schema", help="Unity Catalog schema (Databricks runs)")
    parser.add_argument("--snapshot-dir", help="where snapshot JSON goes")
    args = parser.parse_args(argv)

    store: Store
    if args.catalog and args.schema:
        store = UnityCatalogStore(args.catalog, args.schema)
    elif args.lake:
        store = ParquetStore(Path(args.lake))
    else:
        parser.error("pass --lake for local runs or --catalog and --schema for Databricks")
    snapshot_dir = args.snapshot_dir or (f"{args.lake}/snapshot" if args.lake else None)

    if spark is None:
        if args.lake:
            from caregap.pipeline.spark import local_session

            spark = local_session()
        else:
            spark = SparkSession.builder.getOrCreate()

    layers = LAYERS if args.layer == "all" else (args.layer,)
    for layer in layers:
        print(f"[pipeline] {layer}")
        if layer == "bronze":
            if not args.source:
                parser.error("--source is required for the bronze layer")
            run_bronze(spark, store, args.source)
        elif layer == "silver":
            run_silver(spark, store)
        elif layer == "gold":
            run_gold(spark, store, args.year)
        elif layer == "snapshot":
            if not snapshot_dir:
                parser.error("--snapshot-dir is required for the snapshot layer")
            run_snapshot(spark, store, snapshot_dir)


if __name__ == "__main__":
    main()
