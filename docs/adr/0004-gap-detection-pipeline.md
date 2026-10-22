# 4. Gap-detection pipeline: one PySpark codebase, local and Databricks

Date: 2026-10-22. Status: Accepted.

## Context
The brief puts batch data work on Databricks Free Edition (bronze/silver/gold Delta tables,
gap detection in PySpark/SQL, an Asset Bundle). It also requires that a fresh clone runs
end to end following only the README, which cannot assume a Databricks account. CI must
not depend on a cloud workspace either.

## Decision
- **One codebase, two runners.** `caregap.pipeline` is plain PySpark (DataFrame API and
  Spark SQL only, no RDDs or SparkContext, which serverless compute does not support).
  - `make pipeline` runs it locally with PySpark and writes Parquet under `data/lake/`.
    This is the default and what tests and CI use.
  - `make pipeline-databricks` deploys a Databricks Asset Bundle (`databricks.yml`):
    a serverless job (environment version 4) with bronze, silver, gold and snapshot tasks
    that write managed Delta tables in Unity Catalog. Raw NDJSON is uploaded to a UC volume.
  - A two-method `Store` interface (Parquet path vs UC table) is the only difference.
- **Version alignment.** Local PySpark is pinned to 4.0.4 to match serverless environment
  version 4 (Python 3.12, Spark 4.0 via Databricks Connect 17). The job pins
  `environment_version: "4"`.
- **Packaging.** The bundle builds the project wheel with `uv build` and runs it via a
  `python_wheel_task` entry point (`caregap-pipeline`). PySpark is not a runtime dependency
  of the wheel; serverless provides it.
- **Layers.** Bronze keeps raw JSON lines plus ingestion metadata. Silver parses with
  explicit schemas into typed tables. Gold holds `member_gaps` (member x measure x year)
  and `members`.
- **Gap logic.** Wellness visit and eye exam are Spark SQL. Statin PDC uses window
  functions: with early refills shifted forward, fill n ends at
  `max_{k<=n}(day_k - S_{k-1}) + S_n - 1` (S = cumulative supply), a running max, so no
  row-by-row iteration. The 80% threshold is an integer comparison to avoid float edge
  cases. Definitions are the ones in `fixtures/edge_cases/members.yaml`; an acceptance
  test checks all 23 edge-case members x 3 measures.
- **Gold to Postgres.** The snapshot task writes gold as JSON lines (to the volume on
  Databricks, under `data/lake/snapshot` locally). `make sync-gaps` validates the rows,
  upserts members and replaces a measurement year's `care_gaps` in one transaction. JSON
  lines keep the sync free of Spark, Arrow and SQL-warehouse client dependencies.

## Consequences
- The local and Databricks paths can drift only in storage, which is a small surface.
  The full gap logic is tested locally on every `make check`.
- PySpark tests need Java 17+ and add ~45 s to `make check`.
- PyPI ships PySpark 4.0.4 only as a source distribution, so the first `uv sync` builds it
  (several minutes, ~400 MB download).
- The Databricks run is only as reproducible as serverless environment 4; Databricks
  updates Connect patch versions within it.
- Statin coverage ignores supply carried over from fills before the measurement year,
  matching the written definition (PDC starts at the first in-year fill).
