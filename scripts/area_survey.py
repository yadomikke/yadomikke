"""増やすエリアの候補(温泉地)を、ルールに基づいて点数化し、順位を付ける調査用のプログラム。

サイトのページや空室データは変えません。結果は data/area_survey.json と、画面用の表(Markdown)に出します。
楽天APIの呼び出しは約700回(候補ごとに 宿数÷30 回 + 地区コード1回)。

点数のルール(候補の中での順位を0〜100点にそろえてから、重みを付けて足す)
  人気度   40%  エリア内の宿の楽天の口コミ件数の合計(楽天で実際に泊まった人の数に近い)
  近さ     30%  大都市圏の人口 × 距離による減り方(exp(-距離/150km))の合計
  宿数     20%  楽天に載っている宿の数(ページ=入り口の数)
  効率     10%  1軒あたりの口コミ件数(取得の手間は宿数に比例するため)
足切り: 宿が15軒未満 / 今のエリアと宿が3割以上重なる → 対象外。500軒超は「大きい(分けて扱う)」の印を付ける
"""
import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rakuten as rk  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))

WEIGHTS = {"popular": 0.40, "access": 0.30, "hotels": 0.20, "efficiency": 0.10}
MIN_HOTELS, BIG_HOTELS, MAX_OVERLAP = 15, 500, 0.30
DECAY_KM = 150

# 大都市圏(中心の緯度・経度、人口の目安・万人)
METROS = [
    ("首都圏", 35.681, 139.767, 3700), ("関西", 34.702, 135.496, 1800), ("中京", 35.170, 136.881, 900),
    ("福岡・北九州", 33.590, 130.420, 550), ("札幌", 43.068, 141.350, 260), ("仙台", 38.260, 140.882, 230),
    ("広島", 34.397, 132.475, 200),
]

