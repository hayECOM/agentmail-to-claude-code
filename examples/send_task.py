#!/usr/bin/env python3
"""Send a task email into the watched inbox via the AgentMail REST API.

The daemon reacts to inbound mail, so anything that can send a message to the
watched inbox from an allowlisted address can hand Claude Code a task. This is
the companion "sender" side: an agent, cron job, or script triggers a session
by sending mail.

An optional image is attached as base64 that is read and encoded in-process,
so when an agent shells out to this script the raw image bytes never have to
pass through its LLM context.

Usage:
  send_task.py <to_inbox> <subject> <body> [image_path]

Environment:
  AGENTMAIL_API_KEY     API key for the SENDING inbox
  AGENTMAIL_SEND_INBOX  the inbox to send FROM (must be in the daemon's
                        CC_ALLOWED_FROM, e.g. your-agent@agentmail.to)
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.agentmail.to/v0"
API_KEY = os.environ["AGENTMAIL_API_KEY"]
FROM_INBOX = os.environ["AGENTMAIL_SEND_INBOX"]


def send(to: str, subject: str, body: str, image_path: str | None = None) -> None:
    payload = {"to": [to], "subject": subject, "text": body}

    if image_path and os.path.exists(image_path):
        ext = image_path.rsplit(".", 1)[-1].lower()
        with open(image_path, "rb") as f:
            payload["attachments"] = [{
                "filename": f"image.{ext}",
                "content": base64.b64encode(f.read()).decode(),
            }]

    encoded_inbox = urllib.parse.quote(FROM_INBOX, safe="")
    url = f"{API_BASE}/inboxes/{encoded_inbox}/messages/send"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST"
    )
    req.add_header("Authorization", f"Bearer {API_KEY}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        print(json.dumps({"status": "sent", "messageId": result.get("message_id", "")}, indent=2))
    except urllib.error.HTTPError as e:
        print(json.dumps({"status": "error", "code": e.code, "body": e.read().decode()}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: send_task.py <to_inbox> <subject> <body> [image_path]")
        sys.exit(1)
    send(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
