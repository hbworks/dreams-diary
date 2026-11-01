"""Profile-local cron bridge for validation — a shim over the material bridge.

Hermes cron only runs scripts resolved inside the active profile's scripts/ directory,
and both bridges are copied side by side. ``run()`` lives in ``dreams_diary_material.py``
so the two entry points cannot drift apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreams_diary_material import run  # noqa: E402


if __name__ == "__main__":
    run("validate")
