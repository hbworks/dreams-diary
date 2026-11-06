#!/usr/bin/env python3
"""Install the commit log hook into a git repository's .git/hooks/post-commit.

Idempotent: re-running on a repo that already has this hook is a no-op.
Refuses (exit 2) when a different post-commit hook or core.hooksPath exists,
so an existing hook is never overwritten silently.

Usage: python3 scripts/install_commit_hook.py <repo-path>
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "git_commit_log_hook.sh"
MARKER = "# dreams-diary: append one line per commit"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def install(repo: Path) -> tuple[int, str]:
    """Return (exit_code, message). 0 = installed or already current, 2 = refused."""
    if not (repo / ".git").exists():
        return 2, f"not a git repository: {repo}"

    hooks_path = _git(repo, "config", "--get", "core.hooksPath")
    if hooks_path:
        return 2, f"core.hooksPath is set ({hooks_path}); install manually"

    hooks_dir = Path(_git(repo, "rev-parse", "--git-path", "hooks"))
    if not hooks_dir.is_absolute():
        hooks_dir = repo / hooks_dir
    target = hooks_dir / "post-commit"
    wanted = SOURCE.read_text(encoding="utf-8")

    if target.exists():
        current = target.read_text(encoding="utf-8", errors="replace")
        if current == wanted:
            return 0, f"already installed: {target}"
        if MARKER not in current:
            return 2, f"a different post-commit hook exists: {target}; not overwritten"

    hooks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE, target)
    target.chmod(0o755)
    return 0, f"installed: {target}"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    code, message = install(Path(argv[1]).expanduser().resolve())
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
