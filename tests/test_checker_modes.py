"""Upload checker modes: off, optional (per-user switch, starts on) and required.

The mode is server config; the browser only shows it. In optional mode a user's
switch applies to the next request at once, with no restart. Whenever the
checker is on for a user, the plain upload route is refused before any of the
body is read. Synthetic data only; no model provider or network.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from api.auth import AuthenticatedUser, RequestIdentity, local_request_identity
from api.server import create_app
from test_upload_plug import PLUG_URL, SECRET, THREAD_ID, FakePlug, FakeRuntime, _pull
from utils.upload_plug import (
    PLUG_MODE_ENV,
    PLUG_SECRET_ENV,
    PLUG_URL_ENV,
    UploadCheckerSwitch,
    UploadPlugSettings,
)

RAW = [("files", ("raw.csv", b"name,dob\nSynthetic Person,2001-02-03\n", "text/csv"))]


def _client(mode: str, runtime: FakeRuntime | None = None, *, url: str = PLUG_URL) -> TestClient:
    settings = UploadPlugSettings(
        url=url, secret=SECRET, mode=mode, transport=httpx.MockTransport(FakePlug())
    )
    return TestClient(
        create_app(runtime or FakeRuntime(), provider_api_key="unused", upload_plug=settings)
    )


def _plain(client: TestClient):
    return client.post(f"/api/threads/{THREAD_ID}/attachments", files=RAW)


def _set(client: TestClient, on: bool):
    return client.put("/api/upload-plug/checker", json={"checker_on": on})


def _raw_asgi_upload(app) -> tuple[int, bool]:
    """Sends a plain upload straight to the ASGI app; returns (status, body_was_read)."""
    scope = {
        "type": "http", "method": "POST", "path": f"/api/threads/{THREAD_ID}/attachments",
        "raw_path": f"/api/threads/{THREAD_ID}/attachments".encode(), "query_string": b"",
        "headers": [(b"content-type", b"multipart/form-data; boundary=x"), (b"content-length", b"1000")],
        "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1),
        "http_version": "1.1", "root_path": "",
    }
    sent: list[dict] = []
    read = {"body": False}

    async def receive():
        read["body"] = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent[0]["status"], read["body"]


# --- Settings ------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_mode, url, expected",
    [
        ("", "", "off"),
        ("", PLUG_URL, "required"),  # no mode set: the pre-modes behaviour
        ("off", PLUG_URL, "off"),
        ("optional", PLUG_URL, "optional"),
        ("required", PLUG_URL, "required"),
        (" Optional ", PLUG_URL, "optional"),
    ],
)
def test_mode_comes_from_server_config(monkeypatch, env_mode, url, expected) -> None:
    monkeypatch.setenv(PLUG_MODE_ENV, env_mode)
    monkeypatch.setenv(PLUG_URL_ENV, url)
    monkeypatch.setenv(PLUG_SECRET_ENV, SECRET)
    assert UploadPlugSettings.from_env().checker_mode == expected


@pytest.mark.parametrize("mode", ["on", "true", "1", "requried"])
def test_unknown_mode_refuses_to_start(mode: str) -> None:
    with pytest.raises(ValueError):
        create_app(
            FakeRuntime(),
            provider_api_key="unused",
            upload_plug=UploadPlugSettings(url=PLUG_URL, secret=SECRET, mode=mode),
        )


@pytest.mark.parametrize("mode", ["optional", "required"])
def test_checker_modes_without_url_or_secret_refuse_to_start(mode: str) -> None:
    with pytest.raises(ValueError):
        create_app(FakeRuntime(), provider_api_key="unused",
                   upload_plug=UploadPlugSettings(mode=mode, secret=SECRET))
    with pytest.raises(ValueError):
        create_app(FakeRuntime(), provider_api_key="unused",
                   upload_plug=UploadPlugSettings(url=PLUG_URL, mode=mode))


def test_switch_is_per_user_and_fixed_outside_optional() -> None:
    switch = UploadCheckerSwitch("optional")
    assert switch.is_on("a") and switch.is_on("b")
    switch.set_on("a", False)
    assert not switch.is_on("a") and switch.is_on("b")
    switch.set_on("a", True)
    assert switch.is_on("a")
    for mode, on in (("off", False), ("required", True)):
        fixed = UploadCheckerSwitch(mode)
        assert fixed.is_on("a") is on
        with pytest.raises(ValueError):
            fixed.set_on("a", not on)


# --- off -------------------------------------------------------------------------


def test_off_mode_works_as_before_even_with_a_url() -> None:
    runtime = FakeRuntime()
    client = _client("off", runtime)
    assert client.get("/api/upload-plug").json() is None
    response = _plain(client)
    assert response.status_code == 200
    assert [a["deidentified"] for a in response.json()["attachments"]] == [False]
    assert _pull(client).status_code == 404
    assert _set(client, True).status_code == 409


# --- required --------------------------------------------------------------------


def test_required_mode_is_always_on_with_no_switch() -> None:
    runtime = FakeRuntime()
    client = _client("required", runtime)
    info = client.get("/api/upload-plug").json()
    assert (info["mode"], info["checker_on"]) == ("required", True)
    response = _set(client, False)
    assert response.status_code == 409
    assert response.json()["detail"].startswith("CHECKER_MODE_FIXED")
    assert client.get("/api/upload-plug").json()["checker_on"] is True
    assert _plain(client).status_code == 409
    assert runtime.staged == []


def test_required_mode_refuses_a_raw_upload_before_the_body_is_read() -> None:
    runtime = FakeRuntime()
    assert _raw_asgi_upload(_client("required", runtime).app) == (409, False)
    assert runtime.staged == []


def test_required_mode_plug_route_flags_files() -> None:
    runtime = FakeRuntime()
    response = _pull(_client("required", runtime))
    assert response.status_code == 200
    assert runtime.deidentified == [True]


# --- optional --------------------------------------------------------------------


def test_optional_mode_starts_on_and_refuses_a_raw_upload() -> None:
    runtime = FakeRuntime()
    client = _client("optional", runtime)
    info = client.get("/api/upload-plug").json()
    assert (info["mode"], info["checker_on"]) == ("optional", True)
    response = _plain(client)
    assert response.status_code == 409
    assert response.json()["detail"].startswith("UPLOAD_PLUG_REQUIRED")
    assert _raw_asgi_upload(client.app) == (409, False)
    assert runtime.staged == []


def test_optional_mode_switch_applies_at_once_without_restart() -> None:
    runtime = FakeRuntime()
    client = _client("optional", runtime)
    response = _set(client, False)
    assert response.status_code == 200
    assert response.json()["checker_on"] is False
    plain = _plain(client)
    assert plain.status_code == 200
    # The badge stays server-set: a plain upload is never marked as checked.
    assert [a["deidentified"] for a in plain.json()["attachments"]] == [False]
    assert runtime.deidentified == [False]
    assert _set(client, True).json()["checker_on"] is True
    assert _plain(client).status_code == 409
    assert _raw_asgi_upload(client.app) == (409, False)
    assert len(runtime.staged) == 1


def test_optional_mode_plug_route_flags_files() -> None:
    runtime = FakeRuntime()
    response = _pull(_client("optional", runtime))
    assert response.status_code == 200
    assert [a["deidentified"] for a in response.json()["attachments"]] == [True, True]


def test_optional_switch_request_takes_only_a_boolean() -> None:
    client = _client("optional")
    assert client.put("/api/upload-plug/checker", json={"checker_on": False, "user": "x"}).status_code == 422
    assert client.put("/api/upload-plug/checker", json={}).status_code == 422
    assert client.get("/api/upload-plug").json()["checker_on"] is True


def test_optional_switch_of_one_user_does_not_reach_another() -> None:
    runtime = FakeRuntime()
    client = _client("optional", runtime)
    assert _set(client, False).status_code == 200  # the local user turns it off
    other = RequestIdentity(user=AuthenticatedUser(owner_user_id="other-user"), session_id="s")
    client.app.dependency_overrides[local_request_identity] = lambda: other
    assert client.get("/api/upload-plug").json()["checker_on"] is True
    assert _plain(client).status_code == 409
    assert _raw_asgi_upload(client.app) == (409, False)
    assert runtime.staged == []


def test_middleware_refuses_when_the_identity_cannot_be_resolved() -> None:
    runtime = FakeRuntime()
    client = _client("optional", runtime)
    _set(client, False)

    def broken():
        raise RuntimeError("no identity")

    client.app.dependency_overrides[local_request_identity] = broken
    assert _raw_asgi_upload(client.app) == (409, False)
    assert runtime.staged == []


# --- Switch races and files staged under the other setting (real store) ---------


def _real_client(tmp_path, mode: str = "optional"):
    from test_upload_plug_flag import CSV, _plug_settings, _route_runtime

    runtime = _route_runtime(tmp_path)
    settings, handle = _plug_settings([("dataset/dataset_1.csv", "text/csv", CSV)])
    settings = UploadPlugSettings(
        url=settings.url, secret=settings.secret, transport=settings.transport, mode=mode
    )
    client = TestClient(create_app(runtime, provider_api_key="unused", upload_plug=settings))
    return client, runtime, handle


def _send(client: TestClient, attachment_ids: list[str]):
    return client.post(
        f"/api/threads/{THREAD_ID}/messages",
        json={"text": "What is the mean age?", "attachment_ids": attachment_ids},
    )


def test_switch_turned_on_during_a_plain_upload_refuses_the_staging(tmp_path, monkeypatch) -> None:
    import api.server as server

    client, runtime, _handle = _real_client(tmp_path)
    assert _set(client, False).status_code == 200
    original = server._read_bounded_attachment_uploads

    async def read_then_switch_on(*args, **kwargs):
        uploads = await original(*args, **kwargs)
        client.app.state.upload_checker.set_on("local-user", True)
        return uploads

    monkeypatch.setattr(server, "_read_bounded_attachment_uploads", read_then_switch_on)
    response = _plain(client)
    assert response.status_code == 409
    assert not any(p.name == "manifest.json" for p in tmp_path.rglob("*"))


def test_files_staged_with_the_checker_off_cannot_be_sent_once_it_is_on(tmp_path) -> None:
    client, runtime, _handle = _real_client(tmp_path)
    assert _set(client, False).status_code == 200
    staged = _plain(client)
    assert staged.status_code == 200, staged.text
    raw_id = staged.json()["attachments"][0]["id"]
    assert _set(client, True).status_code == 200
    response = _send(client, [raw_id])
    assert response.status_code == 409
    assert response.json()["detail"].startswith("UPLOAD_CHECKER_ON")


def test_raw_files_staged_before_a_restart_in_required_mode_cannot_be_sent(tmp_path) -> None:
    off_client, runtime, _handle = _real_client(tmp_path, mode="off")
    raw_id = _plain(off_client).json()["attachments"][0]["id"]
    from test_upload_plug_flag import CSV, _plug_settings

    settings, _ = _plug_settings([("dataset/dataset_1.csv", "text/csv", CSV)])
    required = TestClient(create_app(runtime, provider_api_key="unused", upload_plug=UploadPlugSettings(
        url=settings.url, secret=settings.secret, transport=settings.transport, mode="required")))
    response = _send(required, [raw_id])
    assert response.status_code == 409
    assert response.json()["detail"].startswith("UPLOAD_CHECKER_ON")


def test_only_checker_files_pass_the_send_check(tmp_path) -> None:
    client, runtime, handle = _real_client(tmp_path)
    pulled = client.post(f"/api/threads/{THREAD_ID}/plug-attachments", json={"handle": handle})
    assert pulled.status_code == 200, pulled.text
    plug_id = pulled.json()["attachments"][0]["id"]
    identity = local_request_identity()
    assert runtime.attachments_passed_checker(identity, THREAD_ID, [plug_id]) is True
    _set(client, False)
    raw_id = _plain(client).json()["attachments"][0]["id"]
    assert runtime.attachments_passed_checker(identity, THREAD_ID, [plug_id, raw_id]) is False


# --- Files already sent in a conversation (later turns and review resumes) -----------


def _thread_with_input(runtime, attachment_id: str):
    """Marks the file as sent (available), as binding it to a message does."""
    from api.schemas import ApiThreadState, RunStatus

    scope = runtime._attachment_scope(local_request_identity(), THREAD_ID)
    runtime.attachment_store.mark_available(scope, attachment_id)
    # The route answers with the thread state; these tests run no graph.
    runtime.state = lambda *args, **kwargs: ApiThreadState(thread_id=THREAD_ID, run=RunStatus(state="idle"))


def test_text_only_turn_is_refused_when_an_earlier_file_was_unchecked(tmp_path) -> None:
    client, runtime, _handle = _real_client(tmp_path)
    _set(client, False)
    raw_id = _plain(client).json()["attachments"][0]["id"]
    _thread_with_input(runtime, raw_id)  # the raw file was sent in an earlier turn
    _set(client, True)
    sent: list[object] = []
    runtime.submit_message = lambda *args, **kwargs: sent.append(args)
    response = _send(client, [])
    assert response.status_code == 409
    assert response.json()["detail"].startswith("UPLOAD_CHECKER_ON")
    assert sent == []


def test_review_resume_is_refused_when_an_earlier_file_was_unchecked(tmp_path) -> None:
    client, runtime, _handle = _real_client(tmp_path)
    _set(client, False)
    raw_id = _plain(client).json()["attachments"][0]["id"]
    _thread_with_input(runtime, raw_id)
    _set(client, True)
    resumed: list[object] = []
    runtime.resume_interrupt = lambda *args, **kwargs: resumed.append(args)
    response = client.post(
        f"/api/threads/{THREAD_ID}/interrupts/i-1/resume", json={"action": "continue"}
    )
    assert response.status_code == 409
    assert response.json()["detail"].startswith("UPLOAD_CHECKER_ON")
    assert resumed == []


def test_conversation_with_only_checked_files_goes_on(tmp_path) -> None:
    client, runtime, handle = _real_client(tmp_path)
    pulled = client.post(f"/api/threads/{THREAD_ID}/plug-attachments", json={"handle": handle})
    plug_id = pulled.json()["attachments"][0]["id"]
    _thread_with_input(runtime, plug_id)
    sent: list[object] = []
    runtime.submit_message = lambda *args, **kwargs: sent.append(args)
    assert _send(client, []).status_code == 200
    assert len(sent) == 1


def test_unchecked_earlier_file_is_fine_while_the_checker_is_off(tmp_path) -> None:
    client, runtime, _handle = _real_client(tmp_path)
    _set(client, False)
    raw_id = _plain(client).json()["attachments"][0]["id"]
    _thread_with_input(runtime, raw_id)
    sent: list[object] = []
    runtime.submit_message = lambda *args, **kwargs: sent.append(args)
    assert _send(client, []).status_code == 200
    assert len(sent) == 1


def test_check_reads_the_store_not_the_conversation_view(tmp_path) -> None:
    """A sent raw file missing from the conversation view (for example after a cancelled turn)
    is still found, because the check reads every upload of the thread from the store."""
    from api.schemas import ApiThreadState, RunStatus

    client, runtime, _handle = _real_client(tmp_path)
    _set(client, False)
    raw_id = _plain(client).json()["attachments"][0]["id"]
    _thread_with_input(runtime, raw_id)
    runtime.state = lambda *args, **kwargs: ApiThreadState(thread_id=THREAD_ID, run=RunStatus(state="idle"))
    _set(client, True)
    runtime.submit_message = lambda *args, **kwargs: None
    assert _send(client, []).status_code == 409


def test_a_staged_but_unsent_raw_file_does_not_block_the_conversation(tmp_path) -> None:
    client, runtime, _handle = _real_client(tmp_path)
    _set(client, False)
    _plain(client)  # staged only, never sent
    from api.schemas import ApiThreadState, RunStatus

    runtime.state = lambda *args, **kwargs: ApiThreadState(thread_id=THREAD_ID, run=RunStatus(state="idle"))
    _set(client, True)
    sent: list[object] = []
    runtime.submit_message = lambda *args, **kwargs: sent.append(args)
    assert _send(client, []).status_code == 200
    assert len(sent) == 1


def test_reading_the_state_never_restarts_a_run_over_unchecked_files(tmp_path) -> None:
    client, runtime, _handle = _real_client(tmp_path)
    _set(client, False)
    raw_id = _plain(client).json()["attachments"][0]["id"]
    _thread_with_input(runtime, raw_id)
    keys: list[object] = []
    stub = runtime.state

    def recording_state(*args, provider_api_key=None, **kwargs):
        keys.append(provider_api_key)
        return stub(*args, **kwargs)

    runtime.state = recording_state
    assert client.get(f"/api/threads/{THREAD_ID}/state").status_code == 200
    _set(client, True)
    assert client.get(f"/api/threads/{THREAD_ID}/state").status_code == 200
    # Off: the key is passed (recovery allowed); on with an unchecked file: no key, read only.
    assert keys == ["unused", None]
