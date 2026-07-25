from __future__ import annotations

from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "miosa_mcp" / "server.py"


def test_product_template_tools_are_distinct_from_sandbox_template_crud():
    source = SERVER.read_text()

    assert 'name="product_template_list"' in source
    assert 'name="product_template_get"' in source
    assert 'name="product_template_readiness"' in source
    assert '"/api/v1/templates"' in source

    assert 'name="sandbox_template_create"' in source
    assert '"/api/v1/sandbox-templates' in source
    assert "For canonical product/template/size readiness" in source


def test_computer_viewer_password_tools_are_exposed():
    source = SERVER.read_text()

    assert 'name="computer_viewer_password"' in source
    assert 'name="computer_rotate_viewer_password"' in source
    assert '/viewer-password"' in source
    assert "/viewer-password/rotate" in source