# 候補: (キー, 表示名, 都道府県, 楽天の地区名に含まれる言葉)
CANDIDATES = [
    ("noboribetsu", "登別", "北海道", ["登別"]), ("jozankei", "定山渓", "北海道", ["定山渓"]),
    ("toya", "洞爺湖", "北海道", ["洞爺"]), ("akan", "阿寒湖", "北海道", ["阿寒"]),
    ("yunokawa", "湯の川", "北海道", ["湯の川", "函館"]), ("sounkyo", "層雲峡", "北海道", ["層雲峡"]),
    ("tokachigawa", "十勝川", "北海道", ["十勝川"]), ("niseko", "ニセコ", "北海道", ["ニセコ"]),
    ("asamushi", "浅虫", "青森", ["浅虫"]), ("hakkoda", "八甲田・酸ヶ湯", "青森", ["八甲田", "酸ヶ湯"]),
    ("hanamaki", "花巻", "岩手", ["花巻"]), ("tsunagi", "繋・雫石", "岩手", ["繋", "雫石"]),
    ("naruko", "鳴子", "宮城", ["鳴子"]), ("akiu", "秋保", "宮城", ["秋保"]), ("sakunami", "作並", "宮城", ["作並"]),
    ("nyuto", "乳頭・田沢湖", "秋田", ["乳頭", "田沢湖"]),
    ("ginzan", "銀山", "山形", ["銀山", "尾花沢"]), ("zao", "蔵王", "山形", ["蔵王"]),
    ("yunohama", "湯野浜・あつみ", "山形", ["湯野浜", "あつみ"]), ("tendo", "天童", "山形", ["天童"]),
    ("iizaka", "飯坂", "福島", ["飯坂"]), ("higashiyama", "東山・会津若松", "福島", ["東山", "会津若松"]),
    ("bandaiatami", "磐梯熱海", "福島", ["磐梯熱海"]), ("urabandai", "裏磐梯", "福島", ["裏磐梯"]),
    ("dake", "岳", "福島", ["岳温泉"]),
    ("shiobara", "塩原", "栃木", ["塩原"]), ("kawaji", "川治", "栃木", ["川治"]),
    ("shima", "四万", "群馬", ["四万"]), ("minakami", "水上・みなかみ", "群馬", ["水上", "みなかみ"]),
    ("manza", "万座", "群馬", ["万座"]), ("oigami", "老神・沼田", "群馬", ["老神", "沼田"]),
    ("chichibu", "秩父", "埼玉", ["秩父"]), ("kamogawa", "鴨川", "千葉", ["鴨川"]),
    ("yugawara", "湯河原", "神奈川", ["湯河原"]), ("isawa", "石和", "山梨", ["石和"]),
    ("izukogen", "伊豆高原", "静岡", ["伊豆高原"]), ("atagawa", "熱川・稲取", "静岡", ["熱川", "稲取", "東伊豆"]),
    ("shimoda", "下田", "静岡", ["下田"]), ("nishiizu", "西伊豆・土肥", "静岡", ["西伊豆", "堂ヶ島", "土肥"]),
    ("izunagaoka", "伊豆長岡", "静岡", ["伊豆長岡"]), ("kanzanji", "舘山寺・浜名湖", "静岡", ["舘山寺", "浜名湖"]),
    ("shirahone", "白骨・乗鞍・上高地", "長野", ["白骨", "乗鞍", "上高地"]), ("nozawa", "野沢", "長野", ["野沢"]),
    ("yudanaka", "湯田中・渋", "長野", ["湯田中", "渋温泉"]), ("bessho", "別所", "長野", ["別所"]),
    ("hakuba", "白馬", "長野", ["白馬"]), ("hirugami", "昼神", "長野", ["昼神"]),
    ("asama", "浅間・美ヶ原", "長野", ["浅間温泉", "美ヶ原"]), ("tateshina", "蓼科", "長野", ["蓼科"]),
    ("echigoyuzawa", "越後湯沢", "新潟", ["湯沢"]), ("tsukioka", "月岡", "新潟", ["月岡"]),
    ("unazuki", "宇奈月", "富山", ["宇奈月"]), ("wakura", "和倉", "石川", ["和倉"]),
    ("kaga", "加賀(山代・山中・片山津)", "石川", ["山代", "山中", "片山津", "加賀"]), ("awazu", "粟津", "石川", ["粟津"]),
    ("awara", "あわら", "福井", ["あわら", "芦原"]),
    ("okuhida", "奥飛騨", "岐阜", ["奥飛騨"]), ("nagashima", "長島", "三重", ["長島"]), ("toba", "鳥羽", "三重", ["鳥羽"]),
    ("yunoyama", "湯の山", "三重", ["湯の山"]), ("gamagori", "蒲郡", "愛知", ["蒲郡"]),
    ("katsuura", "南紀勝浦", "和歌山", ["勝浦"]), ("hongu", "本宮・湯の峰・川湯", "和歌山", ["本宮", "湯の峰", "川湯"]),
    ("ogoto", "雄琴", "滋賀", ["雄琴"]), ("yumura", "湯村", "兵庫", ["湯村"]), ("sumoto", "洲本・淡路", "兵庫", ["洲本", "淡路"]),
    ("totsukawa", "十津川", "奈良", ["十津川"]), ("amanohashidate", "天橋立・宮津", "京都", ["天橋立", "宮津"]),
    ("yunohana", "湯の花・亀岡", "京都", ["湯の花", "亀岡"]),
    ("tamatsukuri", "玉造", "島根", ["玉造"]), ("kaike", "皆生", "鳥取", ["皆生"]), ("misasa", "三朝", "鳥取", ["三朝"]),
    ("yubara", "湯原", "岡山", ["湯原"]), ("yunogo", "湯郷", "岡山", ["湯郷"]),
    ("yuda", "湯田", "山口", ["湯田"]), ("nagatoyumoto", "長門湯本", "山口", ["長門"]),
    ("dogo", "道後", "愛媛", ["道後"]), ("iya", "祖谷・大歩危", "徳島", ["祖谷", "大歩危"]), ("kotohira", "琴平", "香川", ["琴平", "こんぴら"]),
    ("ureshino", "嬉野", "佐賀", ["嬉野"]), ("takeo", "武雄", "佐賀", ["武雄"]),
    ("unzen", "雲仙", "長崎", ["雲仙"]), ("obama", "小浜", "長崎", ["小浜"]),
    ("ibusuki", "指宿", "鹿児島", ["指宿"]), ("kirishima", "霧島", "鹿児島", ["霧島"]),
    ("aso", "阿蘇・内牧", "熊本", ["阿蘇", "内牧"]), ("yamaga", "山鹿", "熊本", ["山鹿"]), ("hitoyoshi", "人吉", "熊本", ["人吉"]),
    ("hita", "日田・天ヶ瀬", "大分", ["日田", "天ヶ瀬"]), ("harazuru", "原鶴", "福岡", ["原鶴"]), ("futsukaichi", "二日市", "福岡", ["二日市"]),
]


