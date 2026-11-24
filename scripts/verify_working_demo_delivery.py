#!/usr/bin/env python3
"""Verify the compiled frontend and its source manifest; run npm build first.

This intentionally checks frontend delivery only, without repository tracking or
ignore-policy requirements. Writing a manifest never builds frontend assets.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_BUILD_INPUTS = {
    "frontend/index.html", "frontend/package.json", "frontend/package-lock.json",
    "frontend/tsconfig.json", "frontend/tsconfig.node.json", "frontend/vite.config.ts",
}


def frontend_source_paths(root: Path) -> list[str]:
    paths = set(FRONTEND_BUILD_INPUTS)
    paths.update(str(path.relative_to(root)) for path in (root / "frontend").glob("tsconfig*.json"))
    for directory in ("src", "public"):
        for path in (root / "frontend" / directory).rglob("*"):
            if path.is_file() and not any(part in {"__tests__", "__mocks__"} for part in path.parts) and not re.search(r"\.(test|spec)\.[^.]+$", path.name):
                paths.add(path.relative_to(root).as_posix())
    return sorted(paths)


def create_frontend_build_manifest(project_root: str | Path = PROJECT_ROOT) -> dict:
    root = Path(project_root)
    lock = json.loads((root / "frontend/package-lock.json").read_text())
    vite_version = lock["packages"]["node_modules/vite"]["version"]
    return {
        "built_at": datetime.now(UTC).isoformat(),
        "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in frontend_source_paths(root)},
        "vite_version": vite_version,
    }


def asset_violations(root: Path) -> list[str]:
    dist = root / "frontend/dist"
    index = dist / "index.html"
    if not index.is_file():
        return ["built frontend index is missing"]
    problems = []
    # Follow the HTML references and local JS imports/CSS urls recursively.
    pending = [index]
    seen = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".html":
            refs = re.findall(r'(?:src|href)\s*=\s*["\x27]([^"\x27]+)', text)
        elif path.suffix == ".css":
            refs = re.findall(r'url\(\s*["\x27]?([^\)"\x27]+)', text)
        elif path.suffix in {".js", ".mjs"}:
            refs = re.findall(r'(?:from\s*|import\s*\(?\s*)["\x27]([^"\x27]+)', text)
        else:
            refs = []
        for ref in refs:
            url = urlsplit(ref)
            if url.scheme or url.netloc or not url.path:
                continue
            if path.suffix in {".js", ".mjs"} and not url.path.startswith((".", "/")):
                continue
            target = (dist / unquote(url.path).lstrip("/") if url.path.startswith("/") else path.parent / unquote(url.path)).resolve()
            if not target.is_relative_to(dist.resolve()) or not target.is_file():
                problems.append(f"built frontend referenced asset is missing or invalid: {ref}")
            elif target.suffix in {".html", ".css", ".js", ".mjs"}:
                pending.append(target)
    if not any(path.is_file() for path in (dist / "assets").glob("*")):
        problems.append("built frontend assets are missing")
    return problems


def collect_delivery_violations(project_root: str | Path = PROJECT_ROOT) -> list[str]:
    root = Path(project_root)
    problems = asset_violations(root)
    try:
        expected = create_frontend_build_manifest(root)
    except (OSError, KeyError, ValueError, TypeError) as error:
        problems.append(f"frontend sources/configuration are missing or invalid: {error}")
        return problems
    try:
        manifest = json.loads((root / "frontend/dist/build-manifest.json").read_text())
        if not isinstance(manifest, dict) or not manifest.get("built_at"):
            raise ValueError("missing build timestamp")
        datetime.fromisoformat(manifest["built_at"])
        if manifest.get("source_sha256") != expected["source_sha256"] or manifest.get("vite_version") != expected["vite_version"]:
            problems.append("frontend build manifest does not match current frontend sources")
    except (OSError, ValueError, TypeError):
        problems.append("frontend build manifest is missing or invalid")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-build-manifest", action="store_true", help="Record source hashes after a real npm build; does not run or replace that build.")
    args = parser.parse_args(argv)
    if args.write_build_manifest:
        problems = asset_violations(PROJECT_ROOT)
        if not problems:
            try:
                manifest = create_frontend_build_manifest(PROJECT_ROOT)
                (PROJECT_ROOT / "frontend/dist/build-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            except (OSError, KeyError, ValueError, TypeError) as error:
                problems = [f"cannot write frontend manifest: {error}"]
        if problems:
            for problem in problems:
                print(f"FAIL: {problem}")
            return 1
    problems = collect_delivery_violations(PROJECT_ROOT)
    for problem in problems:
        print(f"FAIL: {problem}")
    if not problems:
        print("PASS: compiled frontend assets and source manifest are consistent")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
