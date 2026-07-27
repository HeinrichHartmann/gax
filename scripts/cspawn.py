#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Spawn a profiled claude agent in a new cmux tab (self-contained).

1. Resolves the profile from .agents/profiles/<name>.md (there is no
   --profile flag on claude itself; the profile is passed as the system
   prompt).
2. Forks the workspace: creates a git worktree ../<repo>-<profile>-<id>
   on branch <profile>/<id> from main, allows its .envrc, and grants
   scoped permissions via .claude/settings.local.json.
3. Opens a new terminal tab (surface) in the current cmux workspace and
   starts claude inside the worktree.
4. Waits for the claude banner, then sends a kickoff message so the
   interactive session starts working instead of idling at the prompt.

Usage:
    ./scripts/cspawn.py --model sonnet --profile worker --beads "gax-sy6 gax-qo8"
    ./scripts/cspawn.py --profile worker --beads "gdoc"   # by label
"""

import argparse
import json
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

# Permissions granted to forked agent worktrees via .claude/settings.local.json
# (per-checkout, never committed — dies with the worktree).
WORKTREE_PERMISSIONS = {
    "permissions": {
        "allow": [
            "Edit",
            "Write",
            "Bash(direnv exec:*)",
            "Bash(git status:*)",
            "Bash(git diff:*)",
            "Bash(git log:*)",
            "Bash(git add:*)",
            "Bash(git commit:*)",
            "Bash(git rebase main)",
            "Bash(bd:*)",
        ]
    }
}


def sh(*cmd: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        cmd, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def cmux(*args: str) -> str:
    return sh("cmux", *args)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="opus", help="claude model (default: opus)")
    ap.add_argument(
        "--profile",
        required=True,
        help="profile name in .agents/profiles/ (worker, architect) or a path",
    )
    ap.add_argument(
        "--beads",
        default="",
        help='bead IDs or label to scope the agent to, e.g. "gax-cvi.1 gax-75t" or "gdoc"',
    )
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

    # Resolve profile
    profile_path = Path(args.profile)
    if not profile_path.exists():
        profile_path = repo / ".agents" / "profiles" / f"{args.profile}.md"
    if not profile_path.exists():
        sys.exit(f"error: profile not found: {args.profile} ({profile_path})")
    profile_name = profile_path.stem
    system_prompt = profile_path.read_text(encoding="utf-8")

    # Fork workspace: worktree on a fresh branch from main
    agent_id = secrets.token_hex(3)
    branch = f"{profile_name}/{agent_id}"
    worktree = repo.parent / f"{repo.name}-{profile_name}-{agent_id}"
    sh("git", "worktree", "add", str(worktree), "-b", branch, "main", cwd=repo)
    # Allow the worktree's .envrc — otherwise direnv silently falls back
    # to a parent .envrc and agents run against the wrong environment.
    sh("direnv", "allow", str(worktree))
    # Grant edit/test/git permissions scoped to this worktree only
    claude_dir = worktree / ".claude"
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / "settings.local.json").write_text(
        json.dumps(WORKTREE_PERMISSIONS, indent=2) + "\n", encoding="utf-8"
    )

    # Compose scope and write the full prompt into the worktree (dies with it).
    # Passing it via a file avoids shell-quoting a multi-KB argument through
    # the cmux send pipeline.
    parts = [
        system_prompt,
        "",
        "## Session scope",
        f"Your worktree is already created: {worktree} on branch {branch} "
        f"(forked from main). You are running inside it. Skip any worktree "
        f"setup steps from the profile. Never modify the main checkout.",
    ]
    if args.beads:
        parts.append(
            f"Work ONLY on these beads/labels: {args.beads}. "
            f"Inspect them with `bd show <id>` (or `bd list -l <label>`) before starting."
        )
    else:
        parts.append("Find work with `bd ready`.")
    if args.extra_prompt:
        parts.append(args.extra_prompt)
    prompt_file = claude_dir / "system-prompt.md"
    prompt_file.write_text("\n".join(parts), encoding="utf-8")

    # Resolve cmux workspace
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

    # New tab (surface) in the workspace, running claude in the worktree
    out = cmux("new-surface", "--type", "terminal", "--workspace", workspace)
    # "OK surface:39 pane:4 workspace:4" -> surface:39
    surface = next(tok for tok in out.split() if tok.startswith("surface:"))

    cmd_str = (
        f"cd {shlex.quote(str(worktree))} && "
        f"claude --model {shlex.quote(args.model)} "
        f'--system-prompt "$(cat .claude/system-prompt.md)"'
    )
    cmux("send", "--surface", surface, "--workspace", workspace, cmd_str)
    cmux("send-key", "--surface", surface, "--workspace", workspace, "enter")

    # Wait for the claude banner, then send the kickoff
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

    title = f"{profile_name}: {args.beads or 'ready'}"
    cmux("rename-tab", "--surface", surface, "--workspace", workspace, title)

    print(f"spawned:   {surface} in {workspace}")
    print(f"worktree:  {worktree} on {branch}")
    print(f"scope:     {args.beads or 'bd ready'}")
    print(f"kickoff:   {args.kickoff}")


if __name__ == "__main__":
    main()
