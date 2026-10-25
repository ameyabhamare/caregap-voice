"""Gold: care-gap status per member, measure and measurement year.

Implements the working definitions in fixtures/edge_cases/members.yaml (header):

- awv       age >= 65 on Dec 31; met by a wellness (check-up) encounter in the year.
- eye_exam  age 18-75 with type 1/2 diabetes onset by Dec 31; met by a diabetic retinal
            exam in the year, or a prior-year exam with "no apparent retinopathy" in both eyes.
- statin    >= 2 statin fills in the year; PDC from the first fill to Dec 31 with early
            refills shifted forward; met when PDC >= 80%.
- Exclusions: hospice during the year, or died on or before Dec 31. Members who would be
  eligible become "excluded"; others stay "not_eligible".

Wellness and eye exam are Spark SQL; statin PDC uses window functions.
"""

from __future__ import annotations

from functools import reduce

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from caregap.data import codes

MEASURES = ("awv", "eye_exam", "statin")
GOLD_GAPS_COLUMNS = (
    "member_id",
    "measure",
    "measurement_year",
    "status",
    "evidence_date",
    "pdc",
    "reason",
)

_BASE_SQL = """
WITH m AS (
  SELECT member_id, birth_date, deceased_date,
         year(DATE'{ye}') - year(birth_date)
           - CASE WHEN date_format(birth_date, 'MMdd') > '{ye_mmdd}' THEN 1 ELSE 0 END AS age
  FROM silver_members
),
hospice AS (
  SELECT member_id, min(d) AS hospice_date FROM (
    SELECT member_id, start_date AS d FROM silver_encounters
    WHERE type_code = '{hospice_enc}' AND start_date BETWEEN DATE'{ys}' AND DATE'{ye}'
    UNION ALL
    SELECT member_id, performed_date AS d FROM silver_procedures
    WHERE code = '{hospice_proc}' AND performed_date BETWEEN DATE'{ys}' AND DATE'{ye}'
  ) GROUP BY member_id
)
SELECT m.member_id, m.age, h.hospice_date, m.deceased_date,
       (m.deceased_date <= DATE'{ye}' OR h.hospice_date IS NOT NULL) AS excluded,
       CASE WHEN m.deceased_date <= DATE'{ye}' THEN concat('deceased ', m.deceased_date)
            WHEN h.hospice_date IS NOT NULL THEN concat('hospice from ', h.hospice_date)
       END AS exclusion_reason
FROM m LEFT JOIN hospice h USING (member_id)
"""

_AWV_SQL = """
SELECT b.member_id,
       b.age >= 65 AS eligible,
       v.last_visit IS NOT NULL AS met,
       v.last_visit AS evidence_date,
       CASE WHEN b.age < 65 THEN concat('age ', b.age, ' < 65')
            WHEN v.last_visit IS NOT NULL THEN concat('wellness visit ', v.last_visit)
            ELSE 'no wellness visit in {year}' END AS detail
FROM base b
LEFT JOIN (
  SELECT member_id, max(start_date) AS last_visit FROM silver_encounters
  WHERE type_code = '{wellness}' AND start_date BETWEEN DATE'{ys}' AND DATE'{ye}'
  GROUP BY member_id
) v USING (member_id)
"""

_EYE_SQL = """
WITH diabetic AS (
  SELECT DISTINCT member_id FROM silver_conditions
  WHERE code IN ({diabetes}) AND onset_date <= DATE'{ye}'
),
exams AS (
  SELECT member_id, encounter_id, performed_date FROM silver_procedures
  WHERE code = '{eye_exam}' AND performed_date <= DATE'{ye}'
),
negative_encounters AS (
  -- Both eyes recorded as "no apparent retinopathy", and no other severity result.
  SELECT encounter_id FROM silver_observations
  WHERE code IN ('{left}', '{right}')
  GROUP BY encounter_id
  HAVING count(DISTINCT CASE WHEN value_code = '{negative}' THEN code END) = 2
     AND count(CASE WHEN value_code <> '{negative}' THEN 1 END) = 0
),
qualifying AS (
  SELECT member_id, max(performed_date) AS exam_date, max(current_year) AS current_year
  FROM (
    SELECT member_id, performed_date, true AS current_year FROM exams
    WHERE performed_date BETWEEN DATE'{ys}' AND DATE'{ye}'
    UNION ALL
    SELECT e.member_id, e.performed_date, false FROM exams e
    JOIN negative_encounters n ON e.encounter_id = n.encounter_id
    WHERE e.performed_date BETWEEN DATE'{prev_ys}' AND DATE'{prev_ye}'
  ) GROUP BY member_id
)
SELECT b.member_id,
       (d.member_id IS NOT NULL AND b.age BETWEEN 18 AND 75) AS eligible,
       q.member_id IS NOT NULL AS met,
       q.exam_date AS evidence_date,
       CASE WHEN d.member_id IS NULL THEN 'no diabetes diagnosis'
            WHEN b.age NOT BETWEEN 18 AND 75 THEN concat('age ', b.age, ' outside 18-75')
            WHEN q.current_year THEN concat('retinal exam ', q.exam_date)
            WHEN q.member_id IS NOT NULL
              THEN concat('prior-year exam ', q.exam_date, ' with no retinopathy')
            ELSE 'no retinal exam in {year} and no negative result in {prev_year}' END AS detail
FROM base b
LEFT JOIN diabetic d USING (member_id)
LEFT JOIN qualifying q USING (member_id)
"""


