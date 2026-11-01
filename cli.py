#!/usr/bin/env python3
"""dreams-diary CLI — cron の `script` から呼ぶための入口。

    python3 cli.py material [YYYY-MM-DD]   # 素材を stdout に出す（cron 注入用）
    python3 cli.py write    [YYYY-MM-DD]   # 節見出しだけの日記を作る
    python3 cli.py validate [YYYY-MM-DD]   # 検査（JSON）
    python3 cli.py index                   # INDEX.md 再生成
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main as core  # noqa: E402


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "material"
    day = argv[1] if len(argv) > 1 else None
    try:
        if cmd == "material":
            result = core.collect_material(day)
            print(result["material"])
        elif cmd == "write":
            print(core.json_dumps(core.write_skeleton(day)))
        elif cmd == "validate":
            res = core.validate_diary(day)
            print(core.json_dumps(res))
            return 0 if res.get("ok") else 1
        elif cmd == "index":
            print(core.json_dumps(core.build_index()))
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
