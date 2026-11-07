"""data/ の空室データから、公開用のHTMLページ(public/)を作る。

使い方:
    python scripts/build.py
    python scripts/build.py --demo --data-dir preview_data --out-dir preview   # 見本(架空データ)

画面の流れ(旅行サイトで一般的な「日付を選ぶ → 宿カードが並ぶ」形):
    1. 日付を選ぶ(横スクロールのカレンダー帯 + 「次の土曜」「連休」ボタン)
    2. その日に泊まれる宿をカードで表示(写真・評価・料金・予約ボタン)
    3. 31日ぶんを一覧で比べるカレンダー表(下の方)
"""
import argparse
import html
import json
import os
import re
import shutil
import urllib.parse
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scenes import all_scenes, holiday_blocks, area_days  # noqa: E402

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
WD = "月火水木金土日"


# ---- 小さな部品 -------------------------------------------------------

def esc(s):
    return html.escape(str(s), quote=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def md(d):
    return f"{d.month}/{d.day}"


def yen(p):
    return f"{p / 10000:.1f}万" if p >= 10000 else f"{p:,}"


def yen_full(p):
    return f"¥{p:,}"


def parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def safe_url(u):
    """リンク先は http(s) だけ許可する(画像は見本用のdata:imageも許可)"""
    u = str(u or "")
    if u.startswith(("https://", "http://", "data:image/")):
        return u
    return "#"


def stay_link(aff, no, day, adults, nights):
    """宿のプラン一覧(日付・人数つき)へ行く、楽天アフィリエイトのリンク。ログインなしで開けるページです"""
    co = day + timedelta(days=nights)
    target = (f"https://hotel.travel.rakuten.co.jp/hotelinfo/plan/{no}?f_teikei="
              f"&f_nen1={day.year}&f_tuki1={day.month}&f_hi1={day.day}"
              f"&f_nen2={co.year}&f_tuki2={co.month}&f_hi2={co.day}"
              f"&f_heya_su=1&f_otona_su={adults}&f_s1=0&f_s2=0&f_y1=0&f_y2=0&f_y3=0&f_y4=0")
    if not aff:
        return target
    q = urllib.parse.quote(target, safe="")
    return f"https://hb.afl.rakuten.co.jp/hgc/{aff}/?pc={q}&m={q}"


def rewrite_links(cfg, state, aff):
    """空室の行き先を、予約手続きのページ(ログインが必要)から、日付つきのプラン一覧に付け替える"""
    adults = int(cfg["search"].get("adults", 2))
    nights = int(cfg["search"].get("nights", 1))
    n = 0
    for no, days in state.get("cells", {}).items():
        if not re.fullmatch(r"[0-9A-Za-z_-]+", str(no)):
            continue
        for dk, c in days.items():
            try:
                day = datetime.strptime(dk, "%Y-%m-%d").date()
            except ValueError:
                continue
            url = stay_link(aff, no, day, adults, nights)
            for f in c.get("f", {}).values():
                if len(f) > 1:
                    f[1] = url
                    n += 1
            if c.get("any"):
                c["any"][1] = url
    return n


def btn(url, cls, text, ext=True):
    u = safe_url(url)
    if u == "#":
        return f'<span class="{cls}">{text}</span>'
    return f'<a class="{cls}" href="{esc(u)}" target="_blank" rel="sponsored nofollow noopener">{text}</a>'


def day_label(d, holidays):
    mark = "祝" if d.isoformat() in holidays else ""
    return f"{md(d)}({WD[d.weekday()]}{mark})"


def day_class(d, holidays):
    if d.isoformat() in holidays or d.weekday() == 6:
        return "sun"
    return "sat" if d.weekday() == 5 else ""


# ---- 集計 -------------------------------------------------------------

def hotels_for_page(cfg, hotels, state, page):
    feat = page["feature"]
    force = {str(x) for x in cfg["force_include"]}
    out = []
    for h in hotels.get("areas", {}).get(page["area"], []):
        seen = state.get("meta", {}).get(h["no"], {}).get("seen", {})
        if seen.get(feat) or h["no"] in force:
            out.append(h)
    return out


def status(cell, feat):
    """セルの状態: none(未取得) / full(満室) / ok(◯) / part(△)"""
    if not cell:
        return "none"
    if cell["s"] == "f":
        return "full"
    return "ok" if feat in cell.get("f", {}) else "part"


def summarize(rows, dates, feat, state):
    """日付ごとの ◯ の軒数と最安"""
    info = {}
    for d in dates:
        k = d.isoformat()
        n, low = 0, None
        for h in rows:
            c = state["cells"].get(h["no"], {}).get(k)
            if status(c, feat) == "ok":
                n += 1
                p = c["f"][feat][0]
                low = p if low is None else min(low, p)
        info[k] = (n, low)
    return info


def embed_data(cfg, rows, dates, info, state, feat, holidays, default, now):
    """ブラウザ側(JavaScript)で日付を切り替えるためのデータ"""
    hs = []
    for h in rows:
        cells = {}
        for d in dates:
            c = state["cells"].get(h["no"], {}).get(d.isoformat())
            st = status(c, feat)
            if st == "ok":
                p, url, name = c["f"][feat]
                n_d, low_d = info[d.isoformat()]
                cells[d.isoformat()] = ["o", p, safe_url(url or h.get("url")), name, badges_for(cfg, h, p, low_d, n_d), badges_for(cfg, h, p, low_d, n_d, limit=False)]
            elif st == "part":
                a = c.get("any") or [0, ""]
                cells[d.isoformat()] = ["p", a[0], safe_url(a[1] or h.get("plan_url") or h.get("url"))]
            elif st == "full":
                cells[d.isoformat()] = ["f"]
        hs.append({"no": h["no"], "n": h["name"], "u": safe_url(h.get("url")), "pu": safe_url(h.get("plan_url") or h.get("url")),
                   "rv": h.get("review"), "rc": h.get("reviews"), "st": h.get("station", ""),
                   "hp": "hotel/" + h["no"] + "/", "ad": h.get("address", ""), "im": safe_url(h.get("image")) if h.get("image") else "", "c": cells})
    ren = set()
    for a, b in holiday_blocks(dates, holidays):
        for i in range((b - a).days + 1):
            ren.add((a + timedelta(days=i)).isoformat())
    mx = max([info[d.isoformat()][0] for d in dates] or [0])
    crown = {d.isoformat() for d in dates if mx >= 2 and info[d.isoformat()][0] == mx}
    if len(crown) > 3:
        crown = set()   # 同じ軒数の日が多いときは、目印の意味がないので付けない
    ds = []
    for d in dates:
        k = d.isoformat()
        n, low = info[k]
        ds.append({"d": k, "l": day_label(d, holidays), "k": day_class(d, holidays), "n": n, "p": low,
                   "r": 1 if k in ren else 0, "c": 1 if k in crown else 0})
    return {"dates": ds, "hotels": hs, "def": default.isoformat(), "badges": badge_defs(cfg),
            "owls": {"sleepy": owl_html("sleepy")}}


def badges_for(cfg, h, price, day_low, day_n, limit=True):
    """当サイトの基準(config.json の badges)に合うバッジの id を、優先順に最大 badge_max 個返す"""
    try:
        rv = float(h.get("review"))
    except (TypeError, ValueError):
        rv = None
    rc = h.get("reviews") or 0
    out = []
    for b in cfg.get("badges", []):
        if b.get("rule") == "day_cheapest":
            ok = day_low is not None and day_n >= 2 and price == day_low
        else:
            ok = (rv is not None and rv >= b.get("min_review", 0) and rc >= b.get("min_reviews", 0)
                  and b.get("min_price", 0) <= price < b.get("max_price", 10 ** 9))
        if ok:
            out.append(b["id"])
    return out[:cfg.get("badge_max", 2)] if limit else out


def badge_defs(cfg):
    return {b["id"]: {"l": b["label"], "i": b.get("icon", "")} for b in cfg.get("badges", [])}


def badge_html(cfg, ids):
    d = badge_defs(cfg)
    if not ids:
        return ""
    return '<p class="bdg">' + "".join(f'<span class="bd b-{esc(i)}">{esc(d[i]["i"])} {esc(d[i]["l"]).replace("★", "<span class=st>★</span>")}</span>' for i in ids if i in d) + "</p>"


# ---- カード(サーバー側の初期表示。JavaScriptが無くても見える) ---------

def card_html(h, cell, feat, flabel, kind, bd="", flags=""):
    img = safe_url(h.get("image")) if h.get("image") else "#"
    imgtag = ""
    if img != "#":
        imgtag = f'<a class="himg" href="{esc(safe_url(h.get("url")))}" target="_blank" rel="sponsored nofollow noopener" tabindex="-1"><img src="{esc(img)}" alt="" loading="lazy"></a>'
    rv = ""
    if h.get("review"):
        rv = f'<p class="rv"><span class="star">★</span><b>{esc(h["review"])}</b>'
        if h.get("reviews"):
            rv += f' <span>({h["reviews"]:,}件)</span>'
        rv += "</p>"
    loc = " / ".join(x for x in (h.get("station", ""), h.get("address", "")) if x)
    if kind == "ok":
        p, url, name = cell["f"][feat]
        tag = f'<p class="tags"><span class="tag">{esc(flabel)}</span> <span class="rn">{esc(name)}</span></p>'
        price = (bd + f'<p class="pl">1泊2名・1部屋</p><p class="pv">{yen_full(p)}<span>〜</span></p>'
                 + btn(url or h.get("url"), "cta", "空室・プランを見る ›"))
        cls = "hc"
    else:
        a = cell.get("any") or [0, ""]
        tag = f'<p class="tags"><span class="tag gray">{esc(flabel)}の空きは未確認</span></p>'
        price = ('<p class="pl">宿全体の最安目安</p>' + (f'<p class="pv sm">{yen_full(a[0])}<span>〜</span></p>' if a[0] else "")
                 + btn(a[1] or h.get("plan_url") or h.get("url"), "cta ghost", "プラン一覧を見る ›"))
        cls = "hc part"
    return (f'<article class="{cls}">{imgtag}<div class="hb"><h3>{btn(h.get("url"), "hn", esc(h["name"]))}</h3>'
            f'{rv}<p class="loc">{esc(loc)}</p>{tag}{flags}<p class="il"><a href="hotel/{esc(h["no"])}/">この宿の空室カレンダー・料金 ›</a></p></div><div class="hp">{price}</div></article>')


def results_html(cfg, rows, state, feat, flabel, day, dates, info, holidays, now):
    k = day.isoformat()
    oks, parts, fulls = [], [], []
    for h in rows:
        c = state["cells"].get(h["no"], {}).get(k)
        st = status(c, feat)
        if st == "ok":
            oks.append((c["f"][feat][0], h, c))
        elif st == "part":
            parts.append((h, c))
        elif st == "full":
            fulls.append(h)
    oks.sort(key=lambda x: x[0])
    out = ""
    if oks:
        n_d, low_d = info[k]
        out += "".join(card_html(h, c, feat, flabel, "ok", badge_html(cfg, badges_for(cfg, h, p, low_d, n_d)),
                                 flags_html(False, n_d == 1)) for p, h, c in oks)
    else:
        near = [d for d in dates if info[d.isoformat()][0] > 0][:4]
        near.sort(key=lambda d: abs((d - day).days))
        hint = "".join(f'<span class="nb">{esc(day_label(d, holidays))}に空きあり</span>' for d in near[:3])
        out += (f'<div class="none">{owl_html("sleepy")}<div class="nt"><b>この日は「{esc(flabel)}」の空きが見つかりませんでした。</b>'
                f"<p>前後の日付や、空きのある日を選んでみてください。</p>{hint}</div></div>")
    if parts:
        out += (f'<details class="more"><summary>参考: 宿に空きはあるが、{esc(flabel)}の部屋は確認できない宿({len(parts)}軒)</summary>'
                + "".join(card_html(h, c, feat, flabel, "part") for h, c in parts) + "</details>")
    if fulls:
        out += (f'<details class="more"><summary>満室の宿({len(fulls)}軒)</summary><ul class="fl">'
                + "".join(f"<li>{esc(h['name'])}</li>" for h in fulls) + "</ul></details>")
    return out, len(oks)


# ---- スタイル・スクリプト ---------------------------------------------

OWL_SVG = "<path d='M5 9L9 2.5L14 7.5Z M27 9L23 2.5L18 7.5Z' fill='#b9772f'/><ellipse cx='16' cy='18.5' rx='12.5' ry='11.5' fill='#eaa44c'/><ellipse cx='16' cy='22.5' rx='7' ry='6' fill='#fbe6bd'/><circle cx='10.8' cy='15' r='4.6' fill='#fff'/><circle cx='21.2' cy='15' r='4.6' fill='#fff'/><circle cx='11.3' cy='15.5' r='2.3' fill='#3b2a1a'/><circle cx='20.7' cy='15.5' r='2.3' fill='#3b2a1a'/><circle cx='12.1' cy='14.6' r='.9' fill='#fff'/><circle cx='21.5' cy='14.6' r='.9' fill='#fff'/><path d='M14.3 18.6L16 21.4L17.7 18.6Z' fill='#e2672b'/><ellipse cx='7.5' cy='22' rx='1.8' ry='1' fill='#f08a7a' opacity='.6'/><ellipse cx='24.5' cy='22' rx='1.8' ry='1' fill='#f08a7a' opacity='.6'/>"
OWL_URI = 'data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Cpath d=%27M5 9L9 2.5L14 7.5Z M27 9L23 2.5L18 7.5Z%27 fill=%27%23b9772f%27/%3E%3Cellipse cx=%2716%27 cy=%2718.5%27 rx=%2712.5%27 ry=%2711.5%27 fill=%27%23eaa44c%27/%3E%3Cellipse cx=%2716%27 cy=%2722.5%27 rx=%277%27 ry=%276%27 fill=%27%23fbe6bd%27/%3E%3Ccircle cx=%2710.8%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.2%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2711.3%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2720.7%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2712.1%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.5%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Cpath d=%27M14.3 18.6L16 21.4L17.7 18.6Z%27 fill=%27%23e2672b%27/%3E%3Cellipse cx=%277.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08a7a%27 opacity=%27.6%27/%3E%3Cellipse cx=%2724.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08a7a%27 opacity=%27.6%27/%3E%3C/svg%3E'

_EYE_FROM = OWL_SVG.index("<circle cx='11.3'")
_EYE_TO = OWL_SVG.index("<path d='M14.3 18.6")


def owl_inner(mood="normal"):
    """フクロウの表情違い。normal=ふつう / happy=にっこり / sleepy=眠そう"""
    if mood == "normal":
        return OWL_SVG
    st = "stroke='#3b2a1a' stroke-width='1.9' fill='none' stroke-linecap='round'"
    if mood == "happy":
        mid = f"<path d='M7.6 15.8Q10.8 11 14 15.8' {st}/><path d='M18 15.8Q21.2 11 24.4 15.8' {st}/>"
    else:
        mid = (f"<path d='M7.6 14.4Q10.8 18.2 14 14.4' {st}/><path d='M18 14.4Q21.2 18.2 24.4 14.4' {st}/>"
               "<text x='22.5' y='8' font-size='7' font-weight='700' fill='#8a6d3b'>z</text>"
               "<text x='26.5' y='5' font-size='5' font-weight='700' fill='#8a6d3b'>z</text>")
    return OWL_SVG[:_EYE_FROM] + mid + OWL_SVG[_EYE_TO:]


def owl_html(mood, cls="mood"):
    return f'<svg class="{cls}" viewBox="0 0 32 32" aria-hidden="true">{owl_inner(mood)}</svg>'


def flags_html(new, only):
    out = ""
    if only:
        out += '<span class="fg-only">この日はここだけ</span>'
    return f'<p class="flags">{out}</p>' if out else ""


def share_html(url, text):
    u = urllib.parse.quote(url, safe="")
    t = urllib.parse.quote(text, safe="")
    return ('<div class="share"><b>この空室を送る</b>'
            f'<a class="sh line" href="https://social-plugins.line.me/lineit/share?url={u}" target="_blank" rel="noopener nofollow">LINEで送る</a>'
            f'<a class="sh x" href="https://twitter.com/intent/tweet?url={u}&amp;text={t}" target="_blank" rel="noopener nofollow">Xでポスト</a></div>')


CSS = """
:root{--ink:#222;--sub:#5b6168;--bg:#f2f2f2;--card:#fff;--line:#dedede;--cta:#d9261c;--cta2:#b81d14;--price:#c8102e;--navy:#1d3557;--ok:#17703f;--okbg:#e3f4ea;--part:#7a4e00;--partbg:#fff1d0;--star:#f2a100;--link:#0a58a8}
*{box-sizing:border-box;margin:0;padding:0}
html{font-size:16px;-webkit-text-size-adjust:100%}
body{font-family:"Zen Maru Gothic","Hiragino Maru Gothic ProN","Hiragino Sans","Noto Sans JP","Yu Gothic",Meiryo,sans-serif;background:var(--bg);color:var(--ink);line-height:1.7;font-size:16px}
a{color:var(--link)}
.hdr{background:#fff;border-bottom:3px solid var(--cta);padding:10px 16px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.logo{font-size:24px;font-weight:900;color:var(--cta);text-decoration:none;letter-spacing:.06em;display:inline-flex;align-items:center;gap:6px}
.logo svg{width:38px;height:38px;flex:none}
.hdr small{font-size:13px;color:var(--sub)}
.disc{background:#fff7dd;border-bottom:1px solid #efd88a;font-size:13px;line-height:1.6;padding:8px 16px;color:#53430a}
.demo{background:#b42318;color:#fff;text-align:center;padding:10px 12px;font-size:14px;font-weight:700}
.wrap{max-width:960px;margin:0 auto;padding:0 12px}
.hero{padding:18px 4px 6px}
.crumb{font-size:13px;color:var(--sub);margin-bottom:6px}.crumb a{color:var(--sub)}
h1{font-size:24px;line-height:1.4;font-weight:800;color:var(--navy);text-wrap:balance}
.lead{font-size:15px;color:#3b4148;margin-top:6px}
.quick{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 10px}
.quick button{font:inherit;font-size:15px;font-weight:700;padding:10px 16px;min-height:44px;border:2px solid var(--cta);color:var(--cta);background:#fff;border-radius:999px;cursor:pointer}
.quick button:active,.quick button.on{background:var(--cta);color:#fff}
.finder{margin-top:6px}
.dstrip{position:sticky;top:0;z-index:20;background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:0 2px 6px rgba(0,0,0,.08)}
.dscroll{display:flex;gap:8px;overflow-x:auto;-webkit-overflow-scrolling:touch;padding:10px;scroll-snap-type:x proximity}
.dchip{flex:0 0 100px;scroll-snap-align:start;font:inherit;border:2px solid var(--line);border-radius:12px;background:#fff;text-align:center;padding:7px 2px;cursor:pointer;line-height:1.35;min-height:92px;position:relative}
.dchip .l{display:block;font-size:15px;font-weight:800}
.dchip .n{display:block;font-size:15px;font-weight:700;color:var(--ok)}
.dchip .p{display:block;font-size:17px;font-weight:800;color:var(--price);line-height:1.3}
.dchip.sat .l{color:#1e62b0}.dchip.sun .l{color:#c8102e}
.dchip.zero .n{color:#8a9097;font-weight:500}
.dchip.on{border-color:var(--cta);background:#fff1ef}
.rhead{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;margin:18px 2px 4px}
.rhead h2{font-size:20px;line-height:1.4;color:var(--navy)}
.rhead h2 b{color:var(--cta)}
.sort{font-size:14px;color:var(--sub)}
.sort select{font:inherit;font-size:15px;padding:8px 10px;min-height:42px;border:1px solid #bbb;border-radius:8px;background:#fff;color:var(--ink)}
.rsub{font-size:13px;color:var(--sub);margin:0 2px 10px}
.hc{display:grid;grid-template-columns:112px 1fr;gap:4px 12px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:10px 0}
.hc .himg{grid-row:1;display:block}
.hc img{width:112px;height:112px;object-fit:cover;border-radius:8px;display:block;background:#e9e9e9}
.hc:not(:has(.himg)){grid-template-columns:1fr}
.hb{min-width:0}
.hb h3{font-size:17px;line-height:1.4}
.hb h3 .hn,.hb h3 a{color:var(--ink);font-weight:800;text-decoration:none}
.hb h3 a:hover{text-decoration:underline}
.rv{font-size:15px;margin-top:2px}.rv .star{color:var(--star);margin-right:2px}.rv b{font-size:16px}.rv span{color:var(--sub);font-size:13px}
.loc{font-size:13px;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tags{margin-top:4px;font-size:13px}
.tag{display:inline-block;background:#fdeceb;color:var(--cta2);border:1px solid #f4b8b3;border-radius:4px;padding:1px 8px;font-weight:700;font-size:13px}
.tag.gray{background:#f1f2f4;color:#555;border-color:#d4d7db}
.rn{color:#444}
.hp{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px 12px;border-top:1px dashed var(--line);padding-top:10px;margin-top:6px}
.pl{font-size:13px;color:var(--sub);width:100%}
.pv{font-size:28px;font-weight:800;color:var(--price);line-height:1.2}.pv span{font-size:15px}
.pv.sm{font-size:22px}
.cta{display:inline-flex;align-items:center;justify-content:center;min-height:48px;padding:10px 22px;border-radius:10px;background:var(--cta);color:#fff;font-size:16px;font-weight:800;text-decoration:none;box-shadow:0 2px 0 var(--cta2)}
.cta:hover{background:var(--cta2)}
.cta.ghost{background:#fff;color:var(--cta);border:2px solid var(--cta);box-shadow:none}
.none{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin:10px 0}
.none p{font-size:14px;color:var(--sub);margin:4px 0 8px}
.nb{display:inline-block;margin:2px 6px 2px 0;background:var(--okbg);color:var(--ok);font-weight:700;border-radius:999px;padding:6px 14px;font-size:14px}
.more{background:#fff;border:1px solid var(--line);border-radius:12px;padding:4px 12px;margin:10px 0}
.more summary{cursor:pointer;font-weight:700;font-size:15px;padding:10px 0;min-height:44px}
.more .hc{box-shadow:none;margin:8px 0}
.fl{padding:0 0 10px 20px;font-size:14px;color:#444}
.sec{margin:26px 0 8px}
.sec h2{font-size:20px;color:var(--navy);margin-bottom:4px}
.sec p.sub{font-size:14px;color:var(--sub);margin-bottom:8px}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.tabs button{font:inherit;font-size:14px;padding:8px 14px;min-height:42px;border:1px solid #bbb;border-radius:999px;background:#fff;color:var(--navy);cursor:pointer}
.tabs button[aria-pressed=true]{background:var(--navy);border-color:var(--navy);color:#fff;font-weight:700}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line);border-radius:12px;background:#fff}
table{border-collapse:separate;border-spacing:0;font-size:14px}
th,td{border-bottom:1px solid var(--line)}
th.hn{position:sticky;left:0;z-index:2;background:#fff;text-align:left;min-width:128px;max-width:128px;padding:8px 8px 8px 10px;border-right:1px solid var(--line);font-size:14px;line-height:1.4}
th.hn a,th.hn span.nm{font-weight:800;text-decoration:none;color:var(--ink)}
th.hn small{display:block;color:var(--sub);font-weight:400;font-size:12px;margin-top:2px}
thead th{background:#efefef;color:var(--navy);font-size:14px;text-align:center;padding:6px 0;min-width:50px;cursor:pointer;line-height:1.35}
thead th.hn{background:#efefef;z-index:3;cursor:default}
thead th i{display:block;font-style:normal;font-weight:400;font-size:12px}
thead th.sat{color:#1e62b0}thead th.sun{color:#c8102e}
thead th.sel{background:#ffe3df}
td.c{text-align:center;min-width:50px;height:54px;padding:0}
td.c a,td.c span{display:flex;flex-direction:column;align-items:center;justify-content:center;height:54px;text-decoration:none;font-weight:800;font-size:17px;line-height:1.15}
td.c i{font-style:normal;font-size:12px;font-weight:700;color:var(--ink)}
td.ok a,td.ok span{background:var(--okbg);color:var(--ok)}
td.part a,td.part span{background:var(--partbg);color:var(--part)}
td.full a,td.full span{color:#7d848c;font-weight:500;font-size:14px}
td.none span{color:#aab0b7;font-weight:400}
.legend{font-size:13px;color:var(--sub);margin:10px 2px;line-height:1.8}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:6px 16px 10px;margin:22px 0}
.card h2{font-size:18px;margin:10px 0 4px;color:var(--navy)}
details.q{padding:8px 0;border-bottom:1px dashed var(--line)}details.q:last-child{border:0}
details.q summary{cursor:pointer;font-weight:700;font-size:15px;min-height:40px;padding:6px 0}
details.q p{font-size:15px;color:#41474e;margin-top:2px}
footer{border-top:1px solid var(--line);padding:20px 14px 44px;text-align:center;font-size:13px;color:var(--sub);line-height:1.9;background:#fff;margin-top:24px}
.empty{background:#fff6ec;border:1px solid #e3cfa8;border-radius:12px;padding:16px;margin:14px 0;font-size:15px}
.list{display:grid;gap:12px;margin:16px 0}
.list a{display:block;background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;text-decoration:none;color:var(--ink);font-weight:800;font-size:18px}
.list a span{display:block;font-weight:400;font-size:14px;color:var(--sub);margin-top:2px}
.answer{background:#fff;border:1px solid var(--line);border-left:6px solid var(--cta);border-radius:12px;padding:12px 16px 6px;margin:12px 0}
.answer h2{font-size:18px;color:var(--navy);margin-bottom:6px}.answer h2 small{font-size:13px;font-weight:400;color:var(--sub);margin-left:6px}
.answer p{font-size:16px;margin:0 0 8px;color:#222}.answer p.lead2{font-size:17px;font-weight:700;line-height:1.7}
.answer ul.ab{padding-left:20px;margin:0 0 8px;font-size:16px}.answer ul.ab li{margin:2px 0}
.adet{margin:4px 0 8px}.adet summary{cursor:pointer;font-size:14px;color:var(--link);min-height:36px;padding:6px 0}.adet p{font-size:14px;color:var(--sub);margin:4px 0}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 4px;align-items:center}
.chips b{font-size:14px;color:var(--sub);font-weight:600}
.chips a{display:inline-block;padding:9px 16px;min-height:42px;border:1px solid #bbb;border-radius:999px;background:#fff;color:var(--navy);text-decoration:none;font-weight:700;font-size:15px}
.chips a:hover{background:#eef3fa}
.chips .cur{display:inline-block;padding:9px 16px;min-height:42px;border:1px solid var(--navy);border-radius:999px;background:var(--navy);color:#fff;font-weight:700;font-size:15px}
.sw{margin:10px 0 2px}.sw .chips{margin:6px 0}
.ftabs{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.ftabs button{min-height:46px;padding:10px 18px;border:2px solid var(--navy);border-radius:999px;background:#fff;color:var(--navy);font:inherit;font-weight:700;font-size:16px;cursor:pointer}
.ftabs button[aria-pressed=true]{background:var(--navy);color:#fff}
.achips a{display:inline-flex;flex-direction:column;gap:2px;padding:10px 18px;line-height:1.3}
.achips a small{font-weight:400;font-size:12px;color:var(--sub)}
.il{margin-top:4px;font-size:14px}
.hc{position:relative}.hb h3{padding-right:42px}
.fav{position:absolute;top:6px;right:6px;width:44px;height:44px;border:0;background:transparent;font-size:27px;line-height:1;color:#c7a3b1;cursor:pointer;border-radius:50%}
.fav.on{color:#e0457b}
.favtog{font:inherit;font-size:14px;padding:8px 14px;min-height:42px;border:1px solid #e3a5bb;border-radius:999px;background:#fff;color:#b6325f;cursor:pointer;font-weight:700}
.favtog.on{background:#ffeef4}
.bdnote{width:100%;font-size:12px;color:var(--sub);margin:0 0 2px}
.rctl{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.flags{margin-top:4px;display:flex;flex-wrap:wrap;gap:6px;font-size:13px}
.fg-only{background:#fff7dd;color:#7a4e00;border:1px solid #efd88a;border-radius:4px;padding:1px 8px;font-weight:700}
.dchip .mk{position:absolute;top:-9px;left:50%;transform:translateX(-50%);white-space:nowrap;font-size:11px;font-weight:800;background:#d9261c;color:#fff;border-radius:999px;padding:2px 8px;line-height:1.4}
.dchip .n.last{color:#c8102e}
.none{display:flex;gap:12px;align-items:center}.none .mood{width:64px;height:64px;flex:none}.none .nt{min-width:0}
.answer{position:relative}.answer .mood{position:absolute;right:12px;top:10px;width:46px;height:46px}.answer h2{padding-right:58px}
.share{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0}.share b{font-size:14px;color:var(--sub)}
.sh{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:8px 18px;border-radius:999px;color:#fff;font-weight:800;font-size:15px;text-decoration:none}
.sh.line{background:#058a3c}.sh.x{background:#111}
.bdg{width:100%;display:flex;flex-wrap:wrap;gap:6px;margin:0 0 2px}
.bd{display:inline-flex;align-items:center;gap:6px;font-size:14px;font-weight:800;line-height:1;padding:4px 12px 4px 6px;border-radius:999px;background:#fff1c9;color:#7a4e00;border:1px solid #f0c75e}
.bd.b-ichido{background:#ffeef4;color:#b6325f;border-color:#f6b3c8}
.bd .st{color:#f2a100;margin-left:1px}
.bd.b-many{background:#e4f6e8;color:#17703f;border-color:#9bd3ab}
.bd::before{content:"";width:28px;height:28px;flex:none;background:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Cpath d=%27M5 9L9 2.5L14 7.5Z M27 9L23 2.5L18 7.5Z%27 fill=%27%23b9772f%27/%3E%3Cellipse cx=%2716%27 cy=%2718.5%27 rx=%2712.5%27 ry=%2711.5%27 fill=%27%23eaa44c%27/%3E%3Cellipse cx=%2716%27 cy=%2722.5%27 rx=%277%27 ry=%276%27 fill=%27%23fbe6bd%27/%3E%3Ccircle cx=%2710.8%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.2%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2711.3%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2720.7%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2712.1%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.5%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Cpath d=%27M14.3 18.6L16 21.4L17.7 18.6Z%27 fill=%27%23e2672b%27/%3E%3Cellipse cx=%277.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08a7a%27 opacity=%27.6%27/%3E%3Cellipse cx=%2724.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08a7a%27 opacity=%27.6%27/%3E%3C/svg%3E") center/contain no-repeat}
.bd.b-ichido::before{background-image:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Cpath d=%27M5 9L9 2.5L14 7.5Z M27 9L23 2.5L18 7.5Z%27 fill=%27%23e0709b%27/%3E%3Cellipse cx=%2716%27 cy=%2718.5%27 rx=%2712.5%27 ry=%2711.5%27 fill=%27%23f8a9c4%27/%3E%3Cellipse cx=%2716%27 cy=%2722.5%27 rx=%277%27 ry=%276%27 fill=%27%23fff0f5%27/%3E%3Ccircle cx=%2710.8%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.2%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2711.3%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2720.7%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2712.1%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.5%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Cpath d=%27M14.3 18.6L16 21.4L17.7 18.6Z%27 fill=%27%23e2672b%27/%3E%3Cellipse cx=%277.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08aa8%27 opacity=%27.6%27/%3E%3Cellipse cx=%2724.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08aa8%27 opacity=%27.6%27/%3E%3C/svg%3E")}
.bd.b-many::before{background-image:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Cpath d=%27M5 9L9 2.5L14 7.5Z M27 9L23 2.5L18 7.5Z%27 fill=%27%233f9a5a%27/%3E%3Cellipse cx=%2716%27 cy=%2718.5%27 rx=%2712.5%27 ry=%2711.5%27 fill=%27%237fcf8f%27/%3E%3Cellipse cx=%2716%27 cy=%2722.5%27 rx=%277%27 ry=%276%27 fill=%27%23e9f8ec%27/%3E%3Ccircle cx=%2710.8%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.2%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2711.3%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2720.7%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2712.1%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.5%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Cpath d=%27M14.3 18.6L16 21.4L17.7 18.6Z%27 fill=%27%23e2672b%27/%3E%3Cellipse cx=%277.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f4a0a0%27 opacity=%27.6%27/%3E%3Cellipse cx=%2724.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f4a0a0%27 opacity=%27.6%27/%3E%3C/svg%3E")}
.facts{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;font-size:15px;margin:6px 0 10px}
.facts dt{color:var(--sub);font-weight:700}.facts dd{margin:0}
.dsec{background:#fff;border:1px solid var(--line);border-radius:12px;padding:6px 14px 10px;margin:14px 0}
.dsec h2{font-size:19px;color:var(--navy);margin:8px 0 2px}.dsec h2 b{color:var(--cta)}
.dsec .meta{font-size:13px;color:var(--sub);margin-bottom:6px}
.rowl{list-style:none;padding:0}
.rowl li{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:6px 12px;padding:10px 0;border-top:1px dashed var(--line)}
.rowl li:first-child{border-top:0}
.rowl .t{min-width:0;flex:1 1 220px}.rowl .t a.n,.rowl .t .n{font-weight:800;color:var(--ink);text-decoration:none;font-size:16px}
.rowl .t small{display:block;color:var(--sub);font-size:13px;line-height:1.5}
.rowl .pr{font-size:20px;font-weight:800;color:var(--price);white-space:nowrap}.rowl .pr span{font-size:13px}
.rowl .cta{min-height:44px;padding:8px 16px;font-size:15px}
.mini{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin:8px 0}
.mini .h{font-size:12px;color:var(--sub);text-align:center}
.mini a,.mini span{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:56px;border-radius:8px;text-decoration:none;font-size:13px;line-height:1.25;background:#f4f4f4;color:#888}
.mini b{font-size:13px;font-weight:700}.mini i{font-style:normal;font-size:16px;font-weight:800}
.mini .ok{background:var(--okbg);color:var(--ok)}.mini .part{background:var(--partbg);color:var(--part)}.mini .full{background:#eceff2;color:#6b7280}
.mini .pad{background:transparent}
.hl{list-style:none;padding:0}
.hl li{padding:10px 0;border-top:1px dashed var(--line);font-size:15px}
.hl li:first-child{border-top:0}.hl a{font-weight:800}.hl small{display:block;color:var(--sub);font-size:13px}
.stat{border-collapse:collapse;font-size:14px;margin:8px 0;width:100%}
.stat th,.stat td{border:1px solid var(--line);padding:6px 8px;text-align:center;background:#fff}
.stat thead th{background:#efefef;position:static;cursor:default;min-width:0;font-size:13px}
.prose{background:#fff;border:1px solid var(--line);border-radius:12px;padding:8px 18px 16px;margin:14px 0}
.prose h2{font-size:19px;color:var(--navy);margin:18px 0 6px}.prose p,.prose li{font-size:16px;margin:6px 0}
.prose ul{padding-left:22px}
.prose table{margin:8px 0}.prose th,.prose td{padding:6px 10px;text-align:left;font-size:15px;background:#fff;border:1px solid var(--line)}
.fnav{margin-bottom:8px;font-size:14px}
.note{background:#fff7dd;border:1px solid #efd88a;border-radius:10px;padding:10px 14px;font-size:14px;margin:10px 0}
@media(min-width:720px){
  h1{font-size:28px}
  .hc{grid-template-columns:168px 1fr 230px;padding:14px;gap:0 16px}
  .hc img{width:168px;height:126px}
  .hc .hp{grid-column:3;grid-row:1;border-top:0;border-left:1px dashed var(--line);padding:0 0 0 16px;margin:0;flex-direction:column;align-items:flex-end;justify-content:center;text-align:right}
  .hc:not(:has(.himg)){grid-template-columns:1fr 230px}
  .hc:not(:has(.himg)) .hp{grid-column:2}
  .pl{text-align:right}
  .hc .bdg{justify-content:flex-end}
  .cta{width:100%}
}
"""

JS = r"""
(function(){
var D=window.__V;if(!D||!D.hotels.length)return;
var F=D.feat,FL=D.flabel;
var cards=document.getElementById('cards'),rt=document.getElementById('rt'),strip=document.getElementById('dscroll'),
    sortSel=document.getElementById('sortsel'),finder=document.getElementById('finder');
var cur=D.def,mode='p',favOnly=false,filterId='',FK='yadomikke_fav',fav={};
try{var raw=localStorage.getItem(FK);if(raw){JSON.parse(raw).forEach(function(n){fav[n]=1;});}}catch(e){}
function saveFav(){try{localStorage.setItem(FK,JSON.stringify(Object.keys(fav)));}catch(e){}}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e;}
function yen(p){return '¥'+String(p).replace(/\B(?=(\d{3})+(?!\d))/g,',');}
function btn(url,cls,text){
  if(!url||url==='#'){return el('span',cls,text);}
  var a=el('a',cls,text);a.href=url;a.target='_blank';a.rel='sponsored nofollow noopener';return a;}
function dinfo(d){for(var i=0;i<D.dates.length;i++){if(D.dates[i].d===d)return D.dates[i];}return null;}
function card(h,c,kind){
  var art=el('article',kind==='o'?'hc':'hc part');
  if(h.im){var a=btn(h.u,'himg','');if(a.tagName==='A'){a.tabIndex=-1;}var im=el('img');im.src=h.im;im.alt='';im.loading='lazy';a.appendChild(im);art.appendChild(a);}
  var b=el('div','hb'),h3=el('h3');h3.appendChild(btn(h.u,'hn',h.n));b.appendChild(h3);
  if(h.rv){var rv=el('p','rv');rv.appendChild(el('span','star','★'));rv.appendChild(el('b',null,String(h.rv)));
    if(h.rc){rv.appendChild(document.createTextNode(' '));rv.appendChild(el('span',null,'('+h.rc.toLocaleString()+'件)'));}b.appendChild(rv);}
  var loc=[h.st,h.ad].filter(Boolean).join(' / ');b.appendChild(el('p','loc',loc));
  var tg=el('p','tags');
  if(kind==='o'){tg.appendChild(el('span','tag',FL));tg.appendChild(document.createTextNode(' '));tg.appendChild(el('span','rn',c[3]));}
  else{tg.appendChild(el('span','tag gray',FL+'の空きは未確認'));}
  b.appendChild(tg);
  if(kind==='o'){var fg=el('p','flags');
    var di0=dinfo(cur);if(di0&&di0.n===1){fg.appendChild(el('span','fg-only','この日はここだけ'));}
    if(fg.childNodes.length){b.appendChild(fg);}}
  if(h.hp){var il=el('p','il'),ia=el('a',null,'この宿の空室カレンダー・料金 ›');ia.href=h.hp;il.appendChild(ia);b.appendChild(il);}
  art.appendChild(b);
  var hp=el('div','hp');
  if(kind==='o'&&c[4]&&c[4].length){var bg=el('p','bdg');
    c[4].forEach(function(id){var m=D.badges[id];if(m){var sp=el('span','bd b-'+id),parts=((m.i?m.i+' ':'')+m.l).split('★');
      parts.forEach(function(t,k){if(k>0){sp.appendChild(el('span','st','★'));}sp.appendChild(document.createTextNode(t));});bg.appendChild(sp);}});hp.appendChild(bg);}
  if(kind==='o'&&filterId&&c[4].indexOf(filterId)<0&&c[5]&&c[5].indexOf(filterId)>=0&&D.badges[filterId]){hp.appendChild(el('p','bdnote','「'+D.badges[filterId].l.replace('★','')+'」の条件にも当てはまります'));}
  if(kind==='o'){hp.appendChild(el('p','pl','1泊2名・1部屋'));
    var pv=el('p','pv',yen(c[1]));pv.appendChild(el('span',null,'〜'));hp.appendChild(pv);
    hp.appendChild(btn(c[2]||h.u,'cta','空室・プランを見る ›'));}
  else{hp.appendChild(el('p','pl','宿全体の最安目安'));
    if(c[1]){var pv2=el('p','pv sm',yen(c[1]));pv2.appendChild(el('span',null,'〜'));hp.appendChild(pv2);}
    hp.appendChild(btn(c[2]||h.pu,'cta ghost','プラン一覧を見る ›'));}
  art.appendChild(hp);
  var on=!!fav[h.no],fb=el('button','fav'+(on?' on':''),on?'♥':'♡');fb.type='button';
  fb.setAttribute('aria-pressed',on?'true':'false');fb.setAttribute('aria-label',h.n+'をお気に入りに入れる');
  fb.addEventListener('click',function(){if(fav[h.no]){delete fav[h.no];}else{fav[h.no]=1;}saveFav();
    if(favOnly){render();}else{var o=!!fav[h.no];fb.classList.toggle('on',o);fb.textContent=o?'♥':'♡';fb.setAttribute('aria-pressed',o?'true':'false');}updFav();});
  art.appendChild(fb);return art;}
function render(){
  var oks=[],parts=[],fulls=[],i,h,c;
  for(i=0;i<D.hotels.length;i++){h=D.hotels[i];c=h.c[cur];if(!c)continue;if(favOnly&&!fav[h.no])continue;
    if(filterId&&!(c[0]==='o'&&c[5]&&c[5].indexOf(filterId)>=0))continue;
    if(c[0]==='o')oks.push([h,c]);else if(c[0]==='p')parts.push([h,c]);else fulls.push(h);}
  if(mode==='p'){oks.sort(function(a,b){return a[1][1]-b[1][1];});}
  else{oks.sort(function(a,b){return (b[0].rv||0)-(a[0].rv||0);});}
  var di=dinfo(cur);
  rt.textContent='';rt.appendChild(document.createTextNode(di.l+'に泊まれる宿 '));
  rt.appendChild(el('b',null,oks.length+'軒'));
  if(favOnly){rt.appendChild(document.createTextNode(' (お気に入りのみ)'));}
  if(filterId&&D.badges[filterId]){rt.appendChild(document.createTextNode(' (絞り込み: '+D.badges[filterId].l+')'));}
  cards.textContent='';
  if(oks.length){for(i=0;i<oks.length;i++)cards.appendChild(card(oks[i][0],oks[i][1],'o'));}
  else{
    var n=el('div','none');n.innerHTML=D.owls.sleepy;var nt=el('div','nt');n.appendChild(nt);
    if(favOnly&&!Object.keys(fav).length){nt.appendChild(el('b',null,'お気に入りの宿がまだありません。'));nt.appendChild(el('p',null,'カードの♡を押すと、この端末に保存されます。'));}
    else{nt.appendChild(el('b',null,(favOnly||filterId)?'この日は、選んだ条件に合う宿が見つかりませんでした。':'この日は「'+FL+'」の空きが見つかりませんでした。'));
    nt.appendChild(el('p',null,'空きのある日を選んでみてください。'));}
    var near=D.dates.filter(function(x){return x.n>0;});
    var ci=D.dates.indexOf(di);
    near.sort(function(a,b){return Math.abs(D.dates.indexOf(a)-ci)-Math.abs(D.dates.indexOf(b)-ci);});
    near.slice(0,3).forEach(function(x){var s=el('button','nb',x.l+'に空きあり');s.type='button';s.style.border='0';s.style.cursor='pointer';
      s.addEventListener('click',function(){pick(x.d,true);});nt.appendChild(s);});
    cards.appendChild(n);}
  if(parts.length){var dp=el('details','more');dp.appendChild(el('summary',null,'参考: 宿に空きはあるが、'+FL+'の部屋は確認できない宿('+parts.length+'軒)'));
    for(i=0;i<parts.length;i++)dp.appendChild(card(parts[i][0],parts[i][1],'p'));cards.appendChild(dp);}
  if(fulls.length){var df=el('details','more');df.appendChild(el('summary',null,'満室の宿('+fulls.length+'軒)'));
    var ul=el('ul','fl');fulls.forEach(function(x){ul.appendChild(el('li',null,x.n));});df.appendChild(ul);cards.appendChild(df);}
  var chips=strip.children;
  for(i=0;i<chips.length;i++){chips[i].classList.toggle('on',chips[i].getAttribute('data-d')===cur);}
  var ths=document.querySelectorAll('thead th[data-d]');
  for(i=0;i<ths.length;i++){ths[i].classList.toggle('sel',ths[i].getAttribute('data-d')===cur);}
  var qb=document.querySelectorAll('.quick button');
  for(i=0;i<qb.length;i++){qb[i].classList.toggle('on',qb[i].getAttribute('data-d')===cur);}
}
function pick(d,scroll){cur=d;render();
  var on=strip.querySelector('.on');if(on&&on.scrollIntoView){try{on.scrollIntoView({inline:'center',block:'nearest'});}catch(e){}}
  if(scroll&&finder){var r=document.getElementById('rhead').getBoundingClientRect();
    if(r.top<0||r.top>window.innerHeight*0.6){window.scrollTo({top:window.pageYOffset+r.top-90,behavior:'smooth'});}}}
D.dates.forEach(function(x){
  var b=el('button','dchip'+(x.k?' '+x.k:'')+(x.n?'':' zero'));b.type='button';b.setAttribute('data-d',x.d);
  var mk=[];if(x.c){mk.push('👑 空き最多');}if(x.r){mk.push('連休');}
  if(mk.length){b.appendChild(el('span','mk',mk.join(' ')));}
  b.appendChild(el('span','l',x.l));b.appendChild(el('span','n'+(x.n===1?' last':''),x.n===1?'のこり1軒':(x.n?('空 '+x.n+'軒'):'空きなし')));
  if(x.p){b.appendChild(el('span','p',(x.p>=10000?(x.p/10000).toFixed(1)+'万':x.p.toLocaleString())+'〜'));}
  b.addEventListener('click',function(){pick(x.d,false);});strip.appendChild(b);});
document.getElementById('dstrip').hidden=false;
sortSel.addEventListener('change',function(){mode=sortSel.value;render();});
var fs=document.getElementById('fltsel');if(fs){fs.addEventListener('change',function(){filterId=fs.value;render();});}
var ft=document.getElementById('favtog');
function updFav(){if(!ft)return;var n=Object.keys(fav).length;ft.textContent=(favOnly?'♥':'♡')+' お気に入りだけ('+n+')';ft.setAttribute('aria-pressed',favOnly?'true':'false');ft.classList.toggle('on',favOnly);}
if(ft){ft.hidden=false;ft.addEventListener('click',function(){favOnly=!favOnly;updFav();render();});updFav();}
var qb=document.querySelectorAll('.quick button');
for(var q=0;q<qb.length;q++){(function(b){b.addEventListener('click',function(){pick(b.getAttribute('data-d'),true);});})(qb[q]);}
var ths=document.querySelectorAll('thead th[data-d]');
for(var t=0;t<ths.length;t++){(function(th){th.addEventListener('click',function(){pick(th.getAttribute('data-d'),true);});})(ths[t]);}
// カレンダー表の週タブ
var tb=document.getElementById('tabs');
if(tb){var cells=document.querySelectorAll('[data-w]'),bt=tb.querySelectorAll('button');
  function show(w){for(var i=0;i<cells.length;i++){cells[i].hidden=(w!=='all'&&cells[i].getAttribute('data-w')!==w);}
    for(var j=0;j<bt.length;j++){bt[j].setAttribute('aria-pressed',bt[j].getAttribute('data-t')===w?'true':'false');}}
  for(var k=0;k<bt.length;k++){bt[k].addEventListener('click',function(){show(this.getAttribute('data-t'));});}
  tb.hidden=false;show('0');}
render();
})();
"""


def head(cfg, title, desc, canonical, demo, extra="", noindex=False):
    ga = cfg["site"].get("analytics_id", "")
    gtag = ""
    if ga and not demo:
        gtag = (f'<script async src="https://www.googletagmanager.com/gtag/js?id={esc(ga)}"></script>'
                f"<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments)}}"
                f"gtag('js',new Date());gtag('config','{esc(ga)}');</script>")
    robots = '<meta name="robots" content="noindex,nofollow">' if (demo or noindex) else ""
    return (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title><meta name="description" content="{esc(desc)}">'
            f'<link rel="canonical" href="{esc(canonical)}">{robots}'
            f'<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc)}">'
            f'<meta property="og:type" content="website"><meta property="og:url" content="{esc(canonical)}">'
            f'<meta property="og:image" content="{esc(cfg["site"]["url"].rstrip("/"))}/ogp.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
            f'<meta property="og:site_name" content="{esc(cfg["site"]["name"])}"><meta name="twitter:card" content="summary_large_image">'
            f'<link rel="icon" href="{OWL_URI}">'
            '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Maru+Gothic:wght@500;700;900&display=swap">'
            f"{extra}<style>{CSS}</style>{gtag}</head><body>")


def header_html(cfg, demo, root="../"):
    out = ""
    if demo:
        out += '<div class="demo">これは見本です。宿・料金・空室はすべて架空のデータです(公開されません)</div>'
    out += (f'<header class="hdr"><a class="logo" href="{root}">{esc(cfg["site"]["name"])}<svg viewBox="0 0 32 32" role="img" aria-label="フクロウのキャラクター">{OWL_SVG}</svg></a>'
            "<small>宿の空室をまとめてチェック</small></header>"
            '<div class="disc">【広告】当サイトは楽天トラベルのアフィリエイトリンクを含む、<b>非公式</b>の空室まとめサイトです。'
            "予約・決済は楽天トラベル上で行われます。</div>")
    return out


def footer_html(cfg, root="../"):
    return (f'<nav class="fnav"><a href="{root}">トップ</a> ・ <a href="{root}about/">データの取得方法と注意点</a> ・ '
            f'<a href="{root}privacy/">プライバシーポリシー</a></nav>'
            f'© {esc(cfg["site"]["name"])}<br>'
            "当サイトは楽天トラベルのアフィリエイトプログラムに参加しており、掲載リンクから予約が行われると運営者に報酬が発生する場合があります。<br>"
            "楽天トラベル公式サイトではありません。掲載の空室・料金は取得時点の情報で、変更される場合があります。最終的な内容は楽天トラベルの予約ページでご確認ください。<br>"
            '<a href="https://webservice.rakuten.co.jp/" target="_blank" rel="noopener">Supported by Rakuten Developers</a>')


# ---- 要約・事実・履歴(AIにも人にも「答え」が先に見える部品) ----------------

def fmt_dt(x):
    return f"{x.month}/{x.day} {x:%H:%M}"


def freq_text(cfg):
    r = cfg["refresh"]
    return (f"{r['near_days']}日以内の日程は約{r['near_hours']}時間ごと、{r['mid_days']}日以内は約{r['mid_hours']}時間ごと、"
            f"それ以降は約{r['far_hours']}時間ごと")


def cond_text(cfg):
    s = cfg["search"]
    return f"{s['nights']}泊・大人{s['adults']}名・1部屋"


def ld_script(objs):
    return '<script type="application/ld+json">' + json.dumps(objs, ensure_ascii=False).replace("</", "<\\/") + "</script>"


def shell(cfg, demo, root, title, desc, canonical, inner, ld=None, noindex=False, js=False):
    extra = ld_script(ld) if ld else ""
    return (head(cfg, title, desc, canonical, demo, extra, noindex) + header_html(cfg, demo, root)
            + '<div class="wrap">' + inner + "</div>" + f"<footer>{footer_html(cfg, root)}</footer>"
            + (f"<script>{JS}</script>" if js else "") + "</body></html>")


def breadcrumb_ld(items):
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": n, "item": u} for i, (n, u) in enumerate(items)]}


def faq_ld(faq):
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}


