"""Where pipeline tables live: local Parquet files or Unity Catalog Delta tables."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pyspark.sql import DataFrame, SparkSession


class Store(Protocol):
    def read(self, spark: SparkSession, table: str) -> DataFrame: ...

    def write(self, df: DataFrame, table: str) -> None: ...


class ParquetStore:
    """``<root>/<table>/`` Parquet directories, overwritten on each write."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, spark: SparkSession, table: str) -> DataFrame:
        return spark.read.parquet(str(self.root / table))

    def write(self, df: DataFrame, table: str) -> None:
        df.write.mode("overwrite").parquet(str(self.root / table))


class UnityCatalogStore:
    """Managed Delta tables ``<catalog>.<schema>.<table>``, replaced on each write."""

    def __init__(self, catalog: str, schema: str) -> None:
        self.catalog = catalog
        self.schema = schema

    def name(self, table: str) -> str:
        return f"`{self.catalog}`.`{self.schema}`.`{table}`"

    def read(self, spark: SparkSession, table: str) -> DataFrame:
        return spark.read.table(self.name(table))

    def write(self, df: DataFrame, table: str) -> None:
        (
            df.write.format("delta")
            .mode("overwrite")
            .option("overwriteSchema", "true")
            .saveAsTable(self.name(table))
        )
