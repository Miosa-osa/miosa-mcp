import sys

from miosa_mcp.server import main


def test_version_does_not_require_api_key(monkeypatch, capsys):
    monkeypatch.delenv("MIOSA_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["miosa-mcp", "--version"])

    main()

    captured = capsys.readouterr()
    assert captured.out.startswith("miosa-mcp ")
    assert "MIOSA_API_KEY" not in captured.err
