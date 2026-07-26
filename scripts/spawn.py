#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Spawn a profiled claude agent in a forked workspace.

Usage:
    ./scripts/spawn.py --profile worker --beads "gax-cvi.1 gax-75t"
    ./scripts/spawn.py --profile reviewer --extra-prompt "Branch: worker/82092"
    ./scripts/spawn.py --profile architect --no-fork

- Resolves the profile from .agents/profiles/<name>.md and passes it as
  the claude system prompt (there is no --profile flag on claude itself).
- Forks the workspace: creates a git worktree ../<repo>-<profile>-<id>
  on branch <profile>/<id> from main, and starts the agent inside it.
  Use --no-fork to run in the current checkout (architect, experiments).
- Sets the cmux/terminal tab title via OSC escape.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="opus", help="claude model (default: opus)")
    ap.add_argument(
        "--profile",
        required=True,
        help="profile name in .agents/profiles/ (worker, reviewer, architect) or a path",
    )
    ap.add_argument(
        "--beads",
        default="",
        help='bead IDs or label to scope the agent to, e.g. "gax-cvi.1 gax-75t" or "gdoc"',
    )
    ap.add_argument("--extra-prompt", default="", help="appended to the system prompt")
    ap.add_argument("--title", default="", help="cmux tab title (default: <profile>: <beads>)")
    ap.add_argument(
        "--no-fork",
        action="store_true",
        help="run in the current checkout instead of a fresh worktree",
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

    # Fork workspace
    cwd = repo
    workspace_note = ""
    if args.no_fork:
        workspace_note = (
            "You are running in the MAIN checkout (no worktree). "
            "Do not create a worktree."
        )
    else:
        agent_id = secrets.token_hex(3)
        branch = f"{profile_name}/{agent_id}"
        worktree = repo.parent / f"{repo.name}-{profile_name}-{agent_id}"
        sh("git", "worktree", "add", str(worktree), "-b", branch, "main", cwd=repo)
        cwd = worktree
        # Grant edit/test/git permissions scoped to this worktree only
        claude_dir = worktree / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "settings.local.json").write_text(
            json.dumps(WORKTREE_PERMISSIONS, indent=2) + "\n", encoding="utf-8"
        )
        workspace_note = (
            f"Your worktree is already created: {worktree} on branch {branch} "
            f"(forked from main). You are running inside it. Skip any worktree "
            f"setup steps from the profile. Never modify the main checkout."
        )

    # Compose scope
    parts = [system_prompt, "", "## Session scope", workspace_note]
    if args.beads:
        parts.append(
            f"Work ONLY on these beads/labels: {args.beads}. "
            f"Inspect them with `bd show <id>` (or `bd list -l <label>`) before starting."
        )
    else:
        parts.append("Find work with `bd ready`.")
    if args.extra_prompt:
        parts.append(args.extra_prompt)
    full_prompt = "\n".join(parts)

    # cmux tab title
    title = args.title or f"{profile_name}: {args.beads or 'ready'}"
    try:
        subprocess.run(["cmux", "rename-tab", title], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        # Fallback to OSC escape for plain terminals
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()

    print(f"profile:   {profile_name} ({profile_path.relative_to(repo)})")
    print(f"model:     {args.model}")
    print(f"workspace: {cwd}")
    print(f"scope:     {args.beads or 'bd ready'}")

    os.chdir(cwd)
    os.execvp(
        "claude",
        ["claude", "--model", args.model, "--system-prompt", full_prompt],
    )


if __name__ == "__main__":
    main()
