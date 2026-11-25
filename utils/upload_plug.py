"""Optional upload plug: a generic, opt-in hook on the paperclip upload.

When ``REPORT_AGENT_UPLOAD_PLUG_URL`` is empty (the default) nothing changes.
When it is set, the browser hands a chosen ZIP to the plug's page (shown in a
modal), the plug prepares the files, and the browser gets back only an opaque
handle. This module then pulls the prepared study ZIP from the plug, server to
server, with a shared secret, unzips it in memory and returns its files in the
same shape the plain upload route stages, with the plug's per-column record.

The app does not look inside the prepared files beyond checking the archive,
their size, hash and type; what the plug does to them is the plug's business.
The record (contract 4) is validated strictly, because a summary of it is shown
to the model. Nothing pulled from the plug is written to disk or logged here.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

CONTRACT_VERSION = "4"

PLUG_URL_ENV = "REPORT_AGENT_UPLOAD_PLUG_URL"
PLUG_SECRET_ENV = "REPORT_AGENT_UPLOAD_PLUG_SECRET"
PLUG_TIMEOUT_ENV = "REPORT_AGENT_UPLOAD_PLUG_TIMEOUT_SECONDS"
PLUG_REQUIRED_ENV = "REPORT_AGENT_UPLOAD_PLUG_REQUIRED"

_TRUE_VALUES = {"1", "true", "yes"}
_FALSE_VALUES = {"", "0", "false", "no"}

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Contract 4: a role folder, then the file name as uploaded (printable ASCII, no slash or
# backslash, no leading dot), with an extension the app accepts.
_MEMBER_PATH_RE = re.compile(
    r"^(dataset|dictionary|forms|metadata)/[A-Za-z0-9_(][ -.0-\[\]-~]{0,199}"
    r"\.(csv|tsv|xlsx|txt|md|json|xml|docx|pdf)$"
)
# The media type the checker writes for each member extension; anything else is refused.
_MEMBER_MIME = {
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
    "md": "text/markdown",
    "json": "application/json",
    "xml": "application/xml",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}
# Upper bound on the plug's ZIP (each entry stored or deflated no larger than its file;
# no extra fields, comments, data descriptors or ZIP64 records): the files, the end
# record, and per entry a local header (30) and a central entry (46) that each hold the path.
_ZIP_END_RECORD_BYTES = 22
_ZIP_ENTRY_BYTES = 30 + 46
_ZIP_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_ZIP_ENCRYPTED_FLAG = 0x1
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_MIN_SECRET_CHARS = 32
_MAX_MANIFEST_BYTES = 64 * 1024
# Contract 4: the plug's per-column record, the archive's last entry. It is validated here
# and never staged as a chat attachment; a per-file summary of it is kept with each dataset.
_RECORD_PATH = "record/columns.json"
_RECORD_MIME = "application/json"
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_MAX_RECORD_TEXT = 1024
_MAX_RISK_COLUMNS = 64
_RECORD_KEYS = {
    "contract",
    "method",
    "model",
    "prompt_version",
    "gate",
    "statement",
    "not_sent",
    "unchanged_files",
    "columns",
    "risk",
}
_COLUMN_KEYS = {
    "file",
    "sheet",
    "header",
    "action",
    "written",
    "source",
    "reason",
    "cells_changed",
    "note",
}
_RISK_KEYS = {
    "file",
    "sheet",
    "columns",
    "rows",
    "classes",
    "unique_rows",
    "small_rows",
    "with_dates",
}
RECORD_METHODS = ("safe_harbor", "expert_determination")
RECORD_ACTIONS = (
    "keep",
    "name",
    "address",
    "phone",
    "email",
    "identifier",
    "participant_id",
    "date",
    "birth_date",
    "age",
    "zip",
    "free_text",
    "redacted",
)
_RECORD_SOURCES = {"dictionary", "header", "values", "model", "researcher"}
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# The checker writes an uploaded .xls dataset as .xlsx and lowercases extensions.
_DELIVERED_EXTENSION = {"xls": "xlsx"}
_READ_CHUNK_BYTES = 1024 * 1024


class UploadPlugError(Exception):
    """A plug failure. ``code`` is a fixed enum; the message holds no file data."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UploadPlugSettings:
    url: str = ""
    secret: str = field(default="", repr=False)
    timeout_seconds: float = 30.0
    transport: httpx.BaseTransport | None = None
    # Opt-in: when set and no plug URL is configured, direct uploads are refused instead
    # of falling back to the plain file picker. Off by default.
    required: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def refuses_direct_uploads(self) -> bool:
        """With a plug, uploads go through it; with none but required, uploads are off."""
        return self.enabled or self.required

    @property
    def origin(self) -> str:
        """The origin a browser reports for the plug's page: lowercase host, default port dropped."""
        parts = urlsplit(self.url)
        host = (parts.hostname or "").lower()
        if ":" in host:
            host = f"[{host}]"
        default = {"http": 80, "https": 443}.get(parts.scheme)
        port = f":{parts.port}" if parts.port and parts.port != default else ""
        return f"{parts.scheme}://{host}{port}"

    @classmethod
    def from_env(cls) -> "UploadPlugSettings":
        settings = cls(
            url=(os.getenv(PLUG_URL_ENV) or "").strip().rstrip("/"),
            secret=(os.getenv(PLUG_SECRET_ENV) or "").strip(),
            timeout_seconds=float(os.getenv(PLUG_TIMEOUT_ENV) or "30"),
            required=parse_required(os.getenv(PLUG_REQUIRED_ENV)),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Refuse a half-configured plug at startup (fail closed)."""
        if not self.url:
            return
        parts = urlsplit(self.url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(f"{PLUG_URL_ENV} must be an http(s) URL")
        if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
            raise ValueError(f"{PLUG_URL_ENV} must use https unless it is on this computer")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError(f"{PLUG_URL_ENV} must not carry a query, fragment or credentials")
        if parts.path not in ("", "/"):
            raise ValueError(f"{PLUG_URL_ENV} must be the plug's origin only, with no path")
        try:
            parts.port
        except ValueError as exc:
            raise ValueError(f"{PLUG_URL_ENV} has an invalid port") from exc
        if len(self.secret) < _MIN_SECRET_CHARS:
            raise ValueError(
                f"{PLUG_SECRET_ENV} must be set (at least {_MIN_SECRET_CHARS} characters) "
                f"when {PLUG_URL_ENV} is set"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(f"{PLUG_TIMEOUT_ENV} must be positive")


def parse_required(value: str | None) -> bool:
    """``1``, ``true`` or ``yes`` turn the setting on; empty, ``0``, ``false`` or ``no`` leave it
    off. Anything else refuses to start, so a typo never leaves uploads unchecked."""
    text = (value or "").strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise ValueError(f"{PLUG_REQUIRED_ENV} must be 1, true, yes, 0, false, no or empty")


def quiet_http_logs() -> None:
    """httpx logs every request URL at INFO, and delivery URLs carry the handle and thread id.
    Called only when a plug is configured, so a plain app keeps its logging as before."""
    logging.getLogger("httpx").setLevel(logging.WARNING)


def validate_handle(handle: str) -> str:
    if not isinstance(handle, str) or not _HANDLE_RE.fullmatch(handle):
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid handle")
    return handle


def _safe_member_name(name: object) -> str:
    if not isinstance(name, str) or not name or len(name) > 255:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid file name")
    if (
        "/" in name
        or "\\" in name
        or name in {".", ".."}
        or name.startswith(".")
        or any(ord(ch) < 32 for ch in name)
    ):
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid file name")
    return name


def _member_basename(path: object) -> str:
    """Check a manifest path and return the file name the app stages it under: the name the
    researcher uploaded."""
    if not isinstance(path, str) or not _MEMBER_PATH_RE.fullmatch(path):
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid file path")
    return _safe_member_name(path.split("/", 1)[1])


def _unique_names(names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name.lower() in seen:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned duplicate file names")
        seen.add(name.lower())


@dataclass(frozen=True)
class PlugDelivery:
    """One verified delivery.

    ``files``: ``(filename, mime, content)`` in manifest order, the same shape the plain upload
    route passes to staging; ``filename`` is the uploaded file name.
    ``record``: the validated per-column record (never staged, never shown whole to the model).
    ``summaries``: per staged dataset file name, the compact summary kept with that file.
    """

    files: list[tuple[str, str, bytes]] = field(repr=False)
    record: dict[str, Any] = field(repr=False)
    summaries: dict[str, dict[str, Any]] = field(repr=False)


def fetch_delivery(
    settings: UploadPlugSettings,
    handle: str,
    thread_id: str,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> PlugDelivery:
    """Pull the prepared study ZIP for ``handle``, verify it and unzip it in memory.

    Raises ``UploadPlugError`` on any problem; nothing is returned unless the
    archive, every file in it and the record passed every check.
    """
    if not settings.enabled:
        raise UploadPlugError("PLUG_UNREACHABLE", "no upload plug is configured")
    validate_handle(handle)
    base = f"{settings.url.rstrip('/')}/v1/deliveries/{quote(handle, safe='')}"
    headers = {
        "Authorization": f"Bearer {settings.secret}",
        "X-Gate-Contract": CONTRACT_VERSION,
        # No compressed transfer: the read caps below count the bytes as sent.
        "Accept-Encoding": "identity",
    }
    params = {"thread_id": thread_id}
    try:
        with httpx.Client(
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            transport=settings.transport,
            trust_env=False,
        ) as client:
            manifest = _get_manifest(client, base, headers, params)
            # Every check on the manifest runs before the archive is fetched, so an
            # over-cap or malformed delivery is refused without spending it.
            archive_size, archive_digest, members, record = _check_manifest(
                manifest,
                max_files=max_files,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            )
            archive = _get_archive(
                client, f"{base}/archive", headers, params, archive_size, archive_digest
            )
    except httpx.HTTPError as exc:
        raise UploadPlugError("PLUG_UNREACHABLE", "the upload plug could not be reached") from None
    return _unzip_members(archive, members, record)


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 409:
        raise UploadPlugError("CONTRACT_MISMATCH", "the upload plug uses a different contract version")
    if response.status_code in {401, 403}:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug refused this app's credentials")
    if response.status_code in {502, 503, 504}:
        raise UploadPlugError("PLUG_UNREACHABLE", "the upload plug could not be reached")
    if response.status_code != 200:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug did not release these files")


def _get_manifest(client: httpx.Client, url: str, headers: dict, params: dict) -> dict:
    with client.stream("GET", url, headers=headers, params=params) as response:
        _raise_for_status(response)
        body = _read_capped(response, _MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(body)
    except ValueError as exc:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an unreadable manifest") from None
    if not isinstance(manifest, dict) or manifest.get("contract") != CONTRACT_VERSION:
        raise UploadPlugError("CONTRACT_MISMATCH", "the upload plug uses a different contract version")
    if set(manifest) != {"contract", "archive", "members", "record"}:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid manifest")
    return manifest


def _check_manifest(
    manifest: dict,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[int, str, list[tuple[str, str, str, int, str]], tuple[int, str]]:
    """Return the archive size and hash, ``(path, name, mime, size, sha256)`` per member and the
    record's ``(size, sha256)``."""
    entries = manifest.get("members")
    if not isinstance(entries, list) or not entries:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned no files")
    if len(entries) > max_files:
        raise UploadPlugError("TOO_MANY_FILES", "the prepared upload exceeds the attachment count limit")
    checked: list[tuple[str, str, str, int, str]] = []
    paths: set[str] = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "mime", "size", "sha256"}:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid manifest")
        path = entry.get("path")
        name = _member_basename(path)
        if path in paths:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned duplicate file paths")
        paths.add(path)
        size = entry.get("size")
        digest = entry.get("sha256")
        mime = entry.get("mime", "")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid file size")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid checksum")
        if mime != _MEMBER_MIME.get(name.rsplit(".", 1)[-1]):
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid media type")
        if size > max_file_bytes:
            raise UploadPlugError("FILE_TOO_LARGE", "a prepared file exceeds the attachment size limit")
        total += size
        if total > max_total_bytes:
            raise UploadPlugError(
                "MESSAGE_ATTACHMENTS_TOO_LARGE",
                "the prepared upload exceeds the total attachment size limit",
            )
        checked.append((path, name, mime, size, digest))
    _unique_names([name for _path, name, _mime, _size, _digest in checked])

    record = manifest.get("record")
    if not isinstance(record, dict) or set(record) != {"path", "mime", "size", "sha256"}:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid manifest")
    record_size = record.get("size")
    record_digest = record.get("sha256")
    if (
        record.get("path") != _RECORD_PATH
        or record.get("mime") != _RECORD_MIME
        or isinstance(record_size, bool)
        or not isinstance(record_size, int)
        or not 0 < record_size <= _MAX_RECORD_BYTES
        or not isinstance(record_digest, str)
        or not _SHA256_RE.fullmatch(record_digest)
    ):
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid record entry")

    archive = manifest.get("archive")
    if not isinstance(archive, dict) or set(archive) != {"size", "sha256"}:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid manifest")
    archive_size = archive.get("size")
    archive_digest = archive.get("sha256")
    if isinstance(archive_size, bool) or not isinstance(archive_size, int) or archive_size < 0:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid archive size")
    if not isinstance(archive_digest, str) or not _SHA256_RE.fullmatch(archive_digest):
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid checksum")
    bound = total + _ZIP_END_RECORD_BYTES + sum(
        _ZIP_ENTRY_BYTES + 2 * len(path) for path, _name, _mime, _size, _digest in checked
    ) + record_size + _ZIP_ENTRY_BYTES + 2 * len(_RECORD_PATH)
    if archive_size > bound:
        raise UploadPlugError("PLUG_REJECTED", "the prepared archive is larger than its files allow")
    return archive_size, archive_digest, checked, (record_size, record_digest)


