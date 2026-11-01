"""Profile-local cron bridge to the shared Dreams Diary validation CLI.

Hermes cron only runs scripts resolved inside the active profile's scripts/ directory.
This bridge keeps the plugin CLI as the source of truth without copying its implementation.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def _plugin_cli() -> Path:
    hermes_root = Path.home() / ".hermes"
    candidates: list[Path] = []
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "plugins" / "dreams-diary" / "cli.py")
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    if profile:
        candidates.append(
            hermes_root / "profiles" / profile / "plugins" / "dreams-diary" / "cli.py"
        )
    candidates.append(hermes_root / "plugins" / "dreams-diary" / "cli.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "Dreams Diary CLI not found in the active profile or ~/.hermes/plugins; "
        "install the dreams-diary plugin before running this cron bridge."
    )


def main() -> None:
    cli = _plugin_cli()
    sys.argv = [str(cli), "validate"]
    runpy.run_path(str(cli), run_name="__main__")


if __name__ == "__main__":
    main()
