#!/usr/bin/env python3
"""Configure cmux so the launchd daemon can drive it.

cmux's RPC socket defaults to `automation.socketControlMode = "cmuxOnly"`, which
only accepts connections from processes started *inside* cmux. The daemon runs
under launchd and is NOT a cmux child, so every `surface.create` is rejected
("Failed to write to socket (Broken pipe)" / "Access denied - only processes
started inside cmux can connect"). The same call succeeds from an interactive
shell only because that shell is a cmux child -- which is why this is so easy to
miss.

This script sets `automation.socketControlMode` in ~/.config/cmux/cmux.json to a
mode that allows the daemon in:

  allowAll  - any local process running as you can drive cmux (no password).
              The socket is user-only, so the marginal risk is low. Simplest.
  password  - the daemon must present CMUX_SOCKET_PASSWORD; set the same value
              under automation.socketPassword here and in cc.env.

IMPORTANT: socketControlMode is read at app launch. `cmux reload-config` does
NOT apply it and there is no live RPC to set it -- you MUST fully restart cmux
(Quit + reopen) after running this.

Usage:
  python3 setup_cmux.py [--mode allowAll|password] [--password VALUE] [--config PATH]
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import shutil
import subprocess
import sys

DEFAULT_CONFIG = pathlib.Path.home() / ".config" / "cmux" / "cmux.json"
CMUX_BINS = [
    "/Applications/cmux.app/Contents/Resources/bin/cmux",
    shutil.which("cmux") or "",
]


def backup(path: pathlib.Path) -> pathlib.Path:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = path.with_name(f"{path.name}.{ts}.bak")
    shutil.copy2(path, dst)
    return dst


def set_mode(src: str, mode: str, password: str | None) -> str:
    """Set automation.socketControlMode (and optionally socketPassword).

    Targeted edits keep any existing comments/settings intact. Handles three
    cases: an existing uncommented socketControlMode, an existing uncommented
    automation block, or no automation block at all.
    """
    pw_json = None
    if password is not None:
        # JSON-encode the password value safely.
        import json
        pw_json = json.dumps(password)

    # Case 1: an uncommented socketControlMode already exists -> replace value.
    scm_re = re.compile(r'(^\s*"socketControlMode"\s*:\s*)"[^"]*"', re.MULTILINE)
    if scm_re.search(src):
        src = scm_re.sub(rf'\g<1>"{mode}"', src, count=1)
        if pw_json is not None:
            sp_re = re.compile(r'(^\s*"socketPassword"\s*:\s*)("[^"]*"|null)', re.MULTILINE)
            if sp_re.search(src):
                src = sp_re.sub(rf'\g<1>{pw_json}', src, count=1)
        return src

    block_lines = [f'    "socketControlMode": "{mode}"']
    if pw_json is not None:
        block_lines.append(f'    "socketPassword": {pw_json}')
    block_body = ",\n".join(block_lines)

    # Case 2: an uncommented automation block exists -> inject keys after `{`.
    auto_re = re.compile(r'(^\s*"automation"\s*:\s*\{)', re.MULTILINE)
    if auto_re.search(src):
        return auto_re.sub(rf'\g<1>\n{block_body},', src, count=1)

    # Case 3: no automation block -> insert a real one after the opening brace.
    automation = (
        '\n  // Added by agentmail-to-claude-code setup_cmux.py: lets the launchd\n'
        '  // daemon (not a cmux child) drive cmux. Restart cmux to apply.\n'
        '  "automation": {\n'
        f'{block_body}\n'
        '  },\n'
    )
    brace = re.compile(r'\{')
    m = brace.search(src)
    if not m:
        raise SystemExit("config does not look like JSON(C); aborting")
    return src[: m.end()] + automation + src[m.end():]


def validate() -> None:
    for b in CMUX_BINS:
        if b and pathlib.Path(b).exists():
            try:
                out = subprocess.run(
                    [b, "config", "check"], capture_output=True, text=True, timeout=15
                )
                print(out.stdout.strip() or out.stderr.strip())
            except Exception as e:
                print(f"(could not run `cmux config check`: {e})")
            return


def main() -> None:
    ap = argparse.ArgumentParser(description="Configure cmux socket control for the daemon.")
    ap.add_argument("--mode", default="allowAll", choices=["allowAll", "password", "automation"])
    ap.add_argument("--password", default=None, help="socket password (password mode only)")
    ap.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG)
    args = ap.parse_args()

    if args.mode == "password" and not args.password:
        ap.error("--mode password requires --password VALUE (also set CMUX_SOCKET_PASSWORD in cc.env)")

    cfg = args.config
    if not cfg.exists():
        print(f"{cfg} not found. Launch cmux once so it writes the template, then re-run.")
        sys.exit(1)

    bak = backup(cfg)
    print(f"backed up -> {bak}")
    new = set_mode(cfg.read_text(), args.mode, args.password)
    cfg.write_text(new)
    cfg.chmod(0o600)
    print(f"set automation.socketControlMode = {args.mode!r} in {cfg}")
    validate()
    print("\nNEXT: fully restart cmux (Quit + reopen) -- the mode is only read at")
    print("app launch. Verify with:  cmux capabilities | grep access_mode")


if __name__ == "__main__":
    main()
