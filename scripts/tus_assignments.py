#!/usr/bin/env python3
"""tus_assignments.py — LETUS の課題を SQLite に貯める（cabetus の LetusParsing 移植）

LETUS のコースページを走査し、課題（assign / quiz / turnitin 等）の締切・提出状況を
`<data>/assignments.db` に UPSERT する。DB は何度 sync しても壊れない（id = sha1(url)）。

  tus_assignments.py --sync               # LETUS を走査して DB 更新（初回は数分かかる）
  tus_assignments.py --list [--days 7]     # 未完了を締切順に表示
  tus_assignments.py --brief --days 7      # 朝ブリーフィング用テキスト
  tus_assignments.py --json                # 機械可読（cron のエージェント用）
  tus_assignments.py --monitor              # 締切3日以内/期限切れのみ（cron の差分検知用）
  tus_assignments.py --hide "【Y班】"       # タイトルに含む課題を非表示（自分の班外など）

元実装: cabetus の LetusParsing / 課題スキャン（締切パース・提出状況判定の考え方）。
CDP まわりは tusinfo_common.CDP を使う（＝他 bot でも同じ設定で動く）。

締切の扱い:
  deadline 列は epoch ミリ秒（JST 基準）。SQL で日時にするときは
  `datetime(deadline/1000, 'unixepoch', '+9 hours')`。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tusinfo_common as C  # noqa: E402

JST = timezone(timedelta(hours=9))
DB_PATH = C.data_dir() / "assignments.db"
STATE_PATH = C.data_dir() / "assign_sync_state.json"

# 課題とみなすモジュール（cabetus の STRICT_MODS 相当）。
# --broad で標準外モジュール（workshop / feedback / choice / questionnaire / lti）も拾う。
STRICT_MODS = ("assign", "quiz", "turnitintool", "turnitintooltwo")
STANDARD_EXTRA_MODS = ("workshop", "feedback", "choice", "questionnaire", "lti")

DEADLINE_KEYWORDS = [
    "提出期限", "提出締切", "締切日時", "締切", "期限", "終了予定", "終了済み", "終了日時",
    "利用終了日時", "受験終了", "回答終了",
    "Due date", "Closing date", "Close date", "Closes", "Due", "Close",
]

JP_DATE = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:\s*[(（][^)）]*[)）])?\s*(?:(\d{1,2})\s*(?:時|:|：)\s*(\d{1,2})?\s*分?)?")
JP_DATE_NO_YEAR = re.compile(
    r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:\s*[(（][^)）]*[)）])?\s*(?:(\d{1,2})\s*(?:時|:|：)\s*(\d{1,2})?\s*分?)?")
SLASH_DATE = re.compile(r"(\d{4})[/／](\d{1,2})[/／](\d{1,2})(?:\s+(\d{1,2})[:：](\d{2}))?")


# --------------------------------------------------------------------------- #
# テキストユーティリティ（cabetus normalizeText / parseDeadline 相当）
# --------------------------------------------------------------------------- #
def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()


def to_epoch(y: int, mo: int, d: int, h: int, mi: int) -> int | None:
    try:
        return int(datetime(y, mo, d, h, mi, tzinfo=JST).timestamp() * 1000)
    except ValueError:
        return None


def parse_deadline(text: str, now: datetime | None = None) -> int | None:
    """締切テキスト → epoch ms (JST)。年が無ければ今日に近い年で解釈。"""
    now = now or datetime.now(JST)
    t = norm(text)

    m = JP_DATE.search(t)
    if m:
        return to_epoch(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)) if m.group(4) else 23,
                        int(m.group(5)) if m.group(5) else 59)

    m = SLASH_DATE.search(t)
    if m:
        return to_epoch(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)) if m.group(4) else 23,
                        int(m.group(5)) if m.group(5) else 59)

    m = JP_DATE_NO_YEAR.search(t)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        y = now.year if (mo, d) >= (now.month, now.day) else now.year + 1
        return to_epoch(y, mo, d,
                        int(m.group(3)) if m.group(3) else 23,
                        int(m.group(4)) if m.group(4) else 59)
    return None


def extract_deadline_text(plain: str) -> str:
    """締切キーワードの位置から 320 字切り出す（cabetus extractDeadlineText 移植）。"""
    t = norm(plain)
    low = t.lower()
    best = -1
    for kw in DEADLINE_KEYWORDS:
        i = low.find(kw.lower())
        if i >= 0 and (best == -1 or i < best):
            best = i
    return t[best:best + 320] if best >= 0 else ""


def extract_submission_status(text: str, url: str) -> str:
    """submitted / not_submitted / completed / unknown。

    「未提出」を先に見る（"submitted" は "not submitted" の部分文字列）。
    注意: Moodle の実表示は「まだ提出されていません。」で「未提出」に一致しないことがある。
    その場合 unknown になるので、期限切れ＋unknown は mod/assign を直接見て確定させる。
    """
    t = norm(text).lower()
    if "/mod/quiz/" in url.lower():
        if "ステータス 終了" in t or "status finished" in t or "受験済み" in t or "attempt finished" in t:
            return "completed"
        if "利用できません" in t or "not available" in t or "未受験" in t or "not attempted" in t:
            return "not_submitted"
        return "unknown"
    if "未提出" in t or "not submitted" in t or "まだ提出されていません" in t:
        return "not_submitted"
    if "提出済み" in t or "submitted" in t:
        return "submitted"
    return "unknown"


def resolve_lifecycle(text: str, status: str, deadline: int | None, now_ms: int) -> str:
    """before_start / submitted / passed / active。"""
    if "開始予定" in norm(text) and "利用できません" in norm(text):
        return "before_start"
    if status in ("submitted", "completed"):
        return "submitted"
    if deadline is not None and deadline < now_ms:
        return "passed"
    return "active"


# --------------------------------------------------------------------------- #
# DB
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    id            TEXT PRIMARY KEY,          -- sha1(url)
    course_code   TEXT,
    course_name   TEXT,
    title         TEXT,
    url           TEXT,
    mod           TEXT,
    open_at       INTEGER,                   -- epoch ms JST
    deadline      INTEGER,                   -- epoch ms JST
    deadline_raw  TEXT,
    submit_status TEXT,                      -- submitted/not_submitted/completed/unknown
    lifecycle     TEXT,                      -- before_start/active/submitted/passed
    first_seen    TEXT,
    last_seen     TEXT,
    status_changed TEXT,
    hidden        INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sync_log (
    at TEXT, found INTEGER, new INTEGER, changed INTEGER, error TEXT
);
"""


