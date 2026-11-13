#!/usr/bin/env python3
"""tus_class_check.py — CLASS（学園生活支援システム）を読むだけの確認ツール

読み取り専用。ログイン済みブラウザの CDP を一時タブで借りる。

使い方:
  tus_class_check.py board                 # 掲示板「時間割変更」タブの全件＋本文
  tus_class_check.py attendance [--day 11/05]
                                           # 学生出欠状況確認: 履修科目ごとの全日程と `休`
  tus_class_check.py timetable             # 学生時間割表: 曜時限・科目コード・教室・担当
  tus_class_check.py day 99AB123 11/05     # その科目がその日にあるか（休講か）

出力はテキスト（そのまま人に読ませる前提）。機械可読にしたいときは --json。

前提: ブラウザが学認 SSO 済みで DevTools を公開していること（README / skills 参照）。
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

# --------------------------------------------------------------------------- #
# ログイン〜ポータル
# --------------------------------------------------------------------------- #
LOGGEDIN_JS = """(() => {
  const t = document.body.innerText || '';
  return !!document.querySelector('a[aria-label="時間割変更情報を表示します。"]')
    || t.includes('学籍情報') || t.includes('学生時間割表');
})()"""

CLICK_ENTER_JS = """(() => {
  const a = Array.from(document.querySelectorAll('a')).find(x => /ENTER/.test(x.innerText||'') && /PC/.test(x.innerText||''))
        || Array.from(document.querySelectorAll('a')).find(x => /ShibbolethAuthServlet/.test(x.getAttribute('href')||''));
  if (a) { a.click(); return true; } return false; })()"""

BOARD_NAV_JS = """(() => {
  const a = Array.from(document.querySelectorAll('a')).find(e => (e.innerText || '').trim() === '掲示板');
  if (!a) return false;
  a.click();
  return true;
})()"""

ICON_CLICK_JS = """(() => {
  const a = document.querySelector('a[aria-label="時間割変更情報を表示します。"]');
  if (a) a.click();
  return !!a;
})()"""

TAB_AREA_JS = "!!document.querySelector('#funcForm\\\\:tabArea')"


async def goto_portal(cdp: C.CDP) -> bool:
    """CLASS トップ → PC ENTER → ログイン済みポータルまで入る。

    CLASS の URL を直打ちすると chrome-error になることがあるので、必ずトップから入る。
    ログイン判定はアイコン単体に頼らない（描画が遅いと false negative になる）。
    """
    for attempt in (1, 2):
        await cdp.goto(C.CLASS_BASE + "/", settle=1.5)
        if "ENTER" in (await cdp.js("document.body.innerText") or ""):
            await cdp.js(CLICK_ENTER_JS)
        if await cdp.wait_for(LOGGEDIN_JS, timeout=25):
            await asyncio.sleep(1.5)  # メニューバー/ポートレットの描画待ち
            return True
        C.log(f"CLASS: ポータルに入れず（{attempt}回目）")
        await asyncio.sleep(3)
    return False


async def goto_board(cdp: C.CDP) -> bool:
    """掲示板（#funcForm:tabArea のある画面）まで入る。

    ナビの「掲示板」から入るのが確実。ポータルのアイコンは描画状況で存在しないことが
    ある（＝掲示板が開けず休講を取りこぼす）ので fallback 扱い。
    """
    for attempt in (1, 2):
        if not await goto_portal(cdp):
            continue
        if await cdp.js(BOARD_NAV_JS) and await cdp.wait_for(TAB_AREA_JS, timeout=25):
            return True
        if await cdp.js(ICON_CLICK_JS) and await cdp.wait_for(TAB_AREA_JS, timeout=25):
            return True
        C.log(f"CLASS 掲示板: 開けず（{attempt}回目）")
        await asyncio.sleep(3)
    return False


async def click_menu(cdp: C.CDP, label: str, wait_js: str, timeout: float = 25) -> bool:
    """左メニュー（a.ui-menuitem-link）を innerText 一致で click して描画を待つ。"""
    clicked = await cdp.js(
        "(() => { const a = Array.from(document.querySelectorAll('a.ui-menuitem-link'))"
        f".find(x => (x.innerText||'').trim() === {json.dumps(label)});"
        "if (!a) return false; a.click(); return true; })()")
    if not clicked:
        C.log(f"メニュー {label} が見つからない")
        return False
    return await cdp.wait_for(wait_js, timeout=timeout)


# --------------------------------------------------------------------------- #
# 掲示板（時間割変更タブ）
# --------------------------------------------------------------------------- #
# 掲示板は同一ページに10個のタブパネルが同居し、非表示パネルにも dl.keiji が入っている。
# 素の querySelectorAll('dl.keiji') は「全表示」タブの一般掲示100件超を返すので、
# 必ず「時間割変更」タブの href から可視パネルを特定して絞る。
CLASS_ROWS_JS = r"""
(() => {
  const tabs = Array.from(document.querySelectorAll('#funcForm\\:tabArea > ul > li'));
  const tab = tabs.find(x => (x.innerText||'').trim() === '時間割変更');
  if (!tab) return null;
  const id = tab.querySelector('a').getAttribute('href').slice(1);
  const panel = document.getElementById(id);
  if (!panel) return JSON.stringify({rows: [], kensu: '', panel: id});
  const rows = Array.from(panel.querySelectorAll('dl.keiji')).map(dl => {
    const cat = (dl.querySelector('.keijiCategory') || {}).innerText || '';
    const a = dl.querySelector('a.ui-commandlink');
    const txt = (dl.innerText || '').replace(/\s+/g, ' ').trim();
    const m = txt.match(/(\d{4}\/\d{2}\/\d{2})\s*$/);
    const body = txt.replace(cat.trim(), '').trim();
    return {
      cat: cat.trim(),
      subject: a ? (a.innerText || '').trim().replace(/\s+/g, ' ') : body,
      linkId: a ? a.id : '',
      keijiDate: m ? m[1] : ''
    };
  });
  const kensu = (panel.querySelector('.keijiKensu') || {}).innerText || '';
  return JSON.stringify({rows: rows, kensu: kensu.trim(), panel: id});
})()
"""

CLASS_DIALOG_JS = r"""
(() => {
  const d = document.getElementById('bsd00702:dialog');
  if (!d) return null;
  return JSON.stringify({text: (d.innerText || '').replace(/\r/g, '').slice(0, 4000)});
})()
"""

CLASS_DIALOG_SUBJECT_JS = r"""
(() => {
  const d = document.getElementById('bsd00702:dialog');
  if (!d) return null;
  return (d.innerText || '').replace(/\s+/g, ' ').slice(0, 400);
})()
"""


def parse_class_subject(subject: str) -> dict:
    """掲示件名を分解する。

    例: 「【休講】<区分>　2026/12/01(火) 1限　99AB123 科目名Ｂ （後期火１・担当）　担当者名」
    → kind / date_raw / period / code / course
    """
    out: dict = {"kind": C.detect_kind(subject), "date": None, "period": None,
                 "code": None, "course": None}
    body = re.sub(r"^(?:【[^】]{1,12}】\s*)+", "", subject or "").strip()
    m = C.DATE_RE.search(body)
    if m:
        out["date_raw"] = (int(m.group(1)) if m.group(1) else None,
                           int(m.group(2)), int(m.group(3)))
    pm = C.PERIOD_RE.search(body)
    if pm:
        out["period"] = int(pm.group(1))
    cm = re.search(r"\b(\d{2}[A-Z0-9]{5})\b", body)
    if cm:
        out["code"] = cm.group(1)
    tail = body[cm.end():] if cm else body
    name = re.split(r"[（(]", tail)[0]
    # 先頭に付くキャンパス/区分名（例「葛飾教養」）を落とす
    name = re.sub(r"^\s*\S{0,6}(?:教養|専門)\s*", "", name).strip()
    out["course"] = name.strip() or None
    return out


async def fetch_class_body(cdp: C.CDP, link_id: str, subject: str) -> str:
    """掲示のダイアログを開いて本文を取る（実施方法はここにしか書かれないことが多い）。"""
    if not link_id:
        return ""
    await cdp.js(f"(() => {{ const a = document.getElementById({json.dumps(link_id)}); if (a) a.click(); return !!a; }})()")
    txt = ""
    probe = re.sub(r"\s+", "", subject)[:14]   # 件名の空白有無が揃わないので空白除去で照合
    for _ in range(16):
        await asyncio.sleep(0.6)
        raw = await cdp.js(CLASS_DIALOG_SUBJECT_JS)
        if raw and probe and probe in re.sub(r"\s+", "", raw):
            txt = (await cdp.js_json(CLASS_DIALOG_JS) or {}).get("text", "")
            break
    m = re.search(r"本文\s*(.*?)(?:掲示期間|\Z)", txt, re.S)
    body = (m.group(1) if m else txt).strip()
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    await cdp.js("""(() => { const d = document.getElementById('bsd00702:dialog'); if (!d) return false;
        const b = d.querySelector('.ui-dialog-titlebar-close, .ui-dialog-titlebar a'); if (b) { b.click(); return true; } return false; })()""")
    await asyncio.sleep(0.5)
    return body[:1500]


async def scrape_board(cdp: C.CDP, with_body: bool = True) -> tuple[list[dict], str]:
    if not await goto_board(cdp):
        raise RuntimeError("CLASS 掲示板（時間割変更）を開けませんでした（要ログイン？）")
    data = None
    for _ in range(20):
        data = await cdp.js_json(CLASS_ROWS_JS)
        if data and (data.get("rows") or data.get("kensu")):
            break
        await asyncio.sleep(1.0)
    data = data or {"rows": []}
    items = []
    for r in data.get("rows", []):
        subj = r.get("subject") or ""
        if not subj:
            continue
        info = parse_class_subject(subj)
        info.update({"cat": r.get("cat") or info.get("kind"), "subject": subj,
                     "keijiDate": r.get("keijiDate") or "", "linkId": r.get("linkId") or "",
                     "body": ""})
        if with_body and info["linkId"]:
            info["body"] = await fetch_class_body(cdp, info["linkId"], subj)
        items.append(info)
    return items, str(data.get("kensu") or "")


# --------------------------------------------------------------------------- #
# 学生時間割表
# --------------------------------------------------------------------------- #
TIMETABLE_JS = r"""
(() => {
  const tbl = document.querySelector('table.classTable');
  if (!tbl) return JSON.stringify({error: 'no table', term: '', slots: []});
  const ths = Array.from(tbl.querySelectorAll('thead th')).map(th => ({
    t: (th.innerText || '').trim(),
    x: th.getBoundingClientRect().left + th.getBoundingClientRect().width / 2}));
  const out = [];
  Array.from(tbl.querySelectorAll('tbody tr')).forEach(tr => {
    const tds = Array.from(tr.children);
    if (!tds.length) return;
    const jigen = (tds[0].innerText || '').trim();
    tds.slice(1).forEach(td => {
      const blocks = Array.from(td.querySelectorAll('.jugyo-info'));
      if (!blocks.length) return;
      const r = td.getBoundingClientRect();
      const xc = r.left + r.width / 2;
      // td の位置は曜日ヘッダの中心 x と突き合わせる（順番に割り当てると1列ずれる）
      let best = null, bd = 1e9;
      ths.forEach(h => { const d = Math.abs(h.x - xc); if (h.t && d < bd) { bd = d; best = h.t; } });
      blocks.forEach(j => {
        const texts = Array.from(j.querySelectorAll(':scope > div'))
          .map(d => (d.innerText || '').trim().replace(/\s+/g, ' '));
        const nm = ((j.querySelector('.fontB') || {}).innerText || '').replace(/\s+/g, ' ').trim();
        const teacher = texts.find((t, i) => i > 0 && t && t !== nm && !/^\d{2}[A-Z0-9]{5}$/.test(t)
                                        && !/教室|単位|ui-button/.test(t)) || '';
        out.push({day: best, jigen: jigen, name: nm,
                  code: texts.find(t => /^\d{2}[A-Z0-9]{5}$/.test(t)) || '',
                  teacher: teacher,
                  room: (texts.find(t => /教室/.test(t)) || '').replace(/^[^：]*：/, '')});
      });
    });
  });
  const m = (document.body.innerText || '').match(/20\d\d年度\s*[前後]期/);
  return JSON.stringify({term: m ? m[0] : '', slots: out.filter(s => s.code && s.name)});
})()
"""


async def scrape_timetable(cdp: C.CDP) -> dict:
    """学生時間割表 → コマ（曜時限・科目コード・科目名・教室・担当）。"""
    if not await goto_portal(cdp):
        return {"error": "CLASS ポータルに入れない（要ログイン）"}
    if not await click_menu(cdp, "学生時間割表", "!!document.querySelector('table.classTable')"):
        return {"error": "学生時間割表の表を取得できない"}
    await asyncio.sleep(0.8)
    data = await cdp.js_json(TIMETABLE_JS) or {}
    slots = data.get("slots", [])
    codes = {s["code"]: re.sub(r"\s*[（(].*$", "", s["name"]).strip()
             for s in slots if s.get("code")}
    teachers: dict[str, list[str]] = {}
    for s in slots:
        t = C.norm(s.get("teacher") or "")
        if t and s.get("code"):
            teachers.setdefault(t, [])
            if s["code"] not in teachers[t]:
                teachers[t].append(s["code"])
    return {"term": data.get("term", ""), "slots": slots, "codes": codes, "teachers": teachers}


# --------------------------------------------------------------------------- #
# 学生出欠状況確認
# --------------------------------------------------------------------------- #
# マークは日付と**同じセル**の先頭に入る（`休 12/01` = 12/01 が休講）。
# 行の innerText を丸ごと繋ぐと `11/24 休 12/01` のように前の日付に付いて見える — セル単位で読む。
ATTENDANCE_JS = r"""
(() => {
  const tbl = Array.from(document.querySelectorAll('table'))
    .find(t => t.querySelectorAll('tbody tr').length > 3 && t.querySelectorAll('thead th').length > 5);
  if (!tbl) return JSON.stringify({error: 'no table'});
  const heads = Array.from(tbl.querySelectorAll('thead th'))
    .map(th => (th.innerText || '').replace(/\s+/g, ' ').trim());
  const rows = Array.from(tbl.querySelectorAll('tbody tr')).map(tr => {
    const tds = Array.from(tr.children);
    const pick = sel => {
      const el = tds[Number(sel)];
      return el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '';
    };
    const cells = tds.slice(3).map(td => {
      const mark = (td.querySelector('.syuketsuKbnMark') || {}).innerText || '';
      const dateEl = td.querySelector('.jugyoDate');
      const date = dateEl ? (dateEl.innerText || '').trim()
                          : ((td.innerText || '').match(/\d{1,2}\/\d{1,2}/) || [''])[0];
      const list = td.querySelector('.jugyoList');
      return {date: date,
              mark: mark.replace(/\s+/g, ''),
              nolecture: !!(list && list.className.indexOf('nolecture') >= 0)};
    }).filter(c => c.date);
    return {slot: pick(0), course: pick(1), rate: pick(2), cells: cells};
  }).filter(r => r.slot || r.course);
  return JSON.stringify({heads: heads, rows: rows});
})()
"""

SHOW_BTN_JS = """(() => { const b = document.getElementById('funcForm:btnHyoji');
  if (!b) return false; b.click(); return true; })()"""


async def scrape_attendance(cdp: C.CDP, term: str | None = None) -> dict:
    """学生出欠状況確認 → 履修科目ごとの全実施日と休講マーク。

    ``term`` は select#funcForm:kaikoNendoGakki_input のラベル（例 "2026年度 後期"）。
    省略時は画面既定（＝通常は現在の学期）。
    """
    if not await goto_portal(cdp):
        raise RuntimeError("CLASS ポータルに入れない（要ログイン）")
    # 直 URL は Bsa00101.xhtml に落ちて学期選択画面のままなので、メニューから入って「表示」を押す
    if not await click_menu(cdp, "学生出欠状況確認", "!!document.getElementById('funcForm:btnHyoji')"):
        raise RuntimeError("学生出欠状況確認に入れない")

    if term:
        ok = await cdp.js(
            "(() => { const s = document.getElementById('funcForm:kaikoNendoGakki_input'); if (!s) return false;"
            f"const o = Array.from(s.options).find(x => (x.text||'').trim() === {json.dumps(term)});"
            "if (!o) return false; s.value = o.value;"
            "s.dispatchEvent(new Event('change', {bubbles: true}));"
            "s.dispatchEvent(new Event('input', {bubbles: true})); return true; })()")
        if not ok:
            raise RuntimeError(f"学期 {term!r} が選択肢に無い")
        await asyncio.sleep(1.0)

    if not await cdp.js(SHOW_BTN_JS):
        raise RuntimeError("「表示」ボタン(#funcForm:btnHyoji)が見つからない")

    data = None
    for _ in range(12):
        await asyncio.sleep(2.5)
        data = await cdp.js_json(ATTENDANCE_JS)
        if data and data.get("rows"):
            break
    if not data or not data.get("rows"):
        raise RuntimeError("出欠表の行が取れない（描画待ち切れ／学期違い）")
    return data


# --------------------------------------------------------------------------- #
# 表示
# --------------------------------------------------------------------------- #
def show_board(items: list[dict], kensu: str) -> str:
    out = [f"== CLASS 掲示板 / 時間割変更: {len(items)}件 ({kensu})"]
    for it in items:
        out.append(f"- [{it.get('keijiDate')}] {it.get('cat') or ''} {it.get('subject')}")
        parsed = []
        if it.get("code"):
            parsed.append(f"コード {it['code']}")
        if it.get("period"):
            parsed.append(f"{it['period']}限")
        if parsed:
            out.append(f"    解析: {' / '.join(parsed)}")
        if it.get("body"):
            out.append(f"    本文: {it['body'].replace(chr(10), ' / ')[:400]}")
    return "\n".join(out)


def show_attendance(data: dict, code: str | None = None, day: str | None = None) -> str:
    out = ["== 学生出欠状況確認（日付の並び=回数、休=休講）"]
    for r in data.get("rows", []):
        if code and code not in (r.get("course") or ""):
            continue
        dates = [c["date"] for c in r.get("cells", [])]
        if not dates:
            continue
        head = f"- {r.get('slot')} {r.get('course')} 出席率{r.get('rate') or '-'}"
        out.append(head)
        marks = [f"{c['date']}:{c['mark']}" for c in r.get("cells", [])
                 if c["nolecture"] or (c["mark"] and c["mark"] not in ("　", ""))]
        out.append(f"    全{len(dates)}回: {' '.join(dates)}")
        out.append(f"   印付き: {' '.join(marks) if marks else '(なし)'}")
        if day:
            hit = next((c for c in r["cells"] if c["date"] == day), None)
            if hit is None:
                out.append(f"   ⚠ {day} はこの科目の実施日に入っていない")
            elif hit["nolecture"] or hit["mark"] in ("休", "休講"):
                out.append(f"   ★ {day} は休講（{hit['mark'] or 'nolecture'}）")
            else:
                out.append(f"   → {day} は実施（休マークなし）")
    return "\n".join(out)


def show_timetable(tt: dict) -> str:
    if tt.get("error"):
        return f"⚠ {tt['error']}"
    out = [f"== 学生時間割表 {tt.get('term')} / {len(tt.get('slots', []))}コマ "
           f"{len(tt.get('codes', {}))}科目"]
    for s in sorted(tt.get("slots", []), key=lambda x: (str(x.get("day")), str(x.get("jigen")))):
        out.append(f"- {s.get('day')}{s.get('jigen')} {s.get('code')} {s.get('name')}"
                   f" / {s.get('room') or '-'} / {s.get('teacher') or '-'}")
    return "\n".join(out)


async def cmd_day(cdp: C.CDP, code: str, day: str) -> str:
    """その日にその科目があるか（休講か）を、掲示板と出欠表の両方で確かめる。"""
    out = []
    items, kensu = await scrape_board(cdp, with_body=False)
    hit = [i for i in items if (i.get("code") == code) or (code and code in (i.get("subject") or ""))]
    out.append(f"== 掲示板 時間割変更: 全{len(items)}件 ({kensu}) / この科目に触れた掲示 {len(hit)}件")
    for i in hit:
        out.append(f"- [{i.get('keijiDate')}] {i.get('subject')}")
    if not hit:
        out.append("  → この科目の休講/補講掲示は無し（掲示0件は『変更なし』の証拠になる）")
    data = await scrape_attendance(cdp)
    out.append("")
    out.append(show_attendance(data, code=code, day=day))
    return "\n".join(out)


async def main() -> int:
    ap = argparse.ArgumentParser(description="CLASS 読み取りツール（読み取り専用）")
    ap.add_argument("command", choices=["board", "attendance", "timetable", "day"])
    ap.add_argument("args", nargs="*", help="day: 科目コード 日付(MM/DD)")
    ap.add_argument("--term", help="attendance: 学期ラベル（例 '2026年度 後期'）")
    ap.add_argument("--day", help="attendance: 特定日 MM/DD の判定を付ける")
    ap.add_argument("--no-body", action="store_true", help="board: 掲示本文を取らない（速い）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    async with C.CDP() as cdp:
        if a.command == "board":
            items, kensu = await scrape_board(cdp, with_body=not a.no_body)
            print(json.dumps(items, ensure_ascii=False, indent=1) if a.json
                  else show_board(items, kensu))
        elif a.command == "attendance":
            data = await scrape_attendance(cdp, term=a.term)
            print(json.dumps(data, ensure_ascii=False, indent=1) if a.json
                  else show_attendance(data, day=a.day))
        elif a.command == "timetable":
            tt = await scrape_timetable(cdp)
            print(json.dumps(tt, ensure_ascii=False, indent=1) if a.json else show_timetable(tt))
        else:  # day
            if len(a.args) < 2:
                ap.error("day は『科目コード 日付(MM/DD)』を渡す")
            print(await cmd_day(cdp, a.args[0], a.args[1]))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(asyncio.run(main()))
