#!/usr/bin/env python3
"""Install both real Cohere v4 packages and verify retrieval, API, and compiled UI.

Run once per feature delivery. The five-minute limit includes process teardown.
Diagnostics and installed test packages remain in the artifact directory.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import traceback
from typing import Any


def _supervise() -> int:
    """Bound imports, native calls, browsers, and the API in a separate process."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    args, _ = parser.parse_known_args()
    if not 10 <= args.timeout_seconds <= 300:
        parser.error("timeout-seconds must be between 10 and 300")
    worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"], start_new_session=True)
    try:
        return worker.wait(timeout=args.timeout_seconds - 3)
    except subprocess.TimeoutExpired:
        # Chromium may use a separate process group; collect the descendant tree
        # before terminating the worker, so it cannot become orphaned.
        descendants = {worker.pid}
        try:
            rows = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True, timeout=1)
            parents = [tuple(map(int, row.split())) for row in rows.splitlines()]
            while True:
                expanded = descendants | {pid for pid, ppid in parents if ppid in descendants}
                if expanded == descendants:
                    break
                descendants = expanded
        except (subprocess.SubprocessError, ValueError):
            pass
        finally:
            for pid in descendants - {worker.pid}:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.wait(timeout=1)
        args.artifact_dir.mkdir(parents=True, exist_ok=True)
        (args.artifact_dir / "watchdog-timeout.txt").write_text("Real smoke terminated by its external process-tree watchdog.\n", encoding="utf-8")
        return 124
    finally:
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__ == "__main__" and "--worker" not in sys.argv and not any(flag in sys.argv for flag in ("--help", "-h")):
    raise SystemExit(_supervise())

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.auth import LOCAL_SESSION_ID
from db_rag.embedding_startup import assess_study_compatibility, initialize_embedding
from db_rag.session_studies import bind_session_studies
from study_package.installer import install_study_archives
from study_package.registry import discover_studies
from utils.env_loader import load_app_environment

PROFILE = "bedrock-cohere-embed-v4"
IDENTITY = "Bedrock/cohere-embed-v4-1536"
HEADERS = {"X-Epi-Session-ID": LOCAL_SESSION_ID}
STUDIES = {"nhanes-2017-2018": "0.4.0", "report-india-synthetic": "0.5.0"}


def _json_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _check_status(status: dict[str, Any]) -> None:
    assert status["available"] is True, status
    assert status["profile_id"] == PROFILE, status
    assert status["index_compatibility"] == IDENTITY, status
    assert status["retrieval_mode"] == "hybrid_vector_lexical", status
    assert set(status["compatible_study_ids"]) == set(STUDIES), status
    assert not status["incompatible_study_ids"], status


def _retrieval(studies_root: Path, environment: dict[str, str], artifacts: Path) -> dict[str, Any]:
    discovered = discover_studies(studies_root)
    assert {study.study_id: study.package_version for study in discovered.values} == STUDIES
    startup = initialize_embedding(environment)
    status = assess_study_compatibility(startup.status, startup.route, discovered).model_dump(mode="json")
    _json_write(artifacts / "retrieval-startup.json", status)
    _check_status(status)
    bound = bind_session_studies(discovered, embedding_route=startup.route)
    assert all(item.available for item in bound.readiness.values()), bound.readiness
    diagnostics: dict[str, Any] = {"startup": status, "studies": {}}
    probes = {
        "nhanes-2017-2018": ("glycohemoglobin HbA1c long term blood sugar LBXGH", "GHB_J", "LBXGH", "sampling design and survey weights"),
        "report-india-synthetic": ("manufactured cigarette smoking intensity per day CIGPAST", "Baseline Clinical and Demographic Information Cohort A", "CIGPAST", "Cohort A index cases and Cohort B household contacts"),
    }
    for study_id, (query, table, column, knowledge_query) in probes.items():
        study = bound.studies.require(study_id)
        outcome = study.catalog.search_many_with_status([query], limit=10)
        _json_write(artifacts / f"{study_id}-catalog.json", {"status": asdict(outcome.status), "hits": [[hit.model_dump(mode="json") for hit in batch] for batch in outcome.value]})
        assert outcome.status.mode == "hybrid_vector_lexical", outcome.status
        hits = outcome.value[0]
        assert hits and all(hit.source == study.source_id for hit in hits)
        assert any(hit.table == table and hit.column == column and "vector" in hit.matched_by for hit in hits), hits
        design = study.study_design.search_with_status(knowledge_query, limit=5)
        _json_write(artifacts / f"{study_id}-design.json", {"status": asdict(design.status), "hits": [asdict(hit) for hit in design.value]})
        assert design.status.mode == "hybrid_vector_lexical", design.status
        assert design.value and any("vector" in hit.matched_by for hit in design.value)
        if study_id == "nhanes-2017-2018":
            assert any("vector" in h.matched_by and "sample design" in h.section.lower() for h in design.value)
        else:
            assert any("vector" in h.matched_by and "cohort a" in h.text.lower() and "cohort b" in h.text.lower() for h in design.value)
        details: dict[str, Any] = {
            "catalog": [{"table": h.table, "column": h.column, "matched_by": list(h.matched_by)} for h in hits],
            "study_design_ids": [h.id for h in design.value],
        }
        if study_id == "report-india-synthetic":
            assert study.knowledge is not None, "RePORT publication knowledge is required"
            publication = study.knowledge.search_with_status("tuberculosis household contacts progression latent infection", limit=5)
            _json_write(artifacts / f"{study_id}-publication.json", {"status": asdict(publication.status), "hits": [hit.model_dump(mode="json") for hit in publication.value]})
            assert publication.status.mode == "hybrid_vector_lexical", publication.status
            assert publication.value and any("vector" in str(h.provenance.get("matched_by", "")) for h in publication.value)
            details["publication_ids"] = [h.id for h in publication.value]
        diagnostics["studies"][study_id] = details
        _json_write(artifacts / "retrieval-progress.json", diagnostics)
    return diagnostics


