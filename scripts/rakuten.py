"""楽天トラベルAPIの小さなクライアントと、レスポンスの読み取り部品。

標準ライブラリだけで動きます(pip install は不要)。
2026年2月の楽天ウェブサービス移行後の仕様に合わせています:
  - エンドポイントは openapi.rakuten.co.jp
  - applicationId と accessKey の両方が必須
  - affiliateId を付けると、返ってくる各URLがアフィリエイトURLになる
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://openapi.rakuten.co.jp/engine/api/Travel"
DEFAULT_VERSIONS = {
    "VacantHotelSearch": "20170426",
    "SimpleHotelSearch": "20260731",
    "HotelDetailSearch": "20260731",
    "GetAreaClass": "20140210",
}


class ApiError(Exception):
    def __init__(self, status, error, description):
        super().__init__(f"HTTP {status}: {error} - {description}")
        self.status = status
        self.error = error
        self.description = description


class NotFound(ApiError):
    """該当データなし(空室検索では「どの宿も空きなし」の意味)"""


class AuthError(ApiError):
    """IDやアクセスキーの誤り。続行しても無駄なので実行全体を止める"""


class Client:
    def __init__(self, app_id, access_key, affiliate_id="", referer="",
                 base=DEFAULT_BASE, versions=None, interval=1.1, log=print):
        self.app_id = app_id
        self.access_key = access_key
        self.affiliate_id = affiliate_id
        self.referer = referer
        self.base = base.rstrip("/")
        self.versions = dict(DEFAULT_VERSIONS)
        if versions:
            self.versions.update(versions)
        self.interval = interval  # 楽天は同一IDへの連続アクセスに制限があるため間隔をあける
        self.log = log
        self._last = 0.0
        self.requests = 0

    def call(self, api, **params):
        query = {"applicationId": self.app_id, "format": "json", "formatVersion": 2}
        if self.affiliate_id:
            query["affiliateId"] = self.affiliate_id
        query.update({k: v for k, v in params.items() if v is not None})
        url = f"{self.base}/{api}/{self.versions[api]}?" + urllib.parse.urlencode(query)
        headers = {"accessKey": self.access_key, "User-Agent": "vacancy-site/1.0"}
        if self.referer:
            # 「許可されたウェブサイト」の確認には、RefererまたはOriginが使われる。両方を送っておく
            headers["Referer"] = self.referer.rstrip("/") + "/"
            headers["Origin"] = self.referer.rstrip("/")

        last_err = None
        for attempt in range(1, 5):
            wait = self.interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err, desc = _read_error(e)
                if e.code == 404:
                    raise NotFound(404, err, desc)
                if e.code in (400, 401, 403):
                    raise AuthError(e.code, err, desc)
                last_err = ApiError(e.code, err, desc)
                if e.code == 429:
                    time.sleep(30 * attempt)
                else:
                    time.sleep(5 * attempt)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = ApiError(0, "network", str(e))
                time.sleep(5 * attempt)
            except ValueError as e:
                # 楽天側の一時的な不調で、空や壊れた返事が来ることがある。時間をおいて問い合わせ直す
                last_err = ApiError(0, "bad_response", str(e)[:200])
                time.sleep(5 * attempt)
        raise last_err


def _read_error(e):
    try:
        body = json.loads(e.read().decode("utf-8"))
        return body.get("error", "unknown"), body.get("error_description", "")
    except Exception:
        return "unknown", str(e)


# ---- レスポンスの読み取り ----------------------------------------------
# formatVersion の違いや入れ子の深さに左右されにくいよう、
# 「キー名で探す」方式にしています。

def find_first(node, key):
    """入れ子の中から、最初に見つかった key の値を返す"""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            r = find_first(v, key)
            if r is not None:
                return r
    elif isinstance(node, list):
        for x in node:
            r = find_first(x, key)
            if r is not None:
                return r
    return None


def find_all_dicts(node, key):
    """入れ子の中から、key に入っている辞書をすべて集める"""
    out = []

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == key and isinstance(v, dict):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(node)
    return out


def extract_rooms(node):
    """部屋(roomBasicInfo)と、その直後の料金(dailyCharge)を対にして返す"""
    rooms = []
    cur = [None]

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "roomBasicInfo" and isinstance(v, dict):
                    cur[0] = {"basic": v, "charge": None}
                    rooms.append(cur[0])
                elif k == "dailyCharge" and isinstance(v, dict):
                    if cur[0] is not None and cur[0]["charge"] is None:
                        cur[0]["charge"] = v
                else:
                    walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(node)
    return rooms


def split_hotels(data):
    """レスポンスを {施設番号: {"basic": 施設情報, "rooms": [部屋...]}} に整理する"""
    out = {}
    hotels = data.get("hotels") if isinstance(data, dict) else None
    for item in hotels or []:
        basics = find_all_dicts(item, "hotelBasicInfo")
        basic = basics[0] if basics else {}
        no = basic.get("hotelNo")
        if no is None:
            no = find_first(item, "hotelNo")
        if no is None:
            continue
        no = str(no)
        entry = out.setdefault(no, {"basic": {}, "rooms": []})
        if basic and not entry["basic"]:
            entry["basic"] = basic
        entry["rooms"].extend(extract_rooms(item))
    return out


DETAIL_KEYS = ("hotelBasicInfo", "hotelRatingInfo", "hotelDetailInfo", "hotelFacilitiesInfo")


def parse_detail(data):
    """施設詳細APIの結果から、{キー名: 辞書} を取り出す(見つからないキーは入らない)"""
    out = {}
    for k in DETAIL_KEYS:
        ds = find_all_dicts(data, k)
        if ds:
            out[k] = ds[0]
    return out


def page_count(data):
    n = to_int(find_first(data, "pageCount"))
    return n or 1


def to_int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def detail_codes_from_tree(tree, small):
    """地区コードAPIの結果から、小区分 small の下にある細区分コードを集める"""
    found = []

    def collect(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "detailClassCode":
                    found.append(str(v))
                else:
                    collect(v)
        elif isinstance(n, list):
            for x in n:
                collect(x)

    def walk(n):
        if isinstance(n, list):
            idx = next((i for i, x in enumerate(n)
                        if isinstance(x, dict) and x.get("smallClassCode") == small), None)
            if idx is not None:
                collect(n[idx])
                j = idx + 1
                while j < len(n) and not (isinstance(n[j], dict) and "smallClassCode" in n[j]):
                    collect(n[j])
                    j += 1
                return
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            if n.get("smallClassCode") == small:
                collect(n)
                return
            for v in n.values():
                walk(v)

    walk(tree)
    return sorted(set(found))


def find_small_by_name(tree, name):
    """地区コードAPIの結果から、小区分の名前(例: 熱海)に合うものを探し、(大, 中, 小)のコードを返す。見つからなければ None"""
    hits = []

    def walk(n, ctx):
        if isinstance(n, list):
            c = dict(ctx)
            for x in n:
                walk(x, c)
        elif isinstance(n, dict):
            for k in ("largeClassCode", "middleClassCode"):
                if k in n:
                    ctx[k] = n[k]
            if "smallClassCode" in n and name in str(n.get("smallClassName", "")):
                hits.append((ctx.get("largeClassCode"), ctx.get("middleClassCode"), n["smallClassCode"],
                             str(n.get("smallClassName", ""))))
            for v in n.values():
                if isinstance(v, (list, dict)):
                    walk(v, dict(ctx))

    walk(tree, {})
    hits = [h for h in hits if h[0] and h[1]]
    if not hits:
        return None
    hits.sort(key=lambda h: len(h[3]))  # 名前がいちばん近い(短い)ものを選ぶ
    return hits[0][:3]
