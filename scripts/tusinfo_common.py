#!/usr/bin/env python3
"""tusinfo_common.py — TUS の LETUS / CLASS を読むための共通部品

他の bot / 別マシンで使い回す前提なので、**個人情報も固定 ID もここには書かない**。
値は config（既定 `~/.config/tus-info/config.json`）から読む。

  TUS_INFO_CONFIG  設定ファイルのパスを明示（最優先）
  TUS_CDP          CDP の URL（既定 http://127.0.0.1:9223）

同梱の `config.example.json` をコピーして自分の値を埋める。
設定が無くても既定値で動く（読み取りだけならファイルは要らない）。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import sys
import unicodedata
import urllib.request
from datetime import date as _date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

HOME = Path.home()
JST = timezone(timedelta(hours=9))

# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG_PATHS = [
    HOME / ".config" / "tus-info" / "config.json",
    HOME / ".hermes" / "tus_info_config.json",
]

# 大学（TUS）側の固定値 — 個人情報ではない。キャンパス/年度で変わるものは config 側。
LETUS_BASE = "https://letus.ed.tus.ac.jp"
CLASS_BASE = "https://class.admin.tus.ac.jp"
CLASS_PORTAL_URL = f"{CLASS_BASE}/uprx/up/pk/pky001/Pky00102.xhtml"
LETUS_MY = f"{LETUS_BASE}/my/courses.php"

PERIOD_TIME = {1: "08:50", 2: "10:30", 3: "13:00", 4: "14:40", 5: "16:20"}
KIND_ORDER = ["休講", "休校", "補講", "教室変更", "時間割変更", "変更", "試験"]

_PREFIX_RE = re.compile(r"^(?:【[^】]{1,12}】\s*)+")
DATE_RE = re.compile(r"(\d{4})?[/／](\d{1,2})[/／](\d{1,2})")
JP_DATE_RE = re.compile(r"(\d{1,2})月\s*(\d{1,2})日")
PERIOD_RE = re.compile(r"(\d{1,2})\s*限")

VERBOSE = os.environ.get("TUS_VERBOSE") == "1"


def log(msg: str) -> None:
    if VERBOSE:
        print(f"[tusinfo] {msg}", file=sys.stderr, flush=True)


def config_path() -> Path | None:
    env = os.environ.get("TUS_INFO_CONFIG")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    for p in DEFAULT_CONFIG_PATHS:
        if p.exists():
            return p
    return None


def load_config() -> dict:
    """設定を読む。無ければ空 dict（＝既定値で動く）。"""
    p = config_path()
    if not p:
        return {}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:  # noqa: BLE001
        print(f"[tusinfo] 設定を読めません {p}: {e}", file=sys.stderr)
        return {}


CFG = load_config()


def cfg_get(*keys, default=None):
    """`cfg_get("paths", "data")` のように入れ子を辿る。"""
    cur = CFG
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def cdp_url() -> str:
    return os.environ.get("TUS_CDP") or str(cfg_get("cdp_url", default="http://127.0.0.1:9223"))


def data_dir() -> Path:
    p = Path(os.path.expanduser(str(cfg_get("paths", "data", default="~/.local/share/tusinfo"))))
    p.mkdir(parents=True, exist_ok=True)
    return p


def docs_dir() -> Path:
    p = Path(os.path.expanduser(str(cfg_get("paths", "documents", default="~/documents/tusinfo"))))
    p.mkdir(parents=True, exist_ok=True)
    return p


def ignore_patterns() -> list[str]:
    """課題タイトルにこれを含むものは DB で非表示（例: 履修していない班の課題）。"""
    return list(cfg_get("assignments", "ignore_patterns", default=[]) or [])


def period_time(period: int | None) -> str | None:
    return PERIOD_TIME.get(period) if period else None


# --------------------------------------------------------------------------- #
# 文字列ユーティリティ（科目名の突き合わせ・日付/時限の抽出）
# --------------------------------------------------------------------------- #
def norm(s: str) -> str:
    """比較用に正規化: NFKC → 小文字 → 記号/空白除去 → ローマ数字を算用数字へ。"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    for a, b in (("ⅻ", "12"), ("ⅺ", "11"), ("ⅹ", "10"), ("ⅸ", "9"), ("ⅷ", "8"),
                 ("ⅶ", "7"), ("ⅵ", "6"), ("ⅴ", "5"), ("ⅳ", "4"), ("ⅲ", "3"),
                 ("ⅱ", "2"), ("ⅰ", "1")):
        s = s.replace(a, b)
    s = re.sub(r"[\s\u3000]", "", s)
    s = re.sub(r"[（）()【】\[\]「」『』、。,.．\-–—_:：・/／]", "", s)
    s = re.sub(r"iv", "4", s)
    s = re.sub(r"iii", "3", s)
    s = re.sub(r"ii", "2", s)
    s = re.sub(r"v", "5", s)
    s = re.sub(r"i", "1", s) if len(s) <= 4 else re.sub(r"\bi\b", "1", s)
    return s


def base_name(name: str) -> str:
    """「科目名（後期木３・担当）」→「科目名」"""
    return re.split(r"[（(]", name or "")[0].strip()


def sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def course_score(cal_name: str, other_name: str) -> float:
    """科目名どうしの一致度（CLASS の科目名 ↔ LETUS のコース名）。"""
    c = norm(cal_name)
    return max(sim(c, norm(other_name)), sim(c, norm(base_name(other_name))))


def strip_prefix(summary: str) -> str:
    """「【休講】数学演習」→「数学演習」"""
    return _PREFIX_RE.sub("", summary or "").strip().rstrip("？?").strip()


