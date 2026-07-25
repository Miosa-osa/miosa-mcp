#!/usr/bin/env python3
"""Validate and pin the portable action capability contract for miosa-mcp."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
CAPABILITY_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEFAULT_OUTPUT = Path("miosa_mcp/data/action-capabilities.json")


def expected_fingerprint(name: str, version: str) -> str:
    source = f"miosa-capability/{name}@{version}".encode()
    return f"sha256:{hashlib.sha256(source).hexdigest()}"


def validate(contract: dict) -> None:
    if set(contract) != {"version", "capabilities"} or contract.get("version") != 1:
        raise ValueError("unsupported action capability contract")

    seen_capabilities = set()
    seen_tools = set()
    for capability in contract.get("capabilities", []):
        if set(capability) != {"name", "version", "fingerprint", "surfaces"}:
            raise ValueError("portable contract contains policy or unsupported fields")

        name = capability.get("name")
        version = capability.get("version")
        if not isinstance(name, str) or not CAPABILITY_NAME.fullmatch(name):
            raise ValueError(f"invalid capability name: {name!r}")
        if not isinstance(version, str) or not CAPABILITY_VERSION.fullmatch(version):
            raise ValueError(f"invalid capability version: {version!r}")
        if capability.get("fingerprint") != expected_fingerprint(name, version):
            raise ValueError(f"invalid capability fingerprint: {name}")
        if name in seen_capabilities:
            raise ValueError(f"duplicate capability: {name}")
        seen_capabilities.add(name)

        tools = capability.get("surfaces", {}).get("mcp")
        if not isinstance(tools, list):
            raise ValueError(f"invalid MCP aliases: {name}")
        for tool in tools:
            if not isinstance(tool, str) or not tool:
                raise ValueError(f"invalid MCP tool alias: {name}")
            if tool in seen_tools:
                raise ValueError(f"duplicate MCP tool alias: {tool}")
            seen_tools.add(tool)

    if not seen_tools:
        raise ValueError("action capability contract has no MCP tools")


def write_atomic(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(contents)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    contract = json.loads(args.source.read_text())
    validate(contract)
    contents = json.dumps(contract, indent=2, sort_keys=False) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text() != contents:
            raise SystemExit(f"{args.output} is stale; run {Path(__file__).name}")
        return
    write_atomic(args.output, contents)


if __name__ == "__main__":
    main()
