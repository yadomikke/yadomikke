"""(一時的な確認用)楽天市場APIで、ふるさと納税の返礼品がどこまで取れるかを調べてログに出す。
ページは作らず、データも保存しません。確認が終わったら、このファイルと furusato-check.yml は消します。
"""
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rakuten as rk  # noqa: E402

APP, KEY, AFF = (os.environ.get(k, "").strip() for k in ("RAKUTEN_APP_ID", "RAKUTEN_ACCESS_KEY", "RAKUTEN_AFFILIATE_ID"))
BASES = ["https://openapi.rakuten.co.jp/ichibams/api", "https://openapi.rakuten.co.jp/engine/api"]
VER = {"IchibaItem/Search": "20220601", "IchibaGenre/Search": "20140222", "IchibaItem/Ranking": "20220601"}


def hide(s):
    return str(s).replace(AFF, "<AFF>") if AFF else str(s)


def client(base):
    return rk.Client(APP, KEY, AFF, referer="https://yadomikke.com", base=base, versions=VER, interval=1.2)


def try_call(c, api, **p):
    try:
        return c.call(api, **p), None
    except rk.ApiError as e:
        return None, f"{e.status} {e.error}: {e.description}"


def items_of(d):
    out = []
    for it in d.get("Items", []) if isinstance(d, dict) else []:
        out.append(it.get("Item", it) if isinstance(it, dict) else it)
    return out


def show(it):
    keys = ["itemName", "itemPrice", "shopName", "shopCode", "genreId", "availability", "reviewCount", "reviewAverage",
            "pointRate", "postageFlag", "itemUrl", "affiliateUrl", "affiliateRate"]
    for k in keys:
        v = it.get(k)
        if isinstance(v, str) and len(v) > 110:
            v = v[:110] + "…"
        print(f"    {k}: {hide(v)}")
    cap = str(it.get("itemCaption", ""))
    print(f"    itemCaption(先頭200字): {hide(cap[:200])}")
    print(f"    その他の項目名: {sorted(set(it) - set(keys) - {'itemCaption'})}")


def main():
    if not APP or not KEY:
        sys.exit("楽天のIDが設定されていません")
    base = None
    for b in BASES:
        d, err = try_call(client(b), "IchibaItem/Search", keyword="ふるさと納税 米", hits=3)
        print(f"[接続先] {b} -> {'OK' if d else err}")
        if d:
            base = b
            break
    if not base:
        sys.exit("楽天市場APIに接続できませんでした(アプリで楽天市場APIが使えない設定の可能性)")
    c = client(base)
    d, _ = try_call(c, "IchibaItem/Search", keyword="ふるさと納税 米 10kg", hits=30)
    its = items_of(d)
    print(f"\n[1] キーワード「ふるさと納税 米 10kg」: 全{d.get('count')}件 / 取得{len(its)}件")
    gid = Counter(str(i.get("genreId")) for i in its).most_common(5)
    shops = Counter(re.sub(r"[-_].*", "", str(i.get("shopCode"))) for i in its).most_common(5)
    print(f"    多いジャンルID: {gid}")
    print(f"    多いショップコードの頭: {shops}")
    for i in its[:2]:
        print("  --- 例 ---")
        show(i)
    furu = [i for i in its if str(i.get("shopCode", "")).startswith("f")]
    print(f"    ショップコードが f で始まる(=楽天ふるさと納税の自治体と思われる)件数: {len(furu)}/{len(its)}")

    # ふるさと納税のジャンル(いくつかの候補を試す)
    for g in ["553283", "100000"]:
        gd, err = try_call(c, "IchibaGenre/Search", genreId=g)
        if gd:
            cur = gd.get("current") or {}
            ch = gd.get("children") or []
            names = [((x.get("child") or x).get("genreName"), (x.get("child") or x).get("genreId")) for x in ch][:25]
            print(f"\n[2] ジャンル {g}: {cur.get('genreName') if isinstance(cur, dict) else cur} / 子ジャンル{len(ch)}件: {names}")
        else:
            print(f"\n[2] ジャンル {g}: {err}")
    for g in [x for x, _ in gid[:2]]:
        gd, err = try_call(c, "IchibaGenre/Search", genreId=g)
        if gd:
            par = gd.get("parents") or []
            print(f"    ジャンル{g}の親: {[(p.get('parent') or p).get('genreName') for p in par]} / 自身: {(gd.get('current') or {}).get('genreName')}")

    # 自治体ショップに絞った検索(ジャンル指定)と、件数
    for g in ["553283"]:
        sd, err = try_call(c, "IchibaItem/Search", genreId=g, keyword="米", hits=30, sort="-reviewCount")
        if sd:
            its2 = items_of(sd)
            print(f"\n[3] ジャンル{g}×「米」口コミ数順: 全{sd.get('count')}件 / ページ数{sd.get('pageCount')}")
            for i in its2[:5]:
                print(f"    ¥{i.get('itemPrice')} 口コミ{i.get('reviewCount')}件★{i.get('reviewAverage')} 在庫{i.get('availability')} {str(i.get('itemName'))[:60]}")
            kg = [i for i in its2 if re.search(r"(\d+(?:\.\d+)?)\s*(kg|ｋｇ|キロ)", str(i.get("itemName")), re.I)]
            print(f"    商品名から重さ(kg)が読み取れる件数: {len(kg)}/{len(its2)}")
        else:
            print(f"\n[3] ジャンル{g}: {err}")
    # ランキング
    rd, err = try_call(c, "IchibaItem/Ranking", genreId="553283")
    if rd:
        r = items_of(rd)
        print(f"\n[4] ふるさと納税ランキング: {len(r)}件 例: {[(i.get('rank'), str(i.get('itemName'))[:30], i.get('itemPrice')) for i in r[:5]]}")
    else:
        print(f"\n[4] ランキング: {err}")
    print(f"\nAPI呼び出し回数: {c.requests}")


main()
