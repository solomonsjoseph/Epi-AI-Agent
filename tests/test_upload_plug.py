"""Upload plug switch tests.

Baseline section: the plain attachments route behaves as before when no upload
plug is configured. These tests run against a fake runtime so they need no
model provider, database or network.
"""

from __future__ import annotations

import hashlib
import io
import json
import warnings
import zipfile
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient

from api.schemas import (
    AttachmentManifestSummary,
    AttachmentUploadResult,
    RuntimeInfo,
)
from api.server import create_app
from utils.attachment_artifacts import AttachmentLimits
from utils.upload_plug import UploadPlugSettings

THREAD_ID = "thread-1"


@dataclass
class FakeRuntime:
    attachment_limits: AttachmentLimits = field(default_factory=AttachmentLimits)
    staged: list[tuple[str, list[tuple[str, str, bytes]]]] = field(
        default_factory=list
    )
    deidentified: list[bool] = field(default_factory=list)
    deidentification: list[dict | None] = field(default_factory=list)

    def authorize_thread(self, identity, thread_id: str) -> None:
        if thread_id != THREAD_ID:
            raise KeyError(thread_id)

    def runtime_info(self) -> RuntimeInfo:
        return RuntimeInfo(model_name="fake-model")

    def stage_attachments(
        self, identity, thread_id, uploads, *, deidentified=False, deidentification=None
    ):
        self.staged.append((thread_id, list(uploads)))
        self.deidentified.append(deidentified)
        self.deidentification.append(deidentification)
        return AttachmentUploadResult(
            attachments=[
                AttachmentManifestSummary.model_validate(
                    _summary(index, name, content, deidentified)
                )
                for index, (name, _mime, content) in enumerate(uploads)
            ]
        )


def _summary(index: int, name: str, content: bytes, deidentified: bool = False) -> dict:
    fields = AttachmentManifestSummary.model_fields
    values = {
        "id": f"attachment-{index:032x}",
        "filename": name,
        "kind": "table",
        "mime": "text/csv",
        "byte_size": len(content),
        "status": "staged",
        "deidentified": deidentified,
    }
    return {key: value for key, value in values.items() if key in fields}


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def client(runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("REPORT_AGENT_UPLOAD_PLUG_URL", raising=False)
    monkeypatch.delenv("REPORT_AGENT_UPLOAD_PLUG_SECRET", raising=False)
    return TestClient(create_app(runtime, provider_api_key="unused"))


def test_baseline_plain_upload_stages_files(
    client: TestClient, runtime: FakeRuntime
) -> None:
    response = client.post(
        f"/api/threads/{THREAD_ID}/attachments",
        files=[("files", ("study.csv", b"a,b\n1,2\n", "text/csv"))],
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["filename"] for item in body["attachments"]] == ["study.csv"]
    assert runtime.staged == [
        (THREAD_ID, [("study.csv", "text/csv", b"a,b\n1,2\n")])
    ]


def test_baseline_unknown_thread_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/threads/other/attachments",
        files=[("files", ("study.csv", b"a\n", "text/csv"))],
    )
    assert response.status_code == 404


def test_baseline_too_many_files_is_400(client: TestClient) -> None:
    files = [
        ("files", (f"f{i}.csv", b"a\n", "text/csv")) for i in range(11)
    ]
    response = client.post(f"/api/threads/{THREAD_ID}/attachments", files=files)
    assert response.status_code == 400


def test_baseline_runtime_info_shape(client: TestClient) -> None:
    response = client.get("/api/runtime")
    assert response.status_code == 200
    assert response.json()["model_name"] == "fake-model"


# --- Plug on -----------------------------------------------------------------


PLUG_URL = "http://127.0.0.1:8090"
SECRET = "s" * 40
HANDLE = "h" * 32

# Synthetic study files only. Repetitive rows keep DEFLATE well under the raw
# size, so a fixture with one broken rule still fits the archive size bound and
# is refused by the unzip checks, not before the fetch.
DATASET = b"id,year,visit\n" + b"".join(
    b"S%03d,2020,%d\n" % (row, row % 3) for row in range(200)
)
DICTIONARY = json.dumps(
    {"variables": [{"name": "year", "label": "Year of visit"}] * 40}
).encode()
# Contract 4: dictionary, forms and metadata arrive byte for byte as uploaded, so a PDF form
# arrives as a PDF. Staging needs only its signature.
FORM_PDF = b"%PDF-1.4\n% synthetic blank form\n" + b"0" * 64 + b"\n%%EOF\n"
# Contract 4 keeps the researcher's own file names (spaces, parentheses and all).
DATASET_PATH = "dataset/Riverbend Heart (v2)_DATA.csv"
DATASET_NAME = "Riverbend Heart (v2)_DATA.csv"
# What the record names the same file by: its path in the uploaded ZIP.
UPLOADED_DATASET_PATH = "data/Riverbend Heart (v2)_DATA.CSV"
MEMBERS = [
    (DATASET_PATH, "text/csv", DATASET),
    ("dictionary/Data Dictionary.json", "application/json", DICTIONARY),
    ("forms/baseline_crf_blank.pdf", "application/pdf", FORM_PDF),
]
STAGED_NAMES = [DATASET_NAME, "Data Dictionary.json", "baseline_crf_blank.pdf"]
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def _zip(entries, *, method=zipfile.ZIP_DEFLATED) -> bytes:
    """Build a ZIP in memory from ``(name, content)`` or ``(name, content, method)``."""
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate-name fixtures warn on write
        with zipfile.ZipFile(buffer, "w") as bundle:
            for name, content, *rest in entries:
                info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
                info.compress_type = rest[0] if rest else method
                bundle.writestr(info, content)
    return buffer.getvalue()


RECORD_PATH = "record/columns.json"


def _column(file: str, header: str, action: str = "keep", written: str = "unchanged", **override) -> dict:
    """One record column, as the checker writes it (header and fixed words only)."""
    return {
        "action": action,
        "cells_changed": 0 if action == "keep" else 200,
        "file": file,
        "header": header,
        "note": "",
        "reason": "model_choice",
        "sheet": 0,
        "source": "model",
        "written": written,
        **override,
    }


def _risk(file: str, **override) -> dict:
    return {
        "classes": 3,
        "columns": ["visit"],
        "file": file,
        "rows": 200,
        "sheet": 0,
        "small_rows": 0,
        "unique_rows": 0,
        "with_dates": False,
        **override,
    }


def _uploaded(path: str) -> str:
    # the record names each dataset by its path in the uploaded ZIP: here a "data" folder
    return UPLOADED_DATASET_PATH if path == DATASET_PATH else path