def open_db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con


def upsert(con: sqlite3.Connection, rec: dict, now_iso: str) -> tuple[bool, bool]:
    """(new?, changed?) を返す。ignore_patterns に一致する課題は hidden=1。"""
    hide = 1 if any(p in (rec["title"] or "") for p in C.ignore_patterns()) else 0
    old = con.execute("SELECT * FROM assignments WHERE id=?", (rec["id"],)).fetchone()
    if old is None:
        con.execute(
            "INSERT INTO assignments (id, course_code, course_name, title, url, mod,"
            " open_at, deadline, deadline_raw, submit_status, lifecycle,"
            " first_seen, last_seen, hidden) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], rec["course_code"], rec["course_name"], rec["title"], rec["url"],
             rec["mod"], rec["open_at"], rec["deadline"], rec["deadline_raw"],
             rec["submit_status"], rec["lifecycle"], now_iso, now_iso, hide))
        return True, False
    changed = (old["submit_status"] != rec["submit_status"]
               or old["lifecycle"] != rec["lifecycle"]
               or old["deadline"] != rec["deadline"])
    con.execute(
        "UPDATE assignments SET deadline=?, deadline_raw=?, open_at=?, submit_status=?,"
        " lifecycle=?, last_seen=?, status_changed=?, hidden=? WHERE id=?",
        (rec["deadline"], rec["deadline_raw"], rec["open_at"], rec["submit_status"],
         rec["lifecycle"], now_iso, now_iso if changed else old["status_changed"],
         hide or old["hidden"], rec["id"]))
    return False, changed


