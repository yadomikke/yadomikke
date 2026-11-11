"""取得 → 保存 → ページ生成を、ダミーのAPIで通しで確認する。
実行: python3 -I tests/test_e2e.py   (ネットワーク不要・本番データには触れません)
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import rakuten as rk  # noqa: E402
import update as up  # noqa: E402


def room(name, total, url):
    return [{"roomBasicInfo": {"roomClass": "和室", "roomName": name, "reserveUrl": url}},
            {"dailyCharge": {"total": total}}]


class FakeClient:
    def __init__(self, *a, **k):
        self.requests = 0

    def call(self, api, **p):
        self.requests += 1
        if api == "GetAreaClass":
            return {"areaClasses": [{"largeClass": [{"largeClassCode": "japan"}, {"middleClasses": [
                {"middleClass": [{"middleClassCode": "kanagawa"}, {"smallClasses": [
                    {"smallClass": [{"smallClassCode": "hakone", "smallClassName": "箱根"}, {"detailClasses": [{"detailClass": {"detailClassCode": "A"}}]}]}]}]},
                {"middleClass": [{"middleClassCode": "shizuoka"}, {"smallClasses": [
                    {"smallClass": [{"smallClassCode": "atami2", "smallClassName": "熱海"}, {"detailClasses": [{"detailClass": {"detailClassCode": "B"}}]}]}]}]}]}]}]}
        if api == "SimpleHotelSearch":
            if p["smallClassCode"] not in ("hakone", "atami2"):
                raise rk.NotFound(404, "not_found", "not found")
            base = 0 if p["smallClassCode"] == "hakone" else 10
            hs = [[{"hotelBasicInfo": {"hotelNo": n, "hotelName": f"宿{n}", "hotelInformationUrl": f"https://aff/h{n}",
                                       "reviewAverage": 4.2, "reviewCount": 100 + n}}] for n in (base + 1, base + 2, base + 3)]
            return {"pagingInfo": {"pageCount": 1}, "hotels": hs}
        # 空室検索: 宿1=露天あり空き / 宿2=露天なし空き / 宿3=満室
        wanted = [int(x) for x in str(p["hotelNo"]).split(",")]
        out = []
        for n in wanted:
            if n % 10 == 1:
                rooms = [{"roomInfo": room("露天風呂付き和室", 50000 + n, "https://aff/r1")}]
            elif n % 10 == 2:
                rooms = [{"roomInfo": room("和室10畳", 20000, "https://aff/r2")}]
            else:
                continue
            out.append([{"hotelBasicInfo": {"hotelNo": n, "hotelName": f"宿{n}", "planListUrl": f"https://aff/p{n}"}}] + rooms)
        if not out:
            raise rk.NotFound(404, "not_found", "not found")
        return {"pagingInfo": {"pageCount": 1}, "hotels": out}


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["search"]["days_ahead"] = 5
    for a in cfg["areas"].values():
        a.pop("days_ahead", None)
    # 試験は2エリア。熱海はコードをわざと間違えて、名前から探し直せることも確かめる
    cfg["areas"] = {"hakone": cfg["areas"]["hakone"], "atami": dict(cfg["areas"]["atami"], small="wrong")}
    cfg["pages"] = [p for p in cfg["pages"] if p["slug"] in ("hakone-roten", "atami-roten", "hakone-pet")]
    cfg["search"]["probe_checks"] = 2
    cfg["site"]["url"] = "https://example.invalid"
    (tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    up.rk.Client = FakeClient
    os.environ.update(RAKUTEN_APP_ID="x", RAKUTEN_ACCESS_KEY="y", RAKUTEN_AFFILIATE_ID="z")
    sys.argv = ["update.py", "--data-dir", str(tmp / "data"), "--config", str(tmp / "config.json"),
                "--budget-minutes", "5"]
    up.main()

    state = json.loads((tmp / "data" / "state.json").read_text(encoding="utf-8"))
    hotels = json.loads((tmp / "data" / "hotels.json").read_text(encoding="utf-8"))
    check(len(hotels["areas"]["hakone"]) == 3, "宿一覧を3軒保存できる")
    check(len(hotels["areas"]["atami"]) == 3, "エリアのコードが間違っていても、名前(熱海)から探し直して宿を取得できる")
    import scenes
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=9))).date()
    want = {(today + timedelta(days=i)).isoformat() for i in range(5)} | {d.isoformat() for d in scenes.extra_dates(cfg, today)}
    check(set(state["fetched_by"]["hakone"]) == want and set(state["fetched_by"]["atami"]) == want,
          "どのエリアも、通常の5日と、日付別ページ用の先の日付が取得済みになる")
    stats = json.loads((tmp / "data" / "stats.json").read_text(encoding="utf-8"))
    check(stats["obs"] > 0 and stats["since"] == today.isoformat() and stats["price"]["1"]["roten"]["min"][0] == 50001 and "atami/roten" in stats["trans"] and "hakone/roten" in stats["trans"],
          "履歴(観測数・開始日・最安値)が記録される")
    check(state["meta"]["1"]["seen"].get("roten") is True, "露天の部屋が見つかった宿は「露天あり」と記録")
    check(not state["meta"]["2"]["seen"], "露天の無い宿は「露天あり」にならない")

    # 2回目: 判定の回数(probe_checks=2)を超えた「露天なし」の宿は、以後の問い合わせ対象から外れる
    sys.argv += []
    up.main()  # 2回目(まだ新鮮なので「更新不要」で終わる)
    state2 = json.loads((tmp / "data" / "state.json").read_text(encoding="utf-8"))
    check(state2["meta"]["2"]["checks"] >= 2, "露天なしの宿は判定回数が溜まる(外れる条件を満たす)")

    r = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "build.py"),
                        "--data-dir", str(tmp / "data"), "--out-dir", str(tmp / "out"),
                        "--config", str(tmp / "config.json")], capture_output=True, text=True)
    check(r.returncode == 0, "ページ生成が成功する: " + r.stdout.strip().replace("\n", " "))
    page = (tmp / "out" / "hakone-roten" / "index.html").read_text(encoding="utf-8")
    check("宿1" in page, "露天ありの宿は表に載る")
    check("宿11" not in page.split("<tbody>")[1], "別エリア(熱海)の宿は箱根のページに載らない")
    atami = (tmp / "out" / "atami-roten" / "index.html").read_text(encoding="utf-8")
    check("宿11" in atami and "宿1<" not in atami.split("<tbody>")[1], "熱海のページには熱海の宿だけが載る")
    check(not (tmp / "out" / "hakone-pet").exists(), "該当する宿が1軒もない組み合わせ(箱根×ペット可)のページは作られない")
    home = (tmp / "out" / "index.html").read_text(encoding="utf-8")
    check("hakone-roten/" in home and "atami-roten/" in home and "hakone-pet/" not in home, "トップに、あるページだけが並ぶ")
    check("宿2" not in page.split("<tbody>")[1], "露天なしの宿は表に載らない")
    check("宿3" not in page.split("<tbody>")[1], "露天の部屋が一度も見つからない宿は表に載らない")
    import urllib.parse
    plan = urllib.parse.quote("https://hotel.travel.rakuten.co.jp/hotelinfo/plan/1?", safe="")
    check(f"https://hb.afl.rakuten.co.jp/hgc/z/?pc={plan}" in page and "https://aff/r1" not in page,
          "予約リンクは、ログインなしで開ける日付つきのプラン一覧(アフィリエイト経由)になる")
    check('rel="sponsored nofollow noopener"' in page, "広告リンクの印(sponsored)が付く")
    check("【PR】" in page and "Supported by Rakuten Developers" in page, "広告表記(【PR】)とクレジットが入る")
    out = tmp / "out"
    for rel in ("about", "privacy", "hakone-roten/shumatsu", "hakone-roten/nenmatsu", "hakone-roten/xmas", "hakone-roten/hotel/1", "atami-roten/hotel/11"):
        check((out / rel / "index.html").exists(), f"{rel}/ のページが作られる")
    check(not (out / "hakone-roten" / "hotel" / "2").exists(), "露天なしの宿のページは作られない")
    check("いまの空室まとめ" in page and "蓄積中" in page, "冒頭の要約と、履歴の「蓄積中」表示がある(数字は出さない)")
    hub = (out / "roten" / "shumatsu" / "index.html").read_text(encoding="utf-8")
    check("全エリア" in hub and "hakone-roten/shumatsu/" in hub and "atami-roten/shumatsu/" in hub, "全エリアの日程別まとめ(roten/shumatsu/)に、各エリアへのリンクがある")
    check("roten/shumatsu/" in (out / "index.html").read_text(encoding="utf-8"), "トップから全エリアの日程別まとめへ行ける")
    key = cfg["site"]["indexnow_key"]
    check((out / f"{key}.txt").read_text(encoding="utf-8") == key, "IndexNow用の鍵ファイルが作られる")
    sm = (out / "sitemap.xml").read_text(encoding="utf-8")
    check("/hakone-roten/hotel/1/" in sm and "/about/" in sm and "/hakone-roten/xmas/" in sm, "sitemapに全ページが入る")
    rb = (out / "robots.txt").read_text(encoding="utf-8")
    check("OAI-SearchBot" in rb and "Disallow" not in rb, "robots.txtは検索・AIの巡回を許可")
    check("ヤドミッケ" in (out / "index.html").read_text(encoding="utf-8") and "Organization" in (out / "index.html").read_text(encoding="utf-8"), "トップにサイト名とOrganization")
    check("og:image" in page and "social-plugins.line.me" in page and "twitter.com/intent/tweet" in page, "共有画像(og:image)とLINE・Xの共有リンクがある")
    check("id=\"favtog\"" in page, "お気に入りの切り替えボタンがある")
    check((tmp / "out" / "sitemap.xml").exists() and (tmp / "out" / "robots.txt").exists(), "sitemap.xml と robots.txt が作られる")
    # ページを軽くする工夫(表のマスに長い予約リンクを載せない)
    table = page.split("<table>")[1].split("</table>")[0]
    check("hb.afl.rakuten" not in table and 'data-h="1"' in table, "カレンダー表のマスには予約リンクを載せず、宿番号だけを持たせる")
    import re as _re
    vdata = json.loads(_re.search(r"window.__V=(.*?)</script>", page, _re.S).group(1).replace("<\\/", "</"))
    c1 = next(v for v in vdata["hotels"][0]["c"].values() if v[0] == "o")
    check(c1[2] == "" and vdata["lt"] == {"a": "z", "ad": 2, "n": 1}, "埋め込みデータも、組み立てられるリンクは省き、材料(アフィリエイトID・人数・泊数)だけ持つ")
    # 引用されやすい冒頭の一文
    check(f"{today.year}年{today.month}月{today.day}日" in page and "(楽天トラベルの空室データ)で、箱根の客室露天風呂が確認できた宿1軒のうち" in page,
          "冒頭の一文に、年つきの日時・出どころ・母数(宿の数)が入る")
    # 空室データ統計・CSV・AI向け案内
    stp = (out / "stats" / "index.html").read_text(encoding="utf-8")
    check("温泉地の空室データ統計" in stp and '"Dataset"' in stp and "蓄積中" in stp, "空室データ統計ページ(Dataset)ができ、履歴が足りない数字は出さない")
    csv = (out / "stats" / "latest.csv").read_text(encoding="utf-8").splitlines()
    check(csv[0].startswith("area,area_label,condition") and len(csv) >= 1, "統計のCSVが作られる")
    llms = (out / "llms.txt").read_text(encoding="utf-8")
    check(llms.startswith("# ヤドミッケ") and "/hakone-roten/" in llms and "/stats/" in llms, "AI向けの案内(llms.txt)に、主なページと統計が載る")
    check("/stats/" in sm, "sitemapに統計ページが入る")

# 履歴の数え方(前回◯でなかった→◯ / ◯だった→消えた)
sys.path.insert(0, str(ROOT / "scripts"))
from datetime import date as _d, datetime as _dt
st = up.new_stats(_d(2026, 11, 6))
cfgx = {"features": {"roten": {}}}
okc = {"s": "v", "f": {"roten": [40000, "u", "n"]}}
fullc = {"s": "f"}
up.record_stats(st, cfgx, _d(2026, 11, 10), _d(2026, 11, 6), {"a": fullc, "b": okc, "c": None, "d": fullc},
                {"a": okc, "b": fullc, "c": okc, "d": fullc}, {"roten": {"a", "b", "c", "d"}}, 6, _d(2026, 11, 6), "hk")
check(st["trans"]["hk/roten"]["0"] == [2, 1, 1, 1], "履歴: 空き→◯1件/満室のまま1件/◯→消えた1件(初回観測は比較に含めない)")
up.record_stats(st, cfgx, _d(2026, 11, 10), _d(2026, 11, 6), {"a": fullc}, {"a": okc}, {"roten": {"a"}}, 200, _d(2026, 11, 7), "hk")
check(st["trans"]["hk/roten"]["0"] == [2, 1, 1, 1], "履歴: 前回の取得から間が空きすぎた比較は数えない")
check(scenes.all_scenes(cfg, _d(2026, 11, 6))[0]["dates"] == [_d(2026, 11, 6), _d(2026, 11, 7)], "今週末は金・土")
check(scenes.all_scenes(cfg, _d(2026, 11, 6))[2]["dates"] == [_d(2026, 11, 20), _d(2026, 11, 21), _d(2026, 11, 22)], "次の連休は11/21〜23の前夜から")


import indexnow
b = "https://x.example"
urls = [b + "/", b + "/roten/shumatsu/", b + "/hakone-roten/hotel/1/", b + "/hakone-roten/"]
check(indexnow.pick_urls(urls, {}, "d1", b) == urls, "IndexNow: 初回はすべて通知する")
sent = {u: "d1" for u in urls}
check(indexnow.pick_urls(urls, sent, "d1", b) == [], "IndexNow: 同じ日に、通知済みのURLは再通知しない")
check(indexnow.pick_urls(urls + [b + "/new/"], sent, "d2", b) == [b + "/", b + "/roten/shumatsu/", b + "/hakone-roten/", b + "/new/"][:0] + [b + "/new/", b + "/", b + "/roten/shumatsu/", b + "/hakone-roten/"],
      "IndexNow: 翌日は、新しいURLと浅いページだけ通知する(宿ページは再通知しない)")

print("\nすべて成功")