def _record_body(members=MEMBERS, **override) -> dict:
    """Contract 4: the checker's per-column record for ``members``."""
    columns: list[dict] = []
    risk: list[dict] = []
    for path, _mime, _content in members:
        if path.startswith("dataset/"):
            file = _uploaded(path)
            columns += [
                _column(
                    file,
                    "id",
                    "participant_id",
                    "a random study code per participant",
                    reason="header_participant_id",
                    source="header",
                ),
                _column(file, "year"),
                _column(file, "visit"),
            ]
            risk.append(_risk(file))
    return {
        "columns": columns,
        "contract": "4",
        "gate": "0.44.0",
        "method": "safe_harbor",
        "model": "test-model",
        "not_sent": ["README.txt"],
        "prompt_version": "2026-11-25.1",
        "risk": risk,
        "statement": "Columns were changed as listed, following the HIPAA Safe Harbor identifier list.",
        "unchanged_files": [path for path, _m, _c in members if not path.startswith("dataset/")],
        **override,
    }


def _encode(body) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("ascii")


def _record(members=MEMBERS, **override) -> bytes:
    return _encode(_record_body(members, **override))


def _member_zip(members=MEMBERS, *, method=zipfile.ZIP_DEFLATED, record: bytes | None = None) -> bytes:
    record = _record(members) if record is None else record
    return _zip([(path, content) for path, _mime, content in members] + [(RECORD_PATH, record)], method=method)


def _size_bound(members, record: bytes | None = None) -> int:
    """v1_design 15.2: sum(sizes) + 22 + sum(76 + 2 * len(path)), the record counted as an entry."""
    record = _record(members) if record is None else record
    entries = [(path, content) for path, _mime, content in members] + [(RECORD_PATH, record)]
    return sum(len(content) for _path, content in entries) + 22 + sum(76 + 2 * len(path) for path, _c in entries)


def _manifest(members=MEMBERS, archive: bytes | None = None, contract="4", record: bytes | None = None) -> dict:
    record = _record(members) if record is None else record
    archive = _member_zip(members, record=record) if archive is None else archive
    return {
        "contract": contract,
        "archive": {
            "size": len(archive),
            "sha256": hashlib.sha256(archive).hexdigest(),
        },
        "members": [
            {
                "path": path,
                "mime": mime,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, mime, content in members
        ],
        "record": {
            "path": RECORD_PATH,
            "mime": "application/json",
            "size": len(record),
            "sha256": hashlib.sha256(record).hexdigest(),
        },
    }


def _patch_first_entry(
    archive: bytes, local_at: int, central_at: int | None, value: bytes
) -> bytes:
    """Overwrite bytes of the first entry in its local header and, unless None, its central entry."""
    data = bytearray(archive)
    data[local_at : local_at + len(value)] = value
    if central_at is not None:
        central = int.from_bytes(data[-6:-2], "little")  # end record: central directory offset
        data[central + central_at : central + central_at + len(value)] = value
    return bytes(data)


class FakePlug:
    """A contract-4 plug: the manifest at the delivery URL, the ZIP at ``/archive``."""

    def __init__(
        self,
        members=MEMBERS,
        *,
        archive: bytes | None = None,
        manifest: dict | None = None,
        status: int = 200,
        archive_status: int = 200,
        body: bytes = b"",
    ):
        self.archive = _member_zip(members) if archive is None else archive
        self.manifest = manifest if manifest is not None else _manifest(members, self.archive)
        self.status = status
        self.archive_status = archive_status
        self.body = body
        self.requests: list[httpx.Request] = []

    @property
    def archive_requested(self) -> bool:
        return any(r.url.path.endswith("/archive") for r in self.requests)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {SECRET}":
            return httpx.Response(401)
        if request.url.params.get("thread_id") != THREAD_ID:
            return httpx.Response(404)
        if self.status != 200:
            return httpx.Response(self.status, content=self.body)
        path = request.url.path
        base = f"/v1/deliveries/{HANDLE}"
        if path == base:
            return httpx.Response(200, content=json.dumps(self.manifest).encode())
        if path == base + "/archive":
            if self.archive_status != 200:
                return httpx.Response(self.archive_status, content=self.body)
            return httpx.Response(200, content=self.archive)
        return httpx.Response(404)


def _plug_client(runtime: FakeRuntime, plug: FakePlug) -> TestClient:
    settings = UploadPlugSettings(
        url=PLUG_URL, secret=SECRET, transport=httpx.MockTransport(plug)
    )
    return TestClient(
        create_app(runtime, provider_api_key="unused", upload_plug=settings)
    )


def _pull(client: TestClient, handle: str = HANDLE, thread: str = THREAD_ID):
    return client.post(
        f"/api/threads/{thread}/plug-attachments", json={"handle": handle}
    )


def test_plug_off_hides_plug_routes(client: TestClient) -> None:
    assert client.get("/api/upload-plug").json() is None
    assert _pull(client).status_code == 404


def test_plug_on_reports_origin_only() -> None:
    client = _plug_client(FakeRuntime(), FakePlug())
    limits = AttachmentLimits()
    assert client.get("/api/upload-plug").json() == {
        "origin": PLUG_URL,
        "contract": "4",
        "caps": {
            "max_files": limits.max_files_per_message,
            "max_file_bytes": limits.max_bytes,
            "max_total_bytes": limits.max_message_bytes,
            "max_table_rows": limits.max_table_rows,
            "max_table_columns": limits.max_table_columns,
            "max_table_cells": limits.max_table_cells,
        },
    }


def test_plug_caps_follow_the_configured_limits() -> None:
    runtime = FakeRuntime(attachment_limits=AttachmentLimits(max_files_per_message=3))
    caps = _plug_client(runtime, FakePlug()).get("/api/upload-plug").json()["caps"]
    assert caps["max_files"] == 3


def test_plug_on_refuses_plain_upload() -> None:
    runtime = FakeRuntime()
    client = _plug_client(runtime, FakePlug())
    response = client.post(
        f"/api/threads/{THREAD_ID}/attachments",
        files=[("files", ("raw.csv", b"name\nJane\n", "text/csv"))],
    )
    assert response.status_code == 409
    assert response.json()["detail"].startswith("UPLOAD_PLUG_REQUIRED")
    assert runtime.staged == []


def test_plug_pull_unzips_and_stages_members_under_their_uploaded_names() -> None:
    runtime = FakeRuntime()
    plug = FakePlug()
    response = _pull(_plug_client(runtime, plug))
    assert response.status_code == 200
    assert [a["filename"] for a in response.json()["attachments"]] == STAGED_NAMES
    assert runtime.staged == [
        (
            THREAD_ID,
            [
                (DATASET_NAME, "text/csv", DATASET),
                ("Data Dictionary.json", "application/json", DICTIONARY),
                ("baseline_crf_blank.pdf", "application/pdf", FORM_PDF),
            ],
        )
    ]
    # Contract 4 only: the manifest, then the one archive; never the per-file route.
    assert [r.url.path for r in plug.requests] == [
        f"/v1/deliveries/{HANDLE}",
        f"/v1/deliveries/{HANDLE}/archive",
    ]
    assert all(r.headers["x-gate-contract"] == "4" for r in plug.requests)
    assert all(r.url.params["thread_id"] == THREAD_ID for r in plug.requests)


def test_plug_pull_stages_files_flagged_deidentified() -> None:
    runtime = FakeRuntime()
    response = _pull(_plug_client(runtime, FakePlug()))
    assert response.status_code == 200
    assert runtime.deidentified == [True]
    assert [a["deidentified"] for a in response.json()["attachments"]] == [True, True, True]


def test_plug_pull_passes_each_dataset_its_record_summary() -> None:
    runtime = FakeRuntime()
    assert _pull(_plug_client(runtime, FakePlug())).status_code == 200
    # Keyed by staged file name; only dataset files have one.
    assert runtime.deidentification == [
        {
            DATASET_NAME: {
                "method": "safe_harbor",
                "kept_columns": 2,
                "changed_columns": [
                    {
                        "header": "id",
                        "action": "participant_id",
                        "written": "a random study code per participant",
                    }
                ],
                "risk": [
                    {
                        "classes": 3,
                        "columns": ["visit"],
                        "rows": 200,
                        "sheet": 0,
                        "small_rows": 0,
                        "unique_rows": 0,
                        "with_dates": False,
                    }
                ],
            }
        }
    ]


def test_plain_upload_is_never_flagged_even_if_the_form_asks(
    client: TestClient, runtime: FakeRuntime
) -> None:
    # The multipart route has no such field; an extra form field changes nothing.
    response = client.post(
        f"/api/threads/{THREAD_ID}/attachments",
        files=[("files", ("study.csv", b"a,b\n1,2\n", "text/csv"))],
        data={"deidentified": "true", "deidentification": json.dumps({"method": "safe_harbor"})},
    )
    assert response.status_code == 200
    assert runtime.deidentified == [False]
    assert runtime.deidentification == [None]
    assert response.json()["attachments"][0]["deidentified"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"deidentified": True},
        {"record": {"contract": "4", "columns": []}},
        {"deidentification": {"method": "safe_harbor", "kept_columns": 0, "changed_columns": []}},
    ],
)
def test_plug_request_refuses_client_supplied_fields(extra: dict) -> None:
    runtime = FakeRuntime()
    plug = FakePlug()
    response = _plug_client(runtime, plug).post(
        f"/api/threads/{THREAD_ID}/plug-attachments",
        json={"handle": HANDLE, **extra},
    )
    assert response.status_code == 422
    assert plug.requests == []
    assert runtime.staged == []