def km(a, b, c, d):
    r = 6371.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = p2 - p1, math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def area_index(tree):
    """地区コードの一覧を (大, 中, 中の名前, 小, 小の名前, 細, 細の名前) の並びにする"""
    rows = []

    def walk(n, ctx):
        if isinstance(n, list):
            c = dict(ctx)
            for x in n:
                walk(x, c)
        elif isinstance(n, dict):
            for k in ("largeClassCode", "middleClassCode", "middleClassName", "smallClassCode", "smallClassName"):
                if k in n:
                    ctx[k] = n[k]
                    if k == "middleClassCode":
                        ctx.pop("smallClassCode", None)
                        ctx.pop("smallClassName", None)
            if "smallClassCode" in n:
                rows.append((ctx.get("largeClassCode"), ctx.get("middleClassCode"), str(ctx.get("middleClassName", "")),
                             str(n["smallClassCode"]), str(n.get("smallClassName", "")), None, ""))
            if "detailClassCode" in n and ctx.get("smallClassCode"):
                rows.append((ctx.get("largeClassCode"), ctx.get("middleClassCode"), str(ctx.get("middleClassName", "")),
                             str(ctx["smallClassCode"]), str(ctx.get("smallClassName", "")),
                             str(n["detailClassCode"]), str(n.get("detailClassName", ""))))
            for v in n.values():
                if isinstance(v, (list, dict)):
                    walk(v, dict(ctx))

    walk(tree, {})
    return [r for r in rows if r[0] and r[1]]


def match(rows, pref, words):
    """都道府県が合う地区のうち、名前に言葉を含むもの。小区分を優先し、名前の短いものを選ぶ"""
    smalls = [r for r in rows if r[5] is None and pref in r[2] and any(w in r[4] for w in words)]
    if smalls:
        return min(smalls, key=lambda r: len(r[4]))
    details = [r for r in rows if r[5] is not None and pref in r[2] and any(w in r[6] for w in words)]
    if details:
        return min(details, key=lambda r: len(r[6]))
    return None


def search(client, large, middle, small, details):
    out = {}
    for dc in details:
        page = 1
        while True:
            try:
                data = client.call("SimpleHotelSearch", largeClassCode=large, middleClassCode=middle, smallClassCode=small,
                                   detailClassCode=dc, hits=30, page=page, responseType="small", datumType=1)
            except rk.NotFound:
                break
            for no, e in rk.split_hotels(data).items():
                b = e["basic"]
                out[no] = {"reviews": rk.to_int(b.get("reviewCount")) or 0, "review": b.get("reviewAverage"),
                           "lat": b.get("latitude"), "lng": b.get("longitude")}
            if page >= rk.page_count(data):
                break
            page += 1
    return out


def pct_rank(vals):
    """候補の中での順位を0〜100点にする(同じ値は同じ点)"""
    s = sorted(vals)
    n = len(s)
    if n <= 1:
        return [100.0] * n
    return [100.0 * (sum(1 for x in s if x < v) + 0.5 * (sum(1 for x in s if x == v) - 1)) / (n - 1) for v in vals]


