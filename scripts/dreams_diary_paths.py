from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional


def root_from_home(home: Path) -> Optional[Path]:
    """Return the Hermes root for a root or standard profile home."""
    if home.parent.name == "profiles":
        return home.parent.parent
    if (home / "profiles").is_dir() or home.name.startswith("."):
        return home
    return None


def profile_home(root: Path, name: str) -> Path:
    """Resolve an existing profile directory directly beneath ``profiles/``."""
    name = str(name).strip()
    if (
        not name
        or name in {".", ".."}
        or Path(name).is_absolute()
        or "/" in name
        or "\\" in name
        or ":" in name
    ):
        raise ValueError(f"不正なプロファイル名です: {name!r}")

    profiles = root / "profiles"
    profiles_root = profiles.resolve()
    candidate = profiles / name
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"指定されたプロファイルが存在しません: {name!r}") from exc
    if resolved.parent != profiles_root or not resolved.is_dir():
        raise ValueError(f"プロファイルは profiles 直下のディレクトリではありません: {name!r}")
    return candidate


def context_home() -> Optional[Path]:
    """Return Hermes' effective context-local home when called by the host."""
    try:
        import hermes_constants as hc
    except Exception:  # noqa: BLE001 - Hermes may not be importable in standalone CLI
        return None
    get_home = getattr(hc, "get_hermes_home", None)
    if not callable(get_home):
        return None
    try:
        return Path(get_home()).expanduser()
    except Exception:  # noqa: BLE001 - CLI/cron bridge can run outside Hermes
        return None


def hermes_root() -> Path:
    """Resolve the Hermes root using the same rules for the CLI and cron bridge."""
    override = os.environ.get("DREAMS_HERMES_ROOT", "").strip()
    if override:
        return Path(os.path.expanduser(override))

    candidates: List[Path] = []
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        candidates.append(Path(os.path.expanduser(env)))
    context = context_home()
    if context is not None:
        candidates.append(context)
    for candidate in candidates:
        root = root_from_home(candidate)
        if root is not None:
            return root
    return Path(os.path.expanduser("~/.hermes"))
