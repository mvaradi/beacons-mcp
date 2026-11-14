"""Guards against patching or reaching into the MCP SDK.

Generated glue code that works around an SDK gap by poking at its internals breaks the
day the SDK changes. This project uses the SDK's public API only: if something cannot be
done that way, the right move is to stop and say so, not to patch.
"""

import re
from pathlib import Path

import pytest

SOURCE = Path(__file__).parent.parent / "src" / "beacons_mcp"
FILES = sorted(SOURCE.glob("*.py"))


def strip_comments_and_strings(text: str) -> str:
    text = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
    return re.sub(r"(?m)#.*$", "", text)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_private_sdk_modules_are_imported(path):
    code = strip_comments_and_strings(path.read_text(encoding="utf-8"))

    private_imports = re.findall(r"(?:from|import)\s+(mcp(?:\.\w+)*\._\w[\w.]*)", code)

    assert private_imports == []


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_private_sdk_attributes_are_used(path):
    code = strip_comments_and_strings(path.read_text(encoding="utf-8"))

    # e.g. mcp._lowlevel_server, server._tool_manager, ctx._request_context
    private_use = re.findall(
        r"\b(?:mcp|server|apps|ctx|session|lowlevel)\._\w+|\._(?:lowlevel|tool_manager|resource_manager|extensions|mcp_server|request_context|session)\b",
        code,
    )

    assert private_use == []


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_sdk_is_not_monkeypatched(path):
    code = strip_comments_and_strings(path.read_text(encoding="utf-8"))

    patched = re.findall(
        r"\bsetattr\(\s*(?:mcp|server|apps|ctx|session)\b|\b(?:mcp|apps|MCPServer|Apps|Context|Resource)\.\w+\s*=[^=]|\bmock\.patch\b|\bmonkeypatch\b",
        code,
    )

    assert patched == []


def test_the_apps_extension_is_used_through_its_public_names_only():
    code = (SOURCE / "server.py").read_text(encoding="utf-8")

    # The documented entry points: apps.tool, apps.add_resource, ResourceCsp, extensions=[...]
    assert "from mcp.server.apps import Apps, client_supports_apps" in code
    assert "from mcp.server.apps import APP_MIME_TYPE, ResourceCsp" in (
        SOURCE / "viewer.py"
    ).read_text(encoding="utf-8")
    assert "apps.tool(" in code
    assert "apps.add_resource(" in code
    assert "extensions=[apps]" in code


def test_the_guard_itself_catches_the_things_it_is_for():
    bad = "from mcp.server.mcpserver._tool_manager import X\nmcp._lowlevel_server.foo = 1\nsetattr(mcp, 'x', 1)\n"

    assert re.findall(r"(?:from|import)\s+(mcp(?:\.\w+)*\._\w[\w.]*)", bad)
    assert re.findall(r"\b(?:mcp|server|apps|ctx|session|lowlevel)\._\w+", bad)
    assert re.findall(r"\bsetattr\(\s*(?:mcp|server|apps|ctx|session)\b", bad)