@pytest.mark.parametrize("field_name", ["deidentified", "record", "deidentification"])
def test_message_request_refuses_client_supplied_fields(client: TestClient, field_name: str) -> None:
    response = client.post(
        f"/api/threads/{THREAD_ID}/messages",
        json={"text": "hello", "attachment_ids": [], field_name: True},
    )
    assert response.status_code == 422
    assert field_name in json.dumps(response.json())


def test_plug_pull_accepts_a_stored_archive_at_the_size_bound() -> None:
    archive = _member_zip(method=zipfile.ZIP_STORED)
    assert len(archive) == _size_bound(MEMBERS)
    runtime = FakeRuntime()
    response = _pull(_plug_client(runtime, FakePlug(archive=archive)))
    assert response.status_code == 200
    assert [name for name, _mime, _content in runtime.staged[0][1]] == STAGED_NAMES


def test_plug_pull_stages_in_manifest_order_whatever_the_archive_order() -> None:
    runtime = FakeRuntime()
    archive = _member_zip(list(reversed(MEMBERS)), record=_record(MEMBERS))
    response = _pull(_plug_client(runtime, FakePlug(archive=archive)))
    assert response.status_code == 200
    assert [name for name, _mime, _content in runtime.staged[0][1]] == STAGED_NAMES


@pytest.mark.parametrize(
    "handle", ["short", "../" + "a" * 30, "a" * 200, "a b" + "c" * 20]
)
def test_plug_bad_handle_never_calls_plug(handle: str) -> None:
    plug = FakePlug()
    response = _pull(_plug_client(FakeRuntime(), plug), handle=handle)
    assert response.status_code == 422
    assert plug.requests == []


def test_plug_unknown_thread_is_404() -> None:
    plug = FakePlug()
    assert _pull(_plug_client(FakeRuntime(), plug), thread="other").status_code == 404
    assert plug.requests == []


def _refused(plug: FakePlug, runtime: FakeRuntime | None = None) -> str:
    """Pull, require a 422 with nothing staged, and return the detail."""
    runtime = runtime or FakeRuntime()
    response = _pull(_plug_client(runtime, plug))
    assert response.status_code == 422
    assert runtime.staged == []
    return response.json()["detail"]


# Manifest refusals: refused before the archive is fetched, so the delivery is not spent.


def _with_paths(*paths: object) -> dict:
    manifest = _manifest()
    manifest["members"] = [
        {**manifest["members"][0], "path": path} for path in paths
    ]
    return manifest


@pytest.mark.parametrize(
    "path",
    [
        "data/visits.csv",
        "form/visits.pdf",
        "Dataset/visits.csv",
        "record/columns.json",
        "report/report.json",
        "dataset/.visits.csv",
        "dataset/ visits.csv",
        "dataset/-visits.csv",
        "dataset/visits.CSV",
        "dataset/visits.xls",
        "dataset/visits.png",
        "dataset/visits.zip",
        "dataset/visits",
        "dataset/visits.csv ",
        "dataset/visits.csv\n",
        "dataset/vis\tits.csv",
        "dataset/caf\u00e9.csv",
        "dataset/" + "a" * 201 + ".csv",
        "dataset/visits.csv/",
        "visits.csv",
        "study/dataset/visits.csv",
        "/dataset/visits.csv",
        "dataset/../visits.csv",
        "dataset\\visits.csv",
        "dataset/sub\\visits.csv",
        "dataset//visits.csv",
        "",
        5,
        None,
    ],
)
def test_plug_bad_path_is_refused_before_fetch(path: object) -> None:
    plug = FakePlug(manifest=_with_paths(path))
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