def _api_json(url: str) -> dict[str, Any]:
    response = requests.get(url, headers=HEADERS, timeout=10)
    response.raise_for_status()
    return response.json()


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _browser(api_url: str, artifacts: Path, deadline: float) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    options = _api_json(api_url + "/api/runtime/options")
    _json_write(artifacts / "runtime-options.json", options)
    _check_status(options["embedding_startup_status"])
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as error:
            if "Executable doesn't exist" not in str(error):
                raise
            browser = playwright.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        try:
            remaining = lambda: max(1, min(30_000, int((deadline - time.monotonic()) * 1000)))
            page.goto(api_url, wait_until="networkidle", timeout=remaining())
            attachment = page.get_by_test_id("attachment-file-input")
            attachment.wait_for(timeout=remaining())
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/attachments"), timeout=remaining()) as response_info:
                attachment.set_input_files({"name": "cohere-v4-probe.txt", "mimeType": "text/plain", "buffer": b"Cohere v4 package retrieval verification"})
            response = response_info.value
            assert response.ok, response.status
            page.get_by_text("cohere-v4-probe.txt", exact=True).wait_for(timeout=remaining())
            thread_id = response.url.split("/threads/", 1)[1].split("/", 1)[0]
            state = _api_json(f"{api_url}/api/threads/{thread_id}/state")
            _json_write(artifacts / "thread-state.json", state)
            _check_status(state["embedding_startup_status"])
            assert page.locator(".embedding-fallback-notice").count() == 0
            return {"attachment_name": "cohere-v4-probe.txt", "thread_id": thread_id, "fallback_notices": 0}
        finally:
            errors = []
            for name, capture in (
                ("page.txt", lambda: (artifacts / "page.txt").write_text(page.locator("body").inner_text(timeout=2000), encoding="utf-8")),
                ("page.html", lambda: (artifacts / "page.html").write_text(page.content(), encoding="utf-8")),
                ("screenshot.png", lambda: page.screenshot(path=str(artifacts / "screenshot.png"), full_page=True, timeout=2000)),
            ):
                try:
                    capture()
                except Exception as error:
                    errors.append({"artifact": name, "error_type": type(error).__name__})
            try:
                browser.close()
            except Exception as error:
                errors.append({"artifact": "browser teardown", "error_type": type(error).__name__})
            if errors:
                _json_write(artifacts / "capture-errors.json", errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nhanes-archive", type=Path, required=True)
    parser.add_argument("--report-archive", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 10 <= args.timeout_seconds <= 300:
        parser.error("timeout-seconds must be between 10 and 300")
    artifacts = args.artifact_dir.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    # A durable start marker prevents accidentally repeating a failed smoke.
    _json_write(artifacts / "started.json", {"started": time.time(), "timeout_seconds": args.timeout_seconds})
    started = time.monotonic()
    deadline = started + args.timeout_seconds - 15

    def timeout(_signum, _frame):
        raise TimeoutError("Cohere v4 real smoke reached its deadline")

    prior_handler = signal.signal(signal.SIGALRM, timeout)
    signal.alarm(max(1, args.timeout_seconds - 15))
    process = None
    log = None
    result: dict[str, Any] = {"status": "FAIL", "model": IDENTITY}
    load_app_environment(REPO_ROOT)
    environment = dict(os.environ)
    environment["DB_RAG_EMBEDDING_PROFILE"] = PROFILE
    try:
        studies_root = artifacts / "studies"
        install_study_archives([args.nhanes_archive.resolve(), args.report_archive.resolve()], studies_root)
        result["retrieval"] = _retrieval(studies_root, environment, artifacts)
        _json_write(artifacts / "retrieval.json", result["retrieval"])
        runtime = artifacts / "runtime"
        runtime.mkdir()
        environment.update({
            "PYTHONPATH": str(REPO_ROOT),
            "REPORT_AGENT_RUNTIME_ROOT": str(runtime),
            "REPORT_AGENT_CHECKPOINT_DB_PATH": str(runtime / "memory.db"),
            "REPORT_AGENT_STUDY_ROOT": str(artifacts),
            "REPORT_AGENT_STATIC_DIR": str(REPO_ROOT / "frontend/dist"),
        })
        assert (REPO_ROOT / "frontend/dist/index.html").is_file(), "Compiled frontend missing"
        port = _port()
        api_url = f"http://127.0.0.1:{port}"
        log = (artifacts / "api.log").open("w", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.app:build_application", "--factory", "--host", "127.0.0.1", "--port", str(port), "--workers", "1"], cwd=REPO_ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("API exited during startup; inspect api.log")
            try:
                if requests.get(api_url + "/api/health", timeout=1).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.25)
        else:
            raise TimeoutError("API did not become healthy before the smoke deadline")
        result["browser"] = _browser(api_url, artifacts, deadline)
        result["status"] = "PASS"
    except Exception as error:
        secrets = [v for k, v in environment.items() if v and (k.endswith("_API_KEY") or k == "AWS_BEARER_TOKEN_BEDROCK")]
        detail = traceback.format_exc()
        for secret in secrets:
            detail = detail.replace(secret, "[redacted]")
        (artifacts / "traceback.txt").write_text(detail, encoding="utf-8")
        result["error_type"] = type(error).__name__
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prior_handler)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if log is not None:
            log.close()
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        _json_write(artifacts / "result.json", result)
    print(json.dumps({"status": result["status"], "artifact_dir": str(artifacts), "elapsed_seconds": result["elapsed_seconds"]}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