def _get_archive(
    client: httpx.Client,
    url: str,
    headers: dict,
    params: dict,
    size: int,
    digest: str,
) -> bytes:
    with client.stream("GET", url, headers=headers, params=params) as response:
        _raise_for_status(response)
        content = _read_capped(response, size)
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise UploadPlugError("PLUG_REJECTED", "the prepared archive did not match its checksum")
    return content


def _unzip_members(
    archive: bytes,
    members: list[tuple[str, str, str, int, str]],
    record: tuple[int, str],
) -> PlugDelivery:
    """Unzip in memory, all or nothing: the entries must be exactly the manifest's files and
    the record. The record is validated and returned beside the files; it is never staged."""
    try:
        files, body = _read_members(archive, members, record)
    except UploadPlugError:
        raise
    except Exception:
        # zipfile, zlib and struct messages carry entry names: never pass them on.
        raise UploadPlugError(
            "PLUG_REJECTED", "the prepared archive could not be read safely"
        ) from None
    try:
        checked = _check_record(body)
        summaries = _summaries(checked, members)
    except UploadPlugError:
        raise
    except Exception:
        # A value of an unexpected type must refuse, never fail the request another way.
        raise _invalid_record() from None
    return PlugDelivery(files=files, record=checked, summaries=summaries)