@pytest.mark.parametrize(
    "path, mime",
    [
        ("dataset/Riverbend Heart (v2)_DATA.csv", "text/csv"),
        ("dataset/(draft) visits [v3] ~final.tsv", "text/tab-separated-values"),
        ("dataset/" + "a" * 200 + ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("forms/Baseline CRF, blank.pdf", "application/pdf"),
        ("dictionary/Data Dictionary 2025-02-10.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("metadata/README.md", "text/markdown"),
        ("metadata/study.xml", "application/xml"),
        ("metadata/notes.txt", "text/plain"),
    ],
)
def test_plug_uploaded_file_names_are_staged_as_they_are(path: str, mime: str) -> None:
    content = DATASET if path.startswith("dataset/") else b"synthetic content\n"
    members = [(path, mime, content)]
    if not path.startswith("dataset/"):
        members = [MEMBERS[0], *members]
    runtime = FakeRuntime()
    response = _pull(_plug_client(runtime, FakePlug(members)))
    assert response.status_code == 200, response.text
    assert [name for name, _mime, _content in runtime.staged[0][1]][-1] == path.split("/", 1)[1]


def test_plug_duplicate_path_is_refused_before_fetch() -> None:
    plug = FakePlug(manifest=_with_paths(DATASET_PATH, DATASET_PATH))
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


@pytest.mark.parametrize(
    "paths",
    [
        ("dataset/visits.csv", "dataset/VISITS.csv"),
        # unique ignoring case across the whole delivery, not only within a folder
        ("dataset/visits.csv", "metadata/Visits.csv"),
    ],
)
def test_plug_names_equal_ignoring_case_are_refused_before_fetch(paths) -> None:
    plug = FakePlug(manifest=_with_paths(*paths))
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


def test_plug_basename_check_ignores_case() -> None:
    # The path pattern allows upper case; the name check still holds on its own.
    from utils.upload_plug import UploadPlugError, _unique_names

    _unique_names(["visits.csv", "visits_2.csv"])
    with pytest.raises(UploadPlugError) as caught:
        _unique_names(["Visits.csv", "VISITS.csv"])
    assert caught.value.code == "PLUG_REJECTED"


def _many_members(count: int) -> list[tuple[str, str, bytes]]:
    return [(f"dataset/visits {i}.csv", "text/csv", b"a\n1\n") for i in range(1, count + 1)]


@pytest.mark.parametrize(
    "limits, members, code",
    [
        (AttachmentLimits(), _many_members(11), "TOO_MANY_FILES"),
        (AttachmentLimits(max_bytes=len(DATASET) - 1), MEMBERS, "FILE_TOO_LARGE"),
        (
            AttachmentLimits(
                max_message_bytes=len(DATASET) + len(DICTIONARY) + len(FORM_PDF) - 1
            ),
            MEMBERS,
            "MESSAGE_ATTACHMENTS_TOO_LARGE",
        ),
    ],
)
def test_plug_over_cap_manifest_is_not_fetched(limits, members, code: str) -> None:
    plug = FakePlug(members)
    detail = _refused(plug, FakeRuntime(attachment_limits=limits))
    assert detail.startswith(code)
    assert not plug.archive_requested


def test_plug_archive_larger_than_the_size_bound_is_not_fetched() -> None:
    manifest = _manifest()
    manifest["archive"]["size"] = _size_bound(MEMBERS) + 1
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


def test_plug_size_bound_counts_the_record_entry() -> None:
    # A bigger record raises the bound by its size, its two headers and twice its path.
    small, big = _record(), _record(model="m" * 1000)
    assert _size_bound(MEMBERS, big) - _size_bound(MEMBERS, small) == len(big) - len(small)
    manifest = _manifest(record=big)
    manifest["archive"]["size"] = _size_bound(MEMBERS, big) + 1
    plug = FakePlug(manifest=manifest)
    assert _refused(plug) == "PLUG_REJECTED: the prepared archive is larger than its files allow"
    assert not plug.archive_requested


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.pop("members"),
        lambda m: m.update(members=[]),
        lambda m: m.pop("archive"),
        lambda m: m["archive"].update(size=-1),
        lambda m: m["archive"].update(size=True),
        lambda m: m["archive"].update(sha256="0" * 63),
        lambda m: m["members"][0].update(size="12"),
        lambda m: m["members"][0].update(sha256="A" * 64),
        lambda m: m["members"][0].update(mime=5),
        lambda m: m.update(files=m.pop("members")),
    ],
)
def test_plug_malformed_manifest_is_not_fetched(change) -> None:
    manifest = _manifest()
    change(manifest)
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


@pytest.mark.parametrize("contract", ["1", "3", 4, "4.0", None])
def test_plug_manifest_on_another_contract_is_a_mismatch(contract) -> None:
    manifest = _manifest()
    if contract is None:
        manifest.pop("contract")
    else:
        manifest["contract"] = contract
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("CONTRACT_MISMATCH")
    assert not plug.archive_requested


@pytest.mark.parametrize("on_archive", [False, True])
def test_plug_409_is_a_fixed_contract_mismatch(on_archive: bool) -> None:
    body = f"gate says: {DATASET_PATH} contract 3 only".encode()
    plug = (
        FakePlug(archive_status=409, body=body)
        if on_archive
        else FakePlug(status=409, body=body)
    )
    assert _refused(plug) == (
        "CONTRACT_MISMATCH: the upload plug uses a different contract version"
    )
    assert plug.archive_requested is on_archive


def test_plug_403_and_spent_delivery_are_rejected() -> None:
    assert _refused(FakePlug(status=403)).startswith("PLUG_REJECTED")
    assert _refused(FakePlug(archive_status=404)).startswith("PLUG_REJECTED")


# Archive refusals: the archive was fetched; nothing is staged.


def _archive_refusal(archive: bytes, members=MEMBERS) -> str:
    plug = FakePlug(members, archive=archive)
    detail = _refused(plug)
    assert plug.archive_requested
    return detail


def _stored_one(name: str = DATASET_PATH, content: bytes = DATASET) -> bytes:
    # the record (contract 4) follows the member, so the member stays the first entry
    record = _record([(DATASET_PATH, "text/csv", content)])
    return _zip([(name, content), (RECORD_PATH, record)], method=zipfile.ZIP_STORED)


ONE = MEMBERS[:1]


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param([*MEMBERS, ("metadata/notes.txt", "", b"x")], id="extra"),
        pytest.param(MEMBERS[:1], id="missing"),
        pytest.param([*MEMBERS, MEMBERS[0]], id="duplicate"),
        pytest.param([*MEMBERS, ("dataset/", "", b"")], id="directory"),
        pytest.param([*MEMBERS, ("/" + DATASET_PATH, "", b"x")], id="absolute"),
        pytest.param([*MEMBERS, ("dataset/../x.csv", "", b"x")], id="dotdot"),
        pytest.param([*MEMBERS, ("dataset\\x.csv", "", b"x")], id="backslash"),
    ],
)
def test_plug_archive_entries_must_equal_the_manifest(entries) -> None:
    archive = _zip([(path, content) for path, _mime, content in entries])
    assert len(archive) <= _size_bound(MEMBERS)  # refused by the unzip, not the bound
    detail = _archive_refusal(archive)
    assert detail == "PLUG_REJECTED: the prepared archive does not match its manifest"