# --------------------------------------------------------------------------- #
# スクレイピング
# --------------------------------------------------------------------------- #
JS_COURSES = """
Array.from(document.querySelectorAll('a[href*="course/view.php"]')).map(a => ({
  n: (a.innerText||'').trim().replace(/\\s+/g,' '),
  h: (a.getAttribute('href')||'').split('#')[0]
}))
"""

JS_ACTIVITIES = """
Array.from(document.querySelectorAll('li.activity')).map(li => {
  const a = li.querySelector('a.aalink') || li.querySelector('a[href*="/mod/"]');
  if (!a) return null;
  const href = (a.href||'').split('#')[0];
  const m = href.match(/\\/mod\\/([a-z0-9_]+)\\//);
  return {
    title: (a.innerText||'').trim().replace(/\\s+/g,' '),
    url: href, mod: m ? m[1] : '',
    extra: (li.innerText||'').trim().replace(/\\s+/g,' ').slice(0,400)
  };
}).filter(x => x && x.url.includes('/mod/'))
"""

JS_ACTIVITY_PAGE = """
(() => {
  const t = s => (s||'').replace(/\\s+/g,' ').trim();
  const dates = t((document.querySelector('[data-region="activity-dates"]')||{}).innerText);
  const stat  = t((document.querySelector('.submissionstatustable')||{}).innerText
                  || (document.querySelector('[data-region="submission-status"]')||{}).innerText);
  const body  = t(document.body.innerText).slice(0, 3000);
  return JSON.stringify({dates, stat, body, url: location.href});
})()
"""


def want_mod(mod: str, broad: bool) -> bool:
    return mod in (STRICT_MODS + (STANDARD_EXTRA_MODS if broad else ()))


async def scrape(cdp: C.CDP, broad: bool, max_per_course: int, limit: int) -> list[dict]:
    await cdp.goto(C.LETUS_MY, settle=2.5)
    body = await cdp.js("document.body.innerText") or ""
    if "ログインしていません" in body or "not logged in" in body.lower():
        raise RuntimeError("LETUS 未ログイン（ブラウザの SSO セッションを確認）")

    raw = await cdp.js_json(JS_COURSES) or []
    courses: dict[str, dict] = {}
    for c in raw:
        if not c or not c.get("h") or "/course/view.php" not in c["h"]:
            continue
        url = c["h"] if c["h"].startswith("http") else C.LETUS_BASE + c["h"]
        cid = re.search(r"[?&]id=(\d+)", url)
        if not cid:
            continue
        code = re.search(r"[（(]([0-9A-Z]{6,10})[)）]\s*$", c["n"] or "")
        code = code.group(1) if code else ""
        # 科目コード無し = ポータル/事務系コース。--broad のときだけ対象
        if not code and not broad:
            continue
        courses[cid.group(1)] = {"name": c["n"], "url": url, "code": code}
    C.log(f"コース {len(courses)} 件")

    out: list[dict] = []
    for i, (cid, course) in enumerate(sorted(courses.items()), 1):
        if limit and len(out) >= limit:
            break
        await cdp.goto(course["url"], settle=2.0)
        acts = [a for a in (await cdp.js(JS_ACTIVITIES) or [])
                if a and want_mod(a.get("mod", ""), broad)]
        C.log(f"[{i}/{len(courses)}] {course['name']}: 候補 {len(acts)} 件")
        for a in acts[:max_per_course]:
            if limit and len(out) >= limit:
                break
            rec = {
                "id": hashlib.sha1(a["url"].encode()).hexdigest(),
                "course_code": course["code"],
                "course_name": course["name"],
                "title": norm(a["title"]) or "(無題)",
                "url": a["url"],
                "mod": a.get("mod", ""),
                "open_at": None, "deadline": None, "deadline_raw": "",
                "submit_status": "unknown", "lifecycle": "unknown",
            }
            # まずコースページの活動テキストから締切を拾う（ページ遷移の節約）
            dtext = extract_deadline_text(a.get("extra", ""))
            rec["deadline"] = parse_deadline(dtext) if dtext else None
            rec["deadline_raw"] = dtext[:200]
            try:
                await cdp.goto(a["url"], settle=1.5)
                page = await cdp.js_json(JS_ACTIVITY_PAGE) or {}
            except Exception as e:  # noqa: BLE001
                C.log(f"  取得失敗 {a['url']}: {e}")
                out.append(rec)
                continue
            dtext = extract_deadline_text(page.get("dates", "") + " "
                                          + extract_deadline_text(page.get("body", "")))
            if not dtext:
                dtext = extract_deadline_text(page.get("body", ""))
            if dtext:
                rec["deadline_raw"] = dtext[:200]
                rec["deadline"] = parse_deadline(dtext) or rec["deadline"]
            open_m = re.search(r"開始予定[:：]?\s*([0-9/／年月日:：\s]+)", page.get("dates", ""))
            if open_m:
                rec["open_at"] = parse_deadline(open_m.group(1))
            scope = page.get("stat", "") or page.get("body", "")[:800]
            rec["submit_status"] = extract_submission_status(scope, a["url"])
            rec["lifecycle"] = resolve_lifecycle(
                page.get("body", "")[:1500], rec["submit_status"], rec["deadline"],
                int(datetime.now(JST).timestamp() * 1000))
            out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# レポート