def faq_html(faq, title="よくある質問"):
    return (f'<div class="card"><h2>{esc(title)}</h2>'
            + "".join(f'<details class="q"><summary>{esc(q)}</summary><p>{esc(a)}</p></details>' for q, a in faq) + "</div>")


def scene_stats(rows, state, feat, ds):
    """日付のまとまり(今週末など)の集計"""
    per, anyh, low, got = [], set(), None, 0
    for d in ds:
        k = d.isoformat()
        fetched = k in state.get("fetched", {})
        n, l = 0, None
        for h in rows:
            c = state["cells"].get(h["no"], {}).get(k)
            if status(c, feat) == "ok":
                n += 1
                p = c["f"][feat][0]
                l = p if l is None else min(l, p)
                anyh.add(h["no"])
        got += 1 if fetched else 0
        per.append((d, fetched, n, l))
        if l is not None:
            low = l if low is None else min(low, l)
    return {"per": per, "hotels": len(anyh), "low": low, "got": got, "n": len(ds)}


def price_txt(low):
    return f"(最安{yen_full(low)}〜)" if low else ""


def answer_parts(cfg, page, rows, state, today, dates, info, scene_list, newest, holidays, now):
    """冒頭の「答え」。(時刻, 先頭の1〜2文, 箇条書き, 折りたたむ補足) を返す。数字はすべてデータから作る。"""
    feat = page["feature"]
    flabel = cfg["features"][feat]["label"]
    alabel = cfg["areas"][page["area"]]["label"]
    kws = "」「".join(cfg["features"][feat]["keywords"])
    asof = fmt_dt(newest) if newest else ""
    n0, l0 = info[today.isoformat()]
    items = [(f"今日({day_label(today, holidays)})", n0, l0)]
    for sc in scene_list:
        if sc["slug"] not in ("shumatsu", "raishu", "renkyu") or not sc["dates"]:
            continue
        st = scene_stats(rows, state, feat, sc["dates"])
        if st["got"]:
            items.append((f"{sc['name']}({sc['span']})", st["hotels"], st["low"]))
    head_item = next((it for it in items if it[0].startswith("今週末")), items[0])

    def phrase(it):
        return f"{it[1]}軒{price_txt(it[2])}"

    lead = (f"{(asof + '時点で、') if asof else ''}{alabel}の{flabel}の空きは、{head_item[0]}が{phrase(head_item)}です。")
    bullets = [f"{lab}: {phrase((lab, n, low))}" for lab, n, low in items if lab != head_item[0]]
    cands = [(d, *info[d.isoformat()]) for d in dates if info[d.isoformat()][0] > 0]
    if cands:
        best = max(cands, key=lambda x: (x[1], -x[2]))
        cheap = min(cands, key=lambda x: (x[2], x[0]))
        bullets.append(f"向こう{len(dates)}日間で空きが最も多い日: {day_label(best[0], holidays)}の{best[1]}軒")
        bullets.append(f"向こう{len(dates)}日間で最安の日: {day_label(cheap[0], holidays)}の{yen_full(cheap[2])}〜")
    else:
        bullets.append(f"向こう{len(dates)}日間のうち、取得済みの日程では該当する空きは見つかっていません(未取得の日を除く)")
    bullets.append(f"確認できた宿: {len(rows)}軒")
    details = [f"「{flabel}」は、部屋名に「{kws}」を含む部屋のことです。軒数は、その部屋に空きがある宿の数です。料金は{cond_text(cfg)}の最安値です。",
               f"楽天トラベルの空室検索APIから{freq_text(cfg)}に取得した値で、リアルタイムの在庫ではありません。"
               "予約の前に、楽天トラベルの予約ページで空室と料金をご確認ください。"]
    return asof, lead, bullets, details


