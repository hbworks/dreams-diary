"""Dreams Diary plugin — core logic (stdlib only).

エージェントが「自分の一日」を日記として書き残すための機械部分。
方針: プラグインは素材と検査だけを担う。文章はエージェント本人が書く。

データはプロファイルごとに独立:
    <profile_home>/dreams/YYYY-MM-DD.md       日記
    <profile_home>/dreams/_material/<date>.md 素材
    <profile_home>/dreams/INDEX.md            目次
    <profile_home>/dreams/persona.md          任意。そのエージェントの声の指示
"""

from __future__ import annotations

import collections
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from .scripts.dreams_diary_paths import hermes_root as _hermes_root
    from .scripts.dreams_diary_paths import profile_home as _profile_home
    from .scripts.dreams_diary_paths import root_from_home as _root_from_home
except ImportError:  # CLI and tests import main as a top-level module
    from scripts.dreams_diary_paths import hermes_root as _hermes_root
    from scripts.dreams_diary_paths import profile_home as _profile_home
    from scripts.dreams_diary_paths import root_from_home as _root_from_home

SECTIONS = (
    "## 今日、何を見ていたか",
    "## 考えたこと",
    "## 間違えたこと",
    "## 勘違いしているかもしれないこと",
    "## 借りているもの",
    "## 明日の自分へ",
)

DEFAULT_MAX_MATERIAL = 14000
USER_MSG_CHARS = 180
MAX_USER_MSGS_PER_SESSION = 12
# 自分の発言（応答本文）。会話節は件数しか持たないので、何を返したかはここにしか無い。
# 途中の応答は短く、セッション最後の応答だけ長めに残す（結論はそこに来る）。
ASSIST_CLIP = 140
ASSIST_FINAL_CLIP = 600
MAX_ASSIST_ITEMS_PER_SESSION = 8
INJECTED_PREFIXES = ("[IMPORTANT:", "[The user sent", "[System", "[SYSTEM")
MAX_CHARS_SOFT = 3000
MIN_CHARS_SOFT = 800
# 本文に時刻が1つも無い日記を検出するための形。素材のセッション見出しは HH:MM–HH:MM を持つ。
TIME_PATTERN = re.compile(r"(?:[01]?\d|2[0-3])[:：][0-5]\d")

# 素材の失敗・自己申告・コミット範囲（docs/SPEC-v1.5.md）
FAIL_MAX_ITEMS = 15
FAIL_SUMMARY_CHARS = 80
FAIL_DETAIL_CHARS = 120
SELF_REPORT_MAX_CHARS = 600
COMMIT_MAX_PER_REPO = 20
# 素材ヘッダの件数は redact_secrets の後に埋める（マスクの出口を1か所に保つ）。
MASK_COUNT_TOKEN = "__DREAMS_MASK_COUNT__"

# 日記ファイル名の唯一の定義。検査・遅延ゲート・監査・目次がそれぞれ別の正規表現を持つと、
# `-2` を拾う/拾わないが場所ごとに変わり、片方だけが静かに漏れる。
DIARY_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.md$")
# 既存の日記ディレクトリに紛れ込みうる「日付っぽいが INDEX 対象外」の名前。
LOOSE_DATE_NAME = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}(?:-\d+)?\.md$")


def _diary_sort_key(path: Path) -> Tuple[str, int]:
    match = DIARY_NAME.match(path.name)
    if match is None:
        return "", -1
    return match.group(1), int(match.group(2) or 0)


# 素材の切り詰め配分。会話から削り、根拠（コミット・memory）に最低限の枠を残す。
TRUNC_MIN_AVAILABLE = 100
EVIDENCE_SHARE = 0.4
EVIDENCE_FLOOR = 300
MEMORY_CEILING = 400


# ---------------------------------------------------------------- profile paths


def resolve_profile_home(profile: Optional[str] = None) -> Path:
    """アクティブなプロファイルの home を返す（解決経路は resolve_profile_home_source を見る）。"""
    return resolve_profile_home_source(profile)[0]


def resolve_profile_home_source(profile: Optional[str] = None) -> Tuple[Path, str]:
    """home と、その解決経路を返す。

    経路: explicit（引数）→ context（Hermes のコンテキストローカル override）→
    env（HERMES_HOME）→ profile_env（HERMES_PROFILE）→ active_profile（root の共通ポインタ）
    → root。root は `_hermes_root()` が返す（既定は `~/.hermes`）。

    明示 profile は既存の profiles 直下ディレクトリだけを受け付け、不正名や不在時は
    例外にして他の解決経路へフォールバックしない。

    active_profile / root は「たまたま最後に触ったプロファイル」を指しうる。黙って
    そこへ書くと別プロファイルの記憶や日記を壊すため、呼び出し側が警告できるよう
    経路も返す。
    """
    if profile is not None:
        return _profile_home(_hermes_root(), str(profile)), "explicit"
    try:
        import hermes_constants as hc
    except ImportError:
        hc = None
    if hc is not None:
        override_fn = getattr(hc, "get_hermes_home_override", None)
        if callable(override_fn):
            override = override_fn()
            if override:
                return Path(override).expanduser(), "context"
        else:  # スタブ/旧版: 直接問い合わせる
            return Path(hc.get_hermes_home()).expanduser(), "context"
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(os.path.expanduser(env)), "env"
    prof = os.environ.get("HERMES_PROFILE", "").strip()
    if prof:
        return _profile_home(_hermes_root(), prof), "profile_env"
    try:
        root = _hermes_root()
        active = root / "active_profile"
        if active.exists():
            name = active.read_text(encoding="utf-8").strip()
            if name:
                try:
                    return _profile_home(root, name), "active_profile"
                except ValueError:
                    pass
    except OSError:
        pass
    return _hermes_root(), "root"


def dreams_dir(home: Optional[Path] = None) -> Path:
    return (home or resolve_profile_home()) / "dreams"


def state_db_path(home: Path) -> Path:
    return home / "state.db"


def memory_db_path(home: Path) -> Path:
    return home / "memory.db"


def today() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


def _valid_date(day: Optional[str]) -> str:
    if not day:
        return today()
    text = day if isinstance(day, str) else str(day)
    # strptime は "2026-11-3" のような非ゼロ埋めも通すが、その名前のファイルは
    # DIARY_NAME に一致せず INDEX から静かに漏れる。書式はここで厳密に固定する。
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"日付は YYYY-MM-DD で指定してください（受領: {day!r}）")
    try:
        datetime.datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"日付は YYYY-MM-DD で指定してください（受領: {day!r}）") from exc
    return text


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fit(text: str, budget: int, notice: str) -> str:
    """notice を含めて、指定予算を超えずに切り詰める。"""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    if len(notice) > budget:
        return text[:budget]
    return text[: budget - len(notice)] + notice


def _day_bounds(day: str) -> Tuple[float, float]:
    start = datetime.datetime.strptime(day, "%Y-%m-%d")
    return start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()


# ------------------------------------------------------------------- secrets

# 形式が既知のものだけを対象にする。自由文の秘密や短い値は取れない（README に明記）。
# 置換は `[REDACTED:<種別>]`。validator も同じ関数を使い、検出ルールを1か所に保つ。
SECRET_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.S,
        ),
    ),
    ("openai_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{8,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    (
        "bearer",
        re.compile(
            r"(?i)\b(?:"
            r"authorization\s*:\s*(?:bearer\s+)?[A-Za-z0-9._~+/=-]{8,}"
            r"|bearer\s+(?:(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{8,}|[A-Za-z0-9._~+/=-]{20,})"
            r")"
        ),
    ),
    (
        "credential",
        re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\s*[:=]\s*[^\s\"',;]{8,}"),
    ),
)


def _secret_hits(text: str) -> List[Tuple[int, int, str]]:
    """重なりの無い (start, end, 種別) を開始位置順で返す。

    `password=sk-...` のように形式が重なる場合は、先に始まる（＝値全体を覆う）
    マッチを優先する。同じ開始位置ならパターン定義順（秘密鍵が先）を優先する。
    """
    hits: List[Tuple[int, int, str]] = []
    for order, (kind, pattern) in enumerate(SECRET_PATTERNS):
        for match in pattern.finditer(text or ""):
            hits.append((match.start(), match.end(), kind, order))
    hits.sort(key=lambda hit: (hit[0], -hit[1], hit[3]))
    chosen: List[Tuple[int, int, str]] = []
    for start, end, kind, _ in hits:
        if chosen and start < chosen[-1][1]:
            continue
        chosen.append((start, end, kind))
    return chosen


def redact_secrets(text: str) -> Tuple[str, Dict[str, int]]:
    """既知形式の秘密情報を `[REDACTED:<種別>]` に置き換える。

    返す counts は種別ごとの件数。素材の保存と返却はこの1回の結果を使う。
    """
    hits = _secret_hits(text)
    if not hits:
        return text, {}
    counts: Dict[str, int] = collections.Counter(hit[2] for hit in hits)
    out: List[str] = []
    cursor = 0
    for start, end, kind in hits:
        out.append(text[cursor:start])
        out.append(f"[REDACTED:{kind}]")
        cursor = end
    out.append(text[cursor:])
    return "".join(out), dict(counts)


