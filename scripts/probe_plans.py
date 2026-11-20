"""プラン名・部屋名に「カップル・記念日」「赤ちゃん連れ」などの言葉を含む宿が、エリアごとに何軒あるかを数える調査用のプログラム。

サイトのページや空室データは変えません。結果は data/probe_plans.json に保存します(URLは保存しません)。
楽天APIの呼び出しは最大約400回(3エリア × 1日。空きのある宿を口コミの多い順に最大120軒ずつ、部屋・プランを1ページ分見る)。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rakuten as rk  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
AREAS = ["hakone", "atami", "kinugawa"]
GROUPS = {
    "couple": ["カップル", "ご夫婦", "夫婦", "記念日", "アニバーサリー", "プロポーズ", "ハネムーン", "大人限定", "大人専用", "大人だけ"],
    "baby": ["赤ちゃん", "ベビー", "乳幼児", "離乳食", "おむつ", "オムツ", "ママ", "パパ"],
    "kids": ["お子様", "お子さま", "子供", "子ども", "キッズ", "ファミリー", "家族"],
}
MAX_DETAIL = 120          # 1エリアで部屋・プランまで見る宿の上限


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    app, key, aff = (os.environ.get("RAKUTEN_APP_ID", ""), os.environ.get("RAKUTEN_ACCESS_KEY", ""),
                     os.environ.get("RAKUTEN_AFFILIATE_ID", ""))
    if not app or not key:
        raise SystemExit("RAKUTEN_APP_ID と RAKUTEN_ACCESS_KEY が設定されていません")
    client = rk.Client(app, key, aff, referer=cfg["site"]["url"], base=cfg["api"]["base"], versions=cfg["api"]["versions"])
    hotels_all = json.loads((ROOT / "data" / "hotels.json").read_text(encoding="utf-8"))["areas"]
    today = datetime.now(JST).date()
    day = today + timedelta(days=(5 - today.weekday()) % 7 + 7)   # 次の週末の土曜
    base = dict(checkinDate=day.isoformat(), checkoutDate=(day + timedelta(days=1)).isoformat(), adultNum=2, roomNum=1)
    res = {"day": day.isoformat(), "areas": {}}
    for area in AREAS:
        hs = hotels_all.get(area, [])
        names = {h["no"]: h["name"] for h in hs}
        nos = [h["no"] for h in hs]
        vacant = []
        for i in range(0, len(nos), 15):
            page = 1
            while True:
                try:
                    data = client.call("VacantHotelSearch", hotelNo=",".join(nos[i:i + 15]), searchPattern=0, hits=30,
                                       page=page, responseType="small", **base)
                except rk.NotFound:
                    break
                vacant += list(rk.split_hotels(data))
                if page >= rk.page_count(data):
                    break
                page += 1
        vacant = list(dict.fromkeys(vacant))
        # 口コミの多い順に見る(上限あり)
        rv = {h["no"]: h.get("reviews") or 0 for h in hs}
        vacant.sort(key=lambda n: -rv.get(n, 0))
        per_group = {g: {} for g in GROUPS}
        kw_count = {g: {k: 0 for k in ks} for g, ks in GROUPS.items()}
        looked = 0
        for no in vacant[:MAX_DETAIL]:
            try:
                data = client.call("VacantHotelSearch", hotelNo=no, searchPattern=1, hits=30, page=1, responseType="small", **base)
            except rk.NotFound:
                continue
            looked += 1
            rooms = rk.split_hotels(data).get(no, {"rooms": []})["rooms"]
            for g, ks in GROUPS.items():
                hit = []
                for r in rooms:
                    b = r["basic"]
                    text = f"{b.get('planName', '')} {b.get('roomName', '')}"
                    ws = [k for k in ks if k in text]
                    if ws:
                        hit.append((rk.to_int((r.get("charge") or {}).get("total")), b.get("planName", "")[:50], b.get("roomName", "")[:40], ws))
                if hit:
                    for k in {w for x in hit for w in x[3]}:
                        kw_count[g][k] += 1
                    p = min((x for x in hit if x[0]), key=lambda x: x[0], default=hit[0])
                    per_group[g][no] = {"name": names.get(no, no), "min_total": p[0], "plan": p[1], "room": p[2], "words": p[3]}
        res["areas"][area] = {
            "hotels": len(nos), "vacant": len(vacant), "looked": looked,
            "groups": {g: {"hotels": len(v), "examples": list(v.values())[:6]} for g, v in per_group.items()},
            "keyword_hotels": kw_count,
        }
        print(f"{area}: 宿{len(nos)}軒・空き{len(vacant)}軒・プランまで確認{looked}軒 → "
              + " / ".join(f"{g} {len(v)}軒" for g, v in per_group.items()))
    res["api_calls"] = client.requests
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if aff:
        text = text.replace(aff, "<ID>")
    (ROOT / "data" / "probe_plans.json").write_text(text, encoding="utf-8")
    print(f"楽天API {client.requests}回。結果を data/probe_plans.json に保存しました")


if __name__ == "__main__":
    main()