def _read_members(
    archive: bytes,
    members: list[tuple[str, str, str, int, str]],
    record: tuple[int, str],
) -> tuple[list[tuple[str, str, bytes]], bytes]:
    mismatch = UploadPlugError("PLUG_REJECTED", "the prepared archive does not match its manifest")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        # Every entry is checked before any is read. infolist() keeps duplicates, and
        # orig_filename is the name as stored (filename is cut at a NUL byte).
        entries = bundle.infolist()
        for entry in entries:
            name = entry.orig_filename
            if (
                entry.filename != name
                or entry.is_dir()
                or name.startswith("/")
                or "\\" in name
                or ".." in name.split("/")
            ):
                raise mismatch
            if entry.flag_bits & _ZIP_ENCRYPTED_FLAG:
                raise UploadPlugError("PLUG_REJECTED", "the prepared archive holds an encrypted file")
            if entry.compress_type not in _ZIP_METHODS:
                raise UploadPlugError(
                    "PLUG_REJECTED", "the prepared archive uses an unsupported compression method"
                )
        names = [entry.orig_filename for entry in entries]
        expected = [path for path, *_rest in members] + [_RECORD_PATH]
        # the record is the last entry; the members come in any order before it
        if len(names) != len(expected) or set(names) != set(expected) or names[-1] != _RECORD_PATH:
            raise mismatch
        by_path = {entry.orig_filename: entry for entry in entries}
        for path, _name, _mime, size, _digest in members:
            if by_path[path].file_size != size:
                raise mismatch
        record_size, record_digest = record
        if by_path[_RECORD_PATH].file_size != record_size:
            raise mismatch
        files: list[tuple[str, str, bytes]] = []
        for path, name, mime, size, digest in members:
            with bundle.open(by_path[path]) as handle:
                content = handle.read(size + 1)
            if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                raise UploadPlugError("PLUG_REJECTED", "a prepared file did not match its checksum")
            files.append((name, mime, content))
        with bundle.open(by_path[_RECORD_PATH]) as handle:
            body = handle.read(record_size + 1)
    if len(body) != record_size or hashlib.sha256(body).hexdigest() != record_digest:
        raise UploadPlugError("PLUG_REJECTED", "the plug's record did not match its checksum")
    return files, body


