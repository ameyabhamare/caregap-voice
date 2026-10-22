"""Clinical codes shared by the edge-case builder, the fill simulator and (Phase 2) gap logic.

Codes match what Synthea 4.0.0 emits, so hand-built edge cases look like Synthea members.
"""

from __future__ import annotations

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"

# Tag on every resource we create ourselves (edge cases, simulated fills), so they can be
# told apart from Synthea output.
SYNTHETIC_TAG_SYSTEM = "urn:caregap:synthetic"
EDGE_CASE_IDENTIFIER_SYSTEM = "urn:caregap:edge-case"

WELLNESS_ENCOUNTER = ("185349003", "Encounter for check up (procedure)")
HOSPICE_ENCOUNTER = ("305336008", "Admission to hospice (procedure)")
DIABETES_TYPE_2 = ("44054006", "Diabetes mellitus type 2 (disorder)")
DIABETES_TYPE_1 = ("46635009", "Diabetes mellitus type 1 (disorder)")
PREDIABETES = ("714628002", "Prediabetes (finding)")
OPHTHALMIC_ENCOUNTER = ("36228007", "Ophthalmic examination and evaluation (procedure)")
DIABETIC_EYE_EXAM = ("722161008", "Diabetic retinal eye exam (procedure)")

RETINOPATHY_SEVERITY_LEFT = (
    "71490-7",
    "Left eye Diabetic retinopathy severity level by Ophthalmoscopy",
)
RETINOPATHY_SEVERITY_RIGHT = (
    "71491-5",
    "Right eye Diabetic retinopathy severity level by Ophthalmoscopy",
)
NO_RETINOPATHY = ("LA18643-9", "No apparent retinopathy")
MILD_NPDR = ("LA18644-7", "Mild non-proliferative retinopathy")

DIABETES_CODES = frozenset({DIABETES_TYPE_1[0], DIABETES_TYPE_2[0]})

STATIN_INGREDIENTS = (
    "atorvastatin",
    "fluvastatin",
    "lovastatin",
    "pitavastatin",
    "pravastatin",
    "rosuvastatin",
    "simvastatin",
)

SIMVASTATIN_20 = ("312961", "Simvastatin 20 MG Oral Tablet")


def is_statin(display: str) -> bool:
    text = display.lower()
    return any(name in text for name in STATIN_INGREDIENTS)
