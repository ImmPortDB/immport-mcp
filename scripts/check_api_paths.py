#!/usr/bin/env python3
"""
Check that every data-query API path the MCP tools call exists in the API's OpenAPI spec,
the source of truth for the API. Run in the build (buildspec.yml); exits 1 on a missing path.

Paths are read from the source without importing it (standard library only, so it runs on
the build host): the registries in study_data.py (_RESOURCES), results.py (_ASSAYS) and
lookup_cache.py (LOOKUP_TABLES), every literal API path string in immport_mcp/tools/*.py, and every
API path mentioned in the text Claude reads (immport_mcp/resources, immport_mcp/prompts) — so docs cannot
point at endpoints that do not exist. Generic mentions with a wildcard (/result/*) are skipped.

Usage:
    python3 scripts/check_api_paths.py [--spec-url URL]

The spec URL defaults to $IMMPORT_OPENAPI_URL, else the dev API (the API is released to an
environment before the MCP server, so dev already serves what this build will call).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_SPEC_URL = "https://www.dev.immport.org/data/query/v3/api-docs"

# "/api/...", "/result/...", "/drs/..." in plain or f-strings
PATH_LITERAL = re.compile(r'["\'](/(?:api|result|drs)/[^"\'\s]*)["\']')
# the same paths mentioned in prose (resource and prompt text)
PATH_IN_TEXT = re.compile(r'(?<![\w/.])(/(?:api|result|drs|ga4gh)/[\w{}/.-]*[\w}])')


def module_assignment(path: pathlib.Path, name: str):
    """The literal value assigned to a module-level name, without importing the module."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        target = getattr(node, "target", None) or (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == name:
            return ast.literal_eval(node.value)
    raise SystemExit(f"{name} not found in {path}")


def tool_paths() -> dict[str, str]:
    """API path (with {} for any value) → where the tools use it."""
    paths: dict[str, str] = {}
    for slug, (suffix, *_rest) in module_assignment(ROOT / "immport_mcp/tools/study_data.py", "_RESOURCES").items():
        paths[f"/api/study/{suffix}/{{}}" if suffix else "/api/study/{}"] = f"get_study_data(resource='{slug}')"
    for slug, (endpoint, *_rest) in module_assignment(ROOT / "immport_mcp/tools/results.py", "_ASSAYS").items():
        paths[endpoint] = f"get_assay_results(assay_type='{slug}')"
    for table in module_assignment(ROOT / "immport_mcp/services/lookup_cache.py", "LOOKUP_TABLES"):
        paths[f"/api/lookup/{table}"] = f"lookup_controlled_vocabulary(table='{table}')"
    for source in sorted((ROOT / "immport_mcp/tools").glob("*.py")):
        for literal in PATH_LITERAL.findall(source.read_text()):
            template = re.sub(r"\{[^}]*\}", "{}", literal.rstrip("/"))
            # the registry entries above cover the generic templates built from them
            if template not in ("/api/study/{}/{}", "/api/lookup/{}"):
                paths.setdefault(template, f"{source.relative_to(ROOT)}")
    for source in sorted([*(ROOT / "immport_mcp/resources").glob("*.py"), *(ROOT / "immport_mcp/prompts").glob("*.py")]):
        for mention in PATH_IN_TEXT.findall(source.read_text()):
            if "*" not in mention:
                paths.setdefault(re.sub(r"\{[^}]*\}", "{}", mention), f"{source.relative_to(ROOT)} (text)")
    return paths


def matches(tool_path: str, spec_path: str) -> bool:
    a, b = tool_path.strip("/").split("/"), spec_path.strip("/").split("/")
    return len(a) == len(b) and all(x == y or x.startswith("{") or y.startswith("{") for x, y in zip(a, b))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec-url", default=os.environ.get("IMMPORT_OPENAPI_URL", DEFAULT_SPEC_URL))
    args = parser.parse_args()

    with urllib.request.urlopen(args.spec_url, timeout=60) as resp:
        spec_paths = list(json.load(resp)["paths"])

    used = tool_paths()
    missing = {p: where for p, where in used.items() if not any(matches(p, s) for s in spec_paths)}

    print(f"{len(used)} API paths used by the tools, {len(spec_paths)} paths in {args.spec_url}")
    if missing:
        print(f"\n{len(missing)} path(s) the tools call are NOT in the OpenAPI spec:")
        for path, where in sorted(missing.items()):
            print(f"  {path}    ← {where}")
        return 1
    print("All tool paths are in the OpenAPI spec.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
