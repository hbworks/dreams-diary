"""Profile-local cron bridge to the shared Dreams Diary CLI.

Hermes cron only runs scripts resolved inside the active profile's scripts/ directory.
This bridge keeps the plugin CLI as the single source of truth without copying its
implementation. The sibling ``dreams_diary_validate.py`` is a thin shim over ``run()``
so the two entry points cannot drift apart.

Manual use is supported too:

    python3 dreams_diary_validate.py                # 開いているプロファイルの直近の日記
    python3 dreams_diary_validate.py 2026-10-22     # 日付を指定
    python3 dreams_diary_validate.py --profile buddy
    python3 dreams_diary_material.py 2026-10-22     # 素材を stdout へ

プロファイル未指定なら、``HERMES_HOME``（cron / 内蔵ターミナル）、``HERMES_SESSION_PROFILE``、
Hermes Desktop の ``active-profile.json``、最後に更新された ``state.db`` の順で解決する。
日付未指定の validate は「今日」と「昨日」のうち最新の日記ファイルを対象にする。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import runpy
import sys
from pathlib import Path
from typing import List, Optional, Tuple

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DIARY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(-\d+)?\.md$")


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


def _profiles_root() -> Path:
    return Path.home() / ".hermes" / "profiles"


def _existing_profile(name: str) -> str:
    name = (name or "").strip()
    return name if name and (_profiles_root() / name).is_dir() else ""


def _profile_from_home(raw: str) -> str:
    """HERMES_HOME が <root>/profiles/<name> を指しているときだけ名前を返す。"""
    if not raw:
        return ""
    home = Path(os.path.expanduser(raw))
    try:
        if home.parent == _profiles_root():
            return _existing_profile(home.name)
    except OSError:
        return ""
    return ""


def _desktop_active_profile() -> str:
    """Hermes Desktop が最後に開いていたプロファイル（macOS のアプリ状態）。"""
    marker = (
        Path.home()
        / "Library"
        / "Application Support"
        / "Hermes"
        / "active-profile.json"
    )
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return _existing_profile(str(data.get("profile") or ""))


def _most_recent_profile() -> str:
    """最後に state.db が更新されたプロファイル。アプリ状態が無い環境の代替。"""
    best_mtime = 0.0
    best_name = ""
    try:
        entries = list(_profiles_root().iterdir())
    except OSError:
        return ""
    for entry in entries:
        try:
            mtime = (entry / "state.db").stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime, best_name = mtime, entry.name
    return best_name


def pick_profile(argv: List[str]) -> str:
    """手動実行時のプロファイル名。明示指定があるときは空（CLI に任せる）。"""
    for arg in argv:
        if arg in ("-p", "--profile") or arg.startswith("--profile="):
            return ""
    name = _profile_from_home(os.environ.get("HERMES_HOME", ""))
    name = name or _existing_profile(os.environ.get("HERMES_SESSION_PROFILE", ""))
    name = name or _desktop_active_profile()
    name = name or _existing_profile(os.environ.get("HERMES_PROFILE", ""))
    return name or _most_recent_profile()


def newest_diary(home: Optional[Path], today: Optional[datetime.date] = None) -> Optional[Path]:
    """「今日」と「昨日」の日記のうち、いちばん新しい1本（-2 / -3 も含む）。"""
    if home is None:
        return None
    day = today or datetime.date.today()
    window = {
        day.isoformat(),
        (day - datetime.timedelta(days=1)).isoformat(),
    }
    best: Optional[Tuple[Tuple[str, int], Path]] = None
    try:
        entries = list((home / "dreams").iterdir())
    except OSError:
        return None
    for entry in entries:
        match = DIARY_RE.match(entry.name)
        if not match or match.group(1) not in window:
            continue
        variant = int(match.group(2)[1:]) if match.group(2) else 0
        key = (match.group(1), variant)
        if best is None or key > best[0]:
            best = (key, entry)
    return best[1] if best else None


def _has_day_or_path(argv: List[str]) -> bool:
    return any(
        DATE_RE.match(arg) or arg == "--path" or arg.startswith("--path=")
        for arg in argv
    )


def run(command: str = "material") -> None:
    cli = _plugin_cli()
    args = list(sys.argv[1:])
    profile = pick_profile(args)
    if profile:
        args.extend(["--profile", profile])
    if command == "validate" and not _has_day_or_path(args):
        home = _profiles_root() / profile if profile else None
        target = newest_diary(home)
        if target is not None:
            args.extend(["--path", str(target)])
        else:
            args.append(datetime.date.today().isoformat())
    sys.argv = [str(cli), command, *args]
    runpy.run_path(str(cli), run_name="__main__")


def main() -> None:
    run("material")


if __name__ == "__main__":
    main()