def score(rows):
    ok = [r for r in rows if r["eligible"]]
    for key, f in (("popular", lambda r: math.log1p(r["reviews_total"])), ("access", lambda r: r["access"]),
                   ("hotels", lambda r: math.log1p(r["hotels"])), ("efficiency", lambda r: r["reviews_per_hotel"])):
        for r, p in zip(ok, pct_rank([f(r) for r in ok])):
            r.setdefault("points", {})[key] = round(p, 1)
    for r in ok:
        r["score"] = round(sum(WEIGHTS[k] * r["points"][k] for k in WEIGHTS), 1)
    ok.sort(key=lambda r: -r["score"])
    for i, r in enumerate(ok, 1):
        r["rank"] = i
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    data_dir = Path(args.data_dir)
    app, key, aff = (os.environ.get(k, "").strip() for k in ("RAKUTEN_APP_ID", "RAKUTEN_ACCESS_KEY", "RAKUTEN_AFFILIATE_ID"))
    if not app or not key:
        raise SystemExit("RAKUTEN_APP_ID と RAKUTEN_ACCESS_KEY が必要です")
    client = rk.Client(app, key, aff, referer=cfg["site"]["url"], base=cfg["api"]["base"], versions=cfg["api"]["versions"])
    existing = json.loads((data_dir / "hotels.json").read_text(encoding="utf-8")).get("areas", {})
    have = {h["no"] for lst in existing.values() for h in lst}
    have_labels = {v["label"] for v in cfg["areas"].values()}

    tree = client.call("GetAreaClass")
    idx = area_index(tree)
    print(f"地区コード: {len(idx)} 件", flush=True)
    rows = []
    for ck, label, pref, words in CANDIDATES:
        r = {"key": ck, "label": label, "pref": pref}
        m = match(idx, pref, words)
        if not m or label in have_labels:
            r.update(eligible=False, reason="楽天の地区が見つからない" if not m else "今のエリアにある")
            rows.append(r)
            print(f"{label}: {r['reason']}", flush=True)
            continue
        large, middle, mname, small, sname, detail, dname = m
        details = [detail] if detail else (rk.detail_codes_from_tree(tree, small) or [None])
        hs = search(client, large, middle, small, details)
        r["rakuten_area"] = f"{mname} > {sname}" + (f" > {dname}" if detail else "")
        r["codes"] = {"large": large, "middle": middle, "small": small, "detail": detail}
        n = len(hs)
        tot = sum(h["reviews"] for h in hs.values())
        pts = [(float(h["lat"]), float(h["lng"])) for h in hs.values() if h["lat"] and h["lng"]]
        pts = [p for p in pts if 20 <= p[0] <= 46 and 122 <= p[1] <= 154]
        lat = statistics.median(p[0] for p in pts) if pts else None
        lng = statistics.median(p[1] for p in pts) if pts else None
        near = []
        acc = 0.0
        if lat is not None:
            for name, la, ln, pop in METROS:
                d = km(lat, lng, la, ln)
                acc += pop * math.exp(-d / DECAY_KM)
                near.append((d, name))
        ratings = [(float(h["review"]), h["reviews"]) for h in hs.values() if h["review"] and h["reviews"]]
        avg = (sum(a * w for a, w in ratings) / sum(w for _, w in ratings)) if ratings else None
        overlap = (sum(1 for no in hs if no in have) / n) if n else 0
        r.update(hotels=n, reviews_total=tot, reviews_per_hotel=round(tot / n, 1) if n else 0,
                 rating=round(avg, 2) if avg else None, lat=lat, lng=lng, access=round(acc, 1),
                 nearest_metro=(f"{min(near)[1]} 約{round(min(near)[0])}km" if near else ""),
                 overlap=round(overlap, 2), big=n > BIG_HOTELS)
        reason = ("宿が15軒未満" if n < MIN_HOTELS else "今のエリアと3割以上重なる" if overlap >= MAX_OVERLAP else "")
        r["eligible"] = not reason
        if reason:
            r["reason"] = reason
        rows.append(r)
        print(f"{label}: {r['rakuten_area']} 宿{n} 口コミ{tot} 近さ{r['access']} 重なり{overlap:.0%} {reason}", flush=True)

    ranked = score(rows)
    out = {"generated_at": datetime.now(JST).isoformat(timespec="seconds"), "api_requests": client.requests,
           "weights": WEIGHTS, "rules": {"min_hotels": MIN_HOTELS, "big_hotels": BIG_HOTELS, "max_overlap": MAX_OVERLAP, "decay_km": DECAY_KM},
           "ranked": ranked, "excluded": [r for r in rows if not r["eligible"]]}
    (data_dir / "area_survey.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["## 増やすエリアの候補(点数順)", "",
             "| 順位 | エリア | 楽天の地区 | 宿数 | 口コミ合計 | 1軒あたり | 近い都市 | 点数 | 人気 | 近さ | 宿数点 | 効率 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in ranked:
        p = r["points"]
        lines.append(f"| {r['rank']} | {r['label']}{' ※大' if r['big'] else ''} | {r['rakuten_area']} | {r['hotels']} | {r['reviews_total']:,} | "
                     f"{r['reviews_per_hotel']} | {r['nearest_metro']} | **{r['score']}** | {p['popular']} | {p['access']} | {p['hotels']} | {p['efficiency']} |")
    lines += ["", "### 対象外", ""] + [f"- {r['label']}: {r.get('reason', '')}" + (f"(宿{r['hotels']}・重なり{r['overlap']:.0%})" if "hotels" in r else "")
                                     for r in rows if not r["eligible"]]
    lines += ["", f"楽天APIの呼び出し: {client.requests} 回"]
    text = "\n".join(lines)
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
