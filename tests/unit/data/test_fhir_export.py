from pathlib import Path

import httpx
import pytest

from caregap.data.fhir_export import ExportError, bulk_export

STATUS = "http://fhir/$export-poll-status?_jobId=1"


def _handler(polls_before_done: int, deleted: list[str]) -> httpx.MockTransport:
    state = {"polls": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "DELETE":
            deleted.append(url)
            return httpx.Response(202)
        if "/$export?" in url:
            assert request.headers["Prefer"] == "respond-async"
            return httpx.Response(202, headers={"Content-Location": STATUS})
        if url == STATUS:
            state["polls"] += 1
            if state["polls"] <= polls_before_done:
                return httpx.Response(202, headers={"X-Progress": "working"})
            return httpx.Response(
                200,
                json={
                    "output": [
                        {"type": "Patient", "url": "http://fhir/Binary/p1"},
                        {"type": "Patient", "url": "http://fhir/Binary/p2"},
                        {"type": "Encounter", "url": "http://fhir/Binary/e1"},
                    ],
                    "error": [],
                },
            )
        bodies = {
            "http://fhir/Binary/p1": b'{"resourceType":"Patient","id":"a"}',  # no trailing \n
            "http://fhir/Binary/p2": b'{"resourceType":"Patient","id":"b"}\n',
            "http://fhir/Binary/e1": b'{"resourceType":"Encounter","id":"e"}\n',
        }
        return httpx.Response(200, content=bodies[url])

    return httpx.MockTransport(handle)


def test_export_polls_downloads_appends_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("caregap.data.fhir_export.time.sleep", lambda _: None)
    (tmp_path / "Stale.ndjson").write_text("old\n")
    deleted: list[str] = []
    files = bulk_export("http://fhir", tmp_path, log=lambda _: None, transport=_handler(2, deleted))
    assert set(files) == {"Patient", "Encounter"}
    assert (tmp_path / "Patient.ndjson").read_text().splitlines() == [
        '{"resourceType":"Patient","id":"a"}',
        '{"resourceType":"Patient","id":"b"}',
    ]
    assert not (tmp_path / "Stale.ndjson").exists()
    assert deleted == [STATUS]


def test_export_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("caregap.data.fhir_export.time.sleep", lambda _: None)
    with pytest.raises(ExportError, match="did not finish"):
        bulk_export(
            "http://fhir", tmp_path, timeout_seconds=0, log=lambda _: None,
            transport=_handler(10**6, []),
        )  # fmt: skip


def test_export_kickoff_failure(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(400, text="bulk export disabled"))
    with pytest.raises(ExportError, match="kick-off failed"):
        bulk_export("http://fhir", tmp_path, log=lambda _: None, transport=transport)
