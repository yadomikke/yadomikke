"""人数(大人2名・3名・4名)で、楽天の空室APIの返し方がどう変わるかを、少ない回数で確かめる調査用のプログラム。

サイトのページや空室データは変えません。結果は data/probe_adults.json に保存します(アフィリエイトIDは伏せ字)。
楽天APIの呼び出しは約40回。

確かめること
  1. 手順A(15軒まとめて探す)で、3名・4名でも空きの宿が返るか。料金(dailyCharge.total)は人数ぶんの合計か
  2. 手順B(1軒の部屋・プラン)で、3名で泊まれる部屋の名前と料金が取れるか
  3. 返ってくる予約URLに、人数の指定(大人の人数)が入っているか(入っていれば、そのURLで人数どおりに開ける)
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rakuten as rk  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
AREA = "hakone"
N_HOTELS = 45            # 箱根の宿のうち、口コミの多い順に45軒(15軒ずつ3回)
ADULTS = (2, 3, 4)
DETAIL_HOTELS = 3        # 3名で空きのあった宿から、部屋まで見る軒数


def next_days(today):
    """次の金曜と土曜(空きのある宿が多く、比べやすいので、今週末の次の週末)"""
    sat = today + timedelta(days=(5 - today.weekday()) % 7 + 7)
    return [sat - timedelta(days=1), sat]


def mask(s, aff):
    return s.replace(aff, "<ID>") if aff else s


def url_people(url, aff):
    """予約URL(アフィリエイトの場合は中の楽天URLも)から、人数・部屋数らしい項目だけを取り出す"""
    out = {}
    if not url:
        return out
    urls = [url]
    for k, v in parse_qsl(urlsplit(url).query):
        if k == "pc" and v.startswith("http"):
            urls.append(unquote(v))
    for u in urls:
        for k, v in parse_qsl(urlsplit(u).query):
            if re.search(r"otona|adult|ninzu|ninz|heya|room|num|su$", k, re.I):
                out[k] = mask(v, aff)
    return out


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    app, key, aff = (os.environ.get("RAKUTEN_APP_ID", ""), os.environ.get("RAKUTEN_ACCESS_KEY", ""),
                     os.environ.get("RAKUTEN_AFFILIATE_ID", ""))
    if not app or not key:
        raise SystemExit("RAKUTEN_APP_ID と RAKUTEN_ACCESS_KEY が設定されていません")
    client = rk.Client(app, key, aff, referer=cfg["site"]["url"], base=cfg["api"]["base"], versions=cfg["api"]["versions"])
    hotels = json.loads((ROOT / "data" / "hotels.json").read_text(encoding="utf-8"))["areas"][AREA]
    hotels = sorted(hotels, key=lambda h: -(h.get("reviews") or 0))[:N_HOTELS]
    names = {h["no"]: h["name"] for h in hotels}
    nos = [h["no"] for h in hotels]
    days = next_days(datetime.now(JST).date())
    s = cfg["search"]
    res = {"area": AREA, "hotels": len(nos), "days": [d.isoformat() for d in days], "stepA": {}, "stepB": [], "notes": []}

    for d in days:
        for a in ADULTS:
            base = dict(checkinDate=d.isoformat(), checkoutDate=(d + timedelta(days=s["nights"])).isoformat(), adultNum=a, roomNum=1)
            got = {}
            for i in range(0, len(nos), 15):
                batch = nos[i:i + 15]
                page = 1
                while True:
                    try:
                        data = client.call("VacantHotelSearch", hotelNo=",".join(batch), searchPattern=0, hits=30,
                                           page=page, responseType="small", **base)
                    except rk.NotFound:
                        break
                    for no, e in rk.split_hotels(data).items():
                        tot = [rk.to_int((r.get("charge") or {}).get("total")) for r in e["rooms"]]
                        tot = [t for t in tot if t]
                        ch = next((r.get("charge") for r in e["rooms"] if r.get("charge")), {}) or {}
                        got[no] = {"min_total": min(tot) if tot else None, "rooms": len(e["rooms"]),
                                   "charge_keys": sorted(ch.keys()), "charge_sample": {k: ch.get(k) for k in ("total", "rakutenCharge", "chargeFlag") if k in ch},
                                   "hotelMinCharge": rk.to_int(e["basic"].get("hotelMinCharge"))}
                    if page >= rk.page_count(data):
                        break
                    page += 1
            res["stepA"][f"{d.isoformat()}_{a}"] = got
            print(f"{d} 大人{a}名: 空きあり {len(got)}軒 / {len(nos)}軒")

    # 3名・2名の両方で空きのある宿の、最安合計の比(3名の料金が人数ぶんの合計なら、だいたい1.3〜1.6倍)
    for d in days:
        a2, a3 = res["stepA"][f"{d.isoformat()}_2"], res["stepA"][f"{d.isoformat()}_3"]
        both = [no for no in a3 if no in a2 and a2[no]["min_total"] and a3[no]["min_total"]]
        ratios = sorted(round(a3[no]["min_total"] / a2[no]["min_total"], 2) for no in both)
        res["notes"].append({"day": d.isoformat(), "both": len(both), "ratio_3_over_2": ratios,
                             "only3": len([no for no in a3 if no not in a2]), "only2": len([no for no in a2 if no not in a3])})
        print(f"{d}: 2名・3名とも空き {len(both)}軒、3名÷2名の最安料金の比 {ratios}")

    # 手順B: 3名で空きのあった宿の、部屋・プラン
    d = days[-1]
    a3 = res["stepA"][f"{d.isoformat()}_3"]
    for no in list(a3)[:DETAIL_HOTELS]:
        base = dict(checkinDate=d.isoformat(), checkoutDate=(d + timedelta(days=s["nights"])).isoformat(), adultNum=3, roomNum=1)
        try:
            data = client.call("VacantHotelSearch", hotelNo=no, searchPattern=1, hits=30, page=1, responseType="small", **base)
        except rk.NotFound:
            res["stepB"].append({"no": no, "name": names.get(no), "rooms": []})
            continue
        rooms = rk.split_hotels(data).get(no, {"rooms": []})["rooms"]
        out = []
        for r in rooms[:8]:
            b = r["basic"]
            out.append({"roomName": b.get("roomName"), "planName": (b.get("planName") or "")[:40],
                        "total": rk.to_int((r.get("charge") or {}).get("total")),
                        "reserve_url_people": url_people(b.get("reserveUrl", ""), aff)})
        res["stepB"].append({"no": no, "name": names.get(no), "rooms_returned": len(rooms), "rooms": out})
        print(f"手順B {names.get(no)}: 部屋・プラン {len(rooms)}件")
    # 手順Aの予約URL・プラン一覧URLにも、人数の項目があるか
    res["planlist_url_people"] = {}
    for a in ADULTS:
        data = None
        try:
            data = client.call("VacantHotelSearch", hotelNo=nos[0], searchPattern=0, hits=30, page=1, responseType="small",
                               checkinDate=d.isoformat(), checkoutDate=(d + timedelta(days=1)).isoformat(), adultNum=a, roomNum=1)
        except rk.NotFound:
            pass
        if data:
            e = rk.split_hotels(data).get(nos[0], {"basic": {}, "rooms": []})
            res["planlist_url_people"][str(a)] = {
                "planListUrl": url_people(e["basic"].get("planListUrl", ""), aff),
                "reserveUrl": url_people((e["rooms"][0]["basic"].get("reserveUrl", "") if e["rooms"] else ""), aff)}
    res["api_calls"] = client.requests
    text = mask(json.dumps(res, ensure_ascii=False, indent=1), aff)
    (ROOT / "data" / "probe_adults.json").write_text(text, encoding="utf-8")
    print(f"楽天API {client.requests}回。結果を data/probe_adults.json に保存しました")


if __name__ == "__main__":
    main()
