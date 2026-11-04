#!/usr/bin/env python3
"""dreams-diary CLI — cron の `script` と手元の確認のための入口。

    python3 cli.py [--profile NAME] material [YYYY-MM-DD]   # 素材を stdout に出す
    python3 cli.py [--profile NAME] write    [YYYY-MM-DD]   # 節見出しだけの日記を作る
    python3 cli.py [--profile NAME] validate [YYYY-MM-DD]   # 検査（JSON）
    python3 cli.py [--profile NAME] validate --path FILE    # ファイル指定で検査
    python3 cli.py [--profile NAME] audit   [--days N]      # 直近 N 本をまとめて検査
    python3 cli.py [--profile NAME] index                   # INDEX.md 再生成
    python3 cli.py prompt                                   # cron プロンプトを描画して出す
    python3 cli.py prompt --check                           # 保存済みプロンプトとの差分（読み取りのみ）
    python3 cli.py prompt --apply --profile NAME            # 描画結果で置き換え、読み直して検証
    python3 cli.py install --profile NAME [--schedule EXPR] # 新規導入・再セットアップ（冪等）
    python3 cli.py sync --check                             # 全プロファイルのスクリプト・プロンプト点検
    python3 cli.py sync --all                               # 全プロファイルのスクリプト・プロンプト一括同期

プロファイルを特定できない文脈（HERMES_HOME もコンテキストローカル home も無い）では
~/.hermes/active_profile — つまり「最後に触ったプロファイル」— に落ちる。別プロファイルの
日記を黙って読んだり書いたりしないよう、その場合は警告を出して続行する。
`prompt` はプロファイル非依存なので、この解決も警告も行わない。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main as core  # noqa: E402

UNRELIABLE_SOURCES = {"active_profile", "root"}


class _Args(NamedTuple):
    profile: Optional[str]
    profiles: List[str]
    cmd: str
    day: Optional[str]
    days: int
    path: Optional[str]
    mode: Optional[str]  # "check" / "apply" / None
    all_profiles: bool
    schedule: str


def _parse(argv: List[str]) -> _Args:
    """(--profile, command, date, days, path, mode) を取り出す。フラグは前後どちらでもよい。"""
    profiles: List[str] = []
    path: Optional[str] = None
    days = 7
    mode: Optional[str] = None
    all_profiles = False
    schedule = "30 23 * * *"
    rest: List[str] = []
    args = list(argv)
    while args:
        item = args.pop(0)
        if item in ("-p", "--profile"):
            if not args:
                raise ValueError("--profile にはプロファイル名が必要です")
            profiles.append(args.pop(0))
            continue
        if item.startswith("--profile="):
            profiles.append(item.split("=", 1)[1])
            continue
        if item == "--all":
            all_profiles = True
            continue
        if item == "--schedule":
            if not args:
                raise ValueError("--schedule にはスケジュール式が必要です")
            schedule = args.pop(0)
            continue
        if item.startswith("--schedule="):
            schedule = item.split("=", 1)[1]
            continue
        if item == "--path":
            if not args:
                raise ValueError("--path には日記ファイルのパスが必要です")
            path = args.pop(0)
            continue
        if item.startswith("--path="):
            path = item.split("=", 1)[1]
            continue
        if item == "--days":
            if not args or not args[0].isdigit():
                raise ValueError("--days には正の整数が必要です")
            days = int(args.pop(0))
            continue
        if item.startswith("--days="):
            value = item.split("=", 1)[1]
            if not value.isdigit():
                raise ValueError("--days には正の整数が必要です")
            days = int(value)
            continue
        if item in ("--check", "--apply"):
            if mode is not None and mode != item.lstrip("-"):
                raise ValueError("--check と --apply は同時に使えません")
            mode = item.lstrip("-")
            continue
        rest.append(item)
    cmd = rest[0] if rest else "material"
    day = rest[1] if len(rest) > 1 else None
    return _Args(
        profiles[-1] if profiles else None,
        profiles,
        cmd,
        day,
        days,
        path,
        mode,
        all_profiles,
        schedule,
    )


def _warn_unscoped(home: Path, source: str) -> None:
    if source in UNRELIABLE_SOURCES or core.is_root_home(home):
        print(
            f"警告: プロファイルを特定できませんでした（解決経路: {source}）。"
            f"{home} を使います。--profile <name> か HERMES_HOME を指定してください。",
            file=sys.stderr,
        )


def _prompt(args: _Args) -> int:
    """cron プロンプトの描画・差分・適用。プロファイル解決は行わない。"""
    try:
        rendered = core.render_cron_prompt()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.mode == "check":
        try:
            result = core.check_cron_prompt()
        except Exception as exc:  # noqa: BLE001
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(core.json_dumps(result))
        return 0 if result.get("ok") else 1

    if args.mode == "apply":
        targets = list(dict.fromkeys(args.profiles))
        if not targets:
            print(
                "error: --apply には --profile <name> が必要です（複数は列挙する）",
                file=sys.stderr,
            )
            return 2
        results = []
        for name in targets:
            try:
                result = core.apply_cron_prompt(name)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "applied": []}
            results.append(dict(profile=name, **result))
        ok = all(item.get("ok") for item in results)
        print(
            core.json_dumps(
                {"ok": ok, "template_sha": core._text_sha(rendered), "results": results}
            )
        )
        return 0 if ok else 1

    print(rendered)
    return 0


def _install(args: _Args) -> int:
    """新規プロファイル導入または再セットアップ（冪等）。"""
    if not args.profile:
        print("error: install には --profile <name> が必要です", file=sys.stderr)
        return 2
    try:
        res = core.install_profile(args.profile, schedule=args.schedule)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(core.json_dumps(res))
    return 0 if res.get("ok") else 1


def _sync(args: _Args) -> int:
    """スクリプトおよび cron プロンプトの一括点検・同期。"""
    check_only = (args.mode == "check")
    if not check_only and not args.all_profiles and not args.profiles:
        print("error: sync には --all または --profile <name> が必要です", file=sys.stderr)
        return 2
    targets = None if args.all_profiles else (args.profiles if args.profiles else None)
    try:
        res = core.sync_all(check_only=check_only, profiles=targets)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(core.json_dumps(res))
    return 0 if res.get("ok") else 1


def main(argv: List[str]) -> int:
    if any(arg in ("--help", "-h") for arg in argv):
        print(__doc__.strip())
        return 0
    try:
        args = _parse(argv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.mode and args.cmd not in ("prompt", "sync"):
        print(f"error: --{args.mode} は prompt または sync でのみ使えます（指定: {args.cmd}）", file=sys.stderr)
        return 2
    if args.cmd == "prompt":
        return _prompt(args)
    if args.cmd == "install":
        return _install(args)
    if args.cmd == "sync":
        return _sync(args)
    try:
        home, source = core.resolve_profile_home_source(args.profile)
    except Exception as exc:  # noqa: BLE001 - invalid explicit profile is a usage error
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    _warn_unscoped(home, source)
    try:
        if args.cmd == "material":
            result = core.collect_material(args.day, home=home)
            print(result["material"])
        elif args.cmd == "write":
            print(core.json_dumps(core.write_skeleton(args.day, home=home)))
        elif args.cmd == "validate":
            res = core.validate_diary(args.day, path=args.path, home=home)
            print(core.json_dumps(res))
            return 0 if res.get("ok") else 1
        elif args.cmd == "audit":
            res = core.audit_diaries(home=home, days=args.days)
            print(core.json_dumps(res))
            return 0 if res.get("ok") else 1
        elif args.cmd == "index":
            res = core.build_index(home=home)
            print(core.json_dumps(res))
            return 0 if res.get("ok") else 1
        else:
            print(f"unknown command: {args.cmd}", file=sys.stderr)
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
