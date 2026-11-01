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
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
INJECTED_PREFIXES = ("[IMPORTANT:", "[The user sent", "[System", "[SYSTEM")
MAX_CHARS_SOFT = 3000
MIN_CHARS_SOFT = 300


# ---------------------------------------------------------------- profile paths


def resolve_profile_home(profile: Optional[str] = None) -> Path:
    """アクティブなプロファイルの home を返す（解決経路は resolve_profile_home_source を見る）。"""
    return resolve_profile_home_source(profile)[0]


def resolve_profile_home_source(profile: Optional[str] = None) -> Tuple[Path, str]:
    """home と、その解決経路を返す。

    経路: explicit（引数）→ context（Hermes のコンテキストローカル override）→
    env（HERMES_HOME）→ profile_env（HERMES_PROFILE）→ active_profile（~/.hermes の共通ポインタ）
    → root（~/.hermes）。

    active_profile / root は「たまたま最後に触ったプロファイル」を指しうる。黙って
    そこへ書くと別プロファイルの記憶や日記を壊すため、呼び出し側が警告できるよう
    経路も返す。
    """
    if profile and str(profile).strip():
        p = Path(os.path.expanduser(f"~/.hermes/profiles/{str(profile).strip()}"))
        if p.exists():
            return p, "explicit"
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
        p = Path(os.path.expanduser(f"~/.hermes/profiles/{prof}"))
        if p.exists():
            return p, "profile_env"
    try:
        active = Path(os.path.expanduser("~/.hermes/active_profile"))
        if active.exists():
            name = active.read_text(encoding="utf-8").strip()
            if name:
                p = Path(os.path.expanduser(f"~/.hermes/profiles/{name}"))
                if p.exists():
                    return p, "active_profile"
    except OSError:
        pass
    return Path(os.path.expanduser("~/.hermes")), "root"


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
    try:
        datetime.datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"日付は YYYY-MM-DD で指定してください（受領: {day!r}）") from exc
    return day


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _day_bounds(day: str) -> Tuple[float, float]:
    start = datetime.datetime.strptime(day, "%Y-%m-%d")
    return start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()


# ------------------------------------------------------------------ material


def _sessions_material(day: str, home: Path) -> str:
    db = state_db_path(home)
    if not db.exists():
        return "（state.db が見つからない）\n"
    t0, t1 = _day_bounds(day)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """select session_id, role, content, tool_name, timestamp
               from messages
               where timestamp >= ? and timestamp < ?
               order by timestamp""",
            (t0, t1),
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        return f"（会話の取得に失敗: {exc}）\n"
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
            if msg.startswith(INJECTED_PREFIXES):
                continue
            if not s["users"] or s["users"][-1] != msg:
                s["users"].append(msg)
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
        for u in s["users"][:MAX_USER_MSGS_PER_SESSION]:
            out.append(f"- 発言: {u}\n")
        if len(s["users"]) > MAX_USER_MSGS_PER_SESSION:
            out.append(f"- （ほか {len(s['users']) - MAX_USER_MSGS_PER_SESSION} 件の発言）\n")
    return "".join(out)


def _memory_material(day: str, home: Path) -> str:
    db = memory_db_path(home)
    if not db.exists():
        return "（memory.db が見つからない）\n"
    rows = None
    err: Optional[Exception] = None
    # WAL モードの DB は -shm/-wal が無い瞬間に mode=ro で開けないことがある。
    for uri, kw in ((f"file:{db}?mode=ro", {"uri": True}), (str(db), {})):
        try:
            con = sqlite3.connect(uri, timeout=10, **kw)
            rows = con.execute(
                "select category, content from memories where date(created_at) = date(?)",
                (day,),
            ).fetchall()
            con.close()
            err = None
            break
        except sqlite3.Error as exc:
            err = exc
            rows = None
    if rows is None:
        return f"（memory 取得失敗: {err}）\n"
    if not rows:
        return "（0 件。この日 memory に追加された項目はない）\n"
    return "\n".join(f"- [{c}] {_clip(t, 160)}" for c, t in rows) + "\n"


