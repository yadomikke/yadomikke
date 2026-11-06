"""見本表示用の架空データを preview_data/ に作る(本番の data/ には触れません)。

    python scripts/make_demo_data.py
    python scripts/build.py --demo --data-dir preview_data --out-dir preview --config preview_data/preview_config.json
"""
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenes  # noqa: E402
OUT = ROOT / "preview_data"
OUT.mkdir(exist_ok=True)

cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
rnd = random.Random(7)
now = datetime.now(JST)
today = now.date()

names = ["デモ旅館 山ふところ", "デモの宿 湯けむり亭", "デモ荘 せせらぎ", "デモ別邸 松の庵",
         "デモホテル 箱根テラス", "デモ旅館 星の湯", "デモの湯 しののめ", "デモ庵 かすみ"]
hotels = []
for i, n in enumerate(names):
    hotels.append({"no": str(9000001 + i), "name": n, "url": "#", "plan_url": "#",
                   "review": round(rnd.uniform(3.9, 4.8), 2), "reviews": rnd.randint(40, 900),
                   "address": "デモ県デモ市", "station": rnd.choice(["箱根湯本駅 徒歩5分", "強羅駅 徒歩3分", "宮ノ下駅 徒歩8分", ""]),
                   "image": ("data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='168' height='126'%3E"
                             "%3Crect width='100%25' height='100%25' fill='%23cfd6dd'/%3E%3Ctext x='50%25' y='54%25' "
                             "font-size='18' text-anchor='middle' fill='%23667'%3EPHOTO%3C/text%3E%3C/svg%3E")})

alldays = [today + timedelta(days=o) for o in range(cfg["search"]["days_ahead"])] + scenes.extra_dates(cfg, today)
# バッジの見本が出るように、一部の宿の評価・件数・料金を調整
hotels[0].update(review=4.35, reviews=820)      # レビュー多数 (+コスパ良し)
hotels[2].update(review=4.72, reviews=260)      # 一度は泊まりたい(料金が高い宿)
hotels[3].update(review=4.58, reviews=1240)     # 一度は泊まりたい+レビュー多数
hotels[4].update(review=3.92, reviews=640)      # レビュー多数のみ
hotels[5].update(review=3.5, reviews=80)        # バッジなし
BASE = {hotels[2]["no"]: 118000, hotels[3]["no"]: 132000}
cells, meta = {}, {}
for h in hotels:
    base = BASE.get(h["no"], rnd.randint(28000, 90000))
    busy = rnd.uniform(0.25, 0.65)
    cells[h["no"]] = {}
    meta[h["no"]] = {"checks": 5, "seen": {"roten": True}}
    for d in alldays:
        off = (d - today).days
        if off > 24 and off < cfg["search"]["days_ahead"] and rnd.random() < 0.15:
            continue  # 未取得(—)の例
        weekend = d.weekday() >= 4
        if rnd.random() < busy + (0.2 if weekend else 0):
            cells[h["no"]][d.isoformat()] = {"s": "f"}
            continue
        price = int(base * (1.5 if weekend else 1.0) / 100) * 100
        if rnd.random() < 0.25:
            cells[h["no"]][d.isoformat()] = {"s": "v", "any": [price, "#"]}
        else:
            cells[h["no"]][d.isoformat()] = {"s": "v", "any": [price, "#"],
                                             "f": {"roten": [price + rnd.randint(0, 3) * 5000, "#", "露天風呂付き客室(デモ)"]}}

# 最初に表示される土曜は、4種類(3バッジ+なし)がそろうようにする
first_sat = next(x for x in alldays if x.weekday() == 5)
for i, price in ((0, 52000), (2, 118000), (3, 132000), (4, 61000), (5, 47000)):
    no = hotels[i]["no"]
    cells[no][first_sat.isoformat()] = {"s": "v", "any": [price, "#"], "f": {"roten": [price, "#", "露天風呂付き客室(デモ)"]}}
fetched = {d.isoformat(): (now - timedelta(hours=rnd.randint(1, 20))).isoformat(timespec="seconds") for d in alldays}
# 条件の切り替え見本: 箱根の3軒は「ペット可の客室」もある想定
for i in (1, 4, 6):
    no = hotels[i]["no"]
    meta[no]["seen"]["pet"] = True
    for d, c in cells[no].items():
        if c.get("s") == "v" and rnd.random() < 0.6:
            c.setdefault("f", {})["pet"] = [c["any"][0] + 3000, "#", "ペット可の和洋室(デモ)"]
# 2つ目のエリア(熱海)は、箱根の見本をコピーして作る(複数エリアの見え方の確認用)
hotels2 = []
for i, h in enumerate(hotels[:6]):
    n2 = str(9100001 + i)
    hotels2.append(dict(h, no=n2, name=h["name"].replace("デモ", "デモ熱海", 1)))
    cells[n2] = {d: (dict(c) if rnd.random() > 0.2 else {"s": "f"}) for d, c in cells[h["no"]].items()}
    meta[n2] = {"checks": 5, "seen": {"roten": True}}
fetched2 = {d: (now - timedelta(hours=rnd.randint(1, 20))).isoformat(timespec="seconds") for d in list(fetched)[:21]}
(OUT / "hotels.json").write_text(json.dumps({"updated": now.isoformat(timespec="seconds"), "areas": {"hakone": hotels, "atami": hotels2}}, ensure_ascii=False), encoding="utf-8")
(OUT / "state.json").write_text(json.dumps({"cells": cells, "fetched_by": {"hakone": fetched, "atami": fetched2}, "meta": meta, "updated": now.isoformat(timespec="seconds")}, ensure_ascii=False), encoding="utf-8")
pcfg = dict(cfg, areas={k: cfg["areas"][k] for k in ("hakone", "atami")},
            pages=[p for p in cfg["pages"] if p["slug"] in ("hakone-roten", "atami-roten", "hakone-pet")])
(OUT / "preview_config.json").write_text(json.dumps(pcfg, ensure_ascii=False), encoding="utf-8")
# 履歴(見本): 観測開始から30日たった想定の数字
trans = {"hakone/roten": {str(b): [rnd.randint(300, 900), 0, rnd.randint(80, 300), 0] for b in range(6)}}
for b, v in trans["hakone/roten"].items():
    v[1] = int(v[0] * rnd.uniform(0.02, 0.09))
    v[3] = int(v[2] * rnd.uniform(0.15, 0.45))
daily = {}
for back in (3, 2, 1, 0):
    od = (today - timedelta(days=back)).isoformat()
    daily[od] = {"hakone/roten": {d.isoformat(): [rnd.randint(0, 5), 30000] for d in alldays[:10]}}
price = {h["no"]: {"roten": {"min": [30000, today.isoformat(), today.isoformat()], "max": [90000, today.isoformat(), today.isoformat()]}} for h in hotels}
stats = {"since": (today - timedelta(days=29)).isoformat(), "runs": 120, "obs": 12345, "trans": trans, "daily": daily, "price": price}
(OUT / "stats.json").write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
print("demo data written to", OUT)