def test_plug_entry_name_with_a_nul_byte_is_refused() -> None:
    # zipfile cuts filename at a NUL, which would make it equal the manifest path.
    name = DATASET_PATH
    archive = _zip([(name + "Z", DATASET)])
    archive = _patch_first_entry(archive, 30 + len(name), 46 + len(name), b"\x00")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        assert bundle.infolist()[0].filename == name
    detail = _archive_refusal(archive, ONE)
    assert detail == "PLUG_REJECTED: the prepared archive does not match its manifest"


def test_plug_encrypted_entry_is_refused() -> None:
    archive = _patch_first_entry(_stored_one(), 6, 8, (1).to_bytes(2, "little"))
    detail = _archive_refusal(archive, ONE)
    assert detail == "PLUG_REJECTED: the prepared archive holds an encrypted file"


def test_plug_other_compression_is_refused() -> None:
    archive = _zip([(DATASET_PATH, DATASET, zipfile.ZIP_BZIP2)])
    assert len(archive) <= _size_bound(ONE)
    detail = _archive_refusal(archive, ONE)
    assert detail == (
        "PLUG_REJECTED: the prepared archive uses an unsupported compression method"
    )


def test_plug_entry_size_differing_from_the_manifest_is_refused() -> None:
    archive = _zip([(DATASET_PATH, DATASET + b"S999,2020,0\n")])
    detail = _archive_refusal(archive, ONE)
    assert detail == "PLUG_REJECTED: the prepared archive does not match its manifest"


def test_plug_entry_hash_differing_from_the_manifest_is_refused() -> None:
    tampered = DATASET.replace(b"S007", b"S070", 1)
    assert len(tampered) == len(DATASET)
    archive = _zip([(DATASET_PATH, tampered), (RECORD_PATH, _record(ONE))])
    detail = _archive_refusal(archive, ONE)
    assert detail == "PLUG_REJECTED: a prepared file did not match its checksum"


def test_plug_archive_hash_mismatch_is_refused() -> None:
    manifest = _manifest()
    manifest["archive"]["sha256"] = hashlib.sha256(b"another archive").hexdigest()
    plug = FakePlug(manifest=manifest)
    assert _refused(plug) == "PLUG_REJECTED: the prepared archive did not match its checksum"
    assert plug.archive_requested


def test_plug_archive_longer_than_declared_is_refused() -> None:
    archive = _member_zip()
    manifest = _manifest(archive=archive)
    manifest["archive"]["size"] = len(archive) - 1
    plug = FakePlug(manifest=manifest, archive=archive)
    assert _refused(plug) == "PLUG_REJECTED: the upload plug sent more data than declared"


OTHER_DATASET_PATH = DATASET_PATH.replace("(v2)", "(v3)")


@pytest.mark.parametrize(
    "archive",
    [
        pytest.param(b"not a zip archive at all", id="not-a-zip"),
        # A wrong CRC: zipfile names the entry in its error.
        pytest.param(_patch_first_entry(_stored_one(), 14, 16, b"\x00\x00\x00\x00"), id="crc"),
        # Local header name differs from the central directory: zipfile echoes both names.
        pytest.param(
            _patch_first_entry(_stored_one(), 30, None, OTHER_DATASET_PATH.encode()),
            id="header-name",
        ),
        pytest.param(_stored_one()[: len(_stored_one()) // 2], id="truncated"),
    ],
)
def test_plug_zipfile_error_text_is_not_echoed(archive: bytes) -> None:
    detail = _archive_refusal(archive, ONE)
    assert detail == "PLUG_REJECTED: the prepared archive could not be read safely"


def test_plug_zipfile_error_is_not_chained() -> None:
    from utils.upload_plug import UploadPlugError, fetch_delivery

    archive = _patch_first_entry(_stored_one(), 14, 16, b"\x00\x00\x00\x00")
    settings = UploadPlugSettings(
        url=PLUG_URL,
        secret=SECRET,
        transport=httpx.MockTransport(FakePlug(ONE, archive=archive)),
    )
    with pytest.raises(UploadPlugError) as caught:
        fetch_delivery(
            settings, HANDLE, THREAD_ID, max_files=10, max_file_bytes=10**6, max_total_bytes=10**7
        )
    assert caught.value.code == "PLUG_REJECTED"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert "Riverbend" not in str(caught.value)


def test_plug_unreachable_is_502_and_stages_nothing() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    runtime = FakeRuntime()
    settings = UploadPlugSettings(
        url=PLUG_URL, secret=SECRET, transport=httpx.MockTransport(down)
    )
    client = TestClient(
        create_app(runtime, provider_api_key="unused", upload_plug=settings)
    )
    response = _pull(client)
    assert response.status_code == 502
    assert response.json()["detail"].startswith("PLUG_UNREACHABLE")
    assert runtime.staged == []


def test_plug_redirect_is_not_followed() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://example.com/x"})

    settings = UploadPlugSettings(
        url=PLUG_URL, secret=SECRET, transport=httpx.MockTransport(redirect)
    )
    client = TestClient(
        create_app(FakeRuntime(), provider_api_key="unused", upload_plug=settings)
    )
    assert _pull(client).status_code == 422


def test_plug_partial_staging_is_rolled_back() -> None:
    class PartialRuntime(FakeRuntime):
        discarded: list[str] = []

        def stage_attachments(
            self, identity, thread_id, uploads, *, deidentified=False, deidentification=None
        ):
            result = super().stage_attachments(
                identity,
                thread_id,
                uploads[:1],
                deidentified=deidentified,
                deidentification=deidentification,
            )
            result.errors.append(
                {"filename": uploads[1][0], "code": "UNSUPPORTED_FORMAT", "message": "x"}
            )
            return result

        def discard_staged_attachment(self, identity, thread_id, attachment_id):
            self.discarded.append(attachment_id)

    runtime = PartialRuntime()
    response = _pull(_plug_client(runtime, FakePlug()))
    assert response.status_code == 422
    assert "UNSUPPORTED_FORMAT" in response.json()["detail"]
    assert runtime.discarded == ["attachment-" + "0" * 32]


@pytest.mark.parametrize(
    "url, secret",
    [
        (PLUG_URL, ""),
        (PLUG_URL, "short"),
        ("http://example.com", SECRET),
        ("ftp://127.0.0.1:8090", SECRET),
        ("http://127.0.0.1:8090/?x=1", SECRET),
        ("http://127.0.0.1:8090/gate", SECRET),
        ("http://127.0.0.1:99999", SECRET),
    ],
)
def test_half_configured_plug_refuses_to_start(url: str, secret: str) -> None:
    with pytest.raises(ValueError):
        create_app(
            FakeRuntime(),
            provider_api_key="unused",
            upload_plug=UploadPlugSettings(url=url, secret=secret),
        )


def _refuses_before_reading_the_body(app) -> list[dict]:
    """Send a plain upload straight to the ASGI app; any read of the body fails the test."""
    import asyncio

    scope = {
        "type": "http", "method": "POST", "path": f"/api/threads/{THREAD_ID}/attachments",
        "raw_path": f"/api/threads/{THREAD_ID}/attachments".encode(), "query_string": b"",
        "headers": [(b"content-type", b"multipart/form-data; boundary=x"), (b"content-length", b"1000")],
        "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1),
        "http_version": "1.1", "root_path": "",
    }
    sent: list[dict] = []

    async def receive():
        raise AssertionError("the request body was read")

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def test_plug_on_refuses_plain_upload_before_reading_the_body() -> None:
    """Raw bytes must never reach the app process: the 409 comes before any body is read."""
    runtime = FakeRuntime()
    sent = _refuses_before_reading_the_body(_plug_client(runtime, FakePlug()).app)
    assert sent[0]["status"] == 409
    assert runtime.staged == []


def test_gateway_errors_are_unreachable_not_rejected() -> None:
    from utils.upload_plug import UploadPlugError, _raise_for_status

    for status in (502, 503, 504):
        with pytest.raises(UploadPlugError) as caught:
            _raise_for_status(httpx.Response(status))
        assert caught.value.code == "PLUG_UNREACHABLE"


@pytest.mark.parametrize(
    "url, origin",
    [
        ("http://127.0.0.1:8090", "http://127.0.0.1:8090"),
        ("http://LOCALHOST:8090", "http://localhost:8090"),
        ("https://Gate.Example.org:443", "https://gate.example.org"),
        ("http://127.0.0.1:80/", "http://127.0.0.1"),
        ("http://[::1]:8090", "http://[::1]:8090"),
    ],
)
def test_plug_origin_is_what_the_browser_reports(url: str, origin: str) -> None:
    assert UploadPlugSettings(url=url, secret=SECRET).origin == origin


def test_plug_secret_is_not_in_the_settings_repr() -> None:
    assert SECRET not in repr(UploadPlugSettings(url=PLUG_URL, secret=SECRET))


def test_plug_off_leaves_http_logging_alone() -> None:
    import logging

    logger = logging.getLogger("httpx")
    logger.setLevel(logging.NOTSET)
    create_app(FakeRuntime(), provider_api_key="unused", upload_plug=UploadPlugSettings())
    assert logger.level == logging.NOTSET
    create_app(
        FakeRuntime(),
        provider_api_key="unused",
        upload_plug=UploadPlugSettings(url=PLUG_URL, secret=SECRET),
    )
    assert logger.level == logging.WARNING


def test_plug_pull_logs_no_handle_secret_or_thread(caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    response = _pull(_plug_client(FakeRuntime(), FakePlug()))
    assert response.status_code == 200
    text = caplog.text + json.dumps(response.json())
    assert SECRET not in text
    assert HANDLE not in caplog.text
    # Nothing from the archive is logged: no member paths, contents, record or hashes.
    assert "dataset/" not in caplog.text
    assert "Riverbend" not in caplog.text
    assert "participant_id" not in caplog.text
    assert "S001" not in caplog.text
    assert hashlib.sha256(DATASET).hexdigest() not in caplog.text


def test_plug_refusal_logs_no_archive_detail(caplog) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    archive = _patch_first_entry(_stored_one(), 14, 16, b"\x00\x00\x00\x00")
    _archive_refusal(archive, ONE)
    assert "Riverbend" not in caplog.text
    assert "CRC" not in caplog.text


# Review follow-ups: fixed manifest keys, fixed media types, the shared size bound, no
# compressed transfer.


def test_plug_size_bound_matches_the_checker_formula() -> None:
    # The same example is pinned in the gate's tests: one 100-byte member at
    # dataset/dataset_1.csv (21 characters) gives 100 + 22 + 76 + 42 = 240. Contract 4 adds the
    # record as one more entry: a 50-byte record at record/columns.json (19 characters) adds
    # 50 + 76 + 38.
    assert _size_bound([("dataset/dataset_1.csv", "text/csv", b"x" * 100)], record=b"y" * 50) == 240 + 50 + 76 + 38


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda m: m.update(names=["visits.csv"]), id="manifest-extra"),
        pytest.param(lambda m: m["archive"].update(name="study.zip"), id="archive-extra"),
        pytest.param(lambda m: m["members"][0].update(original="visits.csv"), id="member-extra"),
        pytest.param(lambda m: m["members"][0].pop("mime"), id="member-missing-mime"),
    ],
)
def test_plug_manifest_with_unexpected_fields_is_refused_before_fetch(change) -> None:
    manifest = _manifest()
    change(manifest)
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


