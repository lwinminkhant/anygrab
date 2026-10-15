"""Exercise startup configuration without opening a server or loading app state."""

import ast
import os
from pathlib import Path
from unittest.mock import patch

import pytest


def run_entrypoint():
    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text())
    entrypoint = tree.body[-1]
    assert isinstance(entrypoint, ast.If)
    code = ast.Module(body=entrypoint.body, type_ignores=[])
    with patch("uvicorn.run") as run:
        exec(compile(code, str(source), "exec"), {"os": os})
    return run


@pytest.mark.parametrize("port, expected", [(None, 8000), ("10000", 10000), ("1", 1), ("65535", 65535)])
def test_startup_uses_configured_port(monkeypatch, port, expected):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("WORKERS", "1")
    if port is not None:
        monkeypatch.setenv("PORT", port)
    run = run_entrypoint()
    assert run.call_args.kwargs["port"] == expected
    assert run.call_args.kwargs["host"] == "0.0.0.0"


@pytest.mark.parametrize("port", ["", "not-a-port", "1.5", "0", "-1", "65536"])
def test_startup_rejects_invalid_port(monkeypatch, port):
    monkeypatch.setenv("PORT", port)
    with pytest.raises(SystemExit, match="PORT must be an integer between 1 and 65535"):
        run_entrypoint()
