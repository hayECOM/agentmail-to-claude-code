"""Tests for cc-daemon.py helpers and both dispatch backends.

The daemon file has a hyphen in its name, so we load it via importlib.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile

import pytest


def _load_daemon():
    os.environ.setdefault("AGENTMAIL_INBOX", "bot@agentmail.to")
    os.environ.setdefault("AGENTMAIL_API_KEY", "x")
    os.environ.setdefault("CC_TERMINAL", "cmux")
    os.environ.setdefault("CC_ALLOWED_FROM", "alice@example.com, bob@example.com")
    os.environ.setdefault("CC_HOME", tempfile.mkdtemp(prefix="cc-home-"))
    spec = importlib.util.spec_from_file_location(
        "cc_daemon",
        pathlib.Path(__file__).parent / "cc-daemon.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


daemon = _load_daemon()


# --- config parsing ----------------------------------------------------------


def test_allowlist_parsed_and_lowercased():
    assert daemon.ALLOWED_FROM == {"alice@example.com", "bob@example.com"}


def test_safe_name_strips_message_id_angle_brackets():
    out = daemon.safe_name("<mp8w1w77.8dd87270@example.com>")
    assert out == "mp8w1w77.8dd87270_example.com"
    assert "/" not in out and ".." not in out


def test_safe_name_blocks_path_traversal():
    assert daemon.safe_name("../../etc/passwd") == "etc_passwd"


def test_format_size_units():
    assert daemon.format_size(512) == "512 B"
    assert daemon.format_size(2048) == "2.0 KB"
    assert daemon.format_size(5 * 1024 * 1024) == "5.0 MB"


def test_parse_from_extracts_angle_addr():
    assert daemon.parse_from("Alice <ALICE@Example.com>") == "alice@example.com"
    assert daemon.parse_from("bob@example.com") == "bob@example.com"


# --- build_prompt ------------------------------------------------------------


def test_build_prompt_includes_headers_and_body():
    p = daemon.build_prompt("Alice <alice@example.com>", "Do X", "body here", [])
    assert "From: Alice <alice@example.com>" in p
    assert "Subject: Do X" in p
    assert "body here" in p
    assert "Attachments" not in p


def test_build_prompt_lists_attachment_paths():
    atts = [{"path": "/tmp/a.png", "content_type": "image/png", "size": 1234}]
    p = daemon.build_prompt("a@example.com", "subj", "body", atts)
    assert "/tmp/a.png" in p
    assert "use the Read tool" in p


# --- cmux RPC helper ---------------------------------------------------------


def _fake_completed(stdout: str):
    class _R:
        returncode = 0

    r = _R()
    r.stdout = stdout
    r.stderr = ""
    return r


def test_cmux_rpc_no_params(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda cmd, **k: (calls.append(cmd), _fake_completed('{"surface_id":"U1"}'))[1])
    assert daemon._cmux_rpc("surface.create") == {"surface_id": "U1"}
    assert calls[0] == [daemon.CMUX_BIN, "rpc", "surface.create"]


def test_cmux_rpc_encodes_params(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda cmd, **k: (calls.append(cmd), _fake_completed("{}"))[1])
    daemon._cmux_rpc("surface.send_text", {"surface_id": "S", "text": "hi"})
    import json
    assert calls[0][:3] == [daemon.CMUX_BIN, "rpc", "surface.send_text"]
    assert json.loads(calls[0][3]) == {"surface_id": "S", "text": "hi"}


def test_cmux_rpc_empty_stdout(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **k: _fake_completed("  "))
    assert daemon._cmux_rpc("surface.refresh") == {}


# --- cmux open / send --------------------------------------------------------


def test_cmux_open_session_returns_surface_id_when_ready(monkeypatch):
    monkeypatch.setattr(daemon, "_cmux_rpc", lambda m, p=None: {"surface_id": "SID"})
    monkeypatch.setattr(daemon, "_surface_text", lambda s: "... bypass permissions on ...")
    monkeypatch.setattr(daemon.time, "sleep", lambda *_: None)
    assert daemon._cmux_open_session() == "SID"


def test_cmux_open_session_raises_without_id(monkeypatch):
    monkeypatch.setattr(daemon, "_cmux_rpc", lambda m, p=None: {})
    with pytest.raises(RuntimeError):
        daemon._cmux_open_session()


def test_cmux_send_pointer_text_then_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "_cmux_rpc", lambda m, p=None: calls.append((m, p)))
    monkeypatch.setattr(daemon.time, "sleep", lambda *_: None)
    daemon._cmux_send_pointer("SID", "one line")
    assert calls == [
        ("surface.send_text", {"surface_id": "SID", "text": "one line"}),
        ("surface.send_key", {"surface_id": "SID", "key": "enter"}),
    ]


def test_cmux_send_pointer_rejects_multiline():
    with pytest.raises(ValueError):
        daemon._cmux_send_pointer("SID", "line1\nline2")


# --- ghostty backend ---------------------------------------------------------


def test_ghostty_open_session_runs_osascript(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda cmd, **k: (calls.append(cmd), _fake_completed(""))[1])
    handle = daemon._ghostty_open_session()
    assert handle == "ghostty:frontmost"
    assert calls[0][0] == "osascript"


def test_ghostty_send_pointer_pbcopy_then_paste(monkeypatch):
    cmds = []
    monkeypatch.setattr(daemon.subprocess, "run",
                        lambda cmd, **k: (cmds.append(cmd[0]), _fake_completed(""))[1])
    daemon._ghostty_send_pointer("ghostty:frontmost", "one line pointer")
    assert cmds == ["pbcopy", "osascript"]


# --- dispatch routing --------------------------------------------------------


def test_dispatch_routes_to_selected_backend_and_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "PROMPT_ROOT", tmp_path / "prompts")
    monkeypatch.setattr(daemon, "TERMINAL", "cmux")
    sent = {}
    monkeypatch.setattr(daemon, "BACKENDS", {
        "cmux": (lambda: "SID-X", lambda h, p: sent.update(handle=h, pointer=p)),
    })
    daemon.dispatch_to_claude_code("From: a\nSubject: b\n\nbody", "a@example.com", "<m-1@x>")
    prompt_file = tmp_path / "prompts" / "m-1_x.md"
    assert prompt_file.read_text() == "From: a\nSubject: b\n\nbody"
    assert sent["handle"] == "SID-X"
    assert str(prompt_file) in sent["pointer"] and "\n" not in sent["pointer"]


def test_dispatch_unknown_terminal_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "PROMPT_ROOT", tmp_path / "prompts")
    monkeypatch.setattr(daemon, "TERMINAL", "wezterm")
    called = []
    monkeypatch.setattr(daemon, "BACKENDS", {"cmux": (lambda: called.append("open"), None)})
    daemon.dispatch_to_claude_code("p", "a@example.com", "m2")
    assert called == []  # unknown terminal: logged + returns, no backend invoked


def test_dispatch_aborts_when_open_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "PROMPT_ROOT", tmp_path / "prompts")
    monkeypatch.setattr(daemon, "TERMINAL", "cmux")
    sent = []

    def boom():
        raise RuntimeError("no socket")

    monkeypatch.setattr(daemon, "BACKENDS", {"cmux": (boom, lambda h, p: sent.append(p))})
    daemon.dispatch_to_claude_code("p", "a@example.com", "m3")
    assert sent == []
