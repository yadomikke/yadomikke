"""「今週末」「来週末」「次の連休」「年末年始」「クリスマス」の日付を計算する。

update.py(取得する日付を増やす)と build.py(日付別ページを作る)の両方から使います。
ここで言う日付は、すべて「チェックイン日」です。
"""
from datetime import date, timedelta

WD = "月火水木金土日"


def md(d):
    return f"{d.month}/{d.day}"


def dlabel(d, holidays=()):
    mark = "祝" if d.isoformat() in holidays else ""
    return f"{md(d)}({WD[d.weekday()]}{mark})"


def holiday_blocks(days, holidays):
    """連続する休み(土日祝)が3日以上のかたまりを (最初の日, 最後の日) で返す"""
    off = [(d.weekday() >= 5) or (d.isoformat() in holidays) for d in days]
    blocks, i = [], 0
    while i < len(days):
        if off[i]:
            j = i
            while j + 1 < len(days) and off[j + 1]:
                j += 1
            if j - i + 1 >= 3:
                blocks.append((days[i], days[j]))
            i = j + 1
        else:
            i += 1
    return blocks


def _span(ds, holidays):
    if not ds:
        return ""
    if len(ds) == 1:
        return dlabel(ds[0], holidays)
    return f"{dlabel(ds[0], holidays)}〜{dlabel(ds[-1], holidays)}"


def _weekend(today, add_weeks):
    # 「次の土曜」を軸に、前日の金曜(まだ過ぎていなければ)も含める
    sat = today + timedelta(days=(5 - today.weekday()) % 7) + timedelta(days=7 * add_weeks)
    fri = sat - timedelta(days=1)
    return [d for d in (fri, sat) if d >= today]


def all_scenes(cfg, today):
    """日付別ページの定義を順番に返す。dates が空なら「今年の分は終了」のページになる。"""
    holidays = set(cfg.get("holidays", []))
    horizon = int(cfg.get("search", {}).get("scene_days_ahead", 150))
    scenes = []

    def add(slug, name, ds, note):
        scenes.append({"slug": slug, "name": name, "dates": ds, "span": _span(ds, holidays), "note": note})

    add("shumatsu", "今週末", _weekend(today, 0), "金曜泊と土曜泊(チェックイン日)の空室です。")
    add("raishu", "来週末", _weekend(today, 1), "金曜泊と土曜泊(チェックイン日)の空室です。")

    # 次の連休(土日祝で3日以上続くもの)。前日の夜から、最終日の前日まで
    days = [today + timedelta(days=i) for i in range(horizon + 8)]
    ds = []
    for a, b in holiday_blocks(days, holidays):
        if b > today:
            ds = [d for d in [a - timedelta(days=1)] + [a + timedelta(days=i) for i in range((b - a).days)] if d >= today]
            break
    add("renkyu", "次の連休", ds,
        "土日祝が3日以上続く、直近の連休の空室です(前日の夜から、最終日の前日泊まで)。祝日は設定ファイルの一覧にもとづきます。")

    y = today.year if not (today.month == 1 and today.day <= 3) else today.year - 1
    ds = [d for d in (date(y, 12, 28) + timedelta(days=i) for i in range(7)) if d >= today]
    add("nenmatsu", "年末年始", ds, "12/28〜1/3にチェックインする場合の空室です。")

    y = today.year if today <= date(today.year, 12, 25) else today.year + 1
    ds = [d for d in (date(y, 12, 23), date(y, 12, 24), date(y, 12, 25)) if d >= today]
    add("xmas", "クリスマス", ds, "12/23〜12/25にチェックインする場合の空室です。")
    return scenes


def area_days(cfg, area_key):
    """そのエリアで、何日先まで取得するか(エリアごとの指定があればそれを優先)"""
    a = cfg.get("areas", {}).get(area_key, {})
    return int(a.get("days_ahead") or cfg.get("search", {}).get("days_ahead", 31))


def extra_dates(cfg, today, area_key=None):
    """通常の取得範囲(days_ahead)の外にある、日付別ページ用の日付"""
    s = cfg.get("search", {})
    main_n = area_days(cfg, area_key) if area_key else int(s.get("days_ahead", 31))
    horizon = int(s.get("scene_days_ahead", 150))
    out = set()
    for sc in all_scenes(cfg, today):
        for d in sc["dates"]:
            off = (d - today).days
            if main_n <= off <= horizon:
                out.add(d)
    return sorted(out)
