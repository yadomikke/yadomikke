"""エリアごとの空き数と料金の集計(条件を問わない、宿全体の空き)。

update.py(毎日の記録 = 月別レポートの材料)と build.py(エリアページ・相場ページ)の両方から使います。
料金は「その日に空きがある宿の、1泊・大人2名・1部屋の最安値」です。
"""
import statistics
from datetime import timedelta

PRICE_DAYS = 30     # 相場は、今日から何日先までの宿泊日で計算するか
PRICE_MIN_N = 20    # 料金の件数がこれ未満なら、相場は出さない
GROUPS = [("wd", "平日(日〜木)"), ("fri", "金曜"), ("sat", "土曜・祝前日")]


def day_group(d, holidays):
    """宿泊日の区分。祝日の前日は、土曜と同じ「休前日」として扱う"""
    if d.weekday() == 5 or (d + timedelta(days=1)).isoformat() in holidays:
        return "sat"
    return "fri" if d.weekday() == 4 else "wd"


def area_nos(hotels, area):
    return list(dict.fromkeys(h["no"] for h in hotels.get("areas", {}).get(area, [])))


def count(hotels, state, area, ds):
    """日程 ds(複数日なら、どれか1日でも空きがあれば空き)で (確認できた宿の数, 空きのある宿の数)"""
    c = v = 0
    for no in area_nos(hotels, area):
        cs = state.get("cells", {}).get(no, {})
        cells = [cs.get(d.isoformat()) for d in ds if cs.get(d.isoformat())]
        if not cells:
            continue
        c += 1
        v += any(x.get("s") == "v" for x in cells)
    return c, v


def prices(hotels, state, area, today, holidays, days=PRICE_DAYS):
    """{区分: [料金...]}(宿×宿泊日ごとの最安値)"""
    out = {g: [] for g, _ in GROUPS}
    nos = area_nos(hotels, area)
    for off in range(days):
        d = today + timedelta(days=off)
        g = day_group(d, holidays)
        for no in nos:
            c = state.get("cells", {}).get(no, {}).get(d.isoformat())
            if c and c.get("s") == "v" and c.get("any") and c["any"][0]:
                out[g].append(int(c["any"][0]))
    return out


def summary(ps):
    """料金の一覧から {中央値, 下位25%, 上位25%, 件数}。件数が少なければ None"""
    if len(ps) < PRICE_MIN_N:
        return None
    q = statistics.quantiles(sorted(ps), n=4)
    return {"med": int(statistics.median(ps)), "p25": int(q[0]), "p75": int(q[2]), "n": len(ps)}


def next_saturday(today):
    return today + timedelta(days=(5 - today.weekday()) % 7)


def snapshot(cfg, hotels, state, today):
    """その日の観測のまとめ(エリアごと)。月別レポートの材料として stats に貯める"""
    holidays = set(cfg.get("holidays", []))
    out = {}
    for area in cfg["areas"]:
        rec = {"t": list(count(hotels, state, area, [today])),
               "a": list(count(hotels, state, area, [today + timedelta(days=1)])),
               "s": list(count(hotels, state, area, [next_saturday(today)]))}
        for g, ps in prices(hotels, state, area, today, holidays).items():
            sm = summary(ps)
            if sm:
                rec["p_" + g] = sm["med"]
        out[area] = rec
    return out
