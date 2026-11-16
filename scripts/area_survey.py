"""増やすエリアの候補(温泉地)を、ルールに基づいて点数化し、順位を付ける調査用のプログラム。

サイトのページや空室データは変えません。結果は data/area_survey.json と、画面用の表(Markdown)に出します。
楽天APIの呼び出しは約300回(候補ごとに 宿数÷30 回)。温泉街の中心から半径3km以内の宿だけを数える。

点数のルール(候補の中での順位を0〜100点にそろえてから、重みを付けて足す)
  人気度   40%  エリア内の宿の楽天の口コミ件数の合計(楽天で実際に泊まった人の数に近い)
  近さ     30%  大都市圏の人口 × 距離による減り方(exp(-距離/150km))の合計
  宿数     20%  楽天に載っている宿の数(ページ=入り口の数)
  効率     10%  1軒あたりの口コミ件数(取得の手間は宿数に比例するため)
足切り: 宿が15軒未満 / 今のエリアと宿が3割以上重なる → 対象外。500軒超は「大きい(分けて扱う)」の印を付ける
"""
import argparse
import collections
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

# 候補: (キー, 表示名, 都道府県, 温泉街の中心の緯度, 経度)。中心から半径 RADIUS_KM 以内の宿だけを数える
# (楽天の地区名で探すと、市全体のビジネスホテルが混ざるため。位置は目安で、結果の「近くの駅」で確かめる)
RADIUS_KM = 3.0
CANDIDATES = [
    ("noboribetsu", "登別", "北海道", 42.497, 141.146), ("jozankei", "定山渓", "北海道", 42.966, 141.161),
    ("toya", "洞爺湖", "北海道", 42.565, 140.818), ("akan", "阿寒湖", "北海道", 43.434, 144.095),
    ("yunokawa", "湯の川", "北海道", 41.778, 140.789), ("sounkyo", "層雲峡", "北海道", 43.724, 142.946),
    ("tokachigawa", "十勝川", "北海道", 42.937, 143.284), ("niseko", "ニセコ", "北海道", 42.860, 140.705),
    ("asamushi", "浅虫", "青森", 40.896, 140.865), ("sukayu", "酸ヶ湯・八甲田", "青森", 40.649, 140.851),
    ("hanamaki", "花巻温泉", "岩手", 39.437, 141.082), ("tsunagi", "繋", "岩手", 39.698, 141.007),
    ("naruko", "鳴子", "宮城", 38.744, 140.719), ("akiu", "秋保", "宮城", 38.226, 140.715), ("sakunami", "作並", "宮城", 38.317, 140.630),
    ("nyuto", "乳頭", "秋田", 39.802, 140.788),
    ("ginzan", "銀山", "山形", 38.570, 140.530), ("zao", "蔵王温泉", "山形", 38.168, 140.400),
    ("yunohama", "湯野浜", "山形", 38.774, 139.751), ("atsumi", "あつみ", "山形", 38.624, 139.600), ("tendo", "天童", "山形", 38.360, 140.375),
    ("iizaka", "飯坂", "福島", 37.825, 140.444), ("higashiyama", "東山(会津)", "福島", 37.475, 139.957),
    ("bandaiatami", "磐梯熱海", "福島", 37.469, 140.258), ("urabandai", "裏磐梯", "福島", 37.664, 140.057),
    ("dake", "岳", "福島", 37.624, 140.351),
    ("shiobara", "塩原", "栃木", 36.973, 139.817), ("kawaji", "川治", "栃木", 36.876, 139.701),
    ("shima", "四万", "群馬", 36.684, 138.792), ("minakami", "水上", "群馬", 36.778, 138.967),
    ("manza", "万座", "群馬", 36.636, 138.513), ("oigami", "老神", "群馬", 36.735, 139.237),
    ("chichibu", "秩父", "埼玉", 35.992, 139.085), ("kamogawa", "鴨川", "千葉", 35.113, 140.099),
    ("yugawara", "湯河原", "神奈川", 35.140, 139.083), ("isawa", "石和", "山梨", 35.651, 138.633),
    ("izukogen", "伊豆高原", "静岡", 34.904, 139.120), ("atagawa", "熱川・稲取", "静岡", 34.800, 139.060),
    ("shimoda", "下田", "静岡", 34.677, 138.945), ("toi", "土肥(西伊豆)", "静岡", 34.906, 138.784),
    ("izunagaoka", "伊豆長岡", "静岡", 35.039, 138.944), ("kanzanji", "舘山寺", "静岡", 34.756, 137.665),
    ("shirahone", "白骨・乗鞍", "長野", 36.120, 137.620), ("nozawa", "野沢", "長野", 36.923, 138.447),
    ("yudanaka", "湯田中・渋", "長野", 36.736, 138.424), ("bessho", "別所", "長野", 36.358, 138.152),
    ("hakuba", "白馬", "長野", 36.698, 137.846), ("hirugami", "昼神", "長野", 35.474, 137.756),
    ("asama", "浅間温泉", "長野", 36.264, 137.991), ("tateshina", "蓼科", "長野", 36.060, 138.260),
    ("echigoyuzawa", "越後湯沢", "新潟", 36.935, 138.808), ("tsukioka", "月岡", "新潟", 37.880, 139.270),
    ("unazuki", "宇奈月", "富山", 36.814, 137.582), ("wakura", "和倉", "石川", 37.090, 136.920),
    ("yamashiro", "山代", "石川", 36.293, 136.355), ("yamanaka", "山中", "石川", 36.248, 136.371),
    ("katayamazu", "片山津", "石川", 36.346, 136.338), ("awazu", "粟津", "石川", 36.343, 136.418),
    ("awara", "あわら", "福井", 36.222, 136.193),
    ("okuhida", "奥飛騨", "岐阜", 36.210, 137.550), ("nagashima", "長島", "三重", 35.032, 136.712), ("toba", "鳥羽", "三重", 34.481, 136.843),
    ("yunoyama", "湯の山", "三重", 35.017, 136.452), ("gamagori", "蒲郡", "愛知", 34.810, 137.220),
    ("katsuura", "南紀勝浦", "和歌山", 33.629, 135.941), ("hongu", "本宮・湯の峰", "和歌山", 33.829, 135.759),
    ("ogoto", "雄琴", "滋賀", 35.098, 135.882), ("yumura", "湯村", "兵庫", 35.554, 134.488), ("sumoto", "洲本", "兵庫", 34.340, 134.903),
    ("totsukawa", "十津川", "奈良", 33.904, 135.800), ("amanohashidate", "天橋立", "京都", 35.566, 135.192),
    ("yunohana", "湯の花", "京都", 35.033, 135.538),
    ("tamatsukuri", "玉造", "島根", 35.420, 133.012), ("kaike", "皆生", "鳥取", 35.447, 133.360), ("misasa", "三朝", "鳥取", 35.410, 133.869),
    ("yubara", "湯原", "岡山", 35.198, 133.712), ("yunogo", "湯郷", "岡山", 35.017, 134.143),
    ("yuda", "湯田", "山口", 34.164, 131.459), ("nagatoyumoto", "長門湯本", "山口", 34.317, 131.183),
    ("dogo", "道後", "愛媛", 33.852, 132.786), ("iya", "大歩危・祖谷", "徳島", 33.860, 133.770), ("kotohira", "琴平", "香川", 34.184, 133.817),
    ("ureshino", "嬉野", "佐賀", 33.101, 129.989), ("takeo", "武雄", "佐賀", 33.195, 130.016),
    ("unzen", "雲仙", "長崎", 32.744, 130.262), ("obama", "小浜", "長崎", 32.727, 130.208),
    ("ibusuki", "指宿", "鹿児島", 31.236, 130.645), ("kirishima", "霧島温泉郷", "鹿児島", 31.870, 130.850),
    ("aso", "阿蘇内牧", "熊本", 32.981, 131.037), ("yamaga", "山鹿", "熊本", 33.017, 130.692), ("hitoyoshi", "人吉", "熊本", 32.211, 130.762),
    ("amagase", "天ヶ瀬", "大分", 33.287, 131.084), ("harazuru", "原鶴", "福岡", 33.387, 130.766), ("futsukaichi", "二日市", "福岡", 33.506, 130.528),
]