def answer_html(asof, lead, bullets, details, title="いまの空室まとめ", more="数字の意味と注意点", mood="happy"):
    small = f"<small>{esc(asof)} 時点</small>" if asof else ""
    ul = ("<ul class='ab'>" + "".join(f"<li>{esc(b)}</li>" for b in bullets) + "</ul>") if bullets else ""
    det = (f'<details class="adet"><summary>{esc(more)}</summary>' + "".join(f"<p>{esc(d)}</p>" for d in details) + "</details>") if details else ""
    return (f'<section class="answer" id="answer">{owl_html(mood)}<h2>{esc(title)}{small}</h2><p class="lead2">{esc(lead)}</p>{ul}{det}</section>')


LIVE = []   # 実際にページを作る(エリア, 条件)の一覧。main() が決める


def switcher_html(cfg, page, root):
    """エリアと条件を切り替えるリンク(ページがあるものだけ)"""
    same_f = [p for p in LIVE if p["feature"] == page["feature"]]
    same_a = [p for p in LIVE if p["area"] == page["area"]]
    rows = []
    if len(same_f) > 1:
        links = "".join(f'<span class="cur">{esc(cfg["areas"][p["area"]]["label"])}</span>' if p["slug"] == page["slug"]
                        else f'<a href="{root}{esc(p["slug"])}/">{esc(cfg["areas"][p["area"]]["label"])}</a>' for p in same_f)
        rows.append(f'<nav class="chips" aria-label="エリアを選ぶ"><b>エリア</b>{links}</nav>')
    if len(same_a) > 1:
        links = "".join(f'<span class="cur">{esc(cfg["features"][p["feature"]]["label"])}</span>' if p["slug"] == page["slug"]
                        else f'<a href="{root}{esc(p["slug"])}/">{esc(cfg["features"][p["feature"]]["label"])}</a>' for p in same_a)
        rows.append(f'<nav class="chips" aria-label="条件を選ぶ"><b>条件</b>{links}</nav>')
    return f'<div class="sw">{"".join(rows)}</div>' if rows else ""


