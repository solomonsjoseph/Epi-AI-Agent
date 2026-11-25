"""Deliver complete catalog definitions within the existing tool-message limit."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from epi_agent.protocol import (
    ArtifactRef,
    ToolExecutionError,
    ToolResult,
    serialize_tool_result,
)


def _result(payload: dict[str, Any], reference: ArtifactRef) -> ToolResult:
    return ToolResult(
        message=json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ),
        artifacts=(reference,),
    )


def _fits(result: ToolResult) -> bool:
    serialized = json.loads(serialize_tool_result(result))
    return (
        serialized["message"] == result.message
        and len(serialized["artifacts"]) == 1
    )


def _reopen(reference: ArtifactRef, offset: int, text_offset: int = 0) -> dict[str, Any]:
    arguments = {
        "artifact_id": reference.id, "version": reference.version, "offset": offset
    }
    if text_offset:
        arguments["text_offset"] = text_offset
    return {"tool": "dbrag-open_artifact", "arguments": arguments}


def _continuation(
    payload: dict[str, Any], reference: ArtifactRef, end: int, total: int
) -> dict[str, Any] | None:
    if end < total:
        return _reopen(reference, end)
    if "fields" in payload and payload.get("has_more"):
        return {
            "tool": "dbrag-inspect_table",
            "arguments": {
                "table_ref": payload["table_ref"],
                "offset": payload["offset"] + end,
                "limit": len(payload["fields"]),
            },
        }
    return None


def _page(
    payload: dict[str, Any], reference: ArtifactRef, start: int, end: int, total: int
) -> dict[str, Any]:
    page = deepcopy(payload)
    if "fields" in page:
        page["fields"] = page["fields"][start:end]
        page["offset"] = payload["offset"] + start
    else:
        position = 0
        for probe in page["probes"]:
            hits = probe["hits"]
            probe["hits"] = hits[max(0, start - position) : max(0, end - position)]
            probe["returned_count"] = len(probe["hits"])
            probe["table_hits"] = sum(
                "field_ref" not in hit for hit in probe["hits"]
            )
            probe["column_hits"] = len(probe["hits"]) - probe["table_hits"]
            references = [
                hit.get("field_ref", hit.get("table_ref", hit))
                for hit in probe["hits"]
            ]
            probe["unique_table_count"] = len({
                (ref.get("source_id"), ref.get("table")) for ref in references
            })
            probe["unique_column_count"] = len({
                (ref.get("source_id"), ref.get("table"), ref["column"])
                for ref in references if ref.get("column")
            })
            position += len(hits)
        page["offset"] = start
    continuation = _continuation(payload, reference, end, total)
    page.update(
        returned_count=end - start,
        has_more=continuation is not None,
        next_offset=(payload.get("offset", 0) + end if continuation else None),
        next_page=continuation,
        definitions_complete=True,
    )
    return page


def _definition_part(
    payload: dict[str, Any],
    reference: ArtifactRef,
    items: list[dict[str, Any]],
    offset: int,
    text_offset: int,
) -> ToolResult:
    definition = json.dumps(
        items[offset], ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    if text_offset >= len(definition):
        raise ToolExecutionError(
            "METADATA_PAGE_OUT_OF_RANGE", "Definition text offset is out of range.",
            recoverable=True,
        )

    def candidate(end: int) -> ToolResult:
        page = _page(payload, reference, offset, offset, len(items))
        is_last = end == len(definition)
        continuation = (
            _continuation(payload, reference, offset + 1, len(items))
            if is_last
            else _reopen(reference, offset, end)
        )
        page.update(
            definitions_complete=False,
            has_more=continuation is not None,
            next_offset=(
                payload.get("offset", 0) + offset + int(is_last)
                if continuation else None
            ),
            next_page=continuation,
            instructions=(
                "This definition exceeds one message. Read all definition_part pages before using it. "
                "Concatenate their text in offset order to recover the complete JSON definition."
            ),
            definition_part={
                "format": "json",
                "reference": items[offset].get(
                    "field_ref", items[offset].get("table_ref")
                ),
                "offset": text_offset,
                "total_char_count": len(definition),
                "text": definition[text_offset:end],
                "is_last": is_last,
            },
        )
        return _result(page, reference)

    # The last part drops its continuation, so it can fit even if a shorter
    # non-final part cannot. Check it before the monotone prefix search.
    final = candidate(len(definition))
    if _fits(final):
        return final
    low, high = text_offset + 1, len(definition) - 1
    best = None
    while low <= high:
        middle = (low + high) // 2
        result = candidate(middle)
        if _fits(result):
            best = result
            low = middle + 1
        else:
            high = middle - 1
    if best is None:
        raise ToolExecutionError(
            "METADATA_PAGE_TOO_LARGE",
            "Metadata page headers exceed the tool-message limit.", recoverable=True,
        )
    return best


def metadata_tool_result(
    payload: dict[str, Any],
    reference: ArtifactRef,
    *,
    offset: int = 0,
    text_offset: int = 0,
) -> ToolResult:
    """Page saved search hits or table fields without silently shortening definitions."""
    items = (
        payload["fields"]
        if "fields" in payload
        else [hit for probe in payload["probes"] for hit in probe["hits"]]
    )
    if offset > len(items) or (text_offset and offset == len(items)):
        raise ToolExecutionError(
            "METADATA_PAGE_OUT_OF_RANGE", "Metadata page offset is out of range.",
            recoverable=True,
        )
    if text_offset:
        return _definition_part(payload, reference, items, offset, text_offset)

    # A final page has less continuation overhead than an intermediate page.
    final = _result(
        _page(payload, reference, offset, len(items), len(items)), reference
    )
    if _fits(final):
        return final
    best = None
    for end in range(offset, len(items)):
        result = _result(
            _page(payload, reference, offset, end, len(items)), reference
        )
        if not _fits(result):
            break
        if end > offset or not items or offset == len(items):
            best = result
    if best is not None:
        return best
    return _definition_part(payload, reference, items, offset, 0)