def km(a, b, c, d):
    r = 6371.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = p2 - p1, math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def search(client, lat, lng):
    """中心から半径 RADIUS_KM 以内の宿(楽天の施設検索の「位置と半径」で探す)"""
    out = {}
    page = 1
    while True:
        try:
            data = client.call("SimpleHotelSearch", latitude=lat, longitude=lng, searchRadius=RADIUS_KM,
                               hits=30, page=page, responseType="small", datumType=1)
        except rk.NotFound:
            break
        for no, e in rk.split_hotels(data).items():
            b = e["basic"]
            out[no] = {"reviews": rk.to_int(b.get("reviewCount")) or 0, "review": b.get("reviewAverage"),
                       "lat": b.get("latitude"), "lng": b.get("longitude"), "station": b.get("nearestStation") or ""}
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

    rows = []
    for ck, label, pref, clat, clng in CANDIDATES:
        r = {"key": ck, "label": label, "pref": pref, "center": [clat, clng]}
        hs = search(client, clat, clng)
        st = collections.Counter(h["station"] for h in hs.values() if h["station"]).most_common(3)
        r["stations"] = "・".join(f"{k}({v})" for k, v in st)
        n = len(hs)
        tot = sum(h["reviews"] for h in hs.values())
        pts = [(float(h["lat"]), float(h["lng"])) for h in hs.values() if h["lat"] and h["lng"]]
        pts = [p for p in pts if 20 <= p[0] <= 46 and 122 <= p[1] <= 154]
        lat = statistics.median(p[0] for p in pts) if pts else clat
        lng = statistics.median(p[1] for p in pts) if pts else clng
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
        print(f"{label}: 駅 {r['stations']} 宿{n} 口コミ{tot} 近さ{r['access']} 重なり{overlap:.0%} {reason}", flush=True)

    ranked = score(rows)
    out = {"generated_at": datetime.now(JST).isoformat(timespec="seconds"), "api_requests": client.requests,
           "weights": WEIGHTS, "rules": {"radius_km": RADIUS_KM, "min_hotels": MIN_HOTELS, "big_hotels": BIG_HOTELS, "max_overlap": MAX_OVERLAP, "decay_km": DECAY_KM},
           "ranked": ranked, "excluded": [r for r in rows if not r["eligible"]]}
    (data_dir / "area_survey.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["## 増やすエリアの候補(点数順)", "",
             "| 順位 | エリア | 近くの駅(宿数) | 宿数 | 口コミ合計 | 1軒あたり | 近い都市 | 点数 | 人気 | 近さ | 宿数点 | 効率 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in ranked:
        p = r["points"]
        lines.append(f"| {r['rank']} | {r['label']}{' ※大' if r['big'] else ''} | {r['stations']} | {r['hotels']} | {r['reviews_total']:,} | "
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