def _invalid_record() -> UploadPlugError:
    return UploadPlugError("PLUG_REJECTED", "the plug's record is invalid")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise _invalid_record()
    return dict(pairs)


def _refuse_constant(_name: str) -> None:
    raise _invalid_record()


def _text(value: object) -> str:
    if not isinstance(value, str) or len(value) > _MAX_RECORD_TEXT:
        raise _invalid_record()
    return value


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid_record()
    return value


def _texts(value: object, *, max_items: int | None = None) -> list[str]:
    if not isinstance(value, list) or (max_items is not None and len(value) > max_items):
        raise _invalid_record()
    return [_text(item) for item in value]


def _code(value: object, allowed) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _invalid_record()
    return value


def _object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _invalid_record()
    return value


def _check_record(body: bytes) -> dict[str, Any]:
    """Validate the record strictly: exact keys, types, closed lists of codes and bounded
    strings. Any problem refuses the whole delivery with one fixed message."""
    try:
        record = json.loads(
            body.decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=_refuse_constant,
            parse_float=_refuse_constant,
        )
    except UploadPlugError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _invalid_record() from None
    _object(record, _RECORD_KEYS)
    if record["contract"] != CONTRACT_VERSION:
        raise _invalid_record()
    _code(record["method"], RECORD_METHODS)
    for key in ("model", "prompt_version", "gate", "statement"):
        _text(record[key])
    _texts(record["not_sent"])
    _texts(record["unchanged_files"])
    if not isinstance(record["columns"], list) or not isinstance(record["risk"], list):
        raise _invalid_record()
    for column in record["columns"]:
        _object(column, _COLUMN_KEYS)
        for key in ("file", "header", "written", "note"):
            _text(column[key])
        _count(column["sheet"])
        _count(column["cells_changed"])
        _code(column["action"], RECORD_ACTIONS)
        _code(column["source"], _RECORD_SOURCES)
        if not isinstance(column["reason"], str) or not _REASON_RE.fullmatch(column["reason"]):
            raise _invalid_record()
    for row in record["risk"]:
        _object(row, _RISK_KEYS)
        _text(row["file"])
        _texts(row["columns"], max_items=_MAX_RISK_COLUMNS)
        for key in ("sheet", "rows", "classes", "unique_rows", "small_rows"):
            _count(row[key])
        if not isinstance(row["with_dates"], bool):
            raise _invalid_record()
    return record