@pytest.mark.parametrize("mime", ["text/plain", "application/octet-stream", "text/csv\n", ""])
def test_plug_media_type_must_be_the_one_written_for_the_extension(mime) -> None:
    manifest = _manifest()
    manifest["members"][0]["mime"] = mime
    plug = FakePlug(manifest=manifest)
    assert _refused(plug) == "PLUG_REJECTED: the upload plug returned an invalid media type"
    assert not plug.archive_requested


@pytest.mark.parametrize("mime", ["application/octet-stream", "application/x-pdf", "text/plain"])
def test_plug_pdf_member_needs_the_pdf_media_type(mime) -> None:
    manifest = _manifest()
    manifest["members"][2]["mime"] = mime
    plug = FakePlug(manifest=manifest)
    assert _refused(plug) == "PLUG_REJECTED: the upload plug returned an invalid media type"
    assert not plug.archive_requested


def test_plug_asks_for_no_compressed_transfer_and_refuses_one() -> None:
    plug = FakePlug()
    runtime = FakeRuntime()
    assert _pull(_plug_client(runtime, plug)).status_code == 200
    assert all(r.headers.get("accept-encoding") == "identity" for r in plug.requests)

    class GzipPlug(FakePlug):
        def __call__(self, request):
            response = super().__call__(request)
            if request.url.path.endswith("/archive"):
                import gzip

                return httpx.Response(
                    200, content=gzip.compress(self.archive), headers={"Content-Encoding": "gzip"}
                )
            return response

    assert _refused(GzipPlug()) == "PLUG_REJECTED: the upload plug sent an encoded response"


# Contract 4: the per-column record is validated, kept beside the files and never staged.


def test_plug_record_is_validated_and_never_staged() -> None:
    runtime = FakeRuntime()
    response = _pull(_plug_client(runtime, FakePlug()))
    assert response.status_code == 200
    staged = [name for name, _mime, _content in runtime.staged[0][1]]
    assert staged == STAGED_NAMES
    assert all("columns.json" not in a["filename"] for a in response.json()["attachments"])
    assert "deidentification" not in json.dumps(response.json())  # the browser never gets it


