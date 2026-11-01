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
    """アクティブなプロファイルの home を返す。

    優先順: 明示指定 → Hermes のコンテキストローカル home → HERMES_HOME →
    HERMES_PROFILE → ~/.hermes/active_profile → ~/.hermes。
    """
    if profile and str(profile).strip():
        p = Path(os.path.expanduser(f"~/.hermes/profiles/{str(profile).strip()}"))
        if p.exists():
            return p
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        pass
    else:
        return Path(get_hermes_home()).expanduser()
    if os.environ.get("HERMES_HOME", "").strip():
        return Path(os.path.expanduser(os.environ["HERMES_HOME"].strip()))
    prof = os.environ.get("HERMES_PROFILE", "").strip()
    if prof:
        p = Path(os.path.expanduser(f"~/.hermes/profiles/{prof}"))
        if p.exists():
            return p
    try:
        active = Path(os.path.expanduser("~/.hermes/active_profile"))
        if active.exists():
            name = active.read_text(encoding="utf-8").strip()
            if name:
                p = Path(os.path.expanduser(f"~/.hermes/profiles/{name}"))
                if p.exists():
                    return p
    except OSError:
        pass
    return Path(os.path.expanduser("~/.hermes"))


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


def discover_repos(base: Optional[str] = None) -> List[Path]:
    """ハッシュ検証に使うリポジトリ一覧。環境変数 DREAMS_REPOS で上書き可。"""
    env = os.environ.get("DREAMS_REPOS", "").strip()
    if env:
        return [Path(os.path.expanduser(p)) for p in env.split(":") if p.strip()]
    root = Path(os.path.expanduser(base or "~/Dev/git"))
    repos: List[Path] = []
    for cand in (root, *sorted(root.glob("*"))):
        if (cand / ".git").exists():
            repos.append(cand)
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
    body = (
        f"# 夢日記の素材 — {day}\n\n"
        f"生成: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} / プロファイル: {home.name}\n"
        f"日記ファイル: {diary} （{'あり（上書き禁止）' if diary.exists() else 'なし'}）\n\n"
        f"## 会話\n{_sessions_material(day, home)}\n"
        f"## コミット\n{_commits_material(day, repos)}\n"
        f"## この日 memory に追加された項目\n{_memory_material(day, home)}\n"
    )
    truncated = len(body) > max_chars
    body = body[:max_chars]
    if truncated:
        body += "\n\n（素材は上限で切り詰められた。全文は _material 側のファイルを見る）\n"
    material_path = ddir / "_material" / f"{day}.md"
    try:
        material_path.parent.mkdir(parents=True, exist_ok=True)
        material_path.write_text(body, encoding="utf-8")
    except OSError:
        pass
    return {
        "date": day,
        "profile_home": str(home),
        "material_path": str(material_path),
        "diary_path": str(diary),
        "diary_exists": diary.exists(),
        "truncated": truncated,
        "material": body,
    }


# ------------------------------------------------------------------- writing


def write_skeleton(
    day: Optional[str] = None,
    home: Optional[Path] = None,
    author: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """節構成だけ入った空の日記を用意する。本文はエージェントが書く。"""
    day = _valid_date(day)
    home = home or resolve_profile_home()
    ddir = home / "dreams"
    ddir.mkdir(parents=True, exist_ok=True)
    path = ddir / f"{day}.md"
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
        "sessions:\n"
        "messages:\n"
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
    for f in sorted(ddir.iterdir()):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(-\d)?\.md", f.name):
            continue
        text = f.read_text(encoding="utf-8")
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
    path = ddir / "INDEX.md"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return {"ok": True, "count": len(rows), "path": str(path)}


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
