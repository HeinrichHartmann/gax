#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Spawn a profiled claude agent in a new cmux tab of the current workspace.

Wraps scripts/spawn.py with the cmux scaffolding:

1. Opens a new terminal tab (surface) in the current cmux workspace.
2. Types the spawn.py command into it and presses enter.
3. Waits for the claude banner, then sends a kickoff message so the
   interactive session starts working instead of idling at the prompt.

Usage:
    ./scripts/cspawn.py --model sonnet --profile worker --beads "gax-sy6 gax-qo8"
    ./scripts/cspawn.py --profile reviewer --extra-prompt "Review bead gax-1gc. Branch: worker/99624."
"""

import argparse
import shlex
import subprocess
import sys
import time
from pathlib import Path


def sh(*cmd: str) -> str:
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True
    ).stdout.strip()


def cmux(*args: str) -> str:
    return sh("cmux", *args)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="opus", help="claude model (default: opus)")
    ap.add_argument("--profile", required=True, help="profile name or path (see spawn.py)")
    ap.add_argument("--beads", default="", help="bead IDs or label to scope the agent to")
    ap.add_argument("--extra-prompt", default="", help="appended to the system prompt")
    ap.add_argument(
        "--kickoff",
        default="go! Follow your session scope.",
        help="first message sent to the interactive claude session",
    )
    ap.add_argument(
        "--timeout", type=int, default=30, help="seconds to wait for claude banner"
    )
    ap.add_argument(
        "--workspace",
        default="",
        help="cmux workspace ref (e.g. workspace:4); default: the SELECTED "
        "workspace from list-workspaces (current-workspace resolves to the "
        "caller's context, which is wrong when invoked from an agent shell)",
    )
    args = ap.parse_args()

    repo = Path(sh("git", "rev-parse", "--show-toplevel"))
    if args.workspace:
        workspace = args.workspace
    else:
        # Selected workspace = the one the user is looking at
        selected = [
            line for line in cmux("list-workspaces").splitlines()
            if "[selected]" in line
        ]
        if not selected:
            sys.exit("error: no selected workspace found; pass --workspace")
        workspace = selected[0].split()[1] if selected[0].startswith("*") else selected[0].split()[0]

    # 1. New tab (surface) in the current workspace
    out = cmux("new-surface", "--type", "terminal", "--workspace", workspace)
    # "OK surface:39 pane:4 workspace:4" -> surface:39
    surface = next(tok for tok in out.split() if tok.startswith("surface:"))

    # 2. Compose and send the spawn command
    spawn_cmd = [
        "cd", str(repo), "&&", "./scripts/spawn.py",
        "--model", args.model,
        "--profile", args.profile,
    ]
    if args.beads:
        spawn_cmd += ["--beads", args.beads]
    if args.extra_prompt:
        spawn_cmd += ["--extra-prompt", args.extra_prompt]
    # shlex-quote everything except the shell operators
    cmd_str = " ".join(
        tok if tok in ("cd", "&&") else shlex.quote(tok) for tok in spawn_cmd
    )

    cmux("send", "--surface", surface, "--workspace", workspace, cmd_str)
    cmux("send-key", "--surface", surface, "--workspace", workspace, "enter")

    # 3. Wait for the claude banner, then send the kickoff
    deadline = time.time() + args.timeout
    ready = False
    while time.time() < deadline:
        time.sleep(2)
        try:
            screen = cmux(
                "read-screen", "--surface", surface,
                "--workspace", workspace, "--lines", "20",
            )
        except subprocess.CalledProcessError:
            continue
        if "Claude Code" in screen:
            ready = True
            break

    if not ready:
        sys.exit(
            f"error: claude banner not seen on {surface} within {args.timeout}s — "
            f"check the tab manually; kickoff NOT sent"
        )

    time.sleep(2)  # let the input box settle
    cmux("send", "--surface", surface, "--workspace", workspace, args.kickoff)
    time.sleep(1)
    cmux("send-key", "--surface", surface, "--workspace", workspace, "enter")

    title = f"{Path(args.profile).stem}: {args.beads or 'ready'}"
    cmux("rename-tab", "--surface", surface, "--workspace", workspace, title)

    print(f"spawned:  {surface} in {workspace}")
    print(f"scope:    {args.beads or 'bd ready'}")
    print(f"kickoff:  {args.kickoff}")


if __name__ == "__main__":
    main()