def test_fetch_delivery_returns_files_and_the_validated_record() -> None:
    from utils.upload_plug import PlugDelivery, fetch_delivery

    settings = UploadPlugSettings(url=PLUG_URL, secret=SECRET, transport=httpx.MockTransport(FakePlug()))
    delivery = fetch_delivery(
        settings, HANDLE, THREAD_ID, max_files=10, max_file_bytes=10**6, max_total_bytes=10**7
    )
    assert isinstance(delivery, PlugDelivery)
    assert [name for name, _mime, _content in delivery.files] == STAGED_NAMES
    assert delivery.record == _record_body()
    assert list(delivery.summaries) == [DATASET_NAME]
    assert "Riverbend" not in repr(delivery)  # nothing from the delivery in its repr


@pytest.mark.parametrize("contract", ["2", "3"])
def test_plug_older_contract_manifest_is_a_contract_mismatch(contract: str) -> None:
    manifest = _manifest()
    manifest["contract"] = contract
    manifest["report"] = manifest.pop("record")
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("CONTRACT_MISMATCH")
    assert not plug.archive_requested


def test_plug_contract_4_manifest_with_a_report_entry_is_refused() -> None:
    manifest = _manifest()
    manifest["report"] = manifest.pop("record")
    plug = FakePlug(manifest=manifest)
    assert _refused(plug) == "PLUG_REJECTED: the upload plug returned an invalid manifest"
    assert not plug.archive_requested


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda m: m.pop("record"), id="missing"),
        pytest.param(lambda m: m["record"].update(path="report/report.json"), id="old-path"),
        pytest.param(lambda m: m["record"].update(path="metadata/columns.json"), id="path"),
        pytest.param(lambda m: m["record"].update(mime="text/plain"), id="mime"),
        pytest.param(lambda m: m["record"].update(size=0), id="empty"),
        pytest.param(lambda m: m["record"].update(size=-1), id="negative"),
        pytest.param(lambda m: m["record"].update(size=True), id="bool-size"),
        pytest.param(lambda m: m["record"].update(size=2 * 1024 * 1024 + 1), id="too-large"),
        pytest.param(lambda m: m["record"].update(sha256="A" * 64), id="digest"),
        pytest.param(lambda m: m["record"].update(name="x"), id="extra-field"),
        pytest.param(lambda m: m.update(record=[]), id="not-an-object"),
    ],
)
def test_plug_bad_record_entry_is_refused_before_fetch(change) -> None:
    manifest = _manifest()
    change(manifest)
    plug = FakePlug(manifest=manifest)
    assert _refused(plug).startswith("PLUG_REJECTED")
    assert not plug.archive_requested


def test_plug_record_up_to_2_mib_is_not_refused_before_fetch() -> None:
    # The cap is 2 MiB (contract 3 allowed 64 KiB): such an entry passes the manifest checks.
    manifest = _manifest()
    manifest["record"]["size"] = 2 * 1024 * 1024
    manifest["archive"]["size"] = 2 * 1024 * 1024  # under the bound, so the archive is fetched
    plug = FakePlug(manifest=manifest)
    _refused(plug)
    assert plug.archive_requested


INVALID_RECORD = "PLUG_REJECTED: the plug's record is invalid"


def _record_refusal(record: bytes, archive: bytes | None = None, members=MEMBERS) -> str:
    manifest = _manifest(members, record=record, archive=archive)
    archive = _member_zip(members, record=record) if archive is None else archive
    plug = FakePlug(members, manifest=manifest, archive=archive)
    detail = _refused(plug)
    assert plug.archive_requested
    return detail


def _changed(change) -> bytes:
    body = _record_body()
    change(body)
    return _encode(body)


LONG = "x" * 1025

RECORD_CHANGES = {
    # top level
    **{f"missing-{key}": (lambda key: lambda b: b.pop(key))(key) for key in _record_body()},
    "extra-key": lambda b: b.update(tallies=[]),
    "contract-3": lambda b: b.update(contract="3"),
    "contract-int": lambda b: b.update(contract=4),
    "method-unknown": lambda b: b.update(method="limited_data_set"),
    "method-case": lambda b: b.update(method="Safe_Harbor"),
    "model-long": lambda b: b.update(model=LONG),
    "model-not-text": lambda b: b.update(model=None),
    "prompt-version-int": lambda b: b.update(prompt_version=1),
    "gate-long": lambda b: b.update(gate=LONG),
    "statement-long": lambda b: b.update(statement=LONG),
    "not-sent-text": lambda b: b.update(not_sent="README.txt"),
    "not-sent-item": lambda b: b.update(not_sent=[1]),
    "not-sent-long": lambda b: b.update(not_sent=[LONG]),
    "unchanged-item": lambda b: b.update(unchanged_files=[None]),
    "unchanged-long": lambda b: b.update(unchanged_files=[LONG]),
    "columns-object": lambda b: b.update(columns={}),
    "risk-object": lambda b: b.update(risk={}),
    # columns
    **{
        f"column-missing-{key}": (lambda key: lambda b: b["columns"][0].pop(key))(key)
        for key in _column("f", "h")
    },
    "column-extra": lambda b: b["columns"][0].update(value="S001"),
    "column-not-object": lambda b: b["columns"].append("id"),
    "column-file-int": lambda b: b["columns"][0].update(file=1),
    "column-file-long": lambda b: b["columns"][0].update(file=LONG),
    "column-sheet-negative": lambda b: b["columns"][0].update(sheet=-1),
    "column-sheet-text": lambda b: b["columns"][0].update(sheet="0"),
    "column-sheet-bool": lambda b: b["columns"][0].update(sheet=False),
    "column-header-long": lambda b: b["columns"][0].update(header=LONG),
    "column-header-null": lambda b: b["columns"][0].update(header=None),
    "column-action-unsure": lambda b: b["columns"][0].update(action="unsure"),
    "column-action-upper": lambda b: b["columns"][0].update(action="NAME"),
    "column-action-int": lambda b: b["columns"][0].update(action=1),
    "column-written-long": lambda b: b["columns"][0].update(written=LONG),
    "column-source-unknown": lambda b: b["columns"][0].update(source="gate"),
    "column-reason-words": lambda b: b["columns"][0].update(reason="the model said so"),
    "column-reason-int": lambda b: b["columns"][0].update(reason=3),
    "column-reason-empty": lambda b: b["columns"][0].update(reason=""),
    "column-cells-negative": lambda b: b["columns"][0].update(cells_changed=-1),
    "column-cells-float": lambda b: b["columns"][0].update(cells_changed=1.5),
    "column-cells-bool": lambda b: b["columns"][0].update(cells_changed=True),
    "column-note-long": lambda b: b["columns"][0].update(note=LONG),
    # risk
    **{
        f"risk-missing-{key}": (lambda key: lambda b: b["risk"][0].pop(key))(key)
        for key in _risk("f")
    },
    "risk-extra": lambda b: b["risk"][0].update(values=["S001"]),
    "risk-not-object": lambda b: b["risk"].append([]),
    "risk-sheet-negative": lambda b: b["risk"][0].update(sheet=-1),
    "risk-columns-too-many": lambda b: b["risk"][0].update(columns=["c"] * 65),
    "risk-columns-long": lambda b: b["risk"][0].update(columns=[LONG]),
    "risk-columns-item-int": lambda b: b["risk"][0].update(columns=[1]),
    "risk-columns-text": lambda b: b["risk"][0].update(columns="visit"),
    "risk-rows-negative": lambda b: b["risk"][0].update(rows=-1),
    "risk-classes-text": lambda b: b["risk"][0].update(classes="3"),
    "risk-unique-bool": lambda b: b["risk"][0].update(unique_rows=True),
    "risk-small-float": lambda b: b["risk"][0].update(small_rows=1.0),
    "risk-with-dates-int": lambda b: b["risk"][0].update(with_dates=1),
    "risk-with-dates-text": lambda b: b["risk"][0].update(with_dates="true"),
    # the record must describe the delivered datasets, and only them
    "column-for-another-file": lambda b: b["columns"][0].update(file="data/other.csv"),
    "column-for-a-form": lambda b: b["columns"][0].update(file="forms/baseline_crf_blank.pdf"),
    "risk-for-another-file": lambda b: b["risk"][0].update(file="data/other.csv"),
    "dataset-without-columns": lambda b: b.update(columns=[]),
}