def chips_html(scene_list, root, current=None):
    links = "".join(f'<a href="{root}{esc(sc["slug"])}/">{esc(sc["name"])}</a>' for sc in scene_list if sc["slug"] != current)
    return f'<nav class="chips" aria-label="日程から探す"><b>日程から探す</b>{links}</nav>'


def facts_html(cfg, page, nrows, newest, oldest):
    feat = page["feature"]
    flabel = cfg["features"][feat]["label"]
    kws = "」「".join(cfg["features"][feat]["keywords"])
    rows = [
        ("対象エリア", cfg["areas"][page["area"]]["label"]),
        ("対象の部屋", f"{flabel}(部屋名に「{kws}」を含む部屋)"),
        ("検索条件", cond_text(cfg)),
        ("掲載している宿", f"{nrows}軒"),
        ("最新の取得", f"{fmt_dt(newest)}(日本時間)" if newest else "取得前"),
        ("最も古い取得", f"{fmt_dt(oldest)}(日本時間)" if oldest else "取得前"),
        ("更新の頻度(目安)", freq_text(cfg)),
        ("データの出どころ", "楽天ウェブサービス(楽天トラベルの空室検索API・施設検索API)"),
    ]
    dl = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
    return (f'<section class="card" id="facts"><h2>このページのデータの概要</h2><dl class="facts">{dl}</dl>'
            '<p><a href="../about/">取得方法・判定方法・注意点の詳細</a></p></section>')