# --------------------------------------------------------------------------- #
def fmt_ms(ms: int | None) -> str:
    if not ms:
        return "締切不明"
    return datetime.fromtimestamp(ms / 1000, JST).strftime("%m/%d(%a) %H:%M")


def days_left(ms: int | None, now: datetime) -> float | None:
    if not ms:
        return None
    return (ms / 1000 - now.timestamp()) / 86400


def rows_open(con: sqlite3.Connection, include_done: bool = False) -> list[sqlite3.Row]:
    if include_done:
        return con.execute(
            "SELECT * FROM assignments WHERE hidden=0 ORDER BY deadline IS NULL, deadline").fetchall()
    return con.execute(
        "SELECT * FROM assignments WHERE hidden=0 AND lifecycle IN ('active','before_start','unknown')"
        " ORDER BY deadline IS NULL, deadline").fetchall()


def cmd_list(con: sqlite3.Connection, days: float | None, include_done: bool) -> None:
    now = datetime.now(JST)
    rows = rows_open(con, include_done)
    if days is not None:
        rows = [r for r in rows
                if (d := days_left(r["deadline"], now)) is not None and d <= days]
    if not rows:
        print("未完了の課題はありません。")
        return
    for r in rows:
        d = days_left(r["deadline"], now)
        left = f"あと{d:.1f}日" if d is not None and d >= 0 else ("期限切れ" if d is not None else "")
        print(f"- [{r['course_code'] or r['course_name']}] {r['title']}")
        print(f"    締切 {fmt_ms(r['deadline'])} {left} / {r['submit_status']} / {r['lifecycle']}")
        print(f"    {r['url']}")


