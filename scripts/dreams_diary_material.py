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

プロファイル未指定なら、``HERMES_HOME``（cron / 内蔵ターミナル）、Hermes の context-local home、
``HERMES_SESSION_PROFILE``、Hermes Desktop の ``active-profile.json``、最後に更新された ``state.db`` の順で解決する。
日付未指定の validate は「今日」と「昨日」のうち最新の日記ファイルを対象にする。
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import re
import runpy
import sys
from pathlib import Path
from typing import List, Optional

_PATH_HELPER = Path(__file__).resolve().with_name("dreams_diary_paths.py")
if not _PATH_HELPER.is_file():
    raise SystemExit(f"Dreams Diary path helper is missing: {_PATH_HELPER}")
_PATH_SPEC = importlib.util.spec_from_file_location("_dreams_diary_paths", _PATH_HELPER)
if _PATH_SPEC is None or _PATH_SPEC.loader is None:
    raise ImportError(f"Dreams Diary path helper is missing: {_PATH_HELPER}")
_PATH_MODULE = importlib.util.module_from_spec(_PATH_SPEC)
_PATH_SPEC.loader.exec_module(_PATH_MODULE)
_context_home = _PATH_MODULE.context_home
_hermes_root = _PATH_MODULE.hermes_root
_profile_home = _PATH_MODULE.profile_home

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _plugin_cli() -> Path:
    root = _hermes_root()
    candidates: list[Path] = []
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        candidates.append(Path(hermes_home).expanduser() / "plugins" / "dreams-diary" / "cli.py")
    context_home = _context_home()
    if context_home is not None:
        candidates.append(context_home / "plugins" / "dreams-diary" / "cli.py")
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    if profile:
        try:
            profile_home = _profile_home(root, profile)
        except ValueError:
            profile_home = None
        if profile_home is not None:
            candidates.append(profile_home / "plugins" / "dreams-diary" / "cli.py")
    candidates.append(root / "plugins" / "dreams-diary" / "cli.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "Dreams Diary CLI not found in the active profile or <hermes root>/plugins; "
        "install the dreams-diary plugin before running this cron bridge."
    )


def _plugin_core(cli: Path) -> dict:
    """CLI と同じインストール先の core を読み込む。"""
    path = cli.parent / "main.py"
    if not path.is_file():
        raise SystemExit(f"Dreams Diary core not found next to CLI: {path}")
    plugin_root = str(cli.parent)
    added_path = plugin_root not in sys.path
    if added_path:
        sys.path.insert(0, plugin_root)
    try:
        return runpy.run_path(str(path), run_name="dreams_diary_core")
    finally:
        if added_path:
            sys.path.remove(plugin_root)


def _profiles_root() -> Path:
    return _hermes_root() / "profiles"


def _existing_profile(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    try:
        _profile_home(_hermes_root(), name)
    except ValueError:
        return ""
    return name


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
        if not _existing_profile(entry.name):
            continue
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
    context = _context_home()
    name = name or (_profile_from_home(str(context)) if context is not None else "")
    if name:
        return name
    session_profile = os.environ.get("HERMES_SESSION_PROFILE", "").strip()
    if session_profile:
        name = _existing_profile(session_profile)
        if not name:
            raise SystemExit(f"指定されたプロファイルが存在しません: {session_profile}")
        return name
    name = _desktop_active_profile()
    if name:
        return name
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    if profile:
        name = _existing_profile(profile)
        if not name:
            raise SystemExit(f"指定されたプロファイルが存在しません: {profile}")
        return name
    return _most_recent_profile()


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
        core = _plugin_core(cli)
        target = core["newest_diary"](home)
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
