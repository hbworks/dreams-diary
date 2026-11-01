#!/usr/bin/env python3
"""dreams-diary CLI — cron の `script` と手元の確認のための入口。

    python3 cli.py [--profile NAME] material [YYYY-MM-DD]   # 素材を stdout に出す
    python3 cli.py [--profile NAME] write    [YYYY-MM-DD]   # 節見出しだけの日記を作る
    python3 cli.py [--profile NAME] validate [YYYY-MM-DD]   # 検査（JSON）
    python3 cli.py [--profile NAME] index                   # INDEX.md 再生成

プロファイルを特定できない文脈（HERMES_HOME もコンテキストローカル home も無い）では
~/.hermes/active_profile — つまり「最後に触ったプロファイル」— に落ちる。別プロファイルの
日記を黙って読んだり書いたりしないよう、その場合は警告を出して続行する。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main as core  # noqa: E402

UNRELIABLE_SOURCES = {"active_profile", "root"}


def _parse(argv: List[str]) -> Tuple[Optional[str], str, Optional[str]]:
    """(--profile, command, date) を取り出す。--profile は前後どちらでもよい。"""
    profile: Optional[str] = None
    rest: List[str] = []
    args = list(argv)
    while args:
        item = args.pop(0)
        if item in ("-p", "--profile"):
            if not args:
                raise ValueError("--profile にはプロファイル名が必要です")
            profile = args.pop(0)
            continue
        if item.startswith("--profile="):
            profile = item.split("=", 1)[1]
            continue
        rest.append(item)
    cmd = rest[0] if rest else "material"
    day = rest[1] if len(rest) > 1 else None
    return profile, cmd, day


def _warn_unscoped(home: Path, source: str) -> None:
    if source in UNRELIABLE_SOURCES:
        print(
            f"警告: プロファイルを特定できませんでした（解決経路: {source}）。"
            f"{home} を使います。--profile <name> か HERMES_HOME を指定してください。",
            file=sys.stderr,
        )


def main(argv: List[str]) -> int:
    try:
        profile, cmd, day = _parse(argv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    home, source = core.resolve_profile_home_source(profile)
    _warn_unscoped(home, source)
    try:
        if cmd == "material":
            result = core.collect_material(day, home=home)
            print(result["material"])
        elif cmd == "write":
            print(core.json_dumps(core.write_skeleton(day, home=home)))
        elif cmd == "validate":
            res = core.validate_diary(day, home=home)
            print(core.json_dumps(res))
            return 0 if res.get("ok") else 1
        elif cmd == "index":
            print(core.json_dumps(core.build_index(home=home)))
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