REPO_SCAN_MAX_DEPTH = 3
REPO_SCAN_SKIP = {"node_modules", "venv", "__pycache__", "site-packages", "build", "dist"}


def discover_repos(base: Optional[str] = None, max_depth: int = REPO_SCAN_MAX_DEPTH) -> List[Path]:
    """ハッシュ検証に使うリポジトリ一覧。環境変数 DREAMS_REPOS で上書き可。

    1階層だけを見ると `~/Dev/git/<group>/<repo>` のような入れ子のリポジトリが
    「存在しない」ことになり、実在するコミットを捏造と誤判定する。深さ3まで辿る。
    """
    env = os.environ.get("DREAMS_REPOS", "").strip()
    if env:
        return [Path(os.path.expanduser(p)) for p in env.split(":") if p.strip()]
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


def _commits_material(day: str, repos: Optional[List[Path]] = None) -> str:
    repos = repos if repos is not None else discover_repos()
    if not repos:
        return "（リポジトリが見つからない）\n"
    blocks = []
    for repo in repos:
        try:
            log = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "log",
                    f"--since={day} 00:00",
                    f"--until={day} 23:59",
                    "--pretty=format:%h %ad %s",
                    "--date=format:%H:%M",
                    "--stat",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if log:
            lines = [ln for ln in log.splitlines() if ln.strip()][:40]
            blocks.append(f"[{repo.name}]\n" + "\n".join(lines))
    return ("\n\n".join(blocks) + "\n") if blocks else f"（{day} のコミットは無い）\n"


def collect_material(
    day: Optional[str] = None,
    home: Optional[Path] = None,
    max_chars: int = DEFAULT_MAX_MATERIAL,
    repos: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    day = _valid_date(day)
    home = home or resolve_profile_home()
    ddir = home / "dreams"
    diary = ddir / f"{day}.md"
    header = (
        f"# 夢日記の素材 — {day}\n\n"
        f"生成: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} / プロファイル: {home.name}\n"
        f"日記ファイル: {diary} （{'あり（上書き禁止）' if diary.exists() else 'なし'}）\n\n"
    )
    conversation = _sessions_material(day, home)
    commits = _commits_material(day, repos)
    memory = _memory_material(day, home)
    body_full = (
        header
        + f"## 会話\n{conversation}\n"
        + f"## コミット\n{commits}\n"
        + f"## この日 memory に追加された項目\n{memory}\n"
    )

    # 全文は必ず _material 側へ保存する。ここを切り詰めると「全文はそちらを見る」
    # という案内が嘘になり、根拠の再確認ができなくなる。
    material_path = ddir / "_material" / f"{day}.md"
    material_saved = True
    try:
        material_path.parent.mkdir(parents=True, exist_ok=True)
        material_path.write_text(body_full, encoding="utf-8")
    except OSError:
        material_saved = False

    truncated = len(body_full) > max_chars
    body = body_full
    if truncated:
        conv_head = "## 会話\n"
        commit_head = "\n## コミット\n"
        mem_head = "\n## この日 memory に追加された項目\n"
        fixed = len(header) + len(conv_head) + len(commit_head) + len(mem_head)
        note = f"（素材は上限 {max_chars} 字に合わせて切り詰めた。全文: {material_path}）\n"
        available = max_chars - fixed - len(note)
        if available < 100:
            body = body_full[: max(0, max_chars)] + "\n（素材は上限で切り詰めた）\n"
        else:
            # 根拠（コミット・memory）に最低限の枠を確保してから、残りを会話に回す。
            # 会話が長い日に根拠節ごと消えると、日記の裏付けが失われる。
            ev_body = len(commits) + len(memory)
            ev_budget = min(ev_body, max(300, int(available * 0.4)))
            mem_budget = min(len(memory), max(0, min(400, ev_budget)))
            commit_budget = max(0, ev_budget - mem_budget)
            conv_budget = max(0, available - ev_budget)
            conv_text = (
                conversation
                if len(conversation) <= conv_budget
                else conversation[:conv_budget] + "\n（会話は上限で切り詰めた）\n"
            )
            commit_text = (
                commits
                if len(commits) <= commit_budget
                else commits[:commit_budget] + "\n（コミットは上限で切り詰めた）\n"
            )
            memory_text = (
                memory if len(memory) <= mem_budget else memory[:mem_budget] + "\n"
            )
            body = (
                header
                + conv_head
                + conv_text
                + commit_head
                + commit_text
                + mem_head
                + memory_text
                + note
            )
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
    day = _valid_date(day)
    home = home or resolve_profile_home()
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
    author = author or home.name
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
        "sections": list(SECTIONS),
        "counts_filled": bool(sessions_text or messages_text),
        "message": "節見出しだけ置いた。素材を読み、自分の声で各節を埋めること。",
    }