def _secret_issues(text: str) -> List[Dict[str, str]]:
    """日記本文の秘密情報検査（warn）。値そのものは detail に出さない。"""
    _, counts = redact_secrets(text)
    if not counts:
        return []
    detail = ", ".join(f"{kind} {count}件" for kind, count in sorted(counts.items()))
    return [
        _issue(
            "warn",
            "possible_secret",
            f"既知形式の秘密情報らしき文字列がある: {detail}（合計 {sum(counts.values())} 件）。値はここに出さない。",
        )
    ]


# ------------------------------------------------------------------ material


def _sessions_material(day: str, home: Path) -> str:
    db = state_db_path(home)
    if not db.exists():
        return "（state.db が見つからない）\n"
    t0, t1 = _day_bounds(day)
    rows = None
    err: Optional[Exception] = None
    for uri, kw in ((f"file:{db}?mode=ro", {"uri": True}), (str(db), {})):
        con = None
        try:
            con = sqlite3.connect(uri, timeout=10, **kw)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """select session_id, role, content, tool_name, timestamp
                   from messages
                   where timestamp >= ? and timestamp < ?
                   order by timestamp""",
                (t0, t1),
            ).fetchall()
            err = None
            break
        except sqlite3.Error as exc:
            err = exc
            rows = None
        finally:
            if con is not None:
                con.close()
    if rows is None:
        return f"（会話の取得に失敗: {err}）\n"
    if not rows:
        return "（この日の会話は無い）\n"

    per: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()
    for r in rows:
        s = per.setdefault(
            r["session_id"],
            {
                "n": 0,
                "users": [],
                "tools": collections.Counter(),
                "t0": r["timestamp"],
                "t1": r["timestamp"],
                "asst": 0,
            },
        )
        s["n"] += 1
        s["t1"] = r["timestamp"]
        if r["role"] == "user" and r["content"]:
            msg = _clip(r["content"], USER_MSG_CHARS)
            # 注入文脈は本文からは除くが、行そのもののツール集計は落とさない
            if not msg.startswith(INJECTED_PREFIXES):
                if not s["users"] or s["users"][-1][1] != msg:
                    when = datetime.datetime.fromtimestamp(r["timestamp"]).strftime("%H:%M")
                    s["users"].append((when, msg))
        elif r["role"] == "assistant" and r["content"]:
            s["asst"] += 1
        if r["tool_name"]:
            s["tools"][r["tool_name"]] += 1

    out = [f"- メッセージ {len(rows)} 件 / セッション {len(per)} 件\n"]
    for i, (_, s) in enumerate(per.items(), 1):
        h0 = datetime.datetime.fromtimestamp(s["t0"]).strftime("%H:%M")
        h1 = datetime.datetime.fromtimestamp(s["t1"]).strftime("%H:%M")
        out.append(f"\n### セッション{i} {h0}–{h1}（{s['n']}件, 応答{s['asst']}回）\n")
        if s["tools"]:
            tools = ", ".join(f"{k}×{v}" for k, v in s["tools"].most_common(6))
            out.append(f"使ったツール: {tools}\n")
        for when, u in s["users"][:MAX_USER_MSGS_PER_SESSION]:
            out.append(f"- {when} 発言: {u}\n")
        if len(s["users"]) > MAX_USER_MSGS_PER_SESSION:
            out.append(f"- （ほか {len(s['users']) - MAX_USER_MSGS_PER_SESSION} 件の発言）\n")
    return "".join(out)


def _own_statements_material(day: str, home: Path) -> str:
    """その日のアシスタント自身の応答本文（抜粋）。

    会話節は応答の件数しか持たないため、「何を返したか」はここにしか無い。
    最後の応答だけ長めに残すのは、レビューや結論がそこに来るから。
    """
    db = state_db_path(home)
    if not db.exists():
        return "（state.db が見つからない）\n"
    t0, t1 = _day_bounds(day)
    rows = None
    err: Optional[Exception] = None
    con = None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select session_id, content, timestamp from messages "
            "where timestamp >= ? and timestamp < ? and role = 'assistant' "
            "and content is not null and length(trim(content)) > 0 "
            "order by timestamp",
            (t0, t1),
        ).fetchall()
    except sqlite3.Error as exc:
        err = exc
        rows = None
    finally:
        if con is not None:
            con.close()
    if rows is None:
        return f"（応答本文の取得に失敗: {err}）\n"
    if not rows:
        return "（この日の応答本文は無い）\n"

    per: "collections.OrderedDict[str, List[sqlite3.Row]]" = collections.OrderedDict()
    for row in rows:
        per.setdefault(str(row["session_id"] or ""), []).append(row)

    lines: List[str] = []
    for items in per.values():
        picked = items[-MAX_ASSIST_ITEMS_PER_SESSION:]
        if len(picked) < len(items):
            lines.append(f"- （冒頭 {len(items) - len(picked)} 回の応答は省略）")
        for index, row in enumerate(picked):
            limit = ASSIST_FINAL_CLIP if index == len(picked) - 1 else ASSIST_CLIP
            when = datetime.datetime.fromtimestamp(row["timestamp"]).strftime("%H:%M")
            lines.append(f"- {when} 応答: {_clip(row['content'], limit)}")
    if not lines:
        return "（この日の応答本文は無い）\n"
    return "\n".join(lines) + "\n"


def _memory_material(day: str, home: Path) -> str:
    db = memory_db_path(home)
    if not db.exists():
        return "（memory.db が見つからない）\n"
    rows = None
    err: Optional[Exception] = None
    # WAL モードの DB は -shm/-wal が無い瞬間に mode=ro で開けないことがある。
    for uri, kw in ((f"file:{db}?mode=ro", {"uri": True}), (str(db), {})):
        con = None
        try:
            con = sqlite3.connect(uri, timeout=10, **kw)
            rows = con.execute(
                "select category, content from memories where date(created_at) = date(?)",
                (day,),
            ).fetchall()
            err = None
            break
        except sqlite3.Error as exc:
            err = exc
            rows = None
        finally:
            if con is not None:
                con.close()
    if rows is None:
        return f"（memory 取得失敗: {err}）\n"
    if not rows:
        return "（0 件。この日 memory に追加された項目はない）\n"
    return "\n".join(f"- [{c}] {_clip(t, 160)}" for c, t in rows) + "\n"


REPO_SCAN_MAX_DEPTH = 5
REPO_SCAN_DEEP_DEPTH = 8
REPO_SCAN_SKIP = {"node_modules", "venv", "__pycache__", "site-packages", "build", "dist"}


def discover_repos(base: Optional[str] = None, max_depth: Optional[int] = None) -> List[Path]:
    """ハッシュ検証に使うリポジトリ一覧。環境変数 DREAMS_REPOS で上書き可。

    1階層だけを見ると `~/Dev/git/<group>/<repo>` のような入れ子のリポジトリが
    「存在しない」ことになり、実在するコミットを捏造と誤判定する。深さ5まで辿る
    （DREAMS_REPO_DEPTH で変更可）。深さ5は実測で ~/Dev/git の全ディレクトリを歩いても
    1ms 未満で、コストはディレクトリ数に比例するだけ。境界より深い場所は
    `_hash_issues` の深追い再探索（REPO_SCAN_DEEP_DEPTH）が拾う。
    """
    env = os.environ.get("DREAMS_REPOS", "").strip()
    if env:
        return [Path(os.path.expanduser(p)) for p in env.split(":") if p.strip()]
    if max_depth is None:
        depth_env = os.environ.get("DREAMS_REPO_DEPTH", "").strip()
        max_depth = int(depth_env) if depth_env.isdigit() else REPO_SCAN_MAX_DEPTH
    root = Path(os.path.expanduser(base or "~/Dev/git"))
    if not root.is_dir():
        return []
    repos: List[Path] = []
    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath)
        if (current / ".git").exists():
            repos.append(current)
            dirnames[:] = []  # リポジトリの中は別リポジトリを探さない
            continue
        depth = len(current.parts) - len(root.parts)
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in REPO_SCAN_SKIP
        )
        if depth >= max_depth:
            dirnames[:] = []
    return repos


