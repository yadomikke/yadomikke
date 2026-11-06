"""共有されたとき(LINE・Xなど)に出る画像 assets/ogp.png を作る。サイト名を変えたときだけ実行します。

    python scripts/make_ogp.py        (Playwright が入っているパソコンで実行。GitHub上では使いません)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
name = cfg["site"]["name"]
html = f"""<!doctype html><meta charset=utf-8>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Zen+Maru+Gothic:wght@500;700;900&display=swap">
<body style="margin:0;width:1200px;height:630px;background:linear-gradient(135deg,#fff4d6,#ffe3e0);font-family:'Zen Maru Gothic',sans-serif;display:flex;align-items:center;justify-content:center;gap:56px;overflow:hidden">
<svg viewBox="0 0 32 32" width="330" height="330">{build.owl_inner("happy")}</svg>
<div><div style="font-size:112px;font-weight:900;color:#d9261c;letter-spacing:.06em;line-height:1.1">{name}</div>
<div style="font-size:44px;font-weight:700;color:#1d3557;margin-top:18px">宿の空室を、日付からさがす</div>
<div style="font-size:30px;font-weight:500;color:#5b6168;margin-top:14px">楽天トラベルの空室をまとめてチェック(非公式)</div></div>"""
(ROOT / "assets").mkdir(exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1200, "height": 630})
    pg.set_content(html)
    pg.wait_for_timeout(1500)
    pg.evaluate("document.fonts.ready")
    pg.wait_for_timeout(1500)
    pg.screenshot(path=str(ROOT / "assets" / "ogp.png"))
    b.close()
print("assets/ogp.png を作りました")
