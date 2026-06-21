#!/usr/bin/env python3
"""Reply to an inbound cc-daemon email task on completion.

The daemon dispatches each email to an interactive Claude Code session and hands
it a prompt file. That session is a TUI -- there is no stdout for the daemon to
capture and mail back (unlike the old headless dispatcher) -- so the session
itself closes the loop by calling this helper when the task is done. The sender
then gets the completion summary they expect on the original thread.

Provider (AgentMail or Primitive) and credentials are read from cc.env, the same
config the daemon uses, so the dispatched session needs no extra setup.

Usage:
    cc-reply.py --message-id '<id>' --body 'Done -- shipped the fix.'
    echo 'Done -- shipped the fix.' | cc-reply.py --message-id '<id>'
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import urllib.parse


def load_env(path: pathlib.Path) -> None:
    """Load `KEY=value` / `export KEY=value` lines from cc.env into os.environ.

    setdefault, not overwrite: a value already in the environment (e.g. exported
    by the daemon's run script) wins over the file.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def reply_agentmail(message_id: str, body: str) -> None:
    from agentmail import AgentMail

    inbox = os.environ["AGENTMAIL_INBOX"]
    AgentMail().inboxes.messages.reply(inbox_id=inbox, message_id=message_id, text=body)


def reply_primitive(message_id: str, body: str) -> None:
    import httpx

    base = os.environ.get("PRIMITIVE_API_BASE", "https://api.primitive.dev/v1").rstrip("/")
    token = os.environ.get("PRIMITIVE_AUTH_TOKEN") or os.environ.get("PRIMITIVE_API_KEY")
    if not token:
        raise RuntimeError("set PRIMITIVE_AUTH_TOKEN or PRIMITIVE_API_KEY")
    quoted = urllib.parse.quote(message_id, safe="")
    resp = httpx.post(
        f"{base}/emails/{quoted}/reply",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"body_text": body},
        timeout=30,
    )
    resp.raise_for_status()


def main(argv: list[str] | None = None) -> int:
    load_env(pathlib.Path(__file__).resolve().parent / "cc.env")

    parser = argparse.ArgumentParser(description="Reply to a cc-daemon email task.")
    parser.add_argument("--message-id", required=True, help="originating message/email id")
    parser.add_argument("--body", default=None, help="reply text (default: read stdin)")
    args = parser.parse_args(argv)

    body = (args.body if args.body is not None else sys.stdin.read()).strip()
    if not body:
        print("cc-reply: refusing to send an empty reply", file=sys.stderr)
        return 2

    provider = os.environ.get("CC_MAIL_PROVIDER", "agentmail").strip().lower()
    try:
        if provider == "primitive":
            reply_primitive(args.message_id, body)
        else:
            reply_agentmail(args.message_id, body)
    except Exception as exc:  # surface a clear message to the dispatched session
        print(f"cc-reply: failed to send reply: {exc}", file=sys.stderr)
        return 1

    print(f"cc-reply: replied on {args.message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