def detect_kind(text: str) -> str | None:
    """【休講】等の接頭辞、または本文中の語から種別を決める。"""
    m = re.search(r"【([^】]{1,12})】", text or "")
    if m:
        k = unicodedata.normalize("NFKC", m.group(1)).strip()
        for known in KIND_ORDER:
            if known in k:
                return known
        return k
    for known in KIND_ORDER:
        if known in (text or ""):
            return known
    return None


def resolve_year(month: int, day: int, today: _date) -> int:
    """年が書かれていない日付を、今日にいちばん近い年で解釈する。"""
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = _date(y, month, day)
        except ValueError:
            continue
        delta = abs((d - today).days)
        if best is None or delta < best[0]:
            best = (delta, y)
    return best[1]


def extract_period(text: str) -> int | None:
    m = PERIOD_RE.search(text or "")
    if m:
        return int(m.group(1))
    m = re.search(r"(?:period|class|hour)\s*(\d)|(\d)\s*(?:st|nd|rd|th)\s*period", text or "", re.I)
    if m:
        return int(m.group(1) or m.group(2))
    return None


# 休講・変更の判定（CLASS 掲示・LETUS アナウンスの本文用）
JP_HOLIDAY_RE = re.compile(
    r"休講|休校|"
    r"(?:対面|登校)[^。！\n]{0,25}(?:難しい|難しく|できません|できない|なし|行いません|中止)|"
    r"(?:オンデマンド|オンライン)(?:型)?(?:での|による)?(?:実施|授業|対応)|"
    r"授業[^。！\n]{0,12}(?:中止|実施しません|行いません)")
JP_CHANGE_RE = re.compile(r"補講|教室変更|時間割変更|振替(?:授業)?|延期|別室")

# LETUS の新着通知に付く定型フッター。「変更」が予定変更の誤検知に拾われるので除去する。
LETUS_FOOTER = "あなたの通知プレファレンスを変更する"


def detect_kind_text(text: str) -> str | None:
    """掲示・アナウンス本文から種別を推定する（接頭辞 → 変更系 → 休講系 の順）。"""
    t = (text or "").replace(LETUS_FOOTER, "")
    known = detect_kind(t)
    if known in ("休講", "休校", "補講", "教室変更", "時間割変更", "変更"):
        return known
    if JP_CHANGE_RE.search(t):
        return "補講"
    if JP_HOLIDAY_RE.search(t):
        return "休講"
    return None


# --------------------------------------------------------------------------- #
# CDP クライアント（ログイン済みブラウザの一時タブを借りる）
# --------------------------------------------------------------------------- #
class CDP:
    """CDP で一時タブを1枚作り、読み終えたら閉じる。

    ユーザーが操作しているタブには触らない（createTarget / closeTarget のみ）。
    前提: ブラウザがログイン済みで DevTools を公開していること
    （手順は skills/authenticated-browser-container を参照）。
    """

    def __init__(self, url: str | None = None) -> None:
        self.http = url or cdp_url()
        self.ws = None
        self.sid = None
        self.target = None
        self._ids = itertools.count(1)

    async def __aenter__(self) -> "CDP":
        ver = json.load(urllib.request.urlopen(f"{self.http}/json/version", timeout=8))
        from websockets.asyncio.client import connect
        self.ws = await connect(ver["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024,
                                open_timeout=15, close_timeout=5)
        r = await self.send("Target.createTarget", {"url": "about:blank"}, session=False)
        self.target = r["targetId"]
        r = await self.send("Target.attachToTarget", {"targetId": self.target, "flatten": True}, session=False)
        self.sid = r["sessionId"]
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self.target:
                await self.send("Target.closeTarget", {"targetId": self.target}, session=False)
        except Exception:
            pass
        try:
            await self.ws.close()
        except Exception:
            pass

    async def send(self, method: str, params: dict | None = None, session: bool = True) -> dict:
        mid = next(self._ids)
        msg: dict = {"id": mid, "method": method, "params": params or {}}
        if session and self.sid:
            msg["sessionId"] = self.sid
        await self.ws.send(json.dumps(msg))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=90)
            data = json.loads(raw)
            if data.get("id") == mid:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})

    async def js(self, expr: str):
        r = await self.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            raise RuntimeError(f"js error: {str(r['exceptionDetails'])[:300]}")
        return r.get("result", {}).get("value")

    async def js_json(self, expr: str):
        v = await self.js(expr)
        try:
            return json.loads(v) if isinstance(v, str) else v
        except Exception:
            return None

    async def goto(self, url: str, settle: float = 1.0) -> None:
        await self.send("Page.navigate", {"url": url})
        for _ in range(80):
            await asyncio.sleep(0.4)
            try:
                if await self.js("document.readyState") == "complete":
                    break
            except Exception:
                pass
        await asyncio.sleep(settle)

    async def wait_for(self, expr: str, timeout: float = 20.0) -> bool:
        for _ in range(int(timeout / 0.5)):
            try:
                if await self.js(expr):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    async def fetch_b64(self, url_expr: str = "location.href") -> str | None:
        """ページの Cookie を使って現在の URL の中身を base64 で取る（PDF 等の保存用）。"""
        return await self.js(
            f"""(async () => {{ const r = await fetch({url_expr}, {{credentials:'same-origin'}});
                 const b = await r.arrayBuffer(); const u = new Uint8Array(b); let s = '';
                 for (let i = 0; i < u.length; i += 0x8000)
                   s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
                 return btoa(s); }})()""")