BUCKET_LABEL = ["0〜6日先", "7〜13日先", "14〜20日先", "21〜27日先", "28〜34日先", "35日以上先"]
MIN_N = 30        # 1区分あたりの観測数がこれ未満なら、割合は出さない
READY_DAYS = 14   # 観測を始めてからの日数
READY_N = 200     # 全体の観測数


def pct(a, b):
    return f"{a / b * 100:.0f}%" if b >= MIN_N else "—"


def stats_block(stats, cfg, feat, flabel, today, heading="当サイトの観測履歴"):
    since = stats.get("since")
    tr = stats.get("trans", {}).get(feat, {})
    tot_n = sum(v[0] for v in tr.values())
    obs = stats.get("obs", 0)
    try:
        days = (today - datetime.fromisoformat(since).date()).days + 1
    except Exception:
        days = 0
    if not since or days < READY_DAYS or tot_n < READY_N:
        return (f'<section class="card" id="history"><h2>{esc(heading)}</h2>'
                f"<p>空きが出やすい時期や、空きが消えるまでの早さを、当サイト自身の観測から集計しています。"
                f"現在は蓄積中です(観測開始: {esc(since or '—')}、{days}日目、累計{obs:,}件)。"
                f"{READY_DAYS}日分・{READY_N}件の比較がたまるまでは、数字を公開しません。</p></section>")
    rows = ""
    for i, lab in enumerate(BUCKET_LABEL):
        v = tr.get(str(i), [0, 0, 0, 0])
        rows += (f"<tr><th>{lab}</th><td>{v[0]:,}</td><td>{pct(v[1], v[0])}</td><td>{v[2]:,}</td><td>{pct(v[3], v[2])}</td></tr>")
    return (f'<section class="card" id="history"><h2>{esc(heading)}</h2>'
            f"<p>{esc(since)}から{days}日間、累計{obs:,}件の観測にもとづく、当サイトの集計です。"
            f"前回の確認と今回の確認を比べ、「{esc(flabel)}」の空きがどう変わったかを数えています"
            f"(確認の間隔は{esc(freq_text(cfg))}で、同じ日程・同じ宿の組み合わせを繰り返し数えています)。</p>"
            '<div class="scroll"><table class="stat"><thead><tr><th>宿泊日までの日数</th><th>前回◯でなかった件数</th>'
            '<th>今回◯になった割合</th><th>前回◯だった件数</th><th>今回◯でなくなった割合</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
            f"<p class='legend'>件数が{MIN_N}件に満たない区分は「—」にしています。一般的な傾向ではなく、当サイトが観測した範囲の数字です。"
            "「◯でなくなった」は、予約された場合のほか、楽天側の掲載の変更も含みます。</p></section>")


# ---- 宿ごとの集計 -----------------------------------------------------

def hotel_facts(h, state, feat, dates):
    ok, part, full, none = [], 0, 0, 0
    for d in dates:
        c = state["cells"].get(h["no"], {}).get(d.isoformat())
        st = status(c, feat)
        if st == "ok":
            p, url, name = c["f"][feat]
            ok.append((d, p, url, name))
        elif st == "part":
            part += 1
        elif st == "full":
            full += 1
        else:
            none += 1
    return {"ok": ok, "part": part, "full": full, "none": none}


def hotel_sub(h):
    bits = []
    if h.get("review"):
        bits.append(f"★{h['review']}" + (f"({h['reviews']:,}件)" if h.get("reviews") else ""))
    for k in ("station", "address"):
        if h.get(k):
            bits.append(h[k])
    return " ・ ".join(bits)


def row_html(h, d, p, url, name, root_hotel, bd=""):
    sub = hotel_sub(h)
    return (f'<li><div class="t"><a class="n" href="{root_hotel}hotel/{esc(h["no"])}/">{esc(h["name"])}</a>'
            f"<small>{esc(sub)}<br>{esc(name)}</small>{bd}</div>"
            f'<div class="pr">{yen_full(p)}<span>〜</span></div>'
            + btn(url or h.get("url"), "cta", "空室を見る ›") + "</li>")


def page_ctx(cfg, hotels, state, page, now):
    feat = page["feature"]
    today = now.date()
    dates = [today + timedelta(days=i) for i in range(area_days(cfg, page["area"]))]
    rows = hotels_for_page(cfg, hotels, state, page)
    info = summarize(rows, dates, feat, state)
    fetched = [parse_dt(v) for v in state.get("fetched", {}).values()]
    fetched = [x for x in fetched if x]
    newest, oldest = (max(fetched), min(fetched)) if fetched else (None, None)
    return {"feat": feat, "flabel": cfg["features"][feat]["label"], "alabel": cfg["areas"][page["area"]]["label"],
            "today": today, "dates": dates, "rows": rows, "info": info, "newest": newest, "oldest": oldest,
            "holidays": set(cfg.get("holidays", [])), "base": cfg["site"]["url"].rstrip("/"), "state": state, "page": page}


# ---- 日付別ページ(今週末・来週末・連休・年末年始・クリスマス) ----------------

def render_scene(cfg, page, sc, scene_list, ctx, state, stats, demo):
    site, feat, flabel, alabel = cfg["site"], ctx["feat"], ctx["flabel"], ctx["alabel"]
    holidays, rows, newest = ctx["holidays"], ctx["rows"], ctx["newest"]
    canonical = f"{ctx['base']}/{page['slug']}/{sc['slug']}/"
    name = f"{alabel}の{flabel} {sc['name']}の空室"
    st = scene_stats(rows, state, feat, sc["dates"])
    asof = fmt_dt(newest) if newest else ""

    bullets, details = [], []
    if not sc["dates"]:
        lead = f"今年(今回)の{sc['name']}の日程は終了しました。次の日程が近づくと、このページに空室がまとまります。"
    else:
        if st["hotels"]:
            lead = (f"{(asof + '時点で、') if asof else ''}{alabel}の{flabel}は、{sc['name']}のどこか1日でも空きがある宿が{st['hotels']}軒、"
                    f"最安は{yen_full(st['low'])}〜です。")
        elif st["got"]:
            lead = f"{(asof + '時点で、') if asof else ''}{alabel}の{flabel}は、{sc['name']}の取得済みの日程では空きが見つかっていません。"
        else:
            lead = f"{sc['name']}の日程は、まだ楽天トラベルから取得できていません。自動更新で順次取得します。"
        for d, fetched, n, low in st["per"]:
            bullets.append(f"{day_label(d, holidays)}: " + (f"{n}軒{price_txt(low)}" if fetched else "取得待ち"))
        if st["got"] < st["n"]:
            details.append("「取得待ち」の日は、まだ楽天トラベルから取得できていない日です(満室という意味ではありません)。")
    details += [f"{sc['note']}「{flabel}」は、部屋名に「{'」「'.join(cfg['features'][feat]['keywords'])}」を含む部屋のことです。料金は{cond_text(cfg)}の最安値です。",
                f"楽天トラベルの空室検索APIから{freq_text(cfg)}に取得した値で、リアルタイムの在庫ではありません。"]
    parts = [lead, " / ".join(bullets)]

    title_for_share = f"{name}" + (f"【{sc['span']}】" if sc["span"] else "") + f" | {site['name']}"
    body = [f'<div class="hero"><div class="crumb"><a href="../../">{esc(site["name"])}</a> › <a href="../">{esc(page["title"])}</a> › {esc(sc["name"])}</div>'
            f"<h1>{esc(name)}" + (f"<br><small style='font-size:.6em;color:var(--sub)'>{esc(sc['span'])}</small>" if sc["span"] else "") + "</h1></div>",
            answer_html(asof, lead, bullets, details, f"{sc['name']}の空室まとめ", mood=("happy" if st["hotels"] else "sleepy")), chips_html(scene_list, "../", sc["slug"]), share_html(canonical, title_for_share)]
    for d, fetched, n, low in st["per"]:
        k = d.isoformat()
        sec = f'<section class="dsec" id="d-{k}"><h2>{esc(day_label(d, holidays))}に泊まれる宿 '
        if not fetched:
            body.append(sec + '</h2><p class="meta">まだ取得できていません。</p></section>')
            continue
        oks = []
        for h in rows:
            c = state["cells"].get(h["no"], {}).get(k)
            if status(c, feat) == "ok":
                p, url, rn = c["f"][feat]
                oks.append((p, h, url, rn, False))
        oks.sort(key=lambda x: x[0])
        got = parse_dt(state["fetched"].get(k, ""))
        hist = ""
        days_obs = sorted(od for od, v in stats.get("daily", {}).items() if k in v.get(feat, {}))
        if len(days_obs) >= 2:
            first = stats["daily"][days_obs[0]][feat][k]
            hist = f" ・ 初回観測({days_obs[0][5:].replace('-', '/')})は{first[0]}軒 → 現在{n}軒"
        meta = f'<p class="meta">{("取得: " + fmt_dt(got)) if got else ""}{esc(hist)}</p>'
        if oks:
            body.append(sec + f"<b>{n}軒</b></h2>{meta}<ul class='rowl'>"
                        + "".join(row_html(h, d, p, url, rn, "../../", flags_html(nwf, False) + badge_html(cfg, badges_for(cfg, h, p, ctx["info"][k][1] if k in ctx["info"] else None, ctx["info"][k][0] if k in ctx["info"] else 0)))
                                  for p, h, url, rn, nwf in oks) + "</ul></section>")
        else:
            body.append(sec + f"<b>0軒</b></h2>{meta}<p>この日は「{esc(flabel)}」の空きが見つかっていません。</p></section>")
    body.append(f'<p><a href="../#calendar">31日分のカレンダーで比べる</a></p>')

    faq = [
        (f"{sc['name']}に空いている{alabel}の{flabel}はありますか?",
         lead + " " + " / ".join(bullets)),
        ("この情報はいつ取得したものですか?",
         (f"最新の取得は{newest:%Y-%m-%d %H:%M}(日本時間)です。" if newest else "取得前です。") + f"更新は{freq_text(cfg)}です。リアルタイムではありません。"),
    ]
    body.append(faq_html(faq))
    ld = [breadcrumb_ld([(site["name"], ctx["base"] + "/"), (page["title"], f"{ctx['base']}/{page['slug']}/"), (sc["name"], canonical)]),
          {"@context": "https://schema.org", "@type": "CollectionPage", "name": name, "url": canonical, "inLanguage": "ja",
           "dateModified": (newest or datetime.now(JST)).isoformat(timespec="seconds"),
           "isPartOf": {"@type": "WebSite", "name": site["name"], "url": ctx["base"] + "/"}},
          faq_ld(faq)]
    title = f"{name}" + (f"【{sc['span']}】" if sc["span"] else "") + f" | {site['name']}"
    desc = (lead + " " + " / ".join(bullets))[:220]
    return shell(cfg, demo, "../../", title, desc, canonical, "".join(body), ld)


# ---- 全エリアをまたぐ「日程別まとめ」 --------------------------------------

def hub_slug(cfg, fkey):
    return cfg["features"][fkey].get("slug") or fkey


