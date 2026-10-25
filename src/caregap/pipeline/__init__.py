"""Medallion pipeline (bronze -> silver -> gold) and care-gap detection in PySpark.

The same code runs locally (Parquet under data/lake) and as a Databricks serverless job
(Delta tables in Unity Catalog). Modules here import only PySpark and the standard
library (plus caregap.data.codes), and use the DataFrame API only, because Databricks
serverless compute has no RDDs or SparkContext.
"""
