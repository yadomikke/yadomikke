"""楽天APIの応答を模したダミーデータで、読み取りと判定ロジックを確認する。
実行: python3 -I tests/test_parse.py   (ネットワーク不要)
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import rakuten as rk  # noqa: E402
import update as up  # noqa: E402


def room(name, cls, total, url):
    return [{"roomBasicInfo": {"roomClass": cls, "roomName": name, "reserveUrl": url, "planName": "プラン"}},
            {"dailyCharge": {"stayDate": "2026-12-05", "total": total, "rakutenCharge": total // 2}}]


# 旧形式(formatVersion=1)風 / 新形式(formatVersion=2)風の2種類
V1 = {"pagingInfo": {"pageCount": 1}, "hotels": [
    {"hotel": [{"hotelBasicInfo": {"hotelNo": 111, "hotelName": "旅館A", "hotelMinCharge": 20000,
                                   "planListUrl": "https://a/plan"}},
               {"roomInfo": room("和室10畳", "和室", 30000, "https://a/r1")}
               ]},
    {"hotel": [{"hotelBasicInfo": {"hotelNo": 222, "hotelName": "旅館B"}},
               {"roomInfo": room("露天風呂付き和洋室", "和洋室", 88000, "https://b/r1")},
               {"roomInfo": room("露天風呂付き特別室", "特別室", 66000, "https://b/r2")},
               ]},
]}
V2 = {"pagingInfo": {"pageCount": 1}, "hotels": [
    [{"hotelBasicInfo": {"hotelNo": 111, "hotelName": "旅館A"}},
     {"roomInfo": [{"roomBasicInfo": {"roomClass": "和室", "roomName": "和室10畳", "reserveUrl": "https://a/r1"},
                    "dailyCharge": {"total": 30000}}]}],
    [{"hotelBasicInfo": {"hotelNo": 222, "hotelName": "旅館B"}},
     {"roomInfo": [{"roomBasicInfo": {"roomClass": "和洋室", "roomName": "露天風呂付き和洋室", "reserveUrl": "https://b/r1"},
                    "dailyCharge": {"total": 88000}}]}],
]}

AREA_V1 = {"areaClasses": {"largeClasses": [{"largeClass": [
    {"largeClassCode": "japan"},
    {"middleClasses": [{"middleClass": [
        {"middleClassCode": "kanagawa"},
        {"smallClasses": [
            {"smallClass": [{"smallClassCode": "hakone"},
                            {"detailClasses": [{"detailClass": {"detailClassCode": "A"}},
                                               {"detailClass": {"detailClassCode": "B"}}]}]},
            {"smallClass": [{"smallClassCode": "odawara"},
                            {"detailClasses": [{"detailClass": {"detailClassCode": "X"}}]}]},
        ]}]}]}]}]}}
AREA_V2 = {"areaClasses": [{"largeClassCode": "japan", "middleClasses": [
    {"middleClassCode": "kanagawa", "smallClasses": [
        {"smallClassCode": "odawara", "detailClasses": [{"detailClassCode": "X"}]},
        {"smallClassCode": "hakone", "detailClasses": [{"detailClassCode": "A"}, {"detailClassCode": "B"}]},
        {"smallClassCode": "yugawara", "detailClasses": [{"detailClassCode": "Y"}]}]}]}]}


class Fake:
    """Clientの代わり。部屋単位の検索(searchPattern=1)にはV1を返す"""
    def __init__(self, data_a, data_b):
        self.a, self.b, self.requests = data_a, data_b, 0

    def call(self, api, **p):
        self.requests += 1
        if p.get("searchPattern") == 1:
            no = str(p["hotelNo"])
            hs = [h for h in self.b["hotels"] if no in str(h)]
            return {"pagingInfo": {"pageCount": 1}, "hotels": hs}
        return self.a


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


for label, data in (("旧形式", V1), ("新形式", V2)):
    r = rk.split_hotels(data)
    check(set(r) == {"111", "222"}, f"{label}: 宿を2軒読み取れる")
    check(len(r["222"]["rooms"]) >= 1 and r["222"]["rooms"][0]["charge"]["total"] in (88000, 66000),
          f"{label}: 部屋と料金が対になる")

check(rk.detail_codes_from_tree(AREA_V1, "hakone") == ["A", "B"], "地区コード(旧形式): 箱根の細区分だけ取れる")
check(rk.detail_codes_from_tree(AREA_V2, "hakone") == ["A", "B"], "地区コード(新形式): 他の地区が混ざらない")

cfg = up.load_json(ROOT / "config.json", None)
fake = Fake(V1, V1)
cells, notes = up.fetch_date(fake, cfg, ["111", "222", "333"], date(2026, 12, 5))
check(cells["333"] == {"s": "f"}, "空きの無い宿は「満」になる")
check(cells["111"]["s"] == "v" and "f" not in cells["111"], "露天なしの空き宿は「△」(特徴なし)")
check(cells["222"]["f"]["roten"][0] == 66000, "露天の部屋が複数あれば最安を採用")
check(cells["222"]["f"]["roten"][1] == "https://b/r2", "最安の部屋の予約URLを使う")


# 楽天から空・壊れた返事が来ても止まらず、問い合わせ直す
class FakeResp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_client(bodies):
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append(1)
        return FakeResp(bodies[len(calls) - 1])

    orig_open, orig_sleep = rk.urllib.request.urlopen, rk.time.sleep
    rk.urllib.request.urlopen, rk.time.sleep = fake_urlopen, lambda s: None
    try:
        c = rk.Client("app", "key", interval=0)
        try:
            return c.call("VacantHotelSearch", hotelNo=1), len(calls)
        except rk.ApiError as e:
            return e, len(calls)
    finally:
        rk.urllib.request.urlopen, rk.time.sleep = orig_open, orig_sleep


got, n = run_client([b"", b"\xff", b'{"hotels": []}'])
check(got == {"hotels": []} and n == 3, "空や壊れた返事のあとは、問い合わせ直して正しい返事を使う")
got, n = run_client([b""] * 4)
check(isinstance(got, rk.ApiError) and n == 4, "空の返事が続くときは、4回で諦めて楽天側のエラーとして扱う")
print("\nすべて成功")