def render_hub(cfg, fkey, sc, scene_list, area_ctxs, now, demo):
    """条件(例: 客室露天風呂)×日程(例: 今週末)で、全エリアをまとめて見せるページ"""
    site = cfg["site"]
    base = site["url"].rstrip("/")
    flabel = cfg["features"][fkey]["label"]
    hs = hub_slug(cfg, fkey)
    canonical = f"{base}/{hs}/{sc['slug']}/"
    holidays = set(cfg.get("holidays", []))
    newest = max([c["newest"] for _, c in area_ctxs if c["newest"]], default=None)
    asof = fmt_dt(newest) if newest else ""
    name = f"{sc['name']}に空いている{flabel}【全エリア】"

    per_area, hot = [], []
    for page, ctx in area_ctxs:
        st = scene_stats(ctx["rows"], ctx["state"], fkey, sc["dates"])
        per_area.append((page, ctx, st))
        for h in ctx["rows"]:
            best = None
            for d in sc["dates"]:
                c = ctx["state"]["cells"].get(h["no"], {}).get(d.isoformat())
                if status(c, fkey) == "ok":
                    p = c["f"][fkey][0]
                    if best is None or p < best[0]:
                        best = (p, d)
            if best:
                hot.append((best[0], best[1], h, page, ctx))
    hot.sort(key=lambda x: (x[0], x[2]["name"]))
    got_any = any(st["got"] for _, _, st in per_area)
    n_hotels = sum(st["hotels"] for _, _, st in per_area)
    n_areas = sum(1 for _, _, st in per_area if st["hotels"])
    lows = [st["low"] for _, _, st in per_area if st["low"]]

    bullets, details = [], []
    if not sc["dates"]:
        lead = f"今年(今回)の{sc['name']}の日程は終了しました。次の日程が近づくと、このページに空室がまとまります。"
    elif n_hotels:
        top = min((st["low"], c["alabel"]) for _, c, st in per_area if st["low"])
        lead = (f"{(asof + '時点で、') if asof else ''}{sc['name']}({sc['span']})に{flabel}の空きがあるのは、{n_areas}エリア・計{n_hotels}軒です。"
                f"最安は{top[1]}の{yen_full(top[0])}〜です。")
    elif got_any:
        lead = f"{(asof + '時点で、') if asof else ''}{sc['name']}の取得済みの日程では、どのエリアでも{flabel}の空きが見つかっていません。"
    else:
        lead = f"{sc['name']}の日程は、まだ楽天トラベルから取得できていません。自動更新で順次取得します。"
    if sc["dates"]:
        for page, ctx, st in per_area:
            bullets.append(f"{ctx['alabel']}: " + (f"{st['hotels']}軒{price_txt(st['low'])}" if st["got"] else "取得待ち"))
    details += [f"{sc['note']}「{flabel}」は、部屋名に「{'」「'.join(cfg['features'][fkey]['keywords'])}」を含む部屋のことです。料金は{cond_text(cfg)}の最安値です。",
                f"楽天トラベルの空室検索APIから{freq_text(cfg)}に取得した値で、リアルタイムの在庫ではありません。"]

    body = [f'<div class="hero"><div class="crumb"><a href="../../">{esc(site["name"])}</a> › {esc(flabel)} › {esc(sc["name"])}</div>'
            f"<h1>{esc(name)}" + (f"<br><small style='font-size:.6em;color:var(--sub)'>{esc(sc['span'])}</small>" if sc["span"] else "") + "</h1></div>",
            answer_html(asof, lead, bullets, details, f"{sc['name']}の空室まとめ(全エリア)", mood=("happy" if n_hotels else "sleepy")),
            chips_html([s for s in scene_list], "../", sc["slug"]), share_html(canonical, name)]
    if sc["dates"]:
        lis = []
        for page, ctx, st in per_area:
            sub = (f"{st['hotels']}軒に空き{price_txt(st['low'])}" if st["got"] else "取得待ち")
            lis.append(f'<li><a href="../../{esc(page["slug"])}/{esc(sc["slug"])}/">{esc(ctx["alabel"])}の{esc(sc["name"])}</a><small>{esc(sub)}</small></li>')
        body.append(f'<section class="card"><h2>エリア別の空き</h2><ul class="hl">{"".join(lis)}</ul></section>')
        if hot:
            lis = []
            for p, d, h, page, ctx in hot[:12]:
                lis.append(f'<li><a href="../../{esc(page["slug"])}/hotel/{esc(h["no"])}/">{esc(h["name"])}</a>'
                           f'<small>{esc(ctx["alabel"])} ・ {esc(day_label(d, holidays))}に{yen_full(p)}〜</small></li>')
            body.append(f'<section class="card"><h2>{esc(sc["name"])}に泊まれる宿(料金が安い順・全エリア)</h2><ul class="hl">{"".join(lis)}</ul></section>')
    faq = [
        (f"{sc['name']}に空いている{flabel}の宿は、どのエリアにありますか?", lead + (" " + " / ".join(bullets) if bullets else "")),
        ("この情報はいつ取得したものですか?",
         (f"最新の取得は{newest:%Y-%m-%d %H:%M}(日本時間)です。" if newest else "取得前です。") + f"更新は{freq_text(cfg)}です。リアルタイムではありません。"),
    ]
    body.append(faq_html(faq))
    items = [{"@type": "ListItem", "position": i + 1, "name": h["name"], "url": f"{base}/{page['slug']}/hotel/{h['no']}/"}
             for i, (p, d, h, page, ctx) in enumerate(hot[:12])]
    ld = [breadcrumb_ld([(site["name"], base + "/"), (f"{flabel} {sc['name']}", canonical)]),
          {"@context": "https://schema.org", "@type": "CollectionPage", "name": name, "url": canonical, "inLanguage": "ja",
           "dateModified": (newest or datetime.now(JST)).isoformat(timespec="seconds"),
           "isPartOf": {"@type": "WebSite", "name": site["name"], "url": base + "/"}},
          faq_ld(faq)]
    if items:
        ld.append({"@context": "https://schema.org", "@type": "ItemList", "itemListElement": items})
    title = f"{name}" + (f"【{sc['span']}】" if sc["span"] else "") + f" | {site['name']}"
    return shell(cfg, demo, "../../", title, (lead + " " + " / ".join(bullets))[:220], canonical, "".join(body), ld)


# ---- 宿ごとのページ ---------------------------------------------------

def render_hotel(cfg, page, h, ctx, state, stats, demo):
    site, feat, flabel, alabel = cfg["site"], ctx["feat"], ctx["flabel"], ctx["alabel"]
    dates, holidays, newest = ctx["dates"], ctx["holidays"], ctx["newest"]
    hf = hotel_facts(h, state, feat, dates)
    ok = hf["ok"]
    canonical = f"{ctx['base']}/{page['slug']}/hotel/{h['no']}/"
    asof = fmt_dt(newest) if newest else ""
    where = h.get("station") or h.get("address") or alabel
    prices = [p for _, p, _, _ in ok]
    lead = (f"{h['name']}({where})の{flabel}は、" + (f"{asof}時点で、" if asof else "")
            + f"{day_label(dates[0], holidays)}〜{day_label(dates[-1], holidays)}のうち{len(ok)}日に空きがあります。")
    bullets = []
    if ok:
        d0, p0, _, rn0 = ok[0]
        lowd = min(ok, key=lambda x: x[1])
        highd = max(ok, key=lambda x: x[1])
        bullets += [f"直近の空き: {day_label(d0, holidays)} {yen_full(p0)}〜({rn0})",
                    f"この期間の最安: {day_label(lowd[0], holidays)} {yen_full(lowd[1])}〜",
                    f"この期間の最高: {day_label(highd[0], holidays)} {yen_full(highd[1])}〜",
                    f"中央値: {yen_full(int(statistics.median(prices)))}〜"]
    else:
        bullets.append(f"取得済みの日程では、{flabel}の空きは見つかっていません")
    bullets.append(f"宿全体で満室の日: {hf['full']}日 / 未取得の日: {hf['none']}日")
    hist = stats.get("price", {}).get(h["no"], {}).get(feat)
    details = ["未取得の日は、まだ取得できていない日で、満室という意味ではありません。"]
    if hist and "min" in hist:
        bullets.append(f"当サイトの観測での最安: {yen_full(hist['min'][0])}〜({hist['min'][1].replace('-', '/')}泊)")
        details.append(f"「観測での最安」は、当サイトが観測を始めてからの記録です(観測日 {hist['min'][2].replace('-', '/')})。過去の全期間の価格ではありません。")
    details.append(f"料金は{cond_text(cfg)}の最安値で、楽天トラベルの空室検索APIから取得した値です。リアルタイムではないため、最終確認は楽天トラベルでお願いします。")
    s1, s2 = lead, " ".join(b + "。" for b in bullets[:4])

    # 31日カレンダー(曜日を揃える)
    cal = "".join(f'<div class="h">{w}</div>' for w in "月火水木金土日")
    cal += '<span class="pad"></span>' * dates[0].weekday()
    for d in dates:
        c = state["cells"].get(h["no"], {}).get(d.isoformat())
        s = status(c, feat)
        lab = f"<b>{d.month}/{d.day}</b>" if (d.day == 1 or d == dates[0]) else f"<b>{d.day}</b>"
        if s == "ok":
            p, url, _ = c["f"][feat]
            inner_c = f"{lab}<i>◯</i><b>{yen(p)}</b>"
            u = safe_url(url or h.get("url"))
            cal += (f'<a class="ok" href="{esc(u)}" target="_blank" rel="sponsored nofollow noopener">{inner_c}</a>'
                    if u != "#" else f'<span class="ok">{inner_c}</span>')
        elif s == "part":
            cal += f'<span class="part">{lab}<i>△</i></span>'
        elif s == "full":
            cal += f'<span class="full">{lab}<i>満</i></span>'
        else:
            cal += f"<span>{lab}<i>—</i></span>"

    body = [f'<div class="hero"><div class="crumb"><a href="../../../">{esc(site["name"])}</a> › <a href="../../">{esc(page["title"])}</a> › {esc(h["name"])}</div>'
            f"<h1>{esc(h['name'])}の{esc(flabel)} 空室状況</h1><p class=\"lead\">{esc(hotel_sub(h))}</p></div>",
            answer_html(asof, lead, bullets, details, "この宿の空室まとめ", mood=("happy" if ok else "sleepy"))]
    cta_url = (ok[0][2] if ok else None) or h.get("plan_url") or h.get("url")
    body.append('<p style="margin:10px 0">' + btn(cta_url, "cta", "楽天トラベルで空室・料金を確認 ›") + "</p>")
    body.append(share_html(canonical, f"{h['name']}の{flabel} 空室状況 | {site['name']}"))
    if ok:
        body.append('<section class="dsec"><h2>空きのある日と料金</h2><ul class="rowl">'
                    + "".join(f'<li><div class="t"><span class="n">{esc(day_label(d, holidays))}</span><small>{esc(rn)}</small></div>'
                              f'<div class="pr">{yen_full(p)}<span>〜</span></div>' + btn(url or h.get("url"), "cta", "予約ページへ ›") + "</li>"
                              for d, p, url, rn in ok) + "</ul></section>")
    body.append(f'<section class="sec"><h2>{len(dates)}日間のカレンダー</h2><p class="sub">◯=空きあり(数字は最安) ／ △=宿に空きはあるが該当の部屋は確認できない ／ 満=空室なし ／ —=未取得</p>'
                f'<div class="mini">{cal}</div></section>')
    body.append(f'<p><a href="../../">{esc(alabel)}の{esc(flabel)} 全{len(ctx["rows"])}軒の空室表に戻る</a></p>')

    faq = [
        (f"{h['name']}の{flabel}は今空いていますか?", s1 + s2),
        (f"{h['name']}の{flabel}の料金はいくらですか?",
         (f"取得済みの空き日の最安は{yen_full(min(prices))}〜、最高は{yen_full(max(prices))}〜です({cond_text(cfg)})。" if prices
          else "現在、空きが見つかっていないため、料金は取得できていません。")),
        ("この情報はいつのものですか?",
         (f"最新の取得は{newest:%Y-%m-%d %H:%M}(日本時間)です。" if newest else "取得前です。") + "リアルタイムではないため、予約前に楽天トラベルでご確認ください。"),
    ]
    body.append(faq_html(faq))
    hotel_ld = {"@type": "Hotel", "name": h["name"]}
    if h.get("address"):
        hotel_ld["address"] = h["address"]
    ld = [breadcrumb_ld([(site["name"], ctx["base"] + "/"), (page["title"], f"{ctx['base']}/{page['slug']}/"), (h["name"], canonical)]),
          {"@context": "https://schema.org", "@type": "WebPage", "name": f"{h['name']}の{flabel} 空室状況", "url": canonical, "inLanguage": "ja",
           "dateModified": (newest or datetime.now(JST)).isoformat(timespec="seconds"), "about": hotel_ld,
           "isPartOf": {"@type": "WebSite", "name": site["name"], "url": ctx["base"] + "/"}},
          faq_ld(faq)]
    title = f"{h['name']}の{flabel} 空室状況と料金【{md(ctx['today'])}時点】 | {site['name']}"
    return shell(cfg, demo, "../../../", title, (s1 + s2)[:220], canonical, "".join(body), ld)


# ---- 運営方法 -------------------------------------------

