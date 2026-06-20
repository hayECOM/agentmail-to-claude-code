"""Tests for cc-reply.py (the completion-reply helper).

The script has a hyphen in its name, so it is loaded via importlib.
"""

from __future__ import annotations

import importlib.util
import io
import pathlib


def _load_reply():
    spec = importlib.util.spec_from_file_location(
        "cc_reply",
        pathlib.Path(__file__).parent / "cc-reply.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reply = _load_reply()


# --- env loading -------------------------------------------------------------


def test_load_env_strips_export_and_quotes(tmp_path, monkeypatch):
    env = tmp_path / "cc.env"
    env.write_text(
        "# a comment\n"
        "export CC_MAIL_PROVIDER=agentmail\n"
        'export AGENTMAIL_INBOX="bot@agentmail.to"\n'
        "PLAIN=value\n"
        "\n"
    )
    for key in ("CC_MAIL_PROVIDER", "AGENTMAIL_INBOX", "PLAIN"):
        monkeypatch.delenv(key, raising=False)
    reply.load_env(env)
    import os

    assert os.environ["CC_MAIL_PROVIDER"] == "agentmail"
    assert os.environ["AGENTMAIL_INBOX"] == "bot@agentmail.to"
    assert os.environ["PLAIN"] == "value"


def test_load_env_does_not_override_existing(tmp_path, monkeypatch):
    env = tmp_path / "cc.env"
    env.write_text("export AGENTMAIL_INBOX=from-file@agentmail.to\n")
    monkeypatch.setenv("AGENTMAIL_INBOX", "already-set@agentmail.to")
    reply.load_env(env)
    import os

    assert os.environ["AGENTMAIL_INBOX"] == "already-set@agentmail.to"


# --- main routing ------------------------------------------------------------


def test_main_refuses_empty_body(monkeypatch):
    monkeypatch.setattr(reply, "load_env", lambda _p: None)
    monkeypatch.setattr(reply.sys, "stdin", io.StringIO("   \n"))
    assert reply.main(["--message-id", "<m@x>"]) == 2


def test_main_routes_to_agentmail_by_default(monkeypatch):
    sent = {}
    monkeypatch.setattr(reply, "load_env", lambda _p: None)
    monkeypatch.setenv("CC_MAIL_PROVIDER", "agentmail")
    monkeypatch.setattr(reply, "reply_agentmail",
                        lambda mid, body: sent.update(mid=mid, body=body, provider="agentmail"))
    rc = reply.main(["--message-id", "<m@x>", "--body", "Done -- shipped."])
    assert rc == 0
    assert sent == {"mid": "<m@x>", "body": "Done -- shipped.", "provider": "agentmail"}


def test_main_routes_to_primitive_when_configured(monkeypatch):
    sent = {}
    monkeypatch.setattr(reply, "load_env", lambda _p: None)
    monkeypatch.setenv("CC_MAIL_PROVIDER", "primitive")
    monkeypatch.setattr(reply, "reply_primitive",
                        lambda mid, body: sent.update(mid=mid, provider="primitive"))
    rc = reply.main(["--message-id", "em_1", "--body", "done"])
    assert rc == 0
    assert sent == {"mid": "em_1", "provider": "primitive"}


def test_main_reads_body_from_stdin(monkeypatch):
    sent = {}
    monkeypatch.setattr(reply, "load_env", lambda _p: None)
    monkeypatch.setenv("CC_MAIL_PROVIDER", "agentmail")
    monkeypatch.setattr(reply.sys, "stdin", io.StringIO("from stdin\n"))
    monkeypatch.setattr(reply, "reply_agentmail",
                        lambda mid, body: sent.update(body=body))
    assert reply.main(["--message-id", "<m@x>"]) == 0
    assert sent["body"] == "from stdin"


def test_main_returns_1_when_send_fails(monkeypatch):
    monkeypatch.setattr(reply, "load_env", lambda _p: None)
    monkeypatch.setenv("CC_MAIL_PROVIDER", "agentmail")

    def boom(mid, body):
        raise RuntimeError("network down")

    monkeypatch.setattr(reply, "reply_agentmail", boom)
    assert reply.main(["--message-id", "<m@x>", "--body", "done"]) == 1
