#!/usr/bin/env python3
"""tus_letus_check.py — LETUS（Moodle）を読むだけの確認ツール

  courses                                    # 履修コース一覧（科目コード付き）
  course <courseid> [--sections] [--announce] # セクション見出し / アナウンス新着
  assign <activity_id>                       # 課題の締切・提出状況（mod/assign）
  find <科目コード>                           # コース名 or コードからコースIDを引く

読み取り専用（--json で機械可読）。
「その日の実施方法（対処/録画配信/休講）」はセクション見出しと説明文にしか無いことが多いので、
まず `course --sections` で li.section の innerText を読む。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tusinfo_common as C  # noqa: E402

COURSES_JS = r"""
(() => {
  const seen = new Set(); const out = [];
  document.querySelectorAll('a[href*="course/view.php"]').forEach(a => {
    const t = (a.innerText || '').trim().replace(/\s+/g, ' ');
    const h = a.getAttribute('href') || '';
    const m = h.match(/id=(\d+)/);
    if (!t || !m || seen.has(m[1])) return;
    seen.add(m[1]);
    const code = (t.match(/\(([0-9]{2}[A-Z0-9]{5})\)/) || [])[1] || '';
    out.push({id: m[1], name: t, code: code});
  });
  return JSON.stringify(out);
})()
"""

SECTIONS_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('li.section').forEach((sec, i) => {
    const h = sec.querySelector('.sectionname, h3, h4');
    const name = (h ? h.innerText : '').replace(/\s+/g, ' ').trim();
    const desc = (sec.querySelector('.summary, .section-summary, .content') || {}).innerText || '';
    const acts = Array.from(sec.querySelectorAll('li.activity')).map(li => {
      const a = li.querySelector('a.aalink, a[href*="/mod/"]');
      return {label: (li.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
              href: a ? (a.getAttribute('href') || '') : '',
              mod: (li.className.match(/modtype_(\w+)/) || [])[1] || ''};
    });
    out.push({i: i, name: name.slice(0, 200),
              desc: desc.replace(/\s+/g, ' ').trim().slice(0, 800),
              acts: acts});
  });
  return JSON.stringify(out);
})()
"""

ANNOUNCE_FORUM_JS = r"""
(() => {
  const acts = Array.from(document.querySelectorAll('li.activity'));
  const f = acts.map(a => {
    const l = a.querySelector('a.aalink, a[href*="/mod/forum/"]');
    return {label: (a.innerText || '').trim().replace(/\s+/g, ' '),
            href: l ? (l.getAttribute('href') || '') : ''};
  }).filter(x => /アナウンス|お知らせ|連絡|ニュース/.test(x.label) && /mod\/forum\/view\.php/.test(x.href));
  return JSON.stringify(f);
})()
"""

POSTS_JS = r"""
(() => {
  const rows = Array.from(document.querySelectorAll('tr')).filter(tr => tr.querySelector('a[href*="discuss.php"]'));
  const out = rows.map(tr => {
    const a = tr.querySelector('a[href*="discuss.php"]');
    const txt = (tr.innerText || '').replace(/\s+/g, ' ');
    return {url: a.getAttribute('href'),
            title: (a.innerText || '').trim().replace(/\s+/g, ' '),
            cells: Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim().replace(/\s+/g, ' '))};
  });
  const body = document.body.innerText || '';
  return JSON.stringify({empty: body.includes('新しいアナウンスメントは投稿されていません'), rows: out});
})()
"""

BODY_JS = r"""
(() => {
  const posts = Array.from(document.querySelectorAll('.forumpost, article'));
  if (!posts.length) return JSON.stringify({title: document.title, text: ''});
  const first = posts[0];
  const subj = (first.querySelector('.subject, h4, h3') || {}).innerText || '';
  const body = first.querySelector('.post-content, .post-content-container, .content') || first;
  return JSON.stringify({title: subj.trim().replace(/\s+/g, ' '),
                         text: (body.innerText || '').replace(/\s+/g, ' ').slice(0, 2000)});
})()
"""

ASSIGN_JS = r"""
(() => {
  const t = s => (s || '').replace(/\s+/g, ' ').trim();
  const dates = t((document.querySelector('[data-region="activity-dates"]') || {}).innerText);
  const stat = t((document.querySelector('.submissionstatustable') || {}).innerText
                 || (document.querySelector('[data-region="submission-status"]') || {}).innerText);
  let submit = '';
  document.querySelectorAll('button, a').forEach(b => {
    const x = (b.innerText || '').trim();
    if (/提出をアップロード|提出する|Submit assignment|Edit submission/.test(x) && !submit) submit = x;
  });
  const grades = t((document.querySelector('[data-region="grade"]') || {}).innerText).slice(0, 300);
  return JSON.stringify({title: document.title, dates: dates, stat: stat,
                         submit_button: submit, grade: grades, url: location.href});
})()
"""


async def ensure_logged_in(cdp: C.CDP) -> None:
    body = await cdp.js("document.body.innerText") or ""
    if "ログインしていません" in body or "not logged in" in body.lower():
        raise RuntimeError("LETUS 未ログイン（ブラウザの SSO セッションを確認）")


async def courses(cdp: C.CDP) -> list[dict]:
    await cdp.goto(C.LETUS_MY, settle=2.5)
    await ensure_logged_in(cdp)
    return await cdp.js_json(COURSES_JS) or []


def course_code(name: str) -> str:
    m = re.search(r"[（(]([0-9A-Z]{6,10})[)）]\s*$", name or "")
    return m.group(1) if m else ""