def render_about(cfg, ctxs, stats, now, demo):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = base + "/about/"
    op = cfg.get("operator", {})
    feats = [f for f in cfg["features"] if any(p["feature"] == f for p in cfg["pages"])] or list(cfg["features"])
    flabel = "」「".join(cfg["features"][f]["label"] for f in feats)
    r = cfg["refresh"]
    parts = [
        '<div class="hero"><h1>データの取得方法と注意点</h1>'
        f'<p class="lead">{esc(site["name"])}が、どのデータを・どう集め・どう判定しているかの説明です。</p></div><div class="prose">',
        "<h2>このサイトについて</h2>"
        "<p>宿の空室を日付ごとにまとめ、楽天トラベルの予約ページへご案内するサイトです。楽天トラベルの公式サイトではありません。"
        "予約・決済・キャンセルは、すべて楽天トラベル上で行われます。</p>",
        "<h2>データの取得元</h2>"
        "<p>楽天ウェブサービスが提供する「楽天トラベル 空室検索API」と「施設検索API」から取得しています。宿の名称・住所・口コミ評価・写真・料金・予約ページのリンクは、そのAPIが返した内容です。"
        "当サイトが数字を推測したり、書き換えたりすることはありません。</p>",
        "<h2>更新の頻度</h2>"
        f"<p>自動で更新しています。日程が近いほど頻繁に取得し、{esc(freq_text(cfg))}を目安にしています。"
        f"{r['near_days']}日以内の取得は約{r['near_hours']}時間ごとですが、取得の混み具合により遅れることがあります。"
        "各ページには、取得した日時を表示しています。リアルタイムの在庫ではありません。</p>",
        "<h2>表示の見方</h2><table><tbody>"
        "<tr><th>◯</th><td>そのページの条件(客室露天風呂など)に合う部屋に空きがある</td></tr>"
        "<tr><th>△</th><td>宿には空きがあるが、条件に合う部屋の空きは確認できない</td></tr>"
        "<tr><th>満</th><td>その日は宿全体で空室が見つからない</td></tr>"
        "<tr><th>—</th><td>まだ取得していない(満室という意味ではありません)</td></tr></tbody></table>"
        f"<p>料金は{esc(cond_text(cfg))}の最安値です。人数・泊数・プランによって変わります。</p>",
        "<h2>条件(客室露天風呂など)の判定方法と限界</h2>"
        "<p>楽天トラベルから届く<b>部屋の名前に、次のキーワードが含まれるか</b>で、機械的に判定しています。宿を1軒ずつ人が確認しているわけではありません。</p><ul>"
        + "".join(f"<li>「{esc(cfg['features'][f]['label'])}」: 部屋名に「{esc('」「'.join(cfg['features'][f]['keywords']))}」を含む</li>" for f in feats)
        + "</ul><ul><li>部屋名に書かれていない設備(たとえば、部屋名に出ていない露天風呂)は、拾えません。</li>"
        "<li>キーワードを含む別の意味の部屋名を、誤って拾う可能性があります。</li>"
        "<li>設備の最終確認は、各宿・楽天トラベルのページでお願いします。</li></ul>",
        "<h2>AIの利用について</h2>"
        "<p>当サイトの制作と、データを自動で集めてページを作る仕組みには、AIツールを使っています。"
        "掲載している空室・料金の数字は、上記のとおりAPIから機械的に取得した値で、AIが推測して作った数値ではありません。</p>",
        "<h2 id=\"badges\">宿につけているバッジの基準</h2>"
        "<p>宿のカードに表示するバッジは、次の基準を機械的に当てはめて付けています。個別に審査したものではなく、広告料や報酬の有無とは無関係です。"
        "口コミ評価と件数は楽天トラベルの掲載値、料金は取得した最安値(" + esc(cond_text(cfg)) + ")です。</p><ul>"
        + "".join(f"<li><b>{esc(b.get('icon', ''))} {esc(b['label'])}</b>: {esc(b.get('desc', ''))}</li>" for b in cfg.get("badges", []))
        + f"</ul><p>1つの宿に付くバッジは最大{cfg.get('badge_max', 2)}個です。基準は変更することがあります。</p>",
        "<h2>広告・収益について</h2>"
        "<p>当サイトは楽天アフィリエイトに参加しています。掲載リンクから予約が行われると、運営者に報酬が発生する場合があります。報酬の有無によって、掲載する宿や順序を変えることはしていません"
        "(宿の並びは、空きのある日数・料金・口コミ件数などデータにもとづきます)。</p>",
    ]
    if op.get("name") or op.get("contact"):
        parts.append("<h2>運営者・お問い合わせ</h2><ul>"
                     + (f"<li>運営者: {esc(op['name'])}</li>" if op.get("name") else "")
                     + (f"<li>連絡先: {esc(op['contact'])}</li>" if op.get("contact") else "")
                     + "</ul>" + (f"<p>{esc(op['note'])}</p>" if op.get("note") else "")
                     + "<p>掲載内容の誤りや、掲載の停止のご希望は、上記までご連絡ください。</p>")
    parts.append("</div>")
    parts.append(f'<p style="margin:16px 0"><a href="../">トップへ戻る</a></p>')
    desc = f"{site['name']}が使うデータ(楽天トラベルAPI)・更新頻度・判定方法・限界・広告についての説明です。"
    ld = [breadcrumb_ld([(site["name"], base + "/"), ("データの取得方法と注意点", canonical)]),
          {"@context": "https://schema.org", "@type": "AboutPage", "name": "データの取得方法と注意点", "url": canonical, "inLanguage": "ja",
           "dateModified": now.isoformat(timespec="seconds"),
           "isPartOf": {"@type": "WebSite", "name": site["name"], "url": base + "/"}}]
    return shell(cfg, demo, "../", f"データの取得方法と注意点 | {site['name']}", desc, canonical, "".join(parts), ld)


def render_privacy(cfg, now, demo):
    """最低限のプライバシーポリシー(個人情報・Cookie・外部サービス・免責)"""
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = base + "/privacy/"
    op = cfg.get("operator", {})
    ga = site.get("analytics_id", "")
    contact = op.get("contact") or "運営者情報に記載の連絡先"
    ga_txt = ("当サイトは、アクセス状況を把握するために「Google アナリティクス」を利用しています。Cookieを使って匿名の利用状況を収集しますが、個人を特定するものではありません。"
              "ブラウザの設定でCookieを無効にすることで、収集を拒否できます。"
              if ga else "現在、アクセス解析ツールは使用していません。")
    inner = (
        '<div class="hero"><h1>プライバシーポリシー</h1></div><div class="prose">'
        "<h2>個人情報の取得</h2><p>当サイトは、会員登録・コメント・問い合わせフォームなど、閲覧者の個人情報を入力していただく機能を設けておらず、個人情報を取得しません。</p>"
        f"<h2>アクセス解析</h2><p>{esc(ga_txt)}</p>"
        "<h2>広告(アフィリエイト)</h2><p>当サイトは楽天アフィリエイトのリンクを掲載しています。リンクをクリックすると、楽天グループ側でCookie等が利用され、予約などの成果が計測されます。"
        "その取り扱いは、楽天グループのプライバシーポリシーに従います。</p>"
        "<h2>外部サービス</h2><p>文字の表示にGoogle Fontsを利用しており、読み込み時に閲覧者のIPアドレス等がGoogleに送信されることがあります。"
        "また、お気に入り(♡)は閲覧者の端末(ブラウザ)内にだけ保存され、当サイトのサーバーには送信されません。</p>"
        "<h2>免責</h2><p>掲載している空室・料金・宿の情報は、楽天ウェブサービスのAPIから取得した時点のものです。正確性・最新性を保証するものではありません。"
        "最終的な内容は、楽天トラベルの予約ページでご確認ください。当サイトの利用により生じた損害について、責任を負いかねます。</p>"
        f"<h2>お問い合わせ</h2><p>本ポリシーや掲載内容に関するお問い合わせは、{esc(contact)}までお願いします。</p>"
        f"<h2>改定</h2><p>必要に応じて、本ポリシーを予告なく改定することがあります。最終更新: {now:%Y年%m月%d日}</p></div>"
        '<p style="margin:16px 0"><a href="../">トップへ戻る</a></p>')
    return shell(cfg, demo, "../", f"プライバシーポリシー | {site['name']}", f"{site['name']}のプライバシーポリシー(個人情報・Cookie・広告・免責)。",
                 canonical, inner, [breadcrumb_ld([(site["name"], base + "/"), ("プライバシーポリシー", canonical)])])


# ---- ページ本体 -------------------------------------------------------