@pytest.mark.parametrize("change", list(RECORD_CHANGES.values()), ids=list(RECORD_CHANGES))
def test_plug_invalid_record_is_refused(change) -> None:
    assert _record_refusal(_changed(change)) == INVALID_RECORD


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"[1, 2]", id="not-object"),
        pytest.param(_record().replace(b'"gate":"0.44.0"', b'"gate":"0.44.0\xc3\xa9"'), id="not-ascii"),
        pytest.param(_record().replace(b'"gate":"0.44.0"', b'"gate":"0.44.0","gate":"x"'), id="duplicate-key"),
        pytest.param(_record().replace(b'"rows":200', b'"rows":NaN'), id="nan"),
        pytest.param(_record().replace(b'"rows":200', b'"rows":2e2'), id="float"),
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="deep"),
    ],
)
def test_plug_unreadable_record_is_refused(record: bytes) -> None:
    assert _record_refusal(record) == INVALID_RECORD


def test_plug_record_with_escaped_text_is_accepted() -> None:
    # ASCII-escaped JSON: a header with a non-ASCII letter arrives as \\u escapes.
    record = _changed(lambda b: b["columns"][1].update(header="ann\u00e9e"))
    assert b"\\u00e9" in record
    runtime = FakeRuntime()
    plug = FakePlug(manifest=_manifest(record=record), archive=_member_zip(record=record))
    assert _pull(_plug_client(runtime, plug)).status_code == 200


def test_plug_record_strings_up_to_1024_characters_are_accepted() -> None:
    record = _changed(lambda b: [b.update(statement="s" * 1024), b["columns"][0].update(header="h" * 1024)])
    runtime = FakeRuntime()
    plug = FakePlug(manifest=_manifest(record=record), archive=_member_zip(record=record))
    assert _pull(_plug_client(runtime, plug)).status_code == 200


def test_plug_record_hash_differing_from_the_manifest_is_refused() -> None:
    record = _record()
    tampered = record.replace(b'"rows":200', b'"rows":201')
    assert len(tampered) == len(record)
    detail = _record_refusal(record, archive=_member_zip(record=tampered))
    assert detail == "PLUG_REJECTED: the plug's record did not match its checksum"


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param([(p, c) for p, _m, c in MEMBERS], id="record-missing"),
        pytest.param([(RECORD_PATH, _record())] + [(p, c) for p, _m, c in MEMBERS], id="record-first"),
        pytest.param(
            [(p, c) for p, _m, c in MEMBERS[:1]] + [(RECORD_PATH, _record())] + [(p, c) for p, _m, c in MEMBERS[1:]],
            id="record-in-the-middle",
        ),
        pytest.param([(p, c) for p, _m, c in MEMBERS] + [(RECORD_PATH, _record())] * 2, id="record-twice"),
        pytest.param(
            [(p, c) for p, _m, c in MEMBERS] + [("report/report.json", _record())], id="old-report-path"
        ),
    ],
)
def test_plug_archive_without_the_record_last_is_refused(entries) -> None:
    detail = _archive_refusal(_zip(entries))
    assert detail == "PLUG_REJECTED: the prepared archive does not match its manifest"


@pytest.mark.parametrize(
    "uploaded, delivered",
    [
        # the record names a file by its path in the uploaded ZIP; the delivery by its role
        # folder and the name with the extension it is written with
        ("data/followup/Follow up visits.csv", "dataset/Follow up visits.csv"),
        ("Dataset/VISITS.CSV", "dataset/visits.csv"),
        ("data/Visits (v2).xls", "dataset/Visits (v2).xlsx"),
        ("data/Visits.XLSX", "dataset/Visits.xlsx"),
        ("dataset/visits.tsv", "dataset/visits.tsv"),
    ],
)
def test_plug_record_columns_join_their_delivered_file(uploaded: str, delivered: str) -> None:
    mime = {
        "csv": "text/csv",
        "tsv": "text/tab-separated-values",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }[delivered.rsplit(".", 1)[1]]
    members = [(delivered, mime, DATASET)]
    body = _record_body(members)
    for row in body["columns"] + body["risk"]:
        row["file"] = uploaded
    record = _encode(body)
    runtime = FakeRuntime()
    plug = FakePlug(members, manifest=_manifest(members, record=record), archive=_member_zip(members, record=record))
    response = _pull(_plug_client(runtime, plug))
    assert response.status_code == 200, response.text
    summary = runtime.deidentification[0][delivered.split("/", 1)[1]]
    assert summary["kept_columns"] == 2
    assert [c["header"] for c in summary["changed_columns"]] == ["id"]
    assert len(summary["risk"]) == 1


def test_plug_record_with_two_datasets_keeps_each_its_own_columns() -> None:
    members = [
        ("dataset/visits.csv", "text/csv", DATASET),
        ("dataset/labs.csv", "text/csv", DATASET),
        MEMBERS[1],
    ]
    body = _record_body(members, method="expert_determination")
    body["columns"].append(
        _column("dataset/labs.csv", "draw_date", "date", "dates shifted by a secret number of days per participant; intervals kept")
    )
    record = _encode(body)
    runtime = FakeRuntime()
    plug = FakePlug(members, manifest=_manifest(members, record=record), archive=_member_zip(members, record=record))
    assert _pull(_plug_client(runtime, plug)).status_code == 200
    summaries = runtime.deidentification[0]
    assert sorted(summaries) == ["labs.csv", "visits.csv"]
    assert [c["header"] for c in summaries["visits.csv"]["changed_columns"]] == ["id"]
    assert [c["header"] for c in summaries["labs.csv"]["changed_columns"]] == ["id", "draw_date"]
    assert {s["method"] for s in summaries.values()} == {"expert_determination"}