async def find_course(cdp: C.CDP, key: str) -> dict | None:
    """科目コード or コース名の一部から履修コースを1件に絞る。

    同名科目が前期/後期の両方に残るので、コード一致（＝5桁英数字）を優先する。
    """
    cs = await courses(cdp)
    hit = [c for c in cs if c.get("code") == key]
    if not hit:
        hit = [c for c in cs if key in (c.get("name") or "") and c.get("code")]
    if len(hit) > 1:
        C.log(f"複数ヒット: {[h['name'] for h in hit]}")
    return hit[0] if hit else None


async def sections(cdp: C.CDP, cid: str) -> list[dict]:
    await cdp.goto(f"{C.LETUS_BASE}/course/view.php?id={cid}", settle=2.5)
    await ensure_logged_in(cdp)
    return await cdp.js_json(SECTIONS_JS) or []


async def announcements(cdp: C.CDP, cid: str) -> dict:
    await cdp.goto(f"{C.LETUS_BASE}/course/view.php?id={cid}", settle=2.0)
    forums = await cdp.js_json(ANNOUNCE_FORUM_JS) or []
    if not forums:
        return {"forum": None, "empty": None, "rows": []}
    href = forums[0]["href"]
    fid = (re.search(r"id=(\d+)", href) or [None, ""])[1]
    await cdp.goto(f"{C.LETUS_BASE}/mod/forum/view.php?id={fid}", settle=1.5)
    data = await cdp.js_json(POSTS_JS) or {}
    return {"forum": {"label": forums[0]["label"], "id": fid}, **data}


async def announcement_body(cdp: C.CDP, url: str) -> dict:
    await cdp.goto(url, settle=1.5)
    return await cdp.js_json(BODY_JS) or {}


async def assignment(cdp: C.CDP, aid: str) -> dict:
    await cdp.goto(f"{C.LETUS_BASE}/mod/assign/view.php?id={aid}", settle=2.0)
    await ensure_logged_in(cdp)
    return await cdp.js_json(ASSIGN_JS) or {}


# --------------------------------------------------------------------------- #
# 表示
# --------------------------------------------------------------------------- #
def show_courses(cs: list[dict]) -> str:
    out = [f"== LETUS コース {len(cs)}件"]
    for c in cs:
        out.append(f"- {c['id']}  {c['code'] or '(コード無し)'}  {c['name']}")
    return "\n".join(out)


def show_sections(secs: list[dict], ann: dict | None = None) -> str:
    out = [f"== LETUS セクション {len(secs)}件"]
    for s in secs:
        out.append(f"[{s['i']}] {s['name']}")
        if s.get("desc"):
            out.append(f"    説明: {s['desc'][:400]}")
        for a in s.get("acts", [])[:12]:
            out.append(f"    - ({a.get('mod') or '?'}) {a.get('label', '')[:150]}")
    if ann is not None:
        out.append("")
        if ann.get("forum") is None:
            out.append("== アナウンスメント: フォーラムが見つからない")
        elif ann.get("empty") or not ann.get("rows"):
            out.append("== アナウンスメント: 投稿なし（空のフォーラムは『変更なし』の証拠にはならない）")
        else:
            out.append(f"== アナウンスメント {len(ann['rows'])}件")
            for r in ann["rows"][:15]:
                out.append(f"- {r['title']}  {r['url']}")
    return "\n".join(out)


def show_assign(d: dict) -> str:
    out = [f"== 課題: {d.get('title', '')}"]
    if d.get("dates"):
        out.append(f"  日付: {d['dates']}")
    if d.get("stat"):
        out.append(f"  状態: {d['stat'][:400]}")
    if d.get("grade"):
        out.append(f"  評定: {d['grade']}")
    if d.get("submit_button"):
        out.append(f"  操作: 「{d['submit_button']}」ボタンあり（＝まだ提出できる）")
    out.append(f"  URL: {d.get('url', '')}")
    return "\n".join(out)


async def main() -> int:
    ap = argparse.ArgumentParser(description="LETUS 読み取りツール")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("courses")
    p = sub.add_parser("course")
    p.add_argument("courseid")
    p.add_argument("--sections", action="store_true")
    p.add_argument("--announce", action="store_true")
    p.add_argument("--post", help="アナウンスの本文を取る（discuss.php の URL）")
    p = sub.add_parser("assign")
    p.add_argument("activity_id")
    p = sub.add_parser("find")
    p.add_argument("key")
    for sp in sub.choices.values():
        sp.add_argument("--json", action="store_true")
    a = ap.parse_args()

    async with C.CDP() as cdp:
        if a.cmd == "courses":
            cs = await courses(cdp)
            print(json.dumps(cs, ensure_ascii=False, indent=1) if a.json else show_courses(cs))
        elif a.cmd == "find":
            c = await find_course(cdp, a.key)
            print(json.dumps(c, ensure_ascii=False, indent=1) if a.json
                  else (f"- {c['id']}  {c['code']}  {c['name']}" if c else "見つからない"))
        elif a.cmd == "course":
            secs = await sections(cdp, a.courseid) if (a.sections or not a.announce) else []
            ann = await announcements(cdp, a.courseid) if a.announce else None
            if a.post:
                print(json.dumps(await announcement_body(cdp, a.post), ensure_ascii=False, indent=1))
            elif a.json:
                print(json.dumps({"sections": secs, "announce": ann}, ensure_ascii=False, indent=1))
            else:
                print(show_sections(secs, ann))
        else:
            d = await assignment(cdp, a.activity_id)
            print(json.dumps(d, ensure_ascii=False, indent=1) if a.json else show_assign(d))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(asyncio.run(main()))