def cmd_brief(con: sqlite3.Connection, days: float) -> str:
    now = datetime.now(JST)
    rows = rows_open(con)
    urgent, soon = [], []
    for r in rows:
        d = days_left(r["deadline"], now)
        if d is None:
            continue
        (urgent if d < 0 else soon).append((d, r))
    urgent.sort(key=lambda x: x[0])
    soon = [x for x in sorted(soon, key=lambda x: x[0]) if x[0] <= days]
    lines = [f"=== LETUS 課題 (未完了 {len(rows)} 件 / 締切{days:g}日以内 {len(soon) + len(urgent)} 件) ==="]
    for label, items in (("【期限切れ・要注意】", urgent), (f"【あと{days:g}日以内】", soon)):
        if not items:
            continue
        lines.append(label)
        for d, r in items:
            lines.append(f"- {fmt_ms(r['deadline'])} ({'期限切れ' if d < 0 else f'あと{d:.1f}日'}) "
                         f"[{r['course_code'] or r['course_name']}] {r['title']} "
                         f"({r['submit_status']}) {r['url']}")
    return "\n".join(lines)


def cmd_monitor(con: sqlite3.Connection, within: float) -> None:
    """締切 N 日以内と期限切れのみ。cron の monitor モード（出力が変わったときだけ起動）用。"""
    now = datetime.now(JST)
    for r in rows_open(con):
        d = days_left(r["deadline"], now)
        if d is not None and d <= within:
            print(f"{fmt_ms(r['deadline'])} {r['course_code']} {r['title']}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
async def cmd_sync(args: argparse.Namespace) -> int:
    con = open_db()
    now_iso = datetime.now(JST).isoformat(timespec="seconds")
    try:
        async with C.CDP() as cdp:
            recs = await scrape(cdp, args.broad, args.max_per_course, args.limit)
    except Exception as e:  # noqa: BLE001
        con.execute("INSERT INTO sync_log VALUES (?,?,?,?,?)", (now_iso, 0, 0, 0, str(e)[:300]))
        con.commit()
        print(f"[assign] 同期失敗: {e}", file=sys.stderr)
        return 1
    new = changed = 0
    for rec in recs:
        n, c = upsert(con, rec, now_iso)
        new += n
        changed += c
    con.execute("INSERT INTO sync_log VALUES (?,?,?,?,?)", (now_iso, len(recs), new, changed, None))
    con.commit()
    print(f"[assign] 同期完了: 取得 {len(recs)} / 新規 {new} / 更新 {changed}")
    if args.verbose:
        for r in recs:
            print(f"  {r['course_code']} {r['title'][:40]} → {fmt_ms(r['deadline'])} "
                  f"{r['submit_status']}/{r['lifecycle']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="LETUS 課題DB")
    p.add_argument("--sync", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--brief", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--monitor", action="store_true")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--done", action="store_true", help="完了分も表示")
    p.add_argument("--broad", action="store_true", help="標準外モジュール・ポータルも対象")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--max-per-course", type=int, default=25)
    p.add_argument("--db", help="DB パスを上書き（テスト用）")
    p.add_argument("--hide", help="タイトルに含む課題を非表示にする")
    p.add_argument("--unhide", help="非表示を解除する")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if args.db:
        global DB_PATH
        DB_PATH = Path(args.db)

    if args.sync:
        return asyncio.run(cmd_sync(args))

    con = open_db()
    if args.hide or args.unhide:
        pat = f"%{args.hide or args.unhide}%"
        val = 1 if args.hide else 0
        con.execute("UPDATE assignments SET hidden=? WHERE title LIKE ?", (val, pat))
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM assignments WHERE title LIKE ? AND hidden=?",
                        (pat, val)).fetchone()[0]
        print(f"{'非表示' if val else '表示'}に {n} 件設定しました: {pat}")
        return 0
    if args.monitor:
        cmd_monitor(con, 3.0)
    elif args.json:
        now = datetime.now(JST)
        rows = [dict(r) for r in rows_open(con, args.done)]
        for r in rows:
            r["deadline_iso"] = (datetime.fromtimestamp(r["deadline"] / 1000, JST).isoformat()
                                 if r["deadline"] else None)
            r["days_left"] = days_left(r["deadline"], now)
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif args.brief:
        print(cmd_brief(con, args.days))
    else:
        cmd_list(con, args.days if args.list else None, args.done)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
