"""Load edge-case members into the running HAPI server (needs `make up`).

The full generate -> load -> export -> validate path is exercised by `make seed` and
`make seed-verify`; HAPI's export scheduler makes it too slow for the test suite.
"""

import os

import httpx
import pytest

from caregap.config import get_settings
from caregap.data.fhir_load import edge_case_sources, load_bundles

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def fhir_url() -> str:
    url = get_settings().fhir_base_url
    try:
        httpx.get(f"{url}/metadata", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        if os.environ.get("CAREGAP_REQUIRE_SERVICES") == "1":
            raise
        pytest.skip(f"HAPI FHIR not reachable ({exc}); run `make up` first")
    return url


def test_edge_cases_load_with_stable_ids_and_reload_is_a_no_op(fhir_url: str) -> None:
    stats = load_bundles(fhir_url, edge_case_sources(), fill_seed=None, log=lambda _: None)
    assert stats.failures == []
    assert stats.bundles == 23

    patient = httpx.get(f"{fhir_url}/Patient/edge-all-gaps").json()
    assert patient["name"][0]["family"] == "Edgecase"
    version = patient["meta"]["versionId"]

    again = load_bundles(fhir_url, edge_case_sources(), fill_seed=None, log=lambda _: None)
    assert again.failures == []
    # Identical content: HAPI treats the PUT as a no-op, so the version does not change.
    assert httpx.get(f"{fhir_url}/Patient/edge-all-gaps").json()["meta"]["versionId"] == version

    dispenses = httpx.get(
        f"{fhir_url}/MedicationDispense", params={"subject": "Patient/edge-all-gaps"}
    ).json()
    assert len(dispenses["entry"]) == 3
