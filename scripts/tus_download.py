#!/usr/bin/env python3
"""tus_download.py — ログイン済みブラウザの Cookie で LETUS のファイルを保存する

Moodle のリソース頁は実ファイルへリダイレクトし、本体はその頁の Cookie で降ってくる
（Cookie を取り出して持ち回る必要はない）。

  tus_download.py letus <mod/resource/view.php?id=N か フル URL> [--out DIR] [--name KEY]
  tus_download.py url <URL> [--out DIR] [--name KEY]

注意:
  - fetch() で資源頁を取って pluginfile.php を探す方法は**効かない**（ビューアは JS が組む）。
    必ず navigate してから location.href を見る。
  - 保存名は ASCII の key にする（日本語名はログ行にだけ出す）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tusinfo_common as C  # noqa: E402


async def download(url: str, out_dir: Path, key: str | None) -> int:
    async with C.CDP() as cdp:
        await cdp.goto(url, settle=2.5)
        real = await cdp.js("location.href") or ""
        if "pluginfile.php" not in real and url != real:
            # mod/url 等の中継頁は外部リンクを拾って開き直す
            href = await cdp.js(
                "(() => { const a = Array.from(document.querySelectorAll('a[href]'))"
                ".find(x => !/letus\\.ed\\.tus\\.ac\\.jp/.test(x.href)); return a ? a.href : ''; })()")
            if href:
                await cdp.goto(href, settle=2.5)
                real = await cdp.js("location.href") or ""
        name = urllib.parse.unquote(real.rsplit("/", 1)[-1].split("?")[0]) or "download.bin"
        ext = Path(name).suffix or ".bin"
        b64 = await cdp.fetch_b64()
        if not b64:
            print(f"取得できず: {real}", file=sys.stderr)
            return 1
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{key or 'file'}{ext}"
        dest.write_bytes(base64.b64decode(b64))
        print(f"saved: {dest}  ({dest.stat().st_size} bytes)  original={name}")
        print(f"       source={real}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LETUS のファイルを保存")
    ap.add_argument("kind", choices=["letus", "url"])
    ap.add_argument("target")
    ap.add_argument("--out", default=None)
    ap.add_argument("--name", default=None, help="保存名（ASCII）")
    a = ap.parse_args()
    url = a.target if a.target.startswith("http") else f"{C.LETUS_BASE}/{a.target.lstrip('/')}"
    out = Path(a.out).expanduser() if a.out else C.docs_dir()
    return asyncio.run(download(url, out, a.name))


if __name__ == "__main__":
    sys.exit(main())