def _statin(spark: SparkSession, year: int) -> DataFrame:
    """PDC with early refills shifted forward, computed without row-by-row iteration.

    With fills ordered by date, cumulative supply S_n and day number d_n, the shifted end
    of fill n is ``max_{k<=n}(d_k - S_{k-1}) + S_n - 1``: a running max over a window.
    """
    ys, ye = F.lit(f"{year}-01-01").cast("date"), F.lit(f"{year}-12-31").cast("date")
    epoch = F.lit("1970-01-01").cast("date")
    fills = spark.table("silver_medication_dispenses").where(
        F.col("is_statin") & F.col("fill_date").between(ys, ye) & (F.col("days_supply") > 0)
    )
    w = Window.partitionBy("member_id").orderBy("fill_date", "dispense_id")
    upto = w.rowsBetween(Window.unboundedPreceding, Window.currentRow)
    cum = F.sum("days_supply").over(upto)
    shifted = (
        fills.withColumn("cum", cum)
        .withColumn("a", F.datediff("fill_date", epoch) - (F.col("cum") - F.col("days_supply")))
        .withColumn("end_n", F.max("a").over(upto) + F.col("cum") - 1)
        .withColumn("start_n", F.col("end_n") - F.col("days_supply") + 1)
        .withColumn(
            "covered",
            F.greatest(F.lit(0), F.least("end_n", F.datediff(ye, epoch)) - F.col("start_n") + 1),
        )
    )
    per_member = shifted.groupBy("member_id").agg(
        F.count("*").alias("fills"),
        F.min("fill_date").alias("first_fill"),
        F.max("fill_date").alias("last_fill"),
        F.sum("covered").alias("covered_days"),
    )
    period = F.datediff(ye, "first_fill") + 1
    base = spark.table("base").select("member_id")
    joined = base.join(per_member, "member_id", "left").withColumn("period", period)
    fills_n = F.coalesce(F.col("fills"), F.lit(0))
    return joined.select(
        "member_id",
        (fills_n >= 2).alias("eligible"),
        # Integer comparison avoids floating-point edge cases at exactly 80%.
        ((fills_n >= 2) & (F.col("covered_days") * 5 >= F.col("period") * 4)).alias("met"),
        F.col("last_fill").alias("evidence_date"),
        F.when(fills_n >= 2, F.col("covered_days") / F.col("period")).alias("pdc"),
        F.when(fills_n < 2, F.format_string(f"%d statin fill(s) in {year}; needs 2", fills_n))
        .otherwise(
            F.format_string(
                "PDC %.1f%% (%d of %d days)",
                F.col("covered_days") / F.col("period") * 100,
                F.col("covered_days"),
                F.col("period"),
            )
        )
        .alias("detail"),
    )


def member_gaps(spark: SparkSession, silver: dict[str, DataFrame], year: int) -> DataFrame:
    """Gold ``member_gaps``: one row per member x measure for ``year``."""
    for name, df in silver.items():
        df.createOrReplaceTempView(name)
    params = {
        "year": year,
        "prev_year": year - 1,
        "ys": f"{year}-01-01",
        "ye": f"{year}-12-31",
        "ye_mmdd": "1231",
        "prev_ys": f"{year - 1}-01-01",
        "prev_ye": f"{year - 1}-12-31",
        "hospice_enc": codes.HOSPICE_ENCOUNTER[0],
        "hospice_proc": codes.HOSPICE_CARE[0],
        "wellness": codes.WELLNESS_ENCOUNTER[0],
        "diabetes": ", ".join(f"'{c}'" for c in sorted(codes.DIABETES_CODES)),
        "eye_exam": codes.DIABETIC_EYE_EXAM[0],
        "left": codes.RETINOPATHY_SEVERITY_LEFT[0],
        "right": codes.RETINOPATHY_SEVERITY_RIGHT[0],
        "negative": codes.NO_RETINOPATHY[0],
    }
    spark.sql(_BASE_SQL.format(**params)).createOrReplaceTempView("base")
    results = {
        "awv": spark.sql(_AWV_SQL.format(**params)),
        "eye_exam": spark.sql(_EYE_SQL.format(**params)),
        "statin": _statin(spark, year),
    }
    base = spark.table("base")
    scored_frames = []
    for measure, df in results.items():
        if "pdc" not in df.columns:
            df = df.withColumn("pdc", F.lit(None).cast("double"))
        scored = df.join(base, "member_id").select(
            "member_id",
            F.lit(measure).alias("measure"),
            F.lit(year).alias("measurement_year"),
            F.when(~F.col("eligible"), "not_eligible")
            .when(F.col("excluded"), "excluded")
            .when(F.col("met"), "met")
            .otherwise("gap")
            .alias("status"),
            F.when(F.col("met"), F.col("evidence_date")).alias("evidence_date"),
            "pdc",
            F.when(F.col("eligible") & F.col("excluded"), F.col("exclusion_reason"))
            .otherwise(F.col("detail"))
            .alias("reason"),
        )
        scored_frames.append(scored)
    return reduce(DataFrame.unionByName, scored_frames)


def gold_members(silver_members: DataFrame) -> DataFrame:
    """Gold ``members``: the member registry outreach reads (synthetic data only)."""
    return silver_members.select(
        "member_id",
        "given_name",
        "family_name",
        "birth_date",
        "gender",
        "language",
        "phone",
        "deceased_date",
        "is_edge_case",
    )
