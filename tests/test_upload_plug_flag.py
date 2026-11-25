"""De-identified flag chain for files pulled through the upload plug.

The flag is set only server side by the plug route, carried through every hop
that copies or projects an attachment, and never set on a plain upload or a
derived file or dataset. Contract 4 adds, on each plug dataset file, a summary
of the plug's per-column record, carried the same way and shown to the model
as fixed words and headers only. Synthetic data only; no model provider,
database or network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ValidationError

import api.schemas as schemas
import api.server as server
from api.runtime import ReportAgentApiRuntime, _conversation, _message_attachments
from utils.attachment_artifacts import LocalAttachmentStore
from utils.attachment_readers import AttachmentReaderService, _manifest_card
from utils.display_history import build_display_history

THREAD_ID = "thread-1"
CSV = b"id,year\nS001,2020\nS002,2021\n"
TEXT = b"Synthetic form text: the visit was on [DATE].\n"


def _on_disk(store: LocalAttachmentStore, scope, attachment_id: str) -> dict:
    return json.loads(store._manifest_path(scope, attachment_id).read_text(encoding="utf-8"))


def _runtime(tmp_path) -> ReportAgentApiRuntime:
    runtime = ReportAgentApiRuntime(
        graph_factory=lambda *args, **kwargs: None,
        default_runtime_settings={},
        models=[],
        runtime_root=tmp_path,
    )
    runtime._thread = lambda *args, **kwargs: None  # no thread store in these tests
    return runtime


# --- Store: written to manifest.json, keyword only --------------------------------


def test_store_writes_the_flag_to_the_manifest(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    flagged = store.stage(THREAD_ID, "dataset_1.csv", "text/csv", CSV, deidentified=True)
    plain = store.stage(THREAD_ID, "study.csv", "text/csv", CSV)
    assert flagged["deidentified"] is True
    assert "deidentified" not in plain  # plain uploads keep their manifest shape
    assert _on_disk(store, THREAD_ID, flagged["id"])["deidentified"] is True
    assert "deidentified" not in _on_disk(store, THREAD_ID, plain["id"])  # only plug files carry the key


def test_store_flag_is_keyword_only(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    with pytest.raises(TypeError):
        store.stage(THREAD_ID, "dataset_1.csv", "text/csv", CSV, True)


def test_staged_to_available_promotion_keeps_the_flag(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    bound = store.stage(THREAD_ID, "dataset_1.csv", "text/csv", CSV, deidentified=True)
    assert store.begin_binding(THREAD_ID, bound["id"])["deidentified"] is True
    assert store.record_inspection(THREAD_ID, bound["id"], {"id": bound["id"]})["deidentified"] is True
    assert store.commit_binding(THREAD_ID, bound["id"])["deidentified"] is True
    assert store.rollback_available(THREAD_ID, bound["id"])["deidentified"] is True
    marked = store.stage(THREAD_ID, "form_1.txt", "text/plain", TEXT, deidentified=True)
    assert store.mark_available(THREAD_ID, marked["id"])["deidentified"] is True
    assert _on_disk(store, THREAD_ID, marked["id"])["deidentified"] is True


# --- Runtime staging -----------------------------------------------------------


def test_runtime_stage_attachments_passes_the_flag(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    flagged = runtime.stage_attachments(
        THREAD_ID, [("dataset_1.csv", "text/csv", CSV)], deidentified=True
    )
    plain = runtime.stage_attachments(THREAD_ID, [("study.csv", "text/csv", CSV)])
    assert [a.deidentified for a in flagged.attachments] == [True]
    assert [a.deidentified for a in plain.attachments] == [False]
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    assert runtime.attachment_store.require(scope, flagged.attachments[0].id)["deidentified"] is True
    assert "deidentified" not in runtime.attachment_store.require(scope, plain.attachments[0].id)  # only plug files carry the key


def _bound_state(tmp_path) -> tuple[dict, str, str]:
    """Bind one flagged and one plain upload to a message, as sending it does."""
    runtime = _runtime(tmp_path)
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    store = runtime.attachment_store
    flagged = store.stage(scope, "dataset_1.csv", "text/csv", CSV, deidentified=True)
    plain = store.stage(scope, "study.csv", "text/csv", CSV)
    payload = runtime._bind_message_payload(
        thread_id=THREAD_ID,
        attachment_thread_id=scope,
        snapshot=SimpleNamespace(values={}),
        message=HumanMessage(content="Describe these files", id="message-1"),
        manifests=[flagged, plain],
    )
    assert store.require(scope, flagged["id"])["deidentified"] is True
    assert "deidentified" not in store.require(scope, plain["id"])  # only plug files carry the key
    return payload, flagged["id"], plain["id"]


def test_message_binding_keeps_the_flag_in_thread_state(tmp_path) -> None:
    payload, flagged_id, plain_id = _bound_state(tmp_path)
    attachments = payload["artifacts"]["attachments"]
    assert attachments[flagged_id]["status"] == "available"
    assert attachments[flagged_id]["deidentified"] is True
    assert "deidentified" not in attachments[plain_id]  # only plug files carry the key


def test_message_history_and_api_carry_the_flag(tmp_path) -> None:
    payload, flagged_id, plain_id = _bound_state(tmp_path)
    user_message = build_display_history(payload)[0]
    projected = {a["id"]: a["deidentified"] for a in user_message.additional_kwargs["attachments"]}
    assert projected == {flagged_id: True, plain_id: False}
    conversation = _conversation(payload)
    assert {a.id: a.deidentified for a in conversation[0].attachments} == projected


# --- Display history and API messages -------------------------------------------


def _history_state() -> dict:
    upload = {
        "id": "attachment-" + "a" * 32,
        "filename": "dataset_1.csv",
        "kind": "tabular",
        "mime": "text/csv",
        "byte_size": len(CSV),
        "status": "available",
        "deidentified": True,
    }
    events = [
        {"type": "user", "event_id": "user-1", "text": "Describe the dataset"},
        {
            "type": "attachment",
            "event_id": "event-1",
            "artifact_id": upload["id"],
            "relationship": "input",
            "parent_event_id": "user-1",
        },
        {"type": "assistant", "event_id": "assistant-1", "text": "Done", "user_turn_hash": "turn-1"},
        {
            "type": "attachment",
            "event_id": "event-2",
            "artifact_id": upload["id"],
            "relationship": "used",
            "parent_event_id": "assistant-1",
        },
        # Derived outputs: a planted flag on their records must never show.
        {
            "type": "attachment",
            "event_id": "event-3",
            "artifact_id": "uploaded-000000000001",
            "relationship": "output",
            "parent_event_id": "assistant-1",
        },
        {
            "type": "attachment",
            "event_id": "event-4",
            "artifact_id": "file-1",
            "relationship": "output",
            "parent_event_id": "assistant-1",
        },
        {
            "type": "attachment",
            "event_id": "event-5",
            "artifact_id": "file-1",
            "relationship": "used",
            "parent_event_id": "assistant-1",
        },
        {
            "type": "attachment",
            "event_id": "event-6",
            "artifact_id": "uploaded-000000000002",
            "relationship": "used",
            "parent_event_id": "assistant-1",
        },
    ]
    return {
        "artifacts": {
            "conversation_events": events,
            "attachments": {upload["id"]: upload},
            "datasets": {
                "uploaded-000000000001": {
                    "kind": "uploaded",
                    "status": "active",
                    "summary": "Derived table",
                    "deidentified": True,
                },
                "uploaded-000000000002": {
                    "kind": "uploaded",
                    "status": "available",
                    "summary": "Derived table",
                    "deidentified": True,
                },
            },
            "files": {
                "file-1": {
                    "kind": "text",
                    "mime": "text/plain",
                    "summary": "Derived note",
                    "status": "available",
                    "deidentified": True,
                }
            },
        }
    }


def test_display_history_flags_uploads_but_never_derived_artifacts() -> None:
    user_message, assistant_message = build_display_history(_history_state())
    assert [a["deidentified"] for a in user_message.additional_kwargs["attachments"]] == [True]
    assert {
        a["relationship"] + ":" + a["id"]: a["deidentified"]
        for a in assistant_message.additional_kwargs["attachments"]
    } == {
        "used:attachment-" + "a" * 32: True,
        "output:uploaded-000000000001": False,
        "output:file-1": False,
        "used:file-1": False,
        "used:uploaded-000000000002": False,
    }


def test_message_attachments_take_only_a_true_flag() -> None:
    message = HumanMessage(
        content="",
        additional_kwargs={
            "attachments": [
                {"id": "a", "relationship": "input", "deidentified": True},
                {"id": "b", "relationship": "input", "deidentified": "true"},
                {"id": "c", "relationship": "input", "deidentified": 1},
                {"id": "d", "relationship": "input"},
            ]
        },
    )
    assert [a.deidentified for a in _message_attachments(message)] == [True, False, False, False]


# --- Artifact state cards -------------------------------------------------------


def test_manifest_card_carries_only_a_true_flag() -> None:
    assert _manifest_card({"deidentified": True})["deidentified"] is True
    assert "deidentified" not in _manifest_card({"deidentified": "true"})  # only plug files carry the key
    assert "deidentified" not in _manifest_card({})  # only plug files carry the key


def test_inspect_cards_carry_the_flag(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    table = store.stage(THREAD_ID, "dataset_1.csv", "text/csv", CSV, deidentified=True)
    form = store.stage(THREAD_ID, "form_1.txt", "text/plain", TEXT, deidentified=True)
    plain = store.stage(THREAD_ID, "notes.txt", "text/plain", TEXT)
    for manifest in (table, form, plain):
        store.mark_available(THREAD_ID, manifest["id"])
    cards = AttachmentReaderService(store, tmp_path).inspect(
        THREAD_ID, [table["id"], form["id"], plain["id"]]
    )
    assert [card.get("deidentified") for card in cards] == [True, True, None]
    assert cards[0]["columns"] == ["id", "year"]


def test_record_cards_for_derived_artifacts_are_never_flagged(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    reader = AttachmentReaderService(
        store,
        tmp_path,
        conversation_artifacts={
            "datasets": {"uploaded-000000000001": {"status": "active", "deidentified": True}},
            "files": {"file-1": {"kind": "text", "mime": "text/plain", "deidentified": True}},
        },
    )
    assert "deidentified" not in reader._available_manifest(THREAD_ID, "uploaded-000000000001")  # only plug files carry the key
    assert "deidentified" not in reader._available_manifest(THREAD_ID, "file-1")  # only plug files carry the key


# --- Schemas: response models default to unflagged; request models refuse it ------


def test_response_models_default_to_unflagged() -> None:
    summary = schemas.AttachmentManifestSummary(
        id="attachment-" + "0" * 32,
        filename="study.csv",
        kind="tabular",
        mime="text/csv",
        byte_size=1,
        status="staged",
    )
    assert summary.deidentified is False
    assert schemas.ConversationAttachment(id="a", relationship="input").deidentified is False


def _request_models() -> list[type[BaseModel]]:
    found = {}
    for module in (schemas, server):
        for name, value in vars(module).items():
            if (
                isinstance(value, type)
                and issubclass(value, BaseModel)
                and name.endswith("Request")
            ):
                found[name] = value
    return [found[name] for name in sorted(found)]


CLIENT_FIELDS = ("deidentified", "deidentification", "record")


def test_no_request_model_accepts_the_flag_or_a_record() -> None:
    models = _request_models()
    assert {model.__name__ for model in models} >= {
        "PlugAttachmentRequest",
        "SubmitMessageRequest",
    }
    for model in models:
        assert model.model_config.get("extra") == "forbid", model.__name__
        for name in CLIENT_FIELDS:
            assert name not in model.model_fields, (model.__name__, name)
    record = {"contract": "4", "method": "safe_harbor", "columns": []}
    for name, value in (("deidentified", True), ("deidentification", record), ("record", record)):
        with pytest.raises(ValidationError):
            server.PlugAttachmentRequest.model_validate({"handle": "h" * 32, name: value})
        with pytest.raises(ValidationError):
            schemas.SubmitMessageRequest.model_validate({"text": "hi", name: value})


def test_response_models_carry_no_record_summary() -> None:
    # The browser has no use for the per-column summary: it stays server side.
    for model in (schemas.AttachmentManifestSummary, schemas.ConversationAttachment):
        assert "deidentification" not in model.model_fields
        assert "record" not in model.model_fields


# --- Model context -------------------------------------------------------------

# Pinned here on purpose: a change to the sentence must change this test too.
PINNED_SENTENCE = (
    "Attachments marked deidentified were de-identified by the upload checker "
    "before this app received them. The checker can miss an identifier, names "
    "especially: if one remains, do not repeat it, infer from it or try to "
    "re-identify anyone. Dataset cards list each changed column with what it now "
    "holds; that text governs. Columns listed as NAME, ADDRESS, PHONE, EMAIL, ID, "
    "FREE_TEXT or REDACTED hold that word in every filled cell: treat them as "
    "removed, not as data. A PARTICIPANT_ID column holds random study codes that "
    "link the same participant across the uploaded files. Under Safe Harbor, date "
    "columns hold the year only. Under Expert Determination, each participant's "
    "dates are shifted by the same hidden number of days, so intervals within a "
    "participant are exact, but calendar dates, seasons and comparisons of "
    "absolute dates between participants are not. A date cell reading DATE was "
    "removed. Ages of 90 and over read 90+. ZIP codes are 3 digits. The "
    "dictionary and forms are unchanged and may describe columns whose values "
    "were removed."
)


def _prompt_cards(prompt: str) -> list[dict]:
    artifact_context = prompt.split("\n\n", 1)[0]
    return json.loads(artifact_context.split(":\n", 1)[1])


def test_deidentified_sentence_is_the_pinned_constant() -> None:
    from epi_agent.agent import DEIDENTIFIED_ATTACHMENTS_NOTE

    assert DEIDENTIFIED_ATTACHMENTS_NOTE == PINNED_SENTENCE
    assert len(PINNED_SENTENCE.split()) <= 170
    assert PINNED_SENTENCE.isascii()


def test_deidentified_sentence_uses_the_card_action_words() -> None:
    from epi_agent.agent import _DEIDENTIFIED_ACTION_WORDS, _DEIDENTIFIED_METHOD_WORDS

    for word in ("NAME", "ADDRESS", "PHONE", "EMAIL", "ID", "FREE_TEXT", "REDACTED", "PARTICIPANT_ID"):
        assert word in _DEIDENTIFIED_ACTION_WORDS.values()
        assert word in PINNED_SENTENCE
    for words in _DEIDENTIFIED_METHOD_WORDS.values():
        assert words in PINNED_SENTENCE
    # every action the host accepts from the record, except keep, has a fixed word
    from utils.upload_plug import RECORD_ACTIONS, RECORD_METHODS

    assert set(_DEIDENTIFIED_ACTION_WORDS) == set(RECORD_ACTIONS) - {"keep"}
    assert set(_DEIDENTIFIED_METHOD_WORDS) == set(RECORD_METHODS)


def test_context_prompt_marks_flagged_cards_and_adds_the_sentence_once(tmp_path) -> None:
    from epi_agent.agent import build_epi_agent_context_prompt

    payload, flagged_id, plain_id = _bound_state(tmp_path)
    prompt = build_epi_agent_context_prompt(payload)
    cards = {card["id"]: card for card in _prompt_cards(prompt)}
    assert cards[flagged_id]["deidentified"] is True
    assert "deidentified" not in cards[plain_id]
    assert prompt.count(PINNED_SENTENCE) == 1
    assert prompt.split("\n\n")[1] == PINNED_SENTENCE


def test_context_prompt_without_flagged_files_is_unchanged(tmp_path) -> None:
    from epi_agent.agent import build_epi_agent_context_prompt

    payload, flagged_id, _plain_id = _bound_state(tmp_path)
    payload["artifacts"]["attachments"][flagged_id].pop("deidentified")
    prompt = build_epi_agent_context_prompt(payload)
    assert "deidentified" not in prompt
    assert PINNED_SENTENCE not in prompt
    assert len(_prompt_cards(prompt)) == 2


def test_context_prompt_never_flags_a_dataset_derived_from_a_flagged_file() -> None:
    from epi_agent.agent import build_epi_agent_context_prompt

    upload_id = "attachment-" + "a" * 32
    state = {
        "authorized_attachment_ids": [upload_id],
        "artifacts": {
            "attachments": {
                upload_id: {
                    "id": upload_id,
                    "kind": "tabular",
                    "status": "available",
                    "filename": "dataset_1.csv",
                    "deidentified": True,
                }
            },
            "datasets": {
                "uploaded-000000000001": {
                    "kind": "uploaded",
                    "status": "active",
                    "columns": ["id", "year"],
                    "provenance": {
                        "source": "message_attachment",
                        "source_attachment_ids": [upload_id],
                    },
                    "deidentified": True,
                }
            },
        },
    }
    cards = {card["id"]: card for card in _prompt_cards(build_epi_agent_context_prompt(state))}
    assert cards[upload_id]["deidentified"] is True
    assert "deidentified" not in cards["uploaded-000000000001"]


# --- End to end through the routes, with the real store ------------------------


def _route_runtime(tmp_path) -> ReportAgentApiRuntime:
    runtime = _runtime(tmp_path)
    runtime.authorize_thread = lambda *args, **kwargs: None
    return runtime


def _pdf(text: str) -> bytes:
    """A minimal one-page PDF showing ``text`` (ASCII), with a correct cross-reference table."""
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


FORM_TEXT = "Baseline visit form: visit date and systolic blood pressure."
DATASET_NAME = "Riverbend Heart (v2)_DATA.csv"
DATASET_PATH = "dataset/" + DATASET_NAME
# Cell canaries: odd strings that are no header, path or fixed word, so finding one in what the
# model sees would mean a cell value leaked.
CELL_CANARIES = ("CANARY7731", "CANARY8842")
PLUG_CSV = (
    b"record_id,first_name,visit_date,sbp\n"
    b"P-CANARY7731,NAME,2020,120\n"
    b"P-CANARY8842,NAME,2021,131\n"
)


def _record_column(header: str, action: str, written: str, **override) -> dict:
    return {
        "action": action,
        "cells_changed": 0 if action == "keep" else 2,
        "file": "data/Riverbend Heart (v2)_DATA.CSV",
        "header": header,
        "note": "",
        "reason": "model_choice",
        "sheet": 0,
        "source": "model",
        "written": written,
        **override,
    }


# Sentinels in every record field the model must never see.
SENTINELS = {
    "note": "SENTINEL_NOTE_5521",
    "reason": "sentinel_reason_5522",
    "statement": "SENTINEL_STATEMENT_5523",
    "model": "SENTINEL_MODEL_5524",
    "prompt_version": "SENTINEL_PROMPT_5525",
    "gate": "SENTINEL_GATE_5526",
    "not_sent": "SENTINEL_NOT_SENT_5527.txt",
    "unchanged": "SENTINEL_UNCHANGED_5528.json",
    "risk": "SENTINEL_RISK_HEADER_5529",
}


def _plug_record(**override) -> dict:
    return {
        "columns": [
            _record_column(
                "record_id",
                "participant_id",
                "a random study code per participant",
                note=SENTINELS["note"],
                reason=SENTINELS["reason"],
            ),
            _record_column("first_name", "name", "every filled cell is NAME"),
            _record_column("visit_date", "date", "year only"),
            _record_column("sbp", "keep", "unchanged"),
        ],
        "contract": "4",
        "gate": SENTINELS["gate"],
        "method": "safe_harbor",
        "model": SENTINELS["model"],
        "not_sent": [SENTINELS["not_sent"]],
        "prompt_version": SENTINELS["prompt_version"],
        "risk": [
            {
                "classes": 2,
                "columns": [SENTINELS["risk"]],
                "file": "data/Riverbend Heart (v2)_DATA.CSV",
                "rows": 2,
                "sheet": 0,
                "small_rows": 2,
                "unique_rows": 2,
                "with_dates": True,
            }
        ],
        "statement": SENTINELS["statement"],
        "unchanged_files": [SENTINELS["unchanged"]],
        **override,
    }


def _plug_settings(members: list[tuple[str, str, bytes]], record: dict | None = None):
    import hashlib
    import io
    import zipfile

    import httpx

    from utils.upload_plug import UploadPlugSettings

    record_bytes = json.dumps(
        _plug_record() if record is None else record, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for path, content in [(p, c) for p, _m, c in members] + [("record/columns.json", record_bytes)]:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            bundle.writestr(info, content)
    archive = buffer.getvalue()
    manifest = {
        "contract": "4",
        "record": {
            "path": "record/columns.json",
            "mime": "application/json",
            "size": len(record_bytes),
            "sha256": hashlib.sha256(record_bytes).hexdigest(),
        },
        "archive": {"size": len(archive), "sha256": hashlib.sha256(archive).hexdigest()},
        "members": [
            {
                "path": path,
                "mime": mime,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, mime, content in members
        ],
    }
    handle = "h" * 32

    def plug(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/deliveries/{handle}":
            return httpx.Response(200, content=json.dumps(manifest).encode())
        if request.url.path == f"/v1/deliveries/{handle}/archive":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    settings = UploadPlugSettings(
        url="http://127.0.0.1:8090", secret="s" * 40, transport=httpx.MockTransport(plug)
    )
    return settings, handle


PLUG_MEMBERS = [
    (DATASET_PATH, "text/csv", PLUG_CSV),
    ("forms/Baseline CRF (blank).pdf", "application/pdf", _pdf(FORM_TEXT)),
    ("dictionary/Data Dictionary.txt", "text/plain", TEXT),
]


def _pull_into_real_store(tmp_path, members=PLUG_MEMBERS, record: dict | None = None):
    from fastapi.testclient import TestClient

    from api.server import create_app

    runtime = _route_runtime(tmp_path)
    settings, handle = _plug_settings(members, record)
    client = TestClient(create_app(runtime, provider_api_key="unused", upload_plug=settings))
    response = client.post(f"/api/threads/{THREAD_ID}/plug-attachments", json={"handle": handle})
    return runtime, response


def test_plug_route_stages_flagged_files_in_the_real_store(tmp_path) -> None:
    runtime, response = _pull_into_real_store(tmp_path)
    assert response.status_code == 200, response.text
    attachments = response.json()["attachments"]
    assert [(a["filename"], a["kind"], a["deidentified"]) for a in attachments] == [
        (DATASET_NAME, "tabular", True),
        ("Baseline CRF (blank).pdf", "document", True),
        ("Data Dictionary.txt", "document", True),
    ]
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    for attachment in attachments:
        assert _on_disk(runtime.attachment_store, scope, attachment["id"])["deidentified"] is True
    # Canary: the archive and the record never reach the app's storage, only the staged files
    # and the per-file summary.
    for stored in tmp_path.rglob("*"):
        if stored.is_file():
            data = stored.read_bytes()
            assert not data.startswith(b"PK\x03\x04"), stored
            assert DATASET_PATH.encode() not in data, stored
            assert b"record/columns.json" not in data, stored
            for sentinel in ("statement", "model", "prompt_version", "gate", "not_sent", "unchanged", "note", "reason"):
                assert SENTINELS[sentinel].encode() not in data, (stored, sentinel)


def test_plug_route_keeps_the_record_summary_on_the_dataset_only(tmp_path) -> None:
    runtime, response = _pull_into_real_store(tmp_path)
    assert response.status_code == 200, response.text
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    manifests = [
        _on_disk(runtime.attachment_store, scope, a["id"]) for a in response.json()["attachments"]
    ]
    assert manifests[0]["deidentification"] == {
        "method": "safe_harbor",
        "kept_columns": 1,
        "changed_columns": [
            {"header": "record_id", "action": "participant_id", "written": "a random study code per participant"},
            {"header": "first_name", "action": "name", "written": "every filled cell is NAME"},
            {"header": "visit_date", "action": "date", "written": "year only"},
        ],
        # stored with the file, never shown to the model
        "risk": [
            {
                "classes": 2,
                "columns": [SENTINELS["risk"]],
                "rows": 2,
                "sheet": 0,
                "small_rows": 2,
                "unique_rows": 2,
                "with_dates": True,
            }
        ],
    }
    assert "deidentification" not in manifests[1]  # the PDF form
    assert "deidentification" not in manifests[2]  # the dictionary
    assert "deidentification" not in json.dumps(response.json())  # never sent to the browser


def test_plug_pdf_form_stages_and_reads_as_is(tmp_path) -> None:
    runtime, response = _pull_into_real_store(tmp_path)
    assert response.status_code == 200, response.text
    pdf = response.json()["attachments"][1]
    store = runtime.attachment_store
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    assert store.read_bytes(scope, pdf["id"]) == PLUG_MEMBERS[1][2]  # byte for byte as delivered
    manifest = store.mark_available(scope, pdf["id"])
    assert (manifest["format"], manifest["mime"]) == ("pdf", "application/pdf")
    document = AttachmentReaderService(store, tmp_path).read_document(scope, pdf["id"])
    assert document["page_count"] == 1
    assert "Baseline visit form" in document["text"]


def _bound_plug_state(tmp_path, record: dict | None = None) -> tuple[dict, list[str], ReportAgentApiRuntime]:
    """Pull through the plug route into the real store, then bind the files to a message."""
    runtime, response = _pull_into_real_store(tmp_path, record=record)
    assert response.status_code == 200, response.text
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    ids = [a["id"] for a in response.json()["attachments"]]
    payload = runtime._bind_message_payload(
        thread_id=THREAD_ID,
        attachment_thread_id=scope,
        snapshot=SimpleNamespace(values={}),
        message=HumanMessage(content="Describe these files", id="message-1"),
        manifests=[runtime.attachment_store.require(scope, attachment_id) for attachment_id in ids],
    )
    return payload, ids, runtime


def test_message_binding_keeps_the_record_summary_in_thread_state(tmp_path) -> None:
    payload, ids, _runtime = _bound_plug_state(tmp_path)
    attachments = payload["artifacts"]["attachments"]
    assert attachments[ids[0]]["status"] == "available"
    assert attachments[ids[0]]["deidentification"]["kept_columns"] == 1
    assert "deidentification" not in attachments[ids[1]]


def test_message_history_and_api_never_carry_the_record_summary(tmp_path) -> None:
    payload, _ids, _runtime = _bound_plug_state(tmp_path)
    user_message = build_display_history(payload)[0]
    projected = json.dumps(user_message.additional_kwargs["attachments"])
    assert "deidentification" not in projected and "participant_id" not in projected
    conversation = _conversation(payload)
    assert "deidentification" not in conversation[0].model_dump_json()


def test_context_card_lists_changed_columns_in_fixed_words(tmp_path) -> None:
    from epi_agent.agent import build_epi_agent_context_prompt

    payload, ids, _runtime = _bound_plug_state(tmp_path)
    prompt = build_epi_agent_context_prompt(payload)
    cards = {card["id"]: card for card in _prompt_cards(prompt)}
    assert cards[ids[0]]["deidentified"] is True
    assert cards[ids[0]]["deidentification"] == {
        "method": "Safe Harbor",
        "kept_columns": 1,
        "changed_columns": [
            {"header": "record_id", "action": "PARTICIPANT_ID", "written": "a random study code per participant"},
            {"header": "first_name", "action": "NAME", "written": "every filled cell is NAME"},
            {"header": "visit_date", "action": "DATE", "written": "year only"},
        ],
    }
    # the form and the dictionary are marked deidentified but list no columns
    assert cards[ids[1]]["deidentified"] is True and "deidentification" not in cards[ids[1]]
    assert cards[ids[2]]["deidentified"] is True and "deidentification" not in cards[ids[2]]
    assert prompt.count(PINNED_SENTENCE) == 1
    assert prompt.split("\n\n")[1] == PINNED_SENTENCE


def test_context_never_holds_cell_values_or_other_record_fields(tmp_path) -> None:
    from epi_agent.agent import build_epi_agent_context_prompt

    payload, ids, _runtime = _bound_plug_state(tmp_path)
    prompt = build_epi_agent_context_prompt(payload)
    for canary in CELL_CANARIES:
        assert canary not in prompt
    for name, sentinel in SENTINELS.items():
        assert sentinel not in prompt, name
    for word in ("risk", "unique_rows", "small_rows", "cells_changed", "source", "statement"):
        assert f'"{word}"' not in prompt, word
    assert "data/Riverbend" not in prompt  # no uploaded path from the record
    # The only text from the record on the card: headers and the checker's written words.
    card = {c["id"]: c for c in _prompt_cards(prompt)}[ids[0]]["deidentification"]
    assert set(card) == {"method", "kept_columns", "changed_columns"}
    for column in card["changed_columns"]:
        assert set(column) == {"header", "action", "written"}


def test_inspect_cards_never_carry_the_record_summary(tmp_path) -> None:
    _payload, ids, runtime = _bound_plug_state(tmp_path)
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    cards = AttachmentReaderService(runtime.attachment_store, tmp_path).inspect(scope, ids)
    text = json.dumps(cards)
    assert [card.get("deidentified") for card in cards] == [True, True, True]
    assert "deidentification" not in text
    for sentinel in SENTINELS.values():
        assert sentinel not in text


def _card_for(summary: object, *, deidentified: bool = True) -> dict:
    from epi_agent.agent import build_epi_agent_context_prompt

    upload_id = "attachment-" + "b" * 32
    attachment = {
        "id": upload_id,
        "kind": "tabular",
        "status": "available",
        "filename": DATASET_NAME,
        "deidentification": summary,
    }
    if deidentified:
        attachment["deidentified"] = True
    state = {
        "authorized_attachment_ids": [upload_id],
        "artifacts": {"attachments": {upload_id: attachment}},
    }
    return {c["id"]: c for c in _prompt_cards(build_epi_agent_context_prompt(state))}[upload_id]


def _summary(changed: int, kept: int = 3, method: str = "expert_determination") -> dict:
    return {
        "method": method,
        "kept_columns": kept,
        "changed_columns": [
            {"header": f"col_{index}", "action": "redacted", "written": "every filled cell is REDACTED"}
            for index in range(changed)
        ],
        "risk": [],
    }


def test_context_card_caps_a_wide_table() -> None:
    card = _card_for(_summary(250))["deidentification"]
    assert card["method"] == "Expert Determination"
    assert len(card["changed_columns"]) == 200
    assert card["changed_columns"][-1]["header"] == "col_199"
    assert card["changed_columns_not_listed"] == 50
    assert "changed_columns_not_listed" not in _card_for(_summary(200))["deidentification"]


def test_context_card_bounds_long_headers_like_other_columns() -> None:
    summary = _summary(1)
    summary["changed_columns"][0]["header"] = "h" * 1024
    column = _card_for(summary)["deidentification"]["changed_columns"][0]
    assert len(column["header"]) == 500 and column["header"].endswith("...")


@pytest.mark.parametrize(
    "summary",
    [
        None,
        "safe_harbor",
        [],
        {**_summary(1), "method": "limited_data_set"},
        {**_summary(1), "kept_columns": -1},
        {**_summary(1), "kept_columns": True},
        {**_summary(1), "changed_columns": "all"},
        {**_summary(0), "changed_columns": [{"header": "x", "action": "unsure", "written": "w"}]},
        {**_summary(0), "changed_columns": [{"header": 1, "action": "name", "written": "w"}]},
        {**_summary(0), "changed_columns": ["x"]},
        {**_summary(1), "method": []},
        {**_summary(1), "method": {}},
        {**_summary(0), "changed_columns": [{"header": "x", "action": [], "written": "w"}]},
        {**_summary(0), "changed_columns": [{"header": "x", "action": {}, "written": "w"}]},
    ],
)
def test_context_card_drops_a_malformed_summary(summary) -> None:
    card = _card_for(summary)
    assert card["deidentified"] is True
    assert "deidentification" not in card


def test_context_card_ignores_a_summary_without_the_flag() -> None:
    card = _card_for(_summary(2), deidentified=False)
    assert "deidentified" not in card and "deidentification" not in card


# --- Store and runtime: the summary rides with the flag only ----------------------


def test_store_writes_the_summary_only_beside_the_flag(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    summary = _summary(1)
    flagged = store.stage(THREAD_ID, DATASET_NAME, "text/csv", CSV, deidentified=True, deidentification=summary)
    unflagged = store.stage(THREAD_ID, "study.csv", "text/csv", CSV, deidentification=summary)
    assert flagged["deidentification"] == summary
    assert _on_disk(store, THREAD_ID, flagged["id"])["deidentification"] == summary
    assert "deidentification" not in unflagged
    assert "deidentification" not in _on_disk(store, THREAD_ID, unflagged["id"])
    with pytest.raises(TypeError):
        store.stage(THREAD_ID, DATASET_NAME, "text/csv", CSV, True, summary)


def test_promotion_keeps_the_summary(tmp_path) -> None:
    store = LocalAttachmentStore(tmp_path)
    summary = _summary(1)
    bound = store.stage(THREAD_ID, DATASET_NAME, "text/csv", CSV, deidentified=True, deidentification=summary)
    assert store.begin_binding(THREAD_ID, bound["id"])["deidentification"] == summary
    assert store.record_inspection(THREAD_ID, bound["id"], {"id": bound["id"]})["deidentification"] == summary
    assert store.commit_binding(THREAD_ID, bound["id"])["deidentification"] == summary


def test_runtime_stage_attachments_passes_each_file_its_summary(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    summary = _summary(1)
    flagged = runtime.stage_attachments(
        THREAD_ID,
        [(DATASET_NAME, "text/csv", CSV), ("form.txt", "text/plain", TEXT)],
        deidentified=True,
        deidentification={DATASET_NAME: summary},
    )
    plain = runtime.stage_attachments(
        THREAD_ID, [("study.csv", "text/csv", CSV)], deidentification={"study.csv": summary}
    )
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    store = runtime.attachment_store
    assert store.require(scope, flagged.attachments[0].id)["deidentification"] == summary
    assert "deidentification" not in store.require(scope, flagged.attachments[1].id)
    assert "deidentification" not in store.require(scope, plain.attachments[0].id)


def test_plain_route_stages_unflagged_files_in_the_real_store(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from api.server import create_app
    from utils.upload_plug import UploadPlugSettings

    runtime = _route_runtime(tmp_path)
    client = TestClient(
        create_app(runtime, provider_api_key="unused", upload_plug=UploadPlugSettings())
    )
    response = client.post(
        f"/api/threads/{THREAD_ID}/attachments",
        files=[("files", ("study.csv", CSV, "text/csv"))],
        data={"deidentified": "true"},
    )
    assert response.status_code == 200, response.text
    attachment = response.json()["attachments"][0]
    assert attachment["deidentified"] is False
    scope = runtime._attachment_scope(THREAD_ID, THREAD_ID)
    assert "deidentified" not in _on_disk(runtime.attachment_store, scope, attachment["id"])  # only plug files carry the key