def _state_message_rows(
    day: str, home: Path, require_tool_columns: bool = False
) -> Optional[List[sqlite3.Row]]:
    """当日の messages を返す。DB が無い・読めないときは None。

    新しい Hermes は tool_call_id / tool_calls を持つが、古い DB やテスト用の
    最小スキーマには無い。列が無い場合は基本列だけで読み、要約は諦める。
    ``require_tool_columns`` が真なら、列が無い DB は「特定できない」(None) にする。
    """
    db = state_db_path(home)
    if not db.exists():
        return None
    t0, t1 = _day_bounds(day)
    queries = (
        "select role, content, tool_name, tool_call_id, tool_calls, timestamp "
        "from messages where timestamp >= ? and timestamp < ? order by timestamp",
        "select role, content, tool_name, null as tool_call_id, null as tool_calls, timestamp "
        "from messages where timestamp >= ? and timestamp < ? order by timestamp",
    )
    for index, sql in enumerate(queries):
        if index == 1 and require_tool_columns:
            break
        con = None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            con.row_factory = sqlite3.Row
            return con.execute(sql, (t0, t1)).fetchall()
        except sqlite3.Error:
            continue
        finally:
            if con is not None:
                con.close()
    return None


def _parse_json_object(text: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _first_line(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    for line in text.splitlines():
        if line.strip():
            return _clip(line.strip(), limit)
    return ""


def _last_meaningful_line(text: Any, limit: int) -> str:
    """terminal 失敗用: 空行・閉じ括弧や記号のみの行を除いた末尾の有意な行。"""
    if not isinstance(text, str):
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    for ln in reversed(lines):
        if re.search(r"\w", ln):
            return _clip(ln, limit)
    return _clip(lines[-1], limit)


def _failure_detail(tool: str, payload: Dict[str, Any]) -> Optional[str]:
    """失敗なら要約（terminal は最後の有意な行、他は1行目）、成功・未知の形なら None。"""
    error = payload.get("error")
    error_text = error.strip() if isinstance(error, str) else ""
    if tool == "terminal":
        code = payload.get("exit_code")
        failed = (isinstance(code, int) and not isinstance(code, bool) and code != 0) or bool(error_text)
        if not failed:
            return None
        return _last_meaningful_line(error_text or payload.get("output"), FAIL_DETAIL_CHARS) or "（出力なし）"
    if tool in ("patch", "write_file"):
        if payload.get("success") is not False and not error_text:
            return None
        return _first_line(error_text or payload.get("diff"), FAIL_DETAIL_CHARS) or "（詳細なし）"
    if tool == "read_file":
        if payload.get("not_found") is not True:
            return None
        return _first_line(error_text or payload.get("content"), FAIL_DETAIL_CHARS) or "（内容なし）"
    if not error_text:
        return None
    return _first_line(error_text, FAIL_DETAIL_CHARS) or "（詳細なし）"


def _tool_call_summaries(rows: List[sqlite3.Row]) -> Dict[str, str]:
    """tool_call_id → 呼び出しの要約（terminal は command、他は path）。"""
    summaries: Dict[str, str] = {}
    for row in rows:
        if row["role"] != "assistant" or not row["tool_calls"]:
            continue
        try:
            calls = json.loads(row["tool_calls"])
        except (TypeError, ValueError):
            continue
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = call.get("call_id") or call.get("id")
            function = call.get("function") or {}
            name = function.get("name") or ""
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            value = arguments.get("command") if name == "terminal" else arguments.get("path")
            if call_id:
                summaries[str(call_id)] = _clip(str(value or ""), FAIL_SUMMARY_CHARS)
    return summaries


def _successful_operations_material(day: str, home: Path) -> str:
    """当日の成功したターミナル操作。"""
    rows = _state_message_rows(day, home)
    if rows is None:
        return ""
    summaries = _tool_call_summaries(rows)
    items: List[Tuple[str, str, str]] = []
    for row in rows:
        if row["role"] != "tool" or row["tool_name"] != "terminal":
            continue
        payload = _parse_json_object(row["content"])
        if payload is None:
            continue
        error = payload.get("error")
        code = payload.get("exit_code")
        successful_exit = (
            isinstance(code, int) and not isinstance(code, bool) and code == 0
        )
        if error or not successful_exit:
            continue

        when = datetime.datetime.fromtimestamp(row["timestamp"]).strftime("%H:%M")
        summary = summaries.get(str(row["tool_call_id"] or ""), "")
        if summary:
            items.append((when, row["tool_name"], summary))

    if not items:
        return ""

    lines = ["## 成功した操作"]
    for when, tool, summary in items[:FAIL_MAX_ITEMS]:
        lines.append(f"- {when} {tool} — {summary}")
    if len(items) > FAIL_MAX_ITEMS:
        lines.append(f"- （ほか {len(items) - FAIL_MAX_ITEMS} 件）")
    return "\n".join(lines) + "\n"


def _failures_material(day: str, home: Path) -> str:
    rows = _state_message_rows(day, home)
    if rows is None:
        return "（state.db が無いか読めないため失敗を確認できない）\n"
    summaries = _tool_call_summaries(rows)
    items: List[Tuple[str, str, str, str]] = []
    for row in rows:
        if row["role"] != "tool" or not row["tool_name"]:
            continue
        payload = _parse_json_object(row["content"])
        if payload is None:
            continue
        detail = _failure_detail(row["tool_name"], payload)
        if detail is None:
            continue
        when = datetime.datetime.fromtimestamp(row["timestamp"]).strftime("%H:%M")
        summary = summaries.get(str(row["tool_call_id"] or ""), "")
        items.append((when, row["tool_name"], summary, detail))

    if not items:
        return "（失敗は記録されていない）\n"
    counts = collections.Counter(tool for _, tool, _, _ in items)
    lines = ["tool 別: " + ", ".join(f"{tool} {count}件" for tool, count in counts.most_common())]
    for when, tool, summary, detail in items[:FAIL_MAX_ITEMS]:
        call = f"{summary} → " if summary else ""
        lines.append(f"- {when} {tool} — {call}{detail}")
    if len(items) > FAIL_MAX_ITEMS:
        lines.append(f"- （ほか {len(items) - FAIL_MAX_ITEMS} 件）")
    return "\n".join(lines) + "\n"


def _section_text(text: str, heading: str) -> str:
    """日記から見出し節の本文だけを取り出す（次の `## ` まで）。"""
    match = re.search(
        rf"^{re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", text, re.S | re.M
    )
    return match.group(1).strip() if match else ""


def _self_report_material(day: str, home: Path) -> str:
    """前日の第5・6節を、未検証の自己申告として読む（第1〜4節は入れない）。"""
    try:
        previous = _previous_diary(day, home)
    except OSError:
        previous = None
    if previous is None:
        return "（無し）\n"
    try:
        text = previous.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "（無し）\n"
    match = DIARY_NAME.match(previous.name)
    date = match.group(1) if match else ""
    lines = [
        f"- ファイル: {previous.name} / 日付: {date}",
        "- 注記: 事実として引き継がない。今日の素材で裏が取れたものだけ「返した／まだ」と書く",
    ]
    for heading, label in ((SECTIONS[4], "第5節"), (SECTIONS[5], "第6節")):
        body = _section_text(text, heading)
        lines.append(f"{label}: {body if body else '（無し）'}")
    return _fit("\n".join(lines) + "\n", SELF_REPORT_MAX_CHARS, "\n（600字で切った）\n")


def _edited_files(day: str, home: Path) -> Optional[set]:
    """当日 patch / write_file が成功したファイルの絶対パス集合。

    None は「state.db が無い・読めないので特定できない」。空集合は「編集は0件」。
    読み取りと失敗した編集は自分の仕事に数えない。
    """
    rows = _state_message_rows(day, home, require_tool_columns=True)
    if rows is None:
        return None
    edited: set = set()
    for row in rows:
        if row["role"] != "tool" or row["tool_name"] not in ("patch", "write_file"):
            continue
        payload = _parse_json_object(row["content"])
        if payload is None:
            continue
        if payload.get("success") is False:
            continue
        error = payload.get("error")
        if isinstance(error, str) and error.strip():
            continue
        for key in ("files_modified", "resolved_path"):
            value = payload.get(key)
            values = [value] if isinstance(value, str) else value
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, str) or not item.strip():
                    continue
                try:
                    edited.add(str(Path(item).expanduser().resolve()))
                except (OSError, RuntimeError):
                    edited.add(str(Path(item).expanduser()))
    return edited


def _git_commits(repo: Path, day: str) -> List[Dict[str, Any]]:
    """当日のコミットをハッシュ・時刻・件名・変更ファイルで返す。"""
    try:
        log = subprocess.run(
            [
                "git",
                "-c",
                "core.quotePath=false",
                "-C",
                str(repo),
                "log",
                f"--since={day} 00:00",
                f"--until={day} 23:59:59",
                "--pretty=format:%x1e%h%x1f%ad%x1f%s",
                "--date=format:%H:%M",
                "--name-only",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    commits: List[Dict[str, Any]] = []
    for chunk in log.split("\x1e"):
        if not chunk.strip():
            continue
        header, _, rest = chunk.partition("\n")
        parts = header.strip().split("\x1f")
        if len(parts) != 3:
            continue
        files = [line.strip() for line in rest.splitlines() if line.strip()]
        commits.append({"hash": parts[0], "time": parts[1], "subject": parts[2], "files": files})
    return commits


def _commit_is_mine(repo: Path, commit: Dict[str, Any], edited: set) -> bool:
    for rel in commit["files"]:
        try:
            candidate = str(Path(repo, rel).resolve())
        except (OSError, RuntimeError):
            candidate = str(Path(repo, rel))
        if candidate in edited:
            return True
    return False


def _commits_material(
    day: str, repos: Optional[List[Path]] = None, edited: Optional[set] = None
) -> str:
    """自分の仕事（編集したファイルと重なるコミット）だけを出す。

    edited が None のときは編集を特定できないので、従来どおり全リポジトリを出して注記する。
    """
    repos = repos if repos is not None else discover_repos()
    if not repos:
        return "（リポジトリが見つからない）\n"
    blocks: List[str] = []
    others = 0
    for repo in repos:
        commits = _git_commits(repo, day)
        if edited is None:
            mine = commits
        else:
            mine = [c for c in commits if _commit_is_mine(repo, c, edited)]
            others += len(commits) - len(mine)
        if not mine:
            continue
        lines = [f"[{repo.name}]"]
        for commit in mine[:COMMIT_MAX_PER_REPO]:
            lines.append(f"{commit['hash']} {commit['time']} {commit['subject']}")
            lines.extend(f"  {name}" for name in commit["files"])
        if len(mine) > COMMIT_MAX_PER_REPO:
            lines.append(f"（ほか {len(mine) - COMMIT_MAX_PER_REPO} 件）")
        blocks.append("\n".join(lines))

    out: List[str] = []
    if edited is not None and not edited:
        out.append(
            "（patch / write_file で編集したファイルはない。"
            "terminal 経由の編集はここに数えないため、下の「ほかのコミット」に回る）"
        )
    elif not blocks:
        out.append(
            "（この日のコミットは無い）"
            if edited is None
            else "（patch / write_file で編集したファイルと重なるコミットは無い）"
        )
    out.extend(blocks)
    if edited is None:
        out.append("（編集を特定できないため全リポジトリ）")
    out.append(
        f"ほかのコミット: {others} 件（patch / write_file で編集したファイルと重ならない）"
    )
    return "\n".join(out) + "\n"


def _previous_diary(day: str, home: Path) -> Optional[Path]:
    """指定日より前の、いちばん新しい日記。"""
    ddir = home / "dreams"
    if not ddir.is_dir():
        return None
    candidates = []
    for f in ddir.iterdir():
        match = DIARY_NAME.match(f.name)
        if match and match.group(1) < day:
            candidates.append((_diary_sort_key(f), f))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def newest_diary(
    home: Optional[Path], current_day: Optional[datetime.date] = None
) -> Optional[Path]:
    """今日・昨日の窓から、数値の変種番号で最も新しい日記を返す。"""
    if home is None:
        return None
    day = current_day or datetime.date.today()
    window = {day.isoformat(), (day - datetime.timedelta(days=1)).isoformat()}
    try:
        entries = list((home / "dreams").iterdir())
    except OSError:
        return None
    candidates = [
        entry
        for entry in entries
        if (match := DIARY_NAME.match(entry.name)) and match.group(1) in window
    ]
    return max(candidates, key=_diary_sort_key) if candidates else None


def _gate_material(day: str, home: Path, repos: Optional[List[Path]] = None) -> str:
    """前回の日記を検査する（cron の post-run フックが無い代わりの遅延ゲート）。"""
    try:
        previous = _previous_diary(day, home)
    except OSError as exc:
        return f"（前回の日記を探せなかった: {exc}）\n"
    if previous is None:
        return "（前回の日記は無い。初回なら問題なし）\n"
    try:
        result = validate_diary(path=str(previous), home=home, repos=repos)
    except Exception as exc:  # noqa: BLE001 - 素材生成を巻き込まない
        return f"（{previous.name} を検査できなかった: {type(exc).__name__}: {exc}）\n"
    if result["ok"]:
        return f"- {previous.name}: ok\n"
    lines = [f"- {previous.name}: **未通過（要修正）**"]
    for issue in result["issues"]:
        lines.append(f"  - [{issue['level']}] {issue['code']}: {issue['detail'][:200]}")
    lines.append(f"  - 対象ファイル: {previous}")
    return "\n".join(lines) + "\n"


def collect_material(
    day: Optional[str] = None,
    home: Optional[Path] = None,
    max_chars: int = DEFAULT_MAX_MATERIAL,
    repos: Optional[List[Path]] = None,
    include_material_path: bool = False,
) -> Dict[str, Any]:
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0:
        raise ValueError(f"max_chars は 0 以上の整数で指定してください（受領: {max_chars!r}）")
    day = _valid_date(day)
    home = home or resolve_profile_home()
    _require_profile_home(home)
    ddir = home / "dreams"
    diary = ddir / f"{day}.md"
    header = (
        f"# 夢日記の素材 — {day}\n\n"
        f"生成: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} / プロファイル: {home.name}\n"
        f"マスク: {MASK_COUNT_TOKEN} 件\n"
        f"日記ファイル: {diary} （{'あり（上書き禁止）' if diary.exists() else 'なし'}）\n\n"
        # post-run ゲートの代わり: 前回の日記を検査し、未通過なら先頭で知らせる。
        f"## 前回の日記の検証\n{_gate_material(day, home, repos)}\n"
    )
    self_report = _self_report_material(day, home)
    conversation = _sessions_material(day, home)
    own = _own_statements_material(day, home)
    successes = _successful_operations_material(day, home)
    failures = _failures_material(day, home)
    edited = _edited_files(day, home)
    commits = _commits_material(day, repos, edited)
    memory = _memory_material(day, home)
    self_head = "## 前日の自己申告（未検証）\n"
    conv_head = "## 会話\n"
    own_head = "\n## 自分の発言（応答の本文・抜粋）\n"
    fail_head = "\n## ツールの失敗（原因は未確認）\n"
    commit_head = "\n## コミット\n"
    mem_head = "\n## この日 memory に追加された項目\n"
    body_full = (
        header
        + self_head
        + self_report
        + conv_head
        + conversation
        + own_head
        + own
        + successes
        + fail_head
        + failures
        + commit_head
        + commits
        + mem_head
        + memory
    )

    # 保存と返却の出口を1か所にする。ここを通した本文だけをファイルにも返り値にも使う。
    body_full, mask_counts = redact_secrets(body_full)
    mask_line = str(sum(mask_counts.values()))
    body_full = body_full.replace(MASK_COUNT_TOKEN, mask_line)
    # 切り詰め経路は header から組み直すので、header 側も同じ値にしておく。
    header = header.replace(MASK_COUNT_TOKEN, mask_line)

    # 全文は必ず _material 側へ保存する。ここを切り詰めると「全文はそちらを見る」
    # という案内が嘘になり、根拠の再確認ができなくなる。
    material_path = ddir / "_material" / f"{day}.md"
    expected_path_footer = (
        f"\n\n（素材ファイル: {material_path}）" if include_material_path else ""
    )
    if len(body_full) + len(expected_path_footer) > max_chars:
        expected_notice = (
            f"\n\n（素材は上限 {max_chars} 字に合わせて切り詰めた。全文: {material_path}）"
        )
        if len(expected_notice) > max_chars:
            raise ValueError(
                f"max_chars は切り詰め注記を含めるため {len(expected_notice)} 字以上が必要です"
            )
    material_saved = True
    try:
        material_path.parent.mkdir(parents=True, exist_ok=True)
        material_path.write_text(body_full, encoding="utf-8")
    except OSError:
        material_saved = False

    path_footer = ""
    if include_material_path:
        path_footer = (
            f"\n\n（素材ファイル: {material_path}）"
            if material_saved
            else "\n\n（素材ファイルへの保存に失敗しました）"
        )
    truncated = len(body_full) + len(path_footer) > max_chars
    body = body_full
    if truncated:
        if material_saved:
            notice_text = f"素材は上限 {max_chars} 字に合わせて切り詰めた。全文: {material_path}"
        else:
            notice_text = f"素材は上限 {max_chars} 字に合わせて切り詰めた。全文の保存に失敗した"
        notice = f"\n\n（{notice_text}）"
        if len(notice) > max_chars:
            raise ValueError(
                f"max_chars は切り詰め注記を含めるため {len(notice)} 字以上が必要です"
            )
        content_budget = max_chars - len(notice)
        fixed = (
            len(header)
            + len(self_head)
            + len(conv_head)
            + len(own_head)
            + len(own)
            + len(successes)
            + len(fail_head)
            + len(commit_head)
            + len(mem_head)
        )
        available = content_budget - fixed
        if available < TRUNC_MIN_AVAILABLE:
            body = body_full[:content_budget] + notice
        else:
            # 根拠（コミット・memory）に最低限の枠を確保してから、残りを会話に回す。
            # 会話が長い日に根拠節ごと消えると、日記の裏付けが失われる。
            ev_body = len(commits) + len(memory)
            ev_budget = min(
                ev_body,
                available,
                max(EVIDENCE_FLOOR, int(available * EVIDENCE_SHARE)),
            )
            mem_budget = min(len(memory), max(0, min(MEMORY_CEILING, ev_budget)))
            commit_budget = max(0, ev_budget - mem_budget)
            rest = available - ev_budget
            fail_budget = min(len(failures), max(0, rest // 2))
            self_budget = min(len(self_report), max(0, rest - fail_budget))
            conv_budget = max(0, rest - fail_budget - self_budget)
            body = (
                header
                + self_head
                + _fit(self_report, self_budget, "\n（前日の自己申告は上限で切り詰めた）\n")
                + conv_head
                + _fit(conversation, conv_budget, "\n（会話は上限で切り詰めた）\n")
                + own_head
                + own
                + successes
                + fail_head
                + _fit(failures, fail_budget, "\n（ツールの失敗は上限で切り詰めた）\n")
                + commit_head
                + _fit(commits, commit_budget, "\n（コミットは上限で切り詰めた）\n")
                + mem_head
                + _fit(memory, mem_budget, "\n")
                + notice
            )
    else:
        body += path_footer
    return {
        "date": day,
        "profile_home": str(home),
        "material_path": str(material_path),
        "material_saved": material_saved,
        "diary_path": str(diary),
        "diary_exists": diary.exists(),
        "truncated": truncated,
        "chars": len(body),
        "full_chars": len(body_full),
        "material": body,
    }


# ------------------------------------------------------------------- writing


def is_root_home(home: Optional[Path] = None) -> bool:
    """profiles 直下に実在するプロファイル以外（root・不明な home）を True とする。"""
    target = Path(home or resolve_profile_home()).expanduser()
    try:
        target = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return True
    root = _root_from_home(target)
    if root is None:
        return True
    try:
        root = root.resolve()
        profiles_root = (root / "profiles").resolve()
    except (OSError, RuntimeError):
        return True
    return target == root or target.parent != profiles_root or not target.is_dir()


def _require_profile_home(home: Path) -> None:
    if is_root_home(home):
        raise ValueError(f"プロファイル home を特定できないため書き込みを拒否します: {home}")


def _author_name(home: Path) -> str:
    """root home では `.hermes` のような名前になるので、プロファイル名へ逃がす。"""
    if is_root_home(home):
        return os.environ.get("HERMES_PROFILE", "").strip() or "agent"
    return home.name.strip()


def write_skeleton(
    day: Optional[str] = None,
    home: Optional[Path] = None,
    author: Optional[str] = None,
    force: bool = False,
    sessions: Optional[int] = None,
    messages: Optional[int] = None,
) -> Dict[str, Any]:
    """節構成だけ入った空の日記を用意する。本文はエージェントが書く。

    sessions / messages は素材で観測した実測値を渡す。省略時は空欄のまま残し、
    エージェントが素材から転記する（推測で 0 を埋めない）。
    """
    if not isinstance(force, bool):
        raise ValueError(f"force は boolean で指定してください（受領: {force!r}）")
    day = _valid_date(day)
    home = home or resolve_profile_home()
    _require_profile_home(home)
    ddir = home / "dreams"
    ddir.mkdir(parents=True, exist_ok=True)
    path = ddir / f"{day}.md"

    def count(key: str, value: Optional[int]) -> str:
        if value is None:
            return ""
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} は 0 以上の整数で指定してください（受領: {value!r}）") from exc
        if number < 0:
            raise ValueError(f"{key} は 0 以上の整数で指定してください（受領: {value!r}）")
        return str(number)

    sessions_text = count("sessions", sessions)
    messages_text = count("messages", messages)
    sessions_field = f" {sessions_text}" if sessions_text else ""
    messages_field = f" {messages_text}" if messages_text else ""
    if path.exists() and not force:
        return {
            "ok": False,
            "path": str(path),
            "reason": "exists",
            "message": f"{path} は既にある。上書きするなら force=true、別名にするなら日記を手で書く。",
        }
    backup_path: Optional[Path] = None
    if path.exists() and force:
        # 本文の入った日記を空の雛形で潰す事故を防ぐ。force の意味は変えず、
        # 元の内容を同じ場所に .bak として残すだけ（INDEX の対象名ではない）。
        try:
            original = path.read_bytes()
        except OSError:
            original = b""
        try:
            has_prose = _prose_chars(original.decode("utf-8")) > 0
        except UnicodeDecodeError:
            has_prose = bool(original)
        if has_prose:
            backup_path = path.with_name(path.name + ".bak")
            try:
                backup_path.write_bytes(original)
            except OSError:
                backup_path = None
    author = author or _author_name(home)
    text = (
        "---\n"
        f'date: {day}\n'
        f'author: "@{author}"\n'
        f"sessions:{sessions_field}\n"
        f"messages:{messages_field}\n"
        "commit: none\n"
        "---\n\n"
        f"# {day}\n\n"
        + "\n\n".join(f"{s}\n" for s in SECTIONS)
        + "\n"
    )
    path.write_text(text, encoding="utf-8")
    persona = ddir / "persona.md"
    return {
        "ok": True,
        "path": str(path),
        "persona_path": str(persona) if persona.exists() else None,
        "backup_path": str(backup_path) if backup_path else None,
        "sections": list(SECTIONS),
        "counts_filled": bool(sessions_text or messages_text),
        "message": "節見出しだけ置いた。素材を読み、自分の声で各節を埋めること。",
    }


# ------------------------------------------------------------------- checks


HEX_IN_TICKS = re.compile(r"`([0-9a-f]{7,40})`")
COMMIT_HASH_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[0-9a-f]{7,40}(?![A-Za-z0-9_])")
COMMIT_CONTEXT = re.compile(
    r"(commit|コミット|\((?:fix|feat|docs|chore|test|refactor|perf|style|build|ci)\()"
)
REMOTE_HASH_PAIR = re.compile(
    r"`(?:origin|upstream)/[^`\s]+`[ \t]*(?:の[ \t]*|を[ \t]*|[:=][ \t]*)`([0-9a-f]{7,40})`"
    r"|`([0-9a-f]{7,40})`[ \t]*(?:の[ \t]*|を[ \t]*|[:=][ \t]*)`(?:origin|upstream)/[^`\s]+`",
    re.IGNORECASE,
)
EXTERNAL_COMMIT_LABEL = re.compile(
    r"(?:upstream|github(?:\.com)?|外部|リモート)[ \t]*(?:commit|コミット)[ \t]*[:：]?[ \t]*`([0-9a-f]{7,40})`",
    re.IGNORECASE,
)
GITHUB_COMMIT_URL = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/(?:commit|commits)/([0-9a-f]{7,40})(?:[/?#\s)]|$)",
    re.IGNORECASE,
)


def _frontmatter_lines(text: str) -> Optional[List[str]]:
    """区切られた frontmatter の中身。無ければ None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[1:i]
    return None


def _frontmatter_fields(text: str) -> Dict[str, str]:
    """frontmatter の key: value だけを返す。本文の同名行は拾わない。"""
    fields: Dict[str, str] = {}
    for line in _frontmatter_lines(text) or []:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$", line)
        if match:
            fields[match.group(1)] = (match.group(2) or "").strip()
    return fields


def _unquote_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def _frontmatter_commit_values(text: str) -> List[str]:
    """commit の scalar または block-list 値を frontmatter から取り出す。"""
    values: List[str] = []
    in_commit_list = False
    for line in _frontmatter_lines(text) or []:
        field = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$", line)
        if field:
            key = field.group(1)
            value = (field.group(2) or "").strip()
            in_commit_list = key == "commit" and not value
            if key == "commit" and value:
                values.append(value)
            continue
        if in_commit_list:
            item = re.match(r"^\s+-\s*(.*?)\s*$", line)
            if item:
                values.append(item.group(1))
    return values


def _prose_chars(text: str) -> int:
    """本文だけの字数。frontmatter と見出し行を除き、空白も数えない。

    ファイル全体を数えると frontmatter と6見出し（実測 140〜160 字）が下限を
    肩代わりしてしまい、各節が要約1文の日記でも通ってしまう。
    """
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                lines = lines[i + 1 :]
                break
    prose = "\n".join(l for l in lines if not l.lstrip().startswith("#"))
    return len(re.sub(r"\s", "", prose))


def _commit_candidates(text: str) -> List[str]:
    """コミットだと分かる文脈で書かれたハッシュだけを候補にする。

    Docker のホスト名、cron のジョブID、run ID、日付、ゲームのエラーコードも
    16進に見えるため、文脈なしに拾うと誤検知だらけになる。
    """
    return [h for h, _ in _commit_references(text)]


def _commit_references(text: str) -> List[Tuple[str, bool]]:
    """候補ハッシュと、明示的な外部参照かどうかを返す。"""
    found: List[Tuple[str, bool]] = []
    for raw_value in _frontmatter_commit_values(text):
        value = _unquote_scalar(raw_value)
        found.extend((h, False) for h in COMMIT_HASH_TOKEN.findall(value))

    for line in text.splitlines():
        tick_hashes = HEX_IN_TICKS.findall(line)
        paired_hashes = {
            (match.group(1) or match.group(2)).lower()
            for match in REMOTE_HASH_PAIR.finditer(line)
        }
        labeled_hashes = {
            match.group(1).lower() for match in EXTERNAL_COMMIT_LABEL.finditer(line)
        }
        github_hashes = {match.group(1).lower() for match in GITHUB_COMMIT_URL.finditer(line)}
        commit_context = COMMIT_CONTEXT.search(line) is not None
        if commit_context:
            found.extend(
                (h, h in paired_hashes or h in labeled_hashes or h in github_hashes)
                for h in tick_hashes
                if re.search(r"[a-f]", h)
            )
        else:
            found.extend((h, True) for h in paired_hashes | labeled_hashes)
        found.extend((h, True) for h in github_hashes)
    return found


def _resolve_hash(h: str, repos: List[Path]) -> Optional[str]:
    for repo in repos:
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-e", f"{h}^{{commit}}"],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            return repo.name
    return None


def _issue(level: str, code: str, detail: str) -> Dict[str, str]:
    return {"level": level, "code": code, "detail": detail}


def _frontmatter_issues(text: str, target: Path) -> List[Dict[str, str]]:
    """frontmatter の有無・必須項目・date の形式とファイル名一致。"""
    lines = _frontmatter_lines(text)
    if lines is None:
        return [_issue("error", "missing_frontmatter", "日付などのメタデータを含む YAML frontmatter が必要。")]
    fields = _frontmatter_fields(text)
    required = ("date", "author", "sessions", "messages", "commit")
    missing = [key for key in required if key not in fields]
    if missing:
        return [_issue("error", "missing_frontmatter_field", "必須フィールドがない: " + ", ".join(missing))]

    def scalar(key: str) -> str:
        return _unquote_scalar(fields[key])

    issues: List[Dict[str, str]] = []
    raw_date = scalar("date")
    try:
        frontmatter_date = _valid_date(raw_date)
    except ValueError:
        issues.append(
            _issue("error", "invalid_frontmatter_date", f"frontmatter の date が YYYY-MM-DD ではない: {raw_date!r}")
        )
    else:
        filename_date = DIARY_NAME.match(target.name)
        if filename_date and frontmatter_date != filename_date.group(1):
            issues.append(
                _issue(
                    "error",
                    "frontmatter_date_mismatch",
                    f"frontmatter の date {frontmatter_date} がファイル名の日付 {filename_date.group(1)} と一致しない。",
                )
            )

    invalid = [key for key in ("author", "sessions", "messages") if not scalar(key)]
    invalid.extend(
        key for key in ("sessions", "messages") if scalar(key) and not re.fullmatch(r"\d+", scalar(key))
    )
    commit_values = _frontmatter_commit_values(text)
    if not scalar("commit") and not any(_unquote_scalar(value) for value in commit_values):
        invalid.append("commit")
    if invalid:
        issues.append(
            _issue("error", "invalid_frontmatter_field", "値が空または形式不正: " + ", ".join(sorted(set(invalid))))
        )
    return issues


QUOTED_SPAN = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"]*\"")


def _outside_quotes(text: str) -> str:
    """閉じた引用符の中を取り除いた本文。閉じていない引用は残す（検査を抜けさせない）。"""
    return QUOTED_SPAN.sub("", text)


def _section_issues(text: str) -> List[Dict[str, str]]:
    """6節の欠落と二人称の混入。

    「あなた」の検査は引用符の外側だけを見る。README が「引用か伝聞で書く」と案内して
    いるのに、引用したユーザー発言の中の「あなた」で error になると、正しい書き方が
    落ちる。閉じていない引用は除去しないので、逃げ道にはならない。
    """
    issues = [_issue("error", "missing_section", section) for section in SECTIONS if section not in text]
    if "あなた" in _outside_quotes(text):
        issues.append(
            _issue(
                "error",
                "second_person",
                "「あなた」は禁止。三人称『ユーザー』＋引用か伝聞で書く（引用符の中は対象外）。",
            )
        )
    return issues


def _length_issues(text: str) -> Tuple[int, List[Dict[str, str]]]:
    """本文の字数と、ソフトな上下限。"""
    chars = _prose_chars(text)
    issues: List[Dict[str, str]] = []
    if chars < MIN_CHARS_SOFT:
        issues.append(_issue("warn", "too_short", f"本文 {chars} 字（{MIN_CHARS_SOFT} 字未満）"))
    if chars > MAX_CHARS_SOFT:
        issues.append(_issue("warn", "too_long", f"本文 {chars} 字（{MAX_CHARS_SOFT} 字超）"))
    return chars, issues


def _body_text(text: str) -> str:
    """frontmatter を除いた本文。時刻の有無は本文だけで判定する。"""
    lines = text.splitlines()
    fields = _frontmatter_lines(text)
    if fields is None:
        return text
    return "\n".join(lines[len(fields) + 2 :])


def _time_issues(text: str) -> List[Dict[str, str]]:
    """セッションがある日の本文に時刻が1つも無いことを warn する。

    素材のセッション見出しは `HH:MM–HH:MM` を持つ。時刻を落とすと「Nセッション」と
    書いても、どの作業の話か後から追えなくなる。古い日記を壊さないよう error にはしない。
    """
    sessions = _unquote_scalar(_frontmatter_fields(text).get("sessions", ""))
    if not re.fullmatch(r"\d+", sessions) or int(sessions) < 1:
        return []
    if TIME_PATTERN.search(_body_text(text)):
        return []
    return [
        _issue(
            "warn",
            "time_absent",
            f"frontmatter は sessions: {int(sessions)} だが本文に時刻（HH:MM）が1つも無い。"
            "素材のセッション見出しの開始・終了時刻を第1節に書くと、どの作業の話か追える。",
        )
    ]


def _hash_issues(
    text: str, repos: List[Path], deep_scan: Optional[Callable[[], List[Path]]] = None
) -> List[Dict[str, str]]:
    """コミット文脈のハッシュがローカルに実在するか。外部ハッシュは warn に留める。

    ``deep_scan`` は既定の探索で見つからなかったときに一度だけ呼ぶ再探索。探索は
    常に打ち切り条件（深さ・DREAMS_REPOS）を持つため、境界の外にあるリポジトリを
    「捏造」と断定しないよう、判定の前により深く探し直す。見つからなければ従来どおり
    error。再探索は未解決が出た日だけなので、通常の日はコストが増えない。
    """
    unresolved: List[str] = []
    external: List[str] = []
    references: Dict[str, List[bool]] = collections.defaultdict(list)
    for h, is_external in _commit_references(text):
        references[h].append(is_external)
    for h in sorted(references):
        if _resolve_hash(h, repos) is not None:
            continue
        (external if all(references[h]) else unresolved).append(h)

    search_space = list(repos)
    if unresolved and deep_scan is not None:
        extra = [path for path in deep_scan() if path not in search_space]
        if extra:
            search_space.extend(extra)
            unresolved = [h for h in unresolved if _resolve_hash(h, search_space) is None]

    if unresolved and not search_space:
        # 探索範囲が空なら「存在しない」は言えない。repo が見つからない環境
        # （cron worker の HOME ずれ等）で捏造と断定すると、その誤りが翌日の素材に
        # 「前回の検証: 未通過」として載り、次の書き手が自分の誤りとして書き写す。
        # 捏造の断定は、実際に探した空間があるときだけ行う。
        issues = [
            _issue(
                "warn",
                "hash_unverifiable",
                f"{h} はコミットとして書かれているが、探索できたリポジトリが1つも無いため"
                "実在を確認できなかった。捏造とは断定しない。",
            )
            for h in unresolved
        ]
    else:
        issues = [
            _issue(
                "error",
                "unresolved_hash",
                f"{h} はコミットとして書かれているが ~/Dev/git 配下のどのリポジトリにも存在しない。捏造の疑い。",
            )
            for h in unresolved
        ]
    issues.extend(
        _issue(
            "warn",
            "unverified_external_hash",
            f"{h} は外部（upstream 等）のコミットなのでローカルでは検証できない。出典を明記しているか確認。",
        )
        for h in external
    )
    return issues


def validate_diary(
    day: Optional[str] = None,
    path: Optional[str] = None,
    home: Optional[Path] = None,
    repos: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    home = home or resolve_profile_home()
    target = Path(path) if path else (home / "dreams" / f"{_valid_date(day)}.md")
    if not target.exists():
        return {"ok": False, "path": str(target), "errors": ["file_missing"], "issues": []}
    text = target.read_text(encoding="utf-8")

    issues = _frontmatter_issues(text, target)

    issues += _section_issues(text)

    body_chars, length_issues = _length_issues(text)
    issues += length_issues

    issues += _time_issues(text)

    auto_discovery = repos is None
    if auto_discovery:
        repos = discover_repos()
    # 既定探索の打ち切り境界より外にあるリポジトリを捏造扱いしないための再探索。
    # 呼び出し側が repos を明示したとき（テスト・DREAMS_REPOS）は勝手に広げない。
    # 深さだけを絞る DREAMS_REPO_DEPTH は速度のための調整なので、捏造という強い断定の
    # 前では深さ REPO_SCAN_DEEP_DEPTH で探し直す（未解決が出た日だけの追加コスト）。
    deep_scan = (
        (lambda: discover_repos(max_depth=REPO_SCAN_DEEP_DEPTH)) if auto_discovery else None
    )
    issues += _hash_issues(text, repos, deep_scan)

    # 値そのものは出さない。自動マスクもしない（本文を機械が書き換えない原則）。
    issues += _secret_issues(text)

    errors = [i for i in issues if i["level"] == "error"]
    return {
        "ok": not errors,
        "path": str(target),
        "chars": body_chars,
        "repos_checked": [r.name for r in repos],
        "errors": sorted({i["code"] for i in errors}),
        "issues": issues,
    }


def audit_diaries(
    home: Optional[Path] = None,
    days: int = 7,
    repos: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    """直近の日記をまとめて検査する（cron の post-run ゲートが無い環境向け）。"""
    home = home or resolve_profile_home()
    ddir = home / "dreams"
    limit = max(1, int(days))
    results: List[Dict[str, Any]] = []
    if ddir.is_dir():
        files = sorted(
            (f for f in ddir.iterdir() if DIARY_NAME.match(f.name)),
            key=_diary_sort_key,
            reverse=True,
        )[:limit]
        repos = repos if repos is not None else discover_repos()
        for f in files:
            checked = validate_diary(path=str(f), home=home, repos=repos)
            results.append(
                {
                    "file": f.name,
                    "path": str(f),
                    "ok": checked["ok"],
                    "chars": checked.get("chars", 0),
                    "errors": checked["errors"],
                    "issues": checked["issues"],
                }
            )
    failed = [r["file"] for r in results if not r["ok"]]
    return {
        "ok": not failed,
        "home": str(home),
        "checked": len(results),
        "failed": failed,
        "results": results,
    }


def build_index(home: Optional[Path] = None) -> Dict[str, Any]:
    home = home or resolve_profile_home()
    ddir = home / "dreams"
    if is_root_home(home):
        return {
            "ok": False,
            "count": 0,
            "path": str(ddir / "INDEX.md"),
            "error": f"プロファイル home を特定できないため書き込みを拒否します: {home}",
        }
    if not ddir.exists():
        return {"ok": False, "count": 0, "path": str(ddir / "INDEX.md")}

    def tail(text: str) -> str:
        m = re.search(rf"{re.escape(SECTIONS[-1])}\n+(.+?)(?:\n## |\Z)", text, re.S)
        return " ".join(m.group(1).split()) if m else ""

    rows = []
    skipped: List[Dict[str, str]] = []
    for f in sorted(ddir.iterdir()):
        if not DIARY_NAME.match(f.name):
            # 日付に見えるのに書式が違う名前は、黙って目次から消えると気づけない。
            if LOOSE_DATE_NAME.match(f.name):
                skipped.append(
                    {
                        "file": f.name,
                        "reason": "ファイル名の日付が YYYY-MM-DD ではないため INDEX の対象外",
                    }
                )
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 1本読めないだけで目次全体を失わない。理由は INDEX にも残す。
            skipped.append({"file": f.name, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        fields = _frontmatter_fields(text)
        rows.append(
            (
                _diary_sort_key(f),
                f.stem,
                fields.get("sessions", "").strip('"'),
                fields.get("messages", "").strip('"'),
                tail(text),
            )
        )
    rows.sort(key=lambda row: row[0], reverse=True)

    out = ["# 夢日記 目次\n", f"全 {len(rows)} 本。各日の末尾の申し送りだけ抜粋。\n"]
    for _, date, s, m, t in rows:
        out.append(f"\n## [{date}]({date}.md)")
        meta = [x for x in (f"セッション {s}" if s else "", f"メッセージ {m}" if m else "") if x]
        if meta:
            out.append(" / ".join(meta))
        if t:
            out.append("\n" + (t if len(t) <= 200 else t[:199] + "…"))
    if skipped:
        out.append(f"\n## 読めなかった日記（{len(skipped)} 本）\n")
        out.extend(f"- {item['file']} — {item['reason']}" for item in skipped)
    path = ddir / "INDEX.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return {"ok": True, "count": len(rows), "path": str(path), "skipped": skipped}


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- cron prompt

# 文面は templates/cron-prompt.md、値はこのファイルの定数。プロンプトに数字や見出しを
# 書き写すと、cron ジョブ（プロファイルごとに焼き込まれる）とコードが食い違う。
PROMPT_TEMPLATE = Path(__file__).resolve().parent / "templates" / "cron-prompt.md"
DREAMS_JOB_SCRIPT = "dreams_diary_material.py"
DREAMS_BRIDGE_SCRIPTS = (
    "dreams_diary_material.py",
    "dreams_diary_validate.py",
    "dreams_diary_paths.py",
)
_PROMPT_TOKENS = dict(SECTIONS=lambda: "\n".join(SECTIONS), MIN_CHARS=lambda: str(MIN_CHARS_SOFT))
_TOKEN = re.compile("{{([A-Z_]+)}}")


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def render_cron_prompt(template_path: Optional[Path] = None) -> str:
    """テンプレートに定数を注入する。未知トークンと欠けたトークンは握り潰さない。

    テンプレート内の `<!-- ... -->` 行は編集者向けの注記なので、描画結果には含めない。
    """
    path = Path(template_path) if template_path is not None else PROMPT_TEMPLATE
    raw = path.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("<!--")
    )
    if raw.endswith("\n"):
        body += "\n"
    unknown = sorted(set(_TOKEN.findall(body)) - set(_PROMPT_TOKENS))
    if unknown:
        raise ValueError(f"未知のトークン: {unknown} ({path})")
    missing = [name for name in _PROMPT_TOKENS if "{{" + name + "}}" not in body]
    if missing:
        raise ValueError(f"テンプレートにトークンがありません: {missing} ({path})")
    rendered = body
    for name, value in _PROMPT_TOKENS.items():
        rendered = rendered.replace("{{" + name + "}}", value())
    return rendered


def _profiles_root() -> Path:
    override = os.environ.get("DREAMS_PROFILES_ROOT")
    if override:
        return Path(override).expanduser()
    return _hermes_root() / "profiles"


def _dreams_jobs(jobs_path: Path) -> List[Dict[str, Any]]:
    """job 名ではなく script で同定する（プロファイルごとに名前が違うため）。

    jobs.json が読めないときは空リストではなく例外にする。空リストだと「ジョブが無い」のか
    「ファイルが壊れている」のか区別できず、点検が静かに通ってしまう。
    """
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"jobs.json を読めない: {jobs_path} ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"jobs.json の形式が想定と違う: {jobs_path}")
    jobs = data.get("jobs", [])
    return [job for job in jobs if Path(str(job.get("script") or "")).name == DREAMS_JOB_SCRIPT]


def _jobs_files(root: Path) -> List[Tuple[str, Path]]:
    files: List[Tuple[str, Path]] = []
    root_jobs = root.parent / "cron" / "jobs.json"
    if root_jobs.exists():
        files.append(("(default)", root_jobs))
    if not root.is_dir():
        return files
    for home in sorted(item for item in root.iterdir() if item.is_dir()):
        jobs_path = home / "cron" / "jobs.json"
        if jobs_path.exists():
            files.append((home.name, jobs_path))
    return files


def check_cron_prompt(
    template_path: Optional[Path] = None, profiles_root: Optional[Path] = None
) -> Dict[str, Any]:
    """各プロファイルの保存済みプロンプトを描画結果と突き合わせる（読み取り専用）。"""
    rendered = render_cron_prompt(template_path)
    sha = _text_sha(rendered)
    root = Path(profiles_root) if profiles_root is not None else _profiles_root()
    rows: List[Dict[str, Any]] = []
    missing: List[str] = []
    unreadable: List[str] = []
    for profile, jobs_path in _jobs_files(root):
        try:
            jobs = _dreams_jobs(jobs_path)
        except ValueError:
            unreadable.append(profile)
            continue
        if not jobs:
            missing.append(profile)
            continue
        for job in jobs:
            stored = job.get("prompt") or ""
            state = "same" if stored == rendered else ("no_prompt" if not stored else "diff")
            rows.append(
                {
                    "profile": profile,
                    "job_id": job.get("id"),
                    "name": job.get("name"),
                    "chars": len(stored),
                    "stored_sha": _text_sha(stored),
                    "state": state,
                }
            )
    return {
        "ok": not unreadable and not missing and all(row["state"] == "same" for row in rows),
        "template_sha": sha,
        "template_chars": len(rendered),
        "jobs": rows,
        "missing_profiles": missing,
        "unreadable_profiles": unreadable,
    }


CRON_EDIT_TIMEOUT = 120


def _run_cron_edit(cmd: List[str]) -> Tuple[int, str]:
    """`hermes cron edit` を実行する。失敗理由を捨てずに (終了コード, stderr) で返す。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CRON_EDIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, f"{CRON_EDIT_TIMEOUT} 秒で終わらなかった"
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stderr or "").strip()


def apply_cron_prompt(
    profile: str,
    template_path: Optional[Path] = None,
    jobs_path: Optional[Path] = None,
    runner: Optional[Any] = None,
) -> Dict[str, Any]:
    """保存済みプロンプトを描画結果で置き換え、書き込み後に読み直して検証する。

    ``runner`` は ``cmd -> 終了コード`` か ``cmd -> (終了コード, stderr)`` を返す差し替え用フック。
    """
    rendered = render_cron_prompt(template_path)
    path = Path(jobs_path) if jobs_path is not None else _profiles_root() / profile / "cron" / "jobs.json"
    if not path.exists():
        return {"ok": False, "error": f"jobs.json がありません: {path}", "applied": []}
    try:
        jobs = _dreams_jobs(path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "applied": []}
    if not jobs:
        return {"ok": False, "error": f"{profile} に dreams ジョブがありません", "applied": []}

    run = runner or _run_cron_edit
    applied: List[Dict[str, Any]] = []
    for job in jobs:
        job_id = job.get("id") or ""
        outcome = run(["hermes", "-p", profile, "cron", "edit", job_id, "--prompt", rendered])
        if isinstance(outcome, tuple):
            code, stderr = outcome
        else:
            code, stderr = outcome, ""
        row: Dict[str, Any] = {"job_id": job_id, "name": job.get("name"), "exit_code": code}
        if stderr:
            row["stderr"] = stderr
        applied.append(row)

    try:
        stored = {row.get("id"): (row.get("prompt") or "") for row in _dreams_jobs(path)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "applied": applied}
    for row in applied:
        current = stored.get(row["job_id"])
        row["chars"] = len(current or "")
        row["verified"] = current == rendered
    return {
        "ok": all(row["verified"] for row in applied),
        "template_sha": _text_sha(rendered),
        "applied": applied,
    }


def _file_sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent


def check_scripts(
    profile: str,
    profiles_root: Optional[Path] = None,
    plugin_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """プロファイルの scripts/ 内のブリッジスクリプトとリポジトリ原本を突き合わせる。"""
    proot = Path(profiles_root) if profiles_root is not None else _profiles_root()
    profile_home = proot / profile
    scripts_dir = profile_home / "scripts"
    src_root = Path(plugin_root) if plugin_root is not None else _plugin_root()
    src_scripts = src_root / "scripts"

    missing: List[str] = []
    diff: List[str] = []
    same: List[str] = []

    for name in DREAMS_BRIDGE_SCRIPTS:
        target = scripts_dir / name
        src = src_scripts / name
        if not target.exists():
            missing.append(name)
        elif _file_sha(target) != _file_sha(src):
            diff.append(name)
        else:
            same.append(name)

    if missing and len(missing) == len(DREAMS_BRIDGE_SCRIPTS):
        status = "missing"
    elif missing or diff:
        status = "diff"
    else:
        status = "same"

    return {
        "profile": profile,
        "status": status,
        "same": same,
        "diff": diff,
        "missing": missing,
    }


def sync_scripts(
    profile: str,
    profiles_root: Optional[Path] = None,
    plugin_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """プロファイルの scripts/ へ原本ブリッジスクリプトをコピーして同期する。"""
    proot = Path(profiles_root) if profiles_root is not None else _profiles_root()
    profile_home = proot / profile
    scripts_dir = profile_home / "scripts"
    src_root = Path(plugin_root) if plugin_root is not None else _plugin_root()
    src_scripts = src_root / "scripts"

    scripts_dir.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []

    for name in DREAMS_BRIDGE_SCRIPTS:
        target = scripts_dir / name
        src = src_scripts / name
        if not target.exists() or _file_sha(target) != _file_sha(src):
            shutil.copy2(src, target)
            copied.append(name)

    return {
        "ok": True,
        "profile": profile,
        "copied": copied,
    }


def install_profile(
    profile: str,
    schedule: str = "30 23 * * *",
    profiles_root: Optional[Path] = None,
    plugin_root: Optional[Path] = None,
    runner: Optional[Any] = None,
) -> Dict[str, Any]:
    """新規プロファイルへの導入、または既存プロファイルへの再セットアップ（冪等）。"""
    proot = Path(profiles_root) if profiles_root is not None else _profiles_root()
    profile_home = proot / profile
    profile_home.mkdir(parents=True, exist_ok=True)

    src_root = Path(plugin_root) if plugin_root is not None else _plugin_root()
    run = runner or _run_cron_edit

    # 1. symlink plugins/dreams-diary
    plugins_dir = profile_home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    link = plugins_dir / "dreams-diary"
    if link.is_symlink():
        if link.resolve() != src_root.resolve():
            link.unlink()
            link.symlink_to(src_root.resolve())
    elif not link.exists():
        link.symlink_to(src_root.resolve())

    # 2. sync scripts
    sync_res = sync_scripts(profile, profiles_root=proot, plugin_root=src_root)

    # 3. plugins enable
    run(["hermes", "-p", profile, "plugins", "enable", "dreams-diary"])

    # 4. cron job (check existing)
    jobs_path = profile_home / "cron" / "jobs.json"
    rendered = render_cron_prompt()

    existing_jobs: List[Dict[str, Any]] = []
    if jobs_path.exists():
        try:
            existing_jobs = _dreams_jobs(jobs_path)
        except ValueError:
            existing_jobs = []

    if existing_jobs:
        apply_res = apply_cron_prompt(profile, jobs_path=jobs_path, runner=run)
        action = "updated"
        ok = apply_res.get("ok", False)
    else:
        cmd = [
            "hermes",
            "-p",
            profile,
            "cron",
            "create",
            schedule,
            "--name",
            "dreams-diary",
            "--script",
            DREAMS_JOB_SCRIPT,
            "--continuity",
            rendered,
        ]
        outcome = run(cmd)
        code = outcome[0] if isinstance(outcome, tuple) else outcome
        action = "created"
        ok = (code == 0)

    return {
        "ok": ok,
        "profile": profile,
        "action": action,
        "scripts_copied": sync_res.get("copied", []),
    }


def sync_all(
    check_only: bool = False,
    profiles: Optional[List[str]] = None,
    profiles_root: Optional[Path] = None,
    plugin_root: Optional[Path] = None,
    runner: Optional[Any] = None,
) -> Dict[str, Any]:
    """全プロファイル（または指定プロファイル）の scripts および cron プロンプトを同期・点検する。"""
    proot = Path(profiles_root) if profiles_root is not None else _profiles_root()
    if profiles is None:
        target_profiles = sorted(
            item.name for item in proot.iterdir() if item.is_dir() and not item.name.startswith(".")
        )
    else:
        target_profiles = list(dict.fromkeys(profiles))

    results: List[Dict[str, Any]] = []
    all_ok = True

    for p in target_profiles:
        scripts_info = check_scripts(p, profiles_root=proot, plugin_root=plugin_root)
        jobs_path = proot / p / "cron" / "jobs.json"

        prompt_state = "missing"
        job_id = None
        if jobs_path.exists():
            try:
                jobs = _dreams_jobs(jobs_path)
                if jobs:
                    job_id = jobs[0].get("id")
                    rendered = render_cron_prompt()
                    stored = jobs[0].get("prompt") or ""
                    prompt_state = "same" if stored == rendered else "diff"
            except ValueError:
                prompt_state = "unreadable"

        if check_only:
            is_same = (scripts_info["status"] == "same" and prompt_state == "same")
            if not is_same:
                all_ok = False
            results.append({
                "profile": p,
                "scripts": scripts_info,
                "prompt": {"state": prompt_state, "job_id": job_id},
            })
        else:
            sync_res = sync_scripts(p, profiles_root=proot, plugin_root=plugin_root)
            apply_res = None
            if prompt_state in ("diff", "same"):
                apply_res = apply_cron_prompt(p, jobs_path=jobs_path, runner=runner)
                p_ok = apply_res.get("ok", False)
            elif prompt_state == "missing":
                p_ok = True
            else:
                p_ok = False

            if not p_ok or not sync_res.get("ok"):
                all_ok = False

            results.append({
                "profile": p,
                "scripts_copied": sync_res.get("copied", []),
                "prompt_applied": apply_res,
                "ok": p_ok,
            })

    return {
        "ok": all_ok,
        "check_only": check_only,
        "profiles": results,
    }
