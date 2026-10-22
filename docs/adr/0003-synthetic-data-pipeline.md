# 3. Synthetic data pipeline

Date: 2026-10-22. Status: Accepted.

## Context
Every later phase (gap detection, outreach, RAG, evals) needs a realistic member
population with known, reproducible content, plus members whose correct gap outcome is
known in advance. No real PHI may be used anywhere.

## Decision
- **Synthea 4.0.0**, jar pinned by sha256, generates ~2,000 adult members (ages 18-90)
  in Massachusetts. All inputs are fixed: seed, clinician seed, reference date *and* end
  date (`-r`/`-e 20260101`; without `-e` Synthea simulates up to today and output drifts),
  population, age range and `synthea/synthea.properties`. Same inputs give byte-identical
  bundles.
- Only the resource types the gap logic needs are exported (Patient, Encounter, Condition,
  Procedure, Observation, MedicationRequest, Medication, plus the referenced Organization,
  Practitioner, PractitionerRole and Location), with 2 years of history. This removes the
  bulkiest types (ExplanationOfBenefit, Claim, DiagnosticReport, DocumentReference,
  Provenance) and roughly halves the volume.
- Synthea runs as a run-once compose service (`tools` profile) on the **Java runtime inside
  the HAPI image** we already pull. That avoids another image download for a fresh clone,
  at the cost of tying Synthea's JRE to HAPI's image version. If a HAPI upgrade breaks
  Synthea, switch the service to a pinned `eclipse-temurin` JRE image.
- **HAPI FHIR persists to Postgres** (a `hapi` database on the shared server) instead of
  H2: ~2,000 members is several hundred thousand resources, and bulk export runs as batch
  jobs against that store.
- The loader rewrites every create (`POST`) into `PUT Type/<id>` with the ID the bundle
  already carries (Synthea's UUIDs; readable `edge-*` IDs for edge cases). Loads are
  idempotent and server IDs are stable across runs.
- **Simulated statin fills.** Synthea models a statin as a yearly prescription renewal
  with no dispense events or days supply, so adherence cannot be measured from its output.
  The loader adds deterministic `MedicationDispense` fills (seeded per member, adherent /
  partial / poor profiles, 30- or 90-day supplies) for members on a statin in 2024-2025.
  They are tagged `urn:caregap:synthetic|simulated-fill` and are the only data we add to
  Synthea members.
- **Edge-case members** are defined in a reviewable YAML file
  (`fixtures/edge_cases/members.yaml`) with expected outcomes per measure. A builder emits
  Synthea-shaped bundles (same codes and resource shapes), tagged `edge-case`. A small
  reference implementation in the tests checks every expectation against the written
  measure definitions.
- Export uses FHIR Bulk Data `$export` (system level) into one NDJSON file per type.
- Validation writes `data/export/manifest.json` with counts and a **content digest** that
  ignores server metadata (`versionId`, `lastUpdated`, `source`) and line order.
  `make seed-verify` seeds twice from an empty server and compares digests.
- Generated data is never committed (`/data` is gitignored). Only inputs are: the
  properties file, the edge-case YAML and code.

## Consequences
- A full `make seed` takes tens of minutes, dominated by the FHIR load. HAPI's batch
  scheduler adds a few minutes to every export regardless of size.
- Simulated fills are our own model of adherence, not Synthea's. Phase 2 statin results
  reflect the profile mix (60% adherent, 25% partial, 15% poor), which is documented and
  tunable in `caregap/data/fills.py`.
- Measure definitions are simplified HEDIS-like rules, written in the edge-case YAML
  header; Phase 2 must reproduce every edge-case expectation exactly.