# ------------------------------------------------------------------- checks


HEX_IN_TICKS = re.compile(r"`([0-9a-f]{7,40})`")
COMMIT_CONTEXT = re.compile(
    r"(commit|コミット|\((?:fix|feat|docs|chore|test|refactor|perf|style|build|ci)\()"
)
EXTERNAL_CONTEXT = re.compile(
    r"(origin/|upstream|GitHub|github\.com|外部|リモート|hermes-agent の)"
)


def _commit_candidates(text: str) -> List[str]:
    """コミットだと分かる文脈で書かれたハッシュだけを候補にする。

    Docker のホスト名、cron のジョブID、run ID、日付、ゲームのエラーコードも
    16進に見えるため、文脈なしに拾うと誤検知だらけになる。
    """
    found: List[str] = []
    for line in text.splitlines():
        m = re.match(r"^commit:\s*([0-9a-f]{7,40})\s*$", line.strip())
        if m:
            found.append(m.group(1))
            continue
        if not (COMMIT_CONTEXT.search(line) or EXTERNAL_CONTEXT.search(line)):
            continue
        found.extend(HEX_IN_TICKS.findall(line))
    return [h for h in found if re.search(r"[a-f]", h)]


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

    issues: List[Dict[str, str]] = []
    lines = text.splitlines()
    frontmatter_end = next(
        (i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    has_frontmatter = (
        bool(lines)
        and lines[0].strip() == "---"
        and frontmatter_end is not None
    )
    if not has_frontmatter:
        issues.append(
            {
                "level": "error",
                "code": "missing_frontmatter",
                "detail": "日付などのメタデータを含む YAML frontmatter が必要。",
            }
        )
    else:
        frontmatter_fields = {}
        frontmatter_lines = lines[1:frontmatter_end]
        for line in frontmatter_lines:
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$", line)
            if match:
                frontmatter_fields[match.group(1)] = (match.group(2) or "").strip()
        required_fields = ("date", "author", "sessions", "messages", "commit")
        missing_fields = [key for key in required_fields if key not in frontmatter_fields]
        if missing_fields:
            issues.append(
                {
                    "level": "error",
                    "code": "missing_frontmatter_field",
                    "detail": "必須フィールドがない: " + ", ".join(missing_fields),
                }
            )
        else:
            def scalar(key: str) -> str:
                value = frontmatter_fields[key].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1].strip()
                return value

            frontmatter_date = scalar("date")
            try:
                frontmatter_date = _valid_date(frontmatter_date)
            except ValueError:
                issues.append(
                    {
                        "level": "error",
                        "code": "invalid_frontmatter_date",
                        "detail": f"frontmatter の date が YYYY-MM-DD ではない: {frontmatter_date!r}",
                    }
                )
            else:
                filename_date = re.fullmatch(
                    r"(\d{4}-\d{2}-\d{2})(?:-\d)?", target.stem
                )
                if filename_date and frontmatter_date != filename_date.group(1):
                    issues.append(
                        {
                            "level": "error",
                            "code": "frontmatter_date_mismatch",
                            "detail": f"frontmatter の date {frontmatter_date} がファイル名の日付 {filename_date.group(1)} と一致しない。",
                        }
                    )

            invalid_fields = [
                key
                for key in ("author", "sessions", "messages")
                if not scalar(key)
            ]
            invalid_fields.extend(
                key
                for key in ("sessions", "messages")
                if scalar(key) and not re.fullmatch(r"\d+", scalar(key))
            )
            has_commit_list = any(
                re.match(r"^\s+-\s+\S", line) for line in frontmatter_lines
            )
            if not scalar("commit") and not has_commit_list:
                invalid_fields.append("commit")
            if invalid_fields:
                issues.append(
                    {
                        "level": "error",
                        "code": "invalid_frontmatter_field",
                        "detail": "値が空または形式不正: " + ", ".join(sorted(set(invalid_fields)),),
                    }
                )

    missing = [s for s in SECTIONS if s not in text]
    for s in missing:
        issues.append({"level": "error", "code": "missing_section", "detail": s})

    if "あなた" in text:
        issues.append(
            {
                "level": "error",
                "code": "second_person",
                "detail": "「あなた」は禁止。三人称『ユーザー』＋引用か伝聞で書く。",
            }
        )

    body_chars = len(re.sub(r"\s", "", text))
    if body_chars < MIN_CHARS_SOFT:
        issues.append(
            {"level": "warn", "code": "too_short", "detail": f"{body_chars} 字（{MIN_CHARS_SOFT} 字未満）"}
        )
    if body_chars > MAX_CHARS_SOFT:
        issues.append(
            {"level": "warn", "code": "too_long", "detail": f"{body_chars} 字（{MAX_CHARS_SOFT} 字超）"}
        )

    repos = repos if repos is not None else discover_repos()
    unresolved: List[str] = []
    external: List[str] = []
    for h in sorted(set(_commit_candidates(text))):
        if _resolve_hash(h, repos) is not None:
            continue
        line = next((ln for ln in text.splitlines() if h in ln), "")
        if EXTERNAL_CONTEXT.search(line):
            external.append(h)
        else:
            unresolved.append(h)
    for h in unresolved:
        issues.append(
            {
                "level": "error",
                "code": "unresolved_hash",
                "detail": f"{h} はコミットとして書かれているが ~/Dev/git 配下のどのリポジトリにも存在しない。捏造の疑い。",
            }
        )
    for h in external:
        issues.append(
            {
                "level": "warn",
                "code": "unverified_external_hash",
                "detail": f"{h} は外部（upstream 等）のコミットなのでローカルでは検証できない。出典を明記しているか確認。",
            }
        )

    errors = [i for i in issues if i["level"] == "error"]
    return {
        "ok": not errors,
        "path": str(target),
        "chars": body_chars,
        "repos_checked": [r.name for r in repos],
        "errors": sorted({i["code"] for i in errors}),
        "issues": issues,
    }


def build_index(home: Optional[Path] = None) -> Dict[str, Any]:
    home = home or resolve_profile_home()
    ddir = home / "dreams"
    if not ddir.exists():
        return {"ok": False, "count": 0, "path": str(ddir / "INDEX.md")}

    def field(text: str, key: str) -> str:
        m = re.search(rf"^{key}:\s*(.+)$", text, re.M)
        return m.group(1).strip().strip('"') if m else ""

    def tail(text: str) -> str:
        m = re.search(rf"{re.escape(SECTIONS[-1])}\n+(.+?)(?:\n## |\Z)", text, re.S)
        return " ".join(m.group(1).split()) if m else ""

    rows = []
    skipped: List[Dict[str, str]] = []
    for f in sorted(ddir.iterdir()):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(-\d)?\.md", f.name):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 1本読めないだけで目次全体を失わない。理由は INDEX にも残す。
            skipped.append({"file": f.name, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        rows.append((f.stem, field(text, "sessions"), field(text, "messages"), tail(text)))
    rows.sort(reverse=True)

    out = ["# 夢日記 目次\n", f"全 {len(rows)} 本。各日の末尾の申し送りだけ抜粋。\n"]
    for date, s, m, t in rows:
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