def render_page(cfg, hotels, state, stats, page, now, demo):
    site = cfg["site"]
    feat = page["feature"]
    flabel = cfg["features"][feat]["label"]
    alabel = cfg["areas"][page["area"]]["label"]
    today = now.date()
    n_days = area_days(cfg, page["area"])
    dates = [today + timedelta(days=i) for i in range(n_days)]
    holidays = set(cfg.get("holidays", []))
    canonical = f"{site['url'].rstrip('/')}/{page['slug']}/"

    rows = hotels_for_page(cfg, hotels, state, page)
    info = summarize(rows, dates, feat, state)

    def n_ok(h):
        return sum(1 for d in dates if status(state["cells"].get(h["no"], {}).get(d.isoformat()), feat) == "ok")

    rows.sort(key=lambda h: (-n_ok(h), -(h.get("reviews") or 0)))

    fetched = [parse_dt(state["fetched"][d.isoformat()]) for d in dates if d.isoformat() in state.get("fetched", {})]
    fetched = [x for x in fetched if x]
    newest, oldest = (max(fetched), min(fetched)) if fetched else (None, None)

    sats = [d for d in dates if d.weekday() == 5]
    default = dates[0]
    if sats and info[sats[0].isoformat()][0] > 0:
        default = sats[0]
    else:
        default = next((d for d in dates if info[d.isoformat()][0] > 0), sats[0] if sats else dates[0])

    # 「次の土曜」「連休」ボタン
    quick = []
    if sats:
        quick.append((f"次の土曜 {md(sats[0])}", sats[0]))
    for a, b in holiday_blocks(dates, holidays):
        quick.append((f"{md(a)}〜の{(b - a).days + 1}連休", a))
        if len(quick) >= 3:
            break
    quick.insert(0, ("今日", today))

    t0 = info[today.isoformat()] if rows else (0, None)
    scene_list = all_scenes(cfg, today)
    ans = (answer_parts(cfg, page, rows, state, today, dates, info, scene_list, newest, holidays, now)
           if rows else ("", "", [], []))
    desc = (f"{alabel}の{flabel}の空室を{n_days}日先まで確認できます。"
            f"{md(today)}({WD[today.weekday()]})は{t0[0]}軒に空き"
            + (f"(取得: {newest:%m/%d %H:%M})" if newest else "")
            + "。1泊2名・楽天トラベルの空室データを自動更新しています(リアルタイムではありません)。")
    title = f"{page['title']}【{md(today)}〜{md(dates[-1])}の空室表】{site['name']}"

    faq = [
        (f"{alabel}で今日空いている{flabel}の宿はどこですか?",
         f"{md(today)}時点で、今日の「{flabel}」の空きは{t0[0]}軒です。日付を選ぶと、その日に泊まれる宿が一覧で表示されます。"
         "データは楽天トラベルの空室情報を定期的に取得したもので、リアルタイムではありません。"),
        ("表の◯・△・満・—は何ですか?",
         f"◯は「{flabel}」と判定した部屋に空きがあること、△は宿に空きはあるが該当の部屋の空きは確認できないこと、"
         "満は空室なし、—はまだ取得していない日です(満室ではありません)。判定は1泊2名の条件です。"),
        ("料金はいつのものですか?",
         (f"{newest:%Y-%m-%d %H:%M}(日本時間)までに取得した、1泊2名・楽天トラベル掲載料金の最安値です。" if newest else "取得後に表示されます。")
         + "人数や泊数を変えると変わります。最終確認は楽天トラベルでお願いします。"),
        (f"「{flabel}」はどう判定していますか?",
         f"楽天トラベルから取得した部屋名に「{'」「'.join(cfg['features'][feat]['keywords'])}」が含まれる部屋を自動で判定しています。"
         "個別に確認した情報ではないため、実際の設備は宿のページでご確認ください。"),
        ("このサイトの利用料金は?", "閲覧は無料です。予約は楽天トラベルで行います。"),
    ]

    # 日付カレンダー表
    weeks = sorted({i // 7 for i in range(n_days)})
    thead = ['<th class="hn" scope="col">宿名</th>']
    for i, d in enumerate(dates):
        thead.append(f'<th class="{day_class(d, holidays)}" data-w="{i // 7}" data-d="{d.isoformat()}" scope="col">{d.day}<i>{WD[d.weekday()]}</i></th>')
    body = []
    for h in rows:
        cs = []
        for i, d in enumerate(dates):
            c = state["cells"].get(h["no"], {}).get(d.isoformat())
            st = status(c, feat)
            w = i // 7
            if st == "none":
                cs.append(f'<td class="c none" data-w="{w}"><span>—</span></td>')
            elif st == "full":
                cs.append(f'<td class="c full" data-w="{w}">{btn_plain(h.get("url"), "満")}</td>')
            elif st == "ok":
                p, url, name = c["f"][feat]
                cs.append(f'<td class="c ok" data-w="{w}" title="{esc(name)} {yen_full(p)}〜">{btn_plain(url or h.get("url"), "◯<i>" + yen(p) + "</i>")}</td>')
            else:
                url = (c.get("any") or [None, ""])[1] or h.get("plan_url") or h.get("url", "")
                cs.append(f'<td class="c part" data-w="{w}">{btn_plain(url, "△")}</td>')
        rv = f"★{h['review']}" if h.get("review") else ""
        body.append(f'<tr><th class="hn" scope="row">{btn_plain(h.get("url"), esc(h["name"]), "nm")}'
                    f"<small>{esc(rv)}</small></th>{''.join(cs)}</tr>")
    tabs = "".join(
        f'<button type="button" data-t="{w}" aria-pressed="false">{md(dates[w * 7])}〜{md(dates[min(w * 7 + 6, n_days - 1)])}</button>'
        for w in weeks) + '<button type="button" data-t="all" aria-pressed="false">全日程</button>'

    ld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": site["name"], "item": site["url"].rstrip("/") + "/"},
            {"@type": "ListItem", "position": 2, "name": page["title"], "item": canonical}]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]},
        {"@context": "https://schema.org", "@type": "CollectionPage", "name": page["title"], "url": canonical,
         "inLanguage": "ja", "dateModified": (newest or now).isoformat(timespec="seconds"),
         "mainEntity": {"@type": "ItemList", "numberOfItems": len(rows), "itemListElement": [
             {"@type": "ListItem", "position": i + 1, "name": h["name"]} for i, h in enumerate(rows)]}},
    ]
    ld_html = '<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + "</script>"

    out = [head(cfg, title, desc, canonical, demo, ld_html), header_html(cfg, demo), '<div class="wrap">']
    out.append(f'<div class="hero"><div class="crumb"><a href="../">{esc(site["name"])}</a> › {esc(alabel)} › {esc(flabel)}</div>'
               f'<h1>{esc(page["title"])}</h1><p class="lead">{esc(page.get("lead", ""))}</p></div>')
    out.append(switcher_html(cfg, page, "../"))

    if rows:
        out.append(answer_html(*ans, mood=("happy" if any(info[d.isoformat()][0] for d in dates) else "sleepy")))
        out.append(chips_html(scene_list, "./"))
        out.append(share_html(canonical, title))
    if not rows:
        out.append('<div class="empty"><b>データを準備中です。</b><br>初回の自動更新が終わると、ここに空室が表示されます。'
                   '(初回は宿の判定のため、数回の更新が必要です)</div>')
    else:
        qb = "".join(f'<button type="button" data-d="{d.isoformat()}">{esc(t)}</button>' for t, d in quick)
        out.append(f'<div class="quick" aria-label="日付をすばやく選ぶ">{qb}</div>')
        out.append('<div class="finder" id="finder"><div class="dstrip" id="dstrip" hidden><div class="dscroll" id="dscroll"></div></div>')
        res, n_def = results_html(cfg, rows, state, feat, flabel, default, dates, info, holidays, now)
        out.append(f'<div class="rhead" id="rhead"><h2 id="rt">{esc(day_label(default, holidays))}に泊まれる宿 <b>{n_def}軒</b></h2>'
                   '<div class="rctl"><button type="button" id="favtog" class="favtog" hidden aria-pressed="false">♡ お気に入りだけ</button><label class="sort">絞り込み <select id="fltsel"><option value="">すべて</option>' + "".join(f'<option value="{esc(b["id"])}">{esc(b["label"])}</option>' for b in cfg.get("badges", [])) + '</select></label><label class="sort">並び替え <select id="sortsel"><option value="p">料金が安い順</option>'
                   '<option value="r">口コミ評価が高い順</option></select></label></div></div>'
                   '<p class="rsub">1泊・大人2名・1部屋の料金です(取得時点の掲載額)。ボタンを押すと楽天トラベルの予約ページが開きます。バッジは当サイト独自の基準で付けています(<a href="../about/#badges">基準を見る</a>)。</p>')
        out.append(f'<div id="cards">{res}</div></div>')
        out.append('<section class="sec" id="calendar"><h2>カレンダーで比べる</h2>'
                   '<p class="sub">日付(数字)をタップすると、その日の宿が上の一覧に表示されます。</p>'
                   f'<div class="tabs" id="tabs" hidden>{tabs}</div>'
                   '<div class="scroll"><table><thead><tr>' + "".join(thead) + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
                   f'<p class="legend"><b>◯</b>=「{esc(flabel)}」の部屋に空きあり(数字は1泊2名の最安) ／ <b>△</b>=宿に空きはあるが該当の部屋は確認できない ／ '
                   "<b>満</b>=空室なし ／ <b>—</b>=未取得(満室ではありません)"
                   + (f"<br>取得: 最新 {newest:%m/%d %H:%M} ・ 最古 {oldest:%m/%d %H:%M}(日本時間)。リアルタイムではありません。最終確認は楽天トラベルでお願いします。" if newest else "")
                   + "</p></section>")
        out.append(facts_html(cfg, page, len(rows), newest, oldest))
        out.append(stats_block(stats, cfg, feat, flabel, today))
        hl = []
        for h in rows:
            hf = hotel_facts(h, state, feat, dates)
            nxt = (f" ・ 直近の空き {day_label(hf['ok'][0][0], holidays)} {yen_full(hf['ok'][0][1])}〜" if hf["ok"] else "")
            hl.append(f'<li><a href="hotel/{esc(h["no"])}/">{esc(h["name"])}</a>'
                      f"<small>{esc(hotel_sub(h))}<br>{len(dates)}日中 {len(hf['ok'])}日に空き{esc(nxt)}</small></li>")
        out.append(f'<section class="card" id="hotels"><h2>{esc(alabel)}の{esc(flabel)}の宿 一覧({len(rows)}軒)</h2><ul class="hl">{"".join(hl)}</ul></section>')
        data = embed_data(cfg, rows, dates, info, state, feat, holidays, default, now)
        data["feat"], data["flabel"] = feat, flabel
        out.append("<script>window.__V=" + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>")

    out.append('<div class="card"><h2>よくある質問</h2>'
               + "".join(f'<details class="q"><summary>{esc(q)}</summary><p>{esc(a)}</p></details>' for q, a in faq) + "</div>")
    out.append(f'</div><footer>{footer_html(cfg, "../")}</footer><script>{JS}</script></body></html>')
    digest = (ans[1] + " " + " / ".join(ans[2][:3])) if rows else ""
    return "".join(out), len(rows), t0[0], digest


def btn_plain(url, text, cls=""):
    """表の中のリンク(見た目はセルのCSSで決まる)"""
    u = safe_url(url)
    c = f' class="{cls}"' if cls else ""
    if u == "#":
        return f"<span{c}>{text}</span>"
    return f'<a{c} href="{esc(u)}" target="_blank" rel="sponsored nofollow noopener">{text}</a>'


def render_home(cfg, digests, now, demo, scene_list=()):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = base + "/"
    inner = ['<div class="hero"><h1>宿の空室を、日付からさがす</h1>'
             '<p class="lead">条件(客室露天風呂など)とエリアを選んでください。</p></div>']
    feats = [(k, f) for k, f in cfg["features"].items() if any(d[0]["feature"] == k for d in digests)]
    if len(feats) > 1:
        inner.append('<div class="ftabs" id="ftabs" role="group" aria-label="条件を選ぶ" hidden>'
                     + "".join(f'<button type="button" data-f="{esc(k)}" aria-pressed="{"true" if i == 0 else "false"}">{esc(f["label"])}</button>' for i, (k, f) in enumerate(feats))
                     + "</div>")
    for fkey, f in feats:
        items = [d for d in digests if d[0]["feature"] == fkey]
        lis = []
        for p, n, today_n, digest in items:
            alabel = cfg["areas"][p["area"]]["label"]
            sub = (f"今日の空き {today_n}軒 / 掲載 {n}軒" if n else "データを準備中")
            lis.append(f'<a href="{esc(p["slug"])}/">{esc(alabel)}<small>{esc(sub)}</small></a>')
        inner.append(f'<section class="card fsec" data-f="{esc(fkey)}"><h2>{esc(f["label"])}の宿を、エリアからさがす</h2>'
                     f'<nav class="chips achips" aria-label="エリアを選ぶ">{"".join(lis)}</nav>'
                     f'<nav class="chips" aria-label="日程から全エリアを探す"><b>日程から全エリアを探す</b>'
                     + "".join(f'<a href="{esc(hub_slug(cfg, fkey))}/{esc(sc["slug"])}/">{esc(sc["name"])}</a>' for sc in scene_list)
                     + '</nav></section>')
    if len(feats) > 1:
        inner.append("<script>(function(){var t=document.getElementById('ftabs');if(!t)return;t.hidden=false;"
                     "var b=t.querySelectorAll('button'),s=document.querySelectorAll('.fsec');"
                     "function show(f){b.forEach(function(x){x.setAttribute('aria-pressed',x.dataset.f===f?'true':'false')});"
                     "s.forEach(function(x){x.hidden=x.dataset.f!==f})}"
                     "b.forEach(function(x){x.addEventListener('click',function(){show(x.dataset.f)})});show(b[0].dataset.f)})();</script>")
    ans = [d for d in digests if d[3]]
    if ans:
        p, n, today_n, digest = ans[0]
        inner.append(f'<section class="answer"><h2><a href="{esc(p["slug"])}/">{esc(p["title"])}</a></h2><p>{esc(digest)}</p>'
                     f'<p><a href="{esc(p["slug"])}/">空室表を見る ›</a></p></section>')
    same = [u for u in site.get("same_as", []) if str(u).startswith("https://")]
    org = {"@type": "Organization", "name": site["name"], "url": canonical}
    if same:
        org["sameAs"] = same
    ld = [{"@context": "https://schema.org", "@type": "WebSite", "name": site["name"], "url": canonical, "inLanguage": "ja"},
          dict(org, **{"@context": "https://schema.org"})]
    title = f"{site['name']} | 宿の空室をまとめてチェック"
    desc = "楽天トラベルの空室を日付ごとにまとめた、非公式の空室チェックサイト。取得日時と判定方法を公開しています。"
    return shell(cfg, demo, "./", title, desc, canonical, "".join(inner), ld)


ARTIFICIAL_BOTS = ["OAI-SearchBot", "ChatGPT-User", "GPTBot", "PerplexityBot", "Perplexity-User", "Claude-SearchBot",
                   "Claude-User", "ClaudeBot", "Google-Extended", "Applebot", "bingbot", "Googlebot"]


def write_robots(out_dir, base):
    lines = ["# 検索・AIアシスタントの巡回を歓迎します(数字の出どころは /about/ に書いています)"]
    for bot in ARTIFICIAL_BOTS:
        lines += [f"User-agent: {bot}", "Allow: /", ""]
    lines += ["User-agent: *", "Allow: /", "", f"Sitemap: {base}/sitemap.xml", ""]
    (out_dir / "robots.txt").write_text("\n".join(lines), encoding="utf-8")


def write_page(out_dir, rel, text):
    d = out_dir / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--out-dir", default=str(ROOT / "public"))
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    cfg = load_json(args.config, None)
    if cfg is None:
        sys.exit("config.json が見つかりません")
    data_dir, out_dir = Path(args.data_dir), Path(args.out_dir)
    hotels = load_json(data_dir / "hotels.json", {"areas": {}})
    state = load_json(data_dir / "state.json", {"cells": {}, "fetched_by": {}, "meta": {}})
    state.setdefault("cells", {})
    aff = os.environ.get("RAKUTEN_AFFILIATE_ID", "").strip() or str(load_json(data_dir / "site_meta.json", {}).get("affiliate_id", ""))
    if not args.demo:
        rewrite_links(cfg, state, aff)
    state.setdefault("fetched_by", {})
    state.setdefault("meta", {})
    stats = load_json(data_dir / "stats.json", {}) or {}
    now = datetime.now(JST)
    base = cfg["site"]["url"].rstrip("/")

    out_dir.mkdir(parents=True, exist_ok=True)
    digests, ctxs, paths = [], {}, ["", "about/", "privacy/"]
    global LIVE
    LIVE = []
    for page in cfg["pages"]:
        ps = dict(state, fetched=state.get("fetched_by", {}).get(page["area"], {}))
        if hotels_for_page(cfg, hotels, ps, page) or not ps["fetched"]:
            LIVE.append(page)
    for page in cfg["pages"]:
        area = page["area"]
        # このエリアだけの見え方(取得日時・履歴は、エリアごとに貯めている)
        pstate = dict(state, fetched=state.get("fetched_by", {}).get(area, {}))
        pre = f"{area}/"
        pstats = dict(stats,
                      trans={f: stats.get("trans", {}).get(pre + f, {}) for f in cfg["features"]},
                      daily={od: {f: v.get(pre + f, {}) for f in cfg["features"]} for od, v in stats.get("daily", {}).items()})
        n_rows_probe = len(hotels_for_page(cfg, hotels, pstate, page))
        if not n_rows_probe and pstate["fetched"]:
            # 取得は進んだのに、該当する宿が1軒もない組み合わせは、ページを作らない(中身のないページを公開しない)
            shutil.rmtree(out_dir / page["slug"], ignore_errors=True)
            print(f"{page['slug']}: 該当する宿がないため、ページを作りません")
            continue
        text, n_rows, today_n, digest = render_page(cfg, hotels, pstate, pstats, page, now, args.demo)
        write_page(out_dir, page["slug"], text)
        digests.append((page, n_rows, today_n, digest))
        paths.append(page["slug"] + "/")
        print(f"{page['slug']}: 宿 {n_rows} 軒 / 本日◯ {today_n} 軒 / {len(text) // 1024} KB")

        ctx = page_ctx(cfg, hotels, pstate, page, now)
        ctxs[page["slug"]] = ctx
        scene_list = all_scenes(cfg, now.date())
        for sc in scene_list:
            write_page(out_dir, f"{page['slug']}/{sc['slug']}", render_scene(cfg, page, sc, scene_list, ctx, pstate, pstats, args.demo))
            paths.append(f"{page['slug']}/{sc['slug']}/")
        n_h = 0
        for h in ctx["rows"]:
            if re.fullmatch(r"[0-9A-Za-z_-]+", str(h["no"])):
                write_page(out_dir, f"{page['slug']}/hotel/{h['no']}", render_hotel(cfg, page, h, ctx, pstate, pstats, args.demo))
                paths.append(f"{page['slug']}/hotel/{h['no']}/")
                n_h += 1
        print(f"  日付別ページ {len(scene_list)} 枚 / 宿ページ {n_h} 枚")

    scene_list = all_scenes(cfg, now.date())
    for fkey in cfg["features"]:
        area_ctxs = [(pg, ctxs[pg["slug"]]) for pg in cfg["pages"] if pg["feature"] == fkey and pg["slug"] in ctxs]
        if not area_ctxs:
            continue
        for sc in scene_list:
            write_page(out_dir, f"{hub_slug(cfg, fkey)}/{sc['slug']}", render_hub(cfg, fkey, sc, scene_list, area_ctxs, now, args.demo))
            paths.append(f"{hub_slug(cfg, fkey)}/{sc['slug']}/")
    write_page(out_dir, "about", render_about(cfg, ctxs, stats, now, args.demo))
    write_page(out_dir, "privacy", render_privacy(cfg, now, args.demo))
    (out_dir / "index.html").write_text(render_home(cfg, digests, now, args.demo, all_scenes(cfg, now.date())), encoding="utf-8")

    og = ROOT / "assets" / "ogp.png"
    if og.exists():
        (out_dir / "ogp.png").write_bytes(og.read_bytes())
    key = str(cfg["site"].get("indexnow_key", "")).strip()
    if re.fullmatch(r"[0-9a-f]{8,128}", key):
        (out_dir / f"{key}.txt").write_text(key, encoding="utf-8")   # IndexNow(Bing等への更新通知)の本人確認用
    op = cfg.get("operator", {})
    if not (op.get("name") or op.get("contact")):
        print("注意: config.json の operator(運営者名・連絡先)が空です。信頼性のため、入力することをおすすめします(手順書を参照)")
    if "YOUR-SITE" in base:
        print("注意: config.json の site.url がまだ初期値です。公開URLに書き換えてください")
    if args.demo:
        return
    mod = (parse_dt(state.get("updated", "")) or now).date().isoformat()
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{esc(base + '/' + p)}</loc><lastmod>{mod}</lastmod></url>" for p in paths) + "</urlset>", encoding="utf-8")
    write_robots(out_dir, base)


if __name__ == "__main__":
    main()
