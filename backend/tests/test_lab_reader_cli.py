"""Unit tests for the lab read CLI transport (lab_reader.read_labs).

The live `claude -p` call can't be self-verified in-session (nested-session hang), so subprocess.run
is mocked. These pin the load-bearing contract of the auth switch: the read drives the subscription
CLI (not the SDK), STRIPS ambient tokens by default, scopes file access with --add-dir, and parses
the --output-format json envelope's `result` field.

Run:  python -m pytest backend/tests/test_lab_reader_cli.py
"""

import json
import os
import sys
import types

# Make `services` / `prompts` importable whether run from repo root or backend/.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from services import lab_reader  # noqa: E402


_GOOD_JSON = json.dumps({
    "results": [{
        "plain_name": "Vitamin D", "analyte_raw": "25-OH Vit D", "value": "18", "unit": "ng/mL",
        "ref_range_text": "30-100", "range_type": "two_sided_numeric", "status": "low",
        "severity_phrase": "a bit low", "confidence": "Likely",
        "plain_meaning": "supports bone health", "clarity": 0.9, "page_index": 0,
        "source_text": "Vitamin D 18 (30-100)",
    }],
    "signals": {"extraction_confidence": 0.92, "analytes_parsed": 1, "render_quality": "clear"},
})


def _fake_proc(stdout="", returncode=0, stderr=""):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def _envelope(result_text, is_error=False):
    return json.dumps({"result": result_text, "is_error": is_error, "num_turns": 2})


def _page(tmp_path):
    p = tmp_path / "page_0.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")  # not a real PNG; the CLI is mocked, only the path is used
    return p


def test_read_labs_uses_subscription_cli_and_strips_tokens(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        captured["input"] = kwargs.get("input")
        return _fake_proc(stdout=_envelope(_GOOD_JSON))

    monkeypatch.setattr(lab_reader, "_resolve_claude_bin", lambda *a, **k: "claude")
    monkeypatch.setattr(lab_reader.subprocess, "run", fake_run)
    # Ambient tokens present — the default desktop posture must STRIP them (subscription login).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-be-stripped")
    monkeypatch.delenv("MIKA_AGENT_USE_API_KEY", raising=False)

    page = _page(tmp_path)
    parsed = lab_reader.read_labs("abcd1234", [page])

    # Parsed the envelope's result field into the validated lab dict.
    assert parsed["results"][0]["plain_name"] == "Vitamin D"
    assert parsed["results"][0]["status"] == "low"
    assert parsed["signals"]["render_quality"] == "clear"

    cmd = captured["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert "--output-format" in cmd and "json" in cmd
    assert "--add-dir" in cmd and str(page.parent) in cmd
    assert "--permission-mode" in cmd and "bypassPermissions" in cmd
    # Default desktop posture: NO token reaches the child.
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in captured["env"]
    # Prompt delivered on stdin (Windows argv cap) and references the page file.
    assert str(page) in captured["input"]


def test_read_labs_honours_explicit_api_key(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _fake_proc(stdout=_envelope(_GOOD_JSON))

    monkeypatch.setattr(lab_reader, "_resolve_claude_bin", lambda *a, **k: "claude")
    monkeypatch.setattr(lab_reader.subprocess, "run", fake_run)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    lab_reader.read_labs("abcd1234", [_page(tmp_path)], api_key="sk-user-key")
    assert captured["env"].get("ANTHROPIC_API_KEY") == "sk-user-key"
    assert "ANTHROPIC_AUTH_TOKEN" not in captured["env"]


def test_read_labs_raises_on_cli_error_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(lab_reader, "_resolve_claude_bin", lambda *a, **k: "claude")
    monkeypatch.setattr(lab_reader.subprocess, "run",
                        lambda cmd, **k: _fake_proc(stdout=_envelope("model error", is_error=True)))
    with pytest.raises(RuntimeError, match="Lab read failed"):
        lab_reader.read_labs("abcd1234", [_page(tmp_path)])


def test_read_labs_raises_when_cli_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(lab_reader, "_resolve_claude_bin", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="Claude Code CLI not found"):
        lab_reader.read_labs("abcd1234", [_page(tmp_path)])


def test_read_labs_strips_json_fence_in_result(tmp_path, monkeypatch):
    fenced = "```json\n" + _GOOD_JSON + "\n```"
    monkeypatch.setattr(lab_reader, "_resolve_claude_bin", lambda *a, **k: "claude")
    monkeypatch.setattr(lab_reader.subprocess, "run",
                        lambda cmd, **k: _fake_proc(stdout=_envelope(fenced)))
    parsed = lab_reader.read_labs("abcd1234", [_page(tmp_path)])
    assert parsed["results"][0]["plain_name"] == "Vitamin D"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
