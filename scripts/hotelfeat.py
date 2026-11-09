"""宿そのものの条件(大浴場・貸切風呂・かけ流し など)の定義と判定。

部屋名のキーワードで判定する「客室露天風呂」「ペット可」とは違い、
楽天トラベルの「施設詳細API(HotelDetailSearch)」で取れる宿の設備・口コミ点数から判定します。
update.py(詳細の取得)と build.py(ページ作り)の両方から使います。
"""
import re

MIN_REVIEWS = 20     # 口コミ点数で絞る条件は、この件数以上の宿だけ対象にする
HIGH = 4.5           # 「評価が高い」の基準

# key: 内部名(URLにも使う)。def: ページに載せる判定方法の説明
FEATURES = {
    "daiyoku": {"label": "大浴場のある宿", "short": "大浴場",
                "def": "楽天トラベルの施設情報(風呂・設備の欄)に「大浴場」の記載がある宿"},
    "kashikiri": {"label": "貸切風呂・家族風呂のある宿", "short": "貸切風呂",
                  "def": "楽天トラベルの施設情報(風呂・設備の欄)に「貸切」「家族風呂」の記載がある宿"},
    "kakenagashi": {"label": "源泉かけ流しの宿", "short": "源泉かけ流し",
                    "def": "楽天トラベルの施設情報(風呂・泉質の欄)に「かけ流し」「源泉100」等の記載がある宿"},
    "chiisana": {"label": "小さな宿(客室10室以下)", "short": "小さな宿",
                 "def": "楽天トラベルの施設情報で、客室数が10室以下と登録されている宿"},
    "osoitsuki": {"label": "夜遅くても泊まれる宿(最終チェックイン22時以降)", "short": "夜遅くチェックイン",
                  "def": "楽天トラベルの施設情報で、最終チェックイン時刻が22時以降と登録されている宿"},
    "ekichika": {"label": "駅から徒歩5分以内の宿", "short": "駅近",
                 "def": "楽天トラベルの施設情報(アクセス欄)に、駅から徒歩5分以内と書かれている宿"},
    "yushoku": {"label": "夕食の口コミ評価が高い宿", "short": "夕食が高評価",
                "def": f"夕食の口コミ平均が{HIGH}以上で、口コミが{MIN_REVIEWS}件以上ある宿"},
    "furohyoka": {"label": "お風呂の口コミ評価が高い宿", "short": "お風呂が高評価",
                  "def": f"お風呂の口コミ平均が{HIGH}以上で、口コミが{MIN_REVIEWS}件以上ある宿"},
    "seiketsu": {"label": "清潔さの口コミ評価が高い宿", "short": "清潔さが高評価",
                 "def": f"清潔感の口コミ平均が{HIGH}以上で、口コミが{MIN_REVIEWS}件以上ある宿"},
    "kids": {"label": "子連れ向けの宿", "short": "子連れ向け",
             "def": "楽天トラベルの施設情報に、キッズ・ベビー・お子様向けの設備やサービスの記載がある宿"},
}

AFTER = {
    "daiyoku": "大浴場", "kashikiri": "貸切|家族風呂|家族湯",
    "kakenagashi": "かけ流し|掛け流し|掛流し|源泉100|源泉１００",
    "kids": "キッズ|ベビー|お子様|お子さま|子供用|子ども用|ちびっこ|添い寝無料",
}


def flat(x):
    """文字列・リスト・辞書のどれでも、ひとつの文章にまとめる"""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return " ".join(flat(v) for v in x.values())
    if isinstance(x, (list, tuple)):
        return " ".join(flat(v) for v in x)
    return str(x)


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def to_int(x):
    f = to_float(x)
    return int(f) if f is not None else None


def clock_minutes(s):
    m = re.search(r"(\d{1,2})\s*[:時]\s*(\d{0,2})", str(s or ""))
    if not m:
        return None
    return int(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 0)


def compact(parts):
    """HotelDetailSearch の結果(キー名ごとの辞書)から、保存する小さな記録を作る"""
    bi = parts.get("hotelBasicInfo", {})
    rt = parts.get("hotelRatingInfo", {})
    dt = parts.get("hotelDetailInfo", {})
    fc = parts.get("hotelFacilitiesInfo", {})
    bath = " ".join(flat(fc.get(k)) for k in ("aboutBath", "bathType", "bathQuality"))
    fac = " ".join(flat(fc.get(k)) for k in ("hotelFacilities", "roomFacilities", "handicappedFacilities"))
    return {
        "rooms": to_int(fc.get("hotelRoomNum")),
        "last_in": str(dt.get("lastCheckinTime") or ""),
        "bt": re.sub(r"\s+", " ", bath).strip()[:700],
        "ft": re.sub(r"\s+", " ", fac).strip()[:900],
        "bath": to_float(rt.get("bathAverage")),
        "dinner": to_float(rt.get("dinnerAverage")),
        "clean": to_float(rt.get("cleanlinessAverage")),
        "stamp": "",
    }


def walk_minutes(h):
    text = f"{h.get('access', '')} {h.get('station', '')}"
    ms = [int(m) for m in re.findall(r"駅[^。、,\s]{0,14}?徒歩\s*(?:約)?\s*(\d{1,3})\s*分", text)]
    return min(ms) if ms else None


def matches(key, h, d):
    """宿 h(hotels.json の1件)と詳細 d(保存した記録)から、条件 key に当てはまるか"""
    if key == "ekichika":
        w = walk_minutes(h)
        return w is not None and w <= 5
    if not d:
        return False
    text = f"{d.get('bt', '')} {d.get('ft', '')}"
    if key in ("daiyoku", "kashikiri", "kakenagashi", "kids"):
        src = d.get("bt", "") if key in ("kashikiri", "kakenagashi") else text
        if key == "daiyoku":
            src = text
        if key == "kids":
            src = d.get("ft", "")
        return re.search(AFTER[key], src) is not None
    if key == "chiisana":
        return bool(d.get("rooms")) and 0 < d["rooms"] <= 10
    if key == "osoitsuki":
        m = clock_minutes(d.get("last_in"))
        return m is not None and m >= 22 * 60
    rv = h.get("reviews") or 0
    col = {"yushoku": "dinner", "furohyoka": "bath", "seiketsu": "clean"}.get(key)
    if col:
        v = d.get(col)
        return v is not None and v >= HIGH and rv >= MIN_REVIEWS
    return False


def matched_nos(hotels_by_area, detail):
    """{条件: {施設番号...}} をエリアをまたいで集める"""
    out = {k: set() for k in FEATURES}
    for lst in hotels_by_area.values():
        for h in lst if isinstance(lst, list) else []:
            for k in FEATURES:
                if matches(k, h, detail.get(h["no"])):
                    out[k].add(h["no"])
    return out