def _join_key(name: str) -> str:
    """The checker's own uniqueness key for a dataset file: the name without its extension
    (ignoring case) and the extension it is delivered with. The record names a file by its
    path in the uploaded ZIP (``data/Visits.CSV``), the delivery by ``dataset/Visits.csv``."""
    stem, dot, extension = name.rsplit("/", 1)[-1].rpartition(".")
    if not dot:
        return name.rsplit("/", 1)[-1].casefold()
    extension = extension.lower()
    return f"{stem.casefold()}.{_DELIVERED_EXTENSION.get(extension, extension)}"


def _summaries(
    record: dict[str, Any], members: list[tuple[str, str, str, int, str]]
) -> dict[str, dict[str, Any]]:
    """Per delivered dataset file: the method, the count of kept columns, every changed column
    (header, action, what it now holds) and that file's uniqueness counts. Every column and
    count row must belong to exactly one delivered dataset file, and every delivered dataset
    file must have its columns in the record."""
    datasets = {
        _join_key(name): name for path, name, *_rest in members if path.startswith("dataset/")
    }
    summaries: dict[str, dict[str, Any]] = {
        name: {"method": record["method"], "kept_columns": 0, "changed_columns": [], "risk": []}
        for name in datasets.values()
    }
    for column in record["columns"]:
        name = datasets.get(_join_key(column["file"]))
        if name is None:
            raise _invalid_record()
        summary = summaries[name]
        if column["action"] == "keep":
            summary["kept_columns"] += 1
        else:
            summary["changed_columns"].append(
                {
                    "header": column["header"],
                    "action": column["action"],
                    "written": column["written"],
                }
            )
    for row in record["risk"]:
        name = datasets.get(_join_key(row["file"]))
        if name is None:
            raise _invalid_record()
        summaries[name]["risk"].append({key: row[key] for key in sorted(_RISK_KEYS - {"file"})})
    for summary in summaries.values():
        if not summary["kept_columns"] and not summary["changed_columns"]:
            raise _invalid_record()
    return summaries


def _read_capped(response: httpx.Response, cap: int) -> bytes:
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise UploadPlugError("PLUG_REJECTED", "the upload plug sent an encoded response")
    content = bytearray()
    for chunk in response.iter_bytes(_READ_CHUNK_BYTES):
        content.extend(chunk)
        if len(content) > cap:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug sent more data than declared")
    return bytes(content)
