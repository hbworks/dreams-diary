"""Dreams Diary plugin for Hermes Agent.

エージェントが自分の一日を日記として残すための道具を提供する。
プラグインは素材収集・雛形・検査だけを行い、文章はエージェントが書く。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict

try:  # package import
    from . import main as core
except ImportError:  # plugin dir on sys.path
    import main as core  # type: ignore

logger = logging.getLogger(__name__)

TOOLSET = "dreams_diary"

_MATERIAL_SCHEMA = {
    "name": "dreams_material",
    "description": (
        "その日の日記素材（会話・使用ツール・git コミット・memory 追加分）を Markdown で返す。"
        "プロファイルごとの state.db / memory.db と ~/Dev/git 配下のコミットを読む。"
        "同じ内容は <profile>/dreams/_material/<date>.md にも保存される。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD。省略時は今日。"},
            "max_chars": {
                "type": "integer",
                "description": "返す素材の上限文字数。既定 14000。",
                "default": core.DEFAULT_MAX_MATERIAL,
            },
        },
    },
}

_WRITE_SCHEMA = {
    "name": "dreams_write",
    "description": (
        "その日の日記ファイルを、frontmatter と6つの節見出しだけの状態で用意する。"
        "本文は書かない。エージェントが dreams_material を読んで自分の声で各節を埋める。"
        "sessions / messages には素材で観測した実測値を渡せる（省略時は空欄のまま残る。"
        "推測で 0 を入れない）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD。省略時は今日。"},
            "author": {"type": "string", "description": "frontmatter に書く作者名。省略時はプロファイル名。"},
            "force": {"type": "boolean", "description": "既存ファイルを上書きする。既定 false。", "default": False},
            "sessions": {
                "type": "integer",
                "minimum": 0,
                "description": "その日のセッション数。素材に書かれた実測値だけを渡す。省略時は空欄。",
            },
            "messages": {
                "type": "integer",
                "minimum": 0,
                "description": "その日のメッセージ数。素材に書かれた実測値だけを渡す。省略時は空欄。",
            },
        },
    },
}

_VALIDATE_SCHEMA = {
    "name": "dreams_validate",
    "description": (
        "書いた日記を機械検査する。6節の有無、「あなた」の混入、字数、"
        "そしてコミットハッシュが ~/Dev/git 配下のどれかのリポジトリに実在するかを確認し、"
        "捏造の疑いを error として返す。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD。省略時は今日。"},
            "path": {"type": "string", "description": "日記ファイルの絶対パス。date より優先。"},
        },
    },
}

_INDEX_SCHEMA = {
    "name": "dreams_index",
    "description": "dreams/ 配下の日記から INDEX.md（各日の申し送り抜粋）を作り直す。",
    "parameters": {"type": "object", "properties": {}},
}


def _material_handler(args: Dict[str, Any], home=None, **_kw: Any) -> str:
    result = core.collect_material(
        day=args.get("date"),
        home=home,
        max_chars=int(args.get("max_chars") or core.DEFAULT_MAX_MATERIAL),
    )
    return result["material"] + f"\n\n（素材ファイル: {result['material_path']}）"


def _write_handler(args: Dict[str, Any], home=None, **_kw: Any) -> str:
    result = core.write_skeleton(
        day=args.get("date"),
        home=home,
        author=args.get("author"),
        force=bool(args.get("force")),
        sessions=args.get("sessions"),
        messages=args.get("messages"),
    )
    return core.json_dumps(result)


def _validate_handler(args: Dict[str, Any], home=None, **_kw: Any) -> str:
    return core.json_dumps(
        core.validate_diary(day=args.get("date"), path=args.get("path"), home=home)
    )


def _index_handler(args: Dict[str, Any], home=None, **_kw: Any) -> str:
    return core.json_dumps(core.build_index(home=home))


def _safe(fn: Callable[..., str], home=None) -> Callable[..., str]:
    def wrapped(args: Dict[str, Any] | None = None, **kwargs: Any) -> str:
        try:
            kwargs["home"] = home
            return fn(args or {}, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception("dreams-diary tool failed")
            return core.json_dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return wrapped


TOOLS = (
    ("dreams_material", _MATERIAL_SCHEMA, _material_handler),
    ("dreams_write", _WRITE_SCHEMA, _write_handler),
    ("dreams_validate", _VALIDATE_SCHEMA, _validate_handler),
    ("dreams_index", _INDEX_SCHEMA, _index_handler),
)


def register(ctx) -> None:
    """Hermes のプラグインコンテキストにツールを登録する。"""
    # PluginContext has no public home accessor. Its manager's home_path is the exact registry
    # scope for this plugin registration; capture it because tool handlers may run on workers
    # where Hermes' ContextVar home override is not propagated.
    manager = getattr(ctx, "_manager", None)
    manager_home = getattr(manager, "home_path", None)
    home = Path(manager_home) if manager_home is not None else core.resolve_profile_home()
    for name, schema, handler in TOOLS:
        if hasattr(ctx, "register_tool"):
            ctx.register_tool(
                name=name,
                toolset=TOOLSET,
                schema=schema,
                handler=_safe(handler, home=home),
            )
        else:  # 古いコンテキスト互換
            register = getattr(ctx, "add_tool", None)
            if callable(register):
                register(name, schema, _safe(handler, home=home))
