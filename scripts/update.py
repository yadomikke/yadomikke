"""楽天トラベルAPIから空室データを集めて data/ に保存する。

使い方:
    python scripts/update.py                  # 通常実行(古い日付から順に更新)
    python scripts/update.py --budget-minutes 12
    python scripts/update.py --reprobe        # 「露天あり」の判定をやり直す

必要な環境変数(GitHubのSecretsに登録します):
    RAKUTEN_APP_ID        楽天ウェブサービスのアプリID
    RAKUTEN_ACCESS_KEY    同アクセスキー
    RAKUTEN_AFFILIATE_ID  楽天アフィリエイトID(これが無いと収益になりません)
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rakuten as rk  # noqa: E402
import scenes  # noqa: E402
import hotelfeat
import areastat  # noqa: E402

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent


def now():
    return datetime.now(JST)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def log(msg):
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


# ---- 宿の一覧(週1回だけ更新) ---------------------------------------

def _search_area(client, large, middle, small, details):
    hotels = {}
    for dc in details:
        page = 1
        while True:
            try:
                data = client.call(
                    "SimpleHotelSearch",
                    largeClassCode=large, middleClassCode=middle,
                    smallClassCode=small, detailClassCode=dc,
                    hits=30, page=page, responseType="small", datumType=1,
                )
            except rk.NotFound:
                break
            for no, e in rk.split_hotels(data).items():
                b = e["basic"]
                hotels[no] = {
                    "no": no,
                    "name": b.get("hotelName", ""),
                    "url": b.get("hotelInformationUrl", ""),
                    "plan_url": b.get("planListUrl", ""),
                    "review": b.get("reviewAverage"),
                    "reviews": rk.to_int(b.get("reviewCount")),
                    "address": (str(b.get("address1", "")) + str(b.get("address2", ""))),
                    "station": b.get("nearestStation", ""),
                    "image": b.get("hotelThumbnailUrl", ""),
                    "lat": b.get("latitude"),
                    "lng": b.get("longitude"),
                    "zip": b.get("postalCode", ""),
                    "access": b.get("access", ""),
                    "parking": b.get("parkingInformation", ""),
                }
            if page >= rk.page_count(data):
                break
            page += 1
    return hotels


def discover_hotels(client, cfg, previous=None):
    """施設検索APIでエリア内の宿を集める。エリアのコードが違っていたら、名前から探し直す"""
    result = {}
    tree = None
    for key, area in cfg["areas"].items():
        large, middle, small = area["large"], area["middle"], area["small"]
        for attempt in (1, 2):
            details = area.get("detail", "auto")
            if details == "auto" or attempt == 2:
                try:
                    if tree is None:
                        tree = client.call("GetAreaClass")
                    details = rk.detail_codes_from_tree(tree, small) or [None]
                except rk.NotFound:
                    details = [None]
            elif not isinstance(details, list):
                details = [details]
            hotels = _search_area(client, large, middle, small, details)
            if hotels or attempt == 2:
                break
            # 0軒だった: コードの間違いかもしれないので、名前(例: 熱海)から探し直す
            try:
                if tree is None:
                    tree = client.call("GetAreaClass")
                found = rk.find_small_by_name(tree, area.get("search_name") or area["label"])
            except rk.NotFound:
                found = None
            if not found:
                break
            large, middle, small = found
            log(f"{area['label']}: コードを {large}/{middle}/{small} に見直しました(configの値で0軒だったため)")
        log(f"{area['label']}: 宿 {len(hotels)} 軒を取得")
        if not hotels and previous and previous.get(key):
            log(f"{area['label']}: 取得が空だったため、前回の一覧を使います")
            hotels = {h["no"]: h for h in previous[key]}
        result[key] = list(hotels.values())
    return result


def ensure_hotels(client, cfg, data_dir, force=False):
    path = data_dir / "hotels.json"
    cur = load_json(path, {})
    if not force and cur.get("updated"):
        age = now() - datetime.fromisoformat(cur["updated"])
        if age < timedelta(days=cfg["refresh"]["hotels_days"]) and cur.get("areas"):
            missing = [k for k in cfg["areas"] if k not in cur["areas"]]
            if not missing:
                return cur
            # config.json に新しく足したエリアだけ、すぐに宿を集める(ほかのエリアは、いつもの周期で更新)
            log(f"新しいエリアの宿を集めます: {', '.join(cfg['areas'][k]['label'] for k in missing)}")
            sub = dict(cfg, areas={k: cfg["areas"][k] for k in missing})
            cur["areas"].update(discover_hotels(client, sub))
            save_json(path, cur)
            return cur
    found = discover_hotels(client, cfg, cur.get("areas"))
    if not any(found.values()):
        if cur.get("areas"):
            log("宿一覧の取得結果が空だったため、前回の一覧を使います")
            return cur
        raise SystemExit("宿の一覧が取得できませんでした。エリアコードとAPIの設定を確認してください。")
    cur = {"updated": now().isoformat(timespec="seconds"), "areas": found}
    save_json(path, cur)
    return cur


# ---- 空室の取得 -------------------------------------------------------

def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def room_total(room):
    return rk.to_int((room.get("charge") or {}).get("total"))


def matches(room, feat):
    text = " ".join(str(room["basic"].get(f, "")) for f in feat["fields"])
    return any(k in text for k in feat["keywords"])


def fetch_date(client, cfg, active, day, light=frozenset()):
    """1日ぶんの空室を取る。戻り値: {施設番号: セル}, {施設番号: 判定メモ}"""
    s = cfg["search"]
    base = dict(
        checkinDate=day.isoformat(),
        checkoutDate=(day + timedelta(days=s["nights"])).isoformat(),
        adultNum=s["adults"], roomNum=1,
    )

    # 手順A: まとめて問い合わせて「その日に空きのある宿」を絞る(15軒ずつ)
    vacant = {}
    for batch in chunks(active, 15):
        page = 1
        while True:
            try:
                data = client.call("VacantHotelSearch", hotelNo=",".join(batch),
                                   searchPattern=0, hits=30, page=page,
                                   responseType="small", **base)
            except rk.NotFound:
                break
            for no, e in rk.split_hotels(data).items():
                vacant[no] = e
            if page >= rk.page_count(data):
                break
            page += 1

    cells, notes = {}, {}
    for no in active:
        if no not in vacant:
            cells[no] = {"s": "f"}
            continue

        if no in light:
            # 宿そのものの条件だけで載せる宿は、部屋の細かい確認(手順B)を省く
            basic = vacant[no]["basic"]
            cell = {"s": "v"}
            if rk.to_int(basic.get("hotelMinCharge")):
                cell["any"] = [rk.to_int(basic["hotelMinCharge"]), basic.get("planListUrl", "")]
            cells[no] = cell
            continue

        # 手順B: 空きのある宿だけ、部屋・プラン単位で細かく見る
        rooms = []
        for page in range(1, s["max_pages"] + 1):
            try:
                data = client.call("VacantHotelSearch", hotelNo=no, searchPattern=1,
                                   hits=30, page=page, responseType="small", **base)
            except rk.NotFound:
                break
            rooms += rk.split_hotels(data).get(no, {"rooms": []})["rooms"]
            if page >= rk.page_count(data):
                break
        if not rooms:
            rooms = vacant[no]["rooms"]

        basic = vacant[no]["basic"]
        priced = [(room_total(r), r) for r in rooms if room_total(r)]
        cell = {"s": "v"}
        if priced:
            p, r = min(priced, key=lambda x: x[0])
            cell["any"] = [p, r["basic"].get("reserveUrl", "")]
        elif rk.to_int(basic.get("hotelMinCharge")):
            cell["any"] = [rk.to_int(basic["hotelMinCharge"]), basic.get("planListUrl", "")]

        feats = {}
        for fname, feat in cfg["features"].items():
            hit = [(room_total(r), r) for r in rooms if matches(r, feat) and room_total(r)]
            if hit:
                p, r = min(hit, key=lambda x: x[0])
                feats[fname] = [p, r["basic"].get("reserveUrl", ""), r["basic"].get("roomName", "")]
        if feats:
            cell["f"] = feats
        cells[no] = cell
        notes[no] = {"rooms_seen": len(rooms), "features": list(feats)}
    return cells, notes


# ---- 宿の詳細(大浴場・貸切風呂・客室数・口コミ点数など) ----------------------
DETAIL_REFRESH_DAYS = 30


def enrich_hotels(client, hotels, data_dir, deadline, cap):
    """施設詳細APIで、宿ごとの設備・客室数・口コミ点数を取り、data/hotel_detail.json に貯める。
    1回の実行で取りすぎないよう cap 軒までにし、まだ取っていない宿を優先する。"""
    path = data_dir / "hotel_detail.json"
    detail = load_json(path, {})
    nos = [h["no"] for lst in hotels["areas"].values() for h in lst]
    cutoff = (now() - timedelta(days=DETAIL_REFRESH_DAYS)).isoformat(timespec="seconds")
    todo = [no for no in nos if no not in detail]
    todo += sorted((no for no in nos if no in detail and detail[no].get("stamp", "") < cutoff),
                   key=lambda no: detail[no].get("stamp", ""))
    todo = todo[:cap]
    if not todo:
        return detail
    log(f"宿の詳細を取得します: {len(todo)}軒(未取得の残り {sum(1 for n in nos if n not in detail)}軒)")
    got = 0
    for no in todo:
        if time.time() > deadline:
            break
        try:
            data = client.call("HotelDetailSearch", hotelNo=no, responseType="large", datumType=1)
        except rk.NotFound:
            detail[no] = {"stamp": now().isoformat(timespec="seconds"), "none": True}
            continue
        parts = rk.parse_detail(data)
        d = hotelfeat.compact(parts)
        d["stamp"] = now().isoformat(timespec="seconds")
        detail[no] = d
        got += 1
        if got % 20 == 0:
            save_json(path, detail)
    save_json(path, detail)
    log(f"宿の詳細を {got}軒ぶん保存しました")
    return detail


# ---- 優先順位 ---------------------------------------------------------

def target_hours(cfg, offset):
    r = cfg["refresh"]
    if offset <= r["near_days"]:
        return r["near_hours"]
    if offset <= r["mid_days"]:
        return r["mid_hours"]
    return r["far_hours"]


def pick_units(cfg, state, today):
    """更新が必要な「エリア×日付」を、古い順(緊急度の高い順)に並べる"""
    plan = []
    for area in cfg["areas"]:
        cand = [today + timedelta(days=off) for off in range(scenes.area_days(cfg, area))]
        cand += scenes.extra_dates(cfg, today, area)  # 年末年始など、先の日程(日付別ページ用)も取得する
        fetched = state["fetched_by"].get(area, {})
        for d in cand:
            off = (d - today).days
            got = fetched.get(d.isoformat())
            if got is None:
                # まだ一度も取っていない日付(新しく足したエリアなど)。既存エリアの更新を先に済ませ、
                # 残った時間で近い日付から順に埋める(既存エリアの「今夜」「明日」の数字を古くしないため)
                score = 0.99 - off / 1000
            else:
                age = (now() - datetime.fromisoformat(got)).total_seconds() / 3600
                score = age / target_hours(cfg, off)
            if score >= 1 or got is None:
                plan.append((score, area, d))
    plan.sort(key=lambda x: -x[0])
    return [(a, d) for _, a, d in plan]


# ---- 履歴の記録 -------------------------------------------------------
# 過去にさかのぼって集めることはできないため、取得のたびに少しずつ貯めます。
#   trans[エリア/特徴][先の日数の区分] = [前回◯でなかった数, そのうち今回◯になった数, 前回◯だった数, そのうち今回◯でなくなった数]
#   daily[観測日][エリア/特徴][宿泊日] = [◯の軒数, 最安値]
#   price[施設番号][特徴] = {"min": [料金, 宿泊日, 観測日], "max": [...]}
MAX_GAP_HOURS = 72   # 前回の取得から間が空きすぎた比較は数えない
KEEP_DAYS = 400


def new_stats(today):
    return {"since": today.isoformat(), "runs": 0, "obs": 0, "trans": {}, "daily": {}, "price": {}}


def cell_ok(cell, feat):
    """None=比較できない / True=◯ / False=◯でない(満室・△)"""
    if not cell:
        return None
    if cell.get("s") == "f":
        return False
    return feat in cell.get("f", {})


def lead_bucket(lead):
    return str(min(max(lead, 0) // 7, 5))


def record_stats(stats, cfg, day, today, old, new, known, gap_hours, obs_day, area=""):
    """1日ぶんの取得結果を履歴に加える。
    old: {施設番号: 前回のセル} / new: {施設番号: 今回のセル} / known: {特徴: {その特徴を持つと判定済みの施設番号}}"""
    b = lead_bucket((day - today).days)
    od = obs_day.isoformat()
    comparable = gap_hours is not None and gap_hours <= MAX_GAP_HOURS
    for feat in cfg["features"]:
        sk = f"{area}/{feat}" if area else feat   # 履歴は「エリア/特徴」ごとに分けて貯める
        tr = stats["trans"].setdefault(sk, {}).setdefault(b, [0, 0, 0, 0])
        n_ok, low = 0, None
        for no, cell in new.items():
            if no not in known.get(feat, ()):
                continue
            now_ok = cell_ok(cell, feat)
            stats["obs"] += 1
            prev = cell_ok(old.get(no), feat)
            if comparable and prev is not None:
                if prev is False:
                    tr[0] += 1
                    tr[1] += 1 if now_ok else 0
                else:
                    tr[2] += 1
                    tr[3] += 0 if now_ok else 1
            if now_ok:
                n_ok += 1
                p = cell["f"][feat][0]
                low = p if low is None else min(low, p)
                pr = stats["price"].setdefault(no, {}).setdefault(feat, {})
                if "min" not in pr or p < pr["min"][0]:
                    pr["min"] = [p, day.isoformat(), od]
                if "max" not in pr or p > pr["max"][0]:
                    pr["max"] = [p, day.isoformat(), od]
        stats["daily"].setdefault(od, {}).setdefault(sk, {})[day.isoformat()] = [n_ok, low]


# ---- 空きの動き(きのうから空きが出た宿・空きが戻りやすい宿・何日前に埋まるか) ----
#   state["changes"]          = [{"no", "a"(エリア), "d"(宿泊日), "at"(気づいた日時), "p"(最安値)}...] 直近の「満→空き」
#   stats["hotel_moves"][施設] = [前回満室だった回数, そこから空きが出た回数, 前回空きだった回数, そこから満室になった回数]
#   stats["fill_lead"][エリア][何日前] = [前回空きだった数, そこから満室になった数, 前回からの経過時間の合計]
CHANGE_KEEP_HOURS = 72


def record_moves(state, stats, area, day, today, old, new, gap_hours, at):
    """前回の取得と比べて、満室⇔空きの入れ替わりを記録する(間が空きすぎた比較は数えない)"""
    if gap_hours is None or gap_hours > MAX_GAP_HOURS:
        return
    lead = str(max((day - today).days, 0))
    stats.setdefault("moves_since", today.isoformat())
    hm = stats.setdefault("hotel_moves", {})
    fl = stats.setdefault("fill_lead", {}).setdefault(area, {}).setdefault(lead, [0, 0, 0.0])
    ch = state.setdefault("changes", [])
    for no, cell in new.items():
        o = old.get(no)
        if not o or not cell:
            continue
        was_v, is_v = o.get("s") == "v", cell.get("s") == "v"
        m = hm.setdefault(no, [0, 0, 0, 0])
        if not was_v:
            m[0] += 1
            if is_v:
                m[1] += 1
                price = (cell.get("any") or [None])[0]
                ch.append({"no": no, "a": area, "d": day.isoformat(), "at": at, "p": price})
        else:
            m[2] += 1
            fl[0] += 1
            fl[2] = round(fl[2] + gap_hours, 2)
            if not is_v:
                m[3] += 1
                fl[1] += 1


def prune_changes(state, now_dt, today):
    cut = (now_dt - timedelta(hours=CHANGE_KEEP_HOURS)).isoformat(timespec="seconds")
    keep = today.isoformat()
    state["changes"] = [c for c in state.get("changes", []) if c["at"] >= cut and c["d"] >= keep]


def prune_stats(stats, today):
    cut = (today - timedelta(days=KEEP_DAYS)).isoformat()
    stats["daily"] = {k: v for k, v in stats["daily"].items() if k >= cut}
    if "area_daily" in stats:
        stats["area_daily"] = {k: v for k, v in stats["area_daily"].items() if k >= cut}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-minutes", type=float, default=25)
    ap.add_argument("--reprobe", action="store_true")
    ap.add_argument("--refresh-hotels", action="store_true")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args()

    cfg = load_json(args.config, None)
    if cfg is None:
        raise SystemExit("config.json が見つかりません")
    data_dir = Path(args.data_dir)

    app_id = os.environ.get("RAKUTEN_APP_ID", "").strip()
    access_key = os.environ.get("RAKUTEN_ACCESS_KEY", "").strip()
    affiliate_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "").strip()
    if not app_id or not access_key:
        raise SystemExit("RAKUTEN_APP_ID と RAKUTEN_ACCESS_KEY が設定されていません(手順書のStep 4を参照)")
    if not affiliate_id:
        log("注意: RAKUTEN_AFFILIATE_ID が未設定です。このままではリンクが収益になりません")

    # サイトを作る側(build.py)が使えるように、アフィリエイトIDを保存する(リンクに入る公開情報です)
    save_json(data_dir / "site_meta.json", {"affiliate_id": affiliate_id})
    client = rk.Client(app_id, access_key, affiliate_id,
                       referer=cfg["site"]["url"], base=cfg["api"]["base"],
                       versions=cfg["api"]["versions"])
    deadline = time.time() + args.budget_minutes * 60
    today = now().date()

    try:
        hotels = ensure_hotels(client, cfg, data_dir, force=args.refresh_hotels)
        state = load_json(data_dir / "state.json", {})
        state.setdefault("cells", {})
        state.pop("fetched", None)          # 旧形式(エリア別でない)は使わない
        state.setdefault("fetched_by", {})  # fetched_by[エリア][日付] = 取得した日時
        state.setdefault("meta", {})
        if args.reprobe:
            state["meta"] = {}
        stats = load_json(data_dir / "stats.json", None) or new_stats(today)
        for k, v in new_stats(today).items():
            stats.setdefault(k, v)

        # 過去の日付を捨てる
        keep = today.isoformat()
        state["fetched_by"] = {a: {d: t for d, t in fd.items() if d >= keep} for a, fd in state["fetched_by"].items()}
        for no in list(state["cells"]):
            state["cells"][no] = {d: c for d, c in state["cells"][no].items() if d >= keep}

        area_nos = {a: sorted({h["no"] for h in hotels["areas"].get(a, [])}) for a in cfg["areas"]}
        excluded = {str(x) for x in cfg["exclude"]}
        probe = cfg["search"]["probe_checks"]

        # 宿の詳細(大浴場など)を、1回に最大60軒ずつ貯める。最初の数回で全宿ぶんがそろう
        detail = enrich_hotels(client, hotels, data_dir, min(deadline, time.time() + 300), cap=60)
        hotel_hit = set().union(*hotelfeat.matched_nos(hotels["areas"], detail).values())

        def is_room_active(no):
            if no in excluded:
                return False
            m = state["meta"].get(no, {})
            return bool(m.get("seen")) or m.get("checks", 0) < probe or no in {str(x) for x in cfg["force_include"]}

        def is_active(no):
            # 部屋名での判定が済んだ宿に加え、宿そのものの条件(大浴場など)に当てはまる宿も、空き状況を取る
            return is_room_active(no) or (no in hotel_hit and no not in excluded)

        units = pick_units(cfg, state, today)
        if not units:
            log("すべて最新です。更新は不要です")
            return
        log(f"更新対象 {len(units)} 件(エリア×日付) / 制限時間 {args.budget_minutes:g} 分")

        done = 0
        for area, day in units:
            if time.time() > deadline:
                log("制限時間に達したので、残りは次回に回します")
                break
            all_nos = area_nos[area]
            active = [n for n in all_nos if is_active(n)]
            if not active:
                continue
            light = {n for n in active if not is_room_active(n)}   # 部屋単位の確認が不要な宿(宿全体の空きと最安だけ取る)
            cells, notes = fetch_date(client, cfg, active, day, light)
            dk = day.isoformat()
            fetched = state["fetched_by"].setdefault(area, {})
            old = {no: state["cells"].get(no, {}).get(dk) for no in cells}
            prev_t = fetched.get(dk)
            gap = ((now() - datetime.fromisoformat(prev_t)).total_seconds() / 3600) if prev_t else None
            known = {f: {no for no in all_nos if state["meta"].get(no, {}).get("seen", {}).get(f)}
                     | {str(x) for x in cfg["force_include"]} for f in cfg["features"]}
            record_stats(stats, cfg, day, today, old, cells, known, gap, now().date(), area)
            record_moves(state, stats, area, day, today, old, cells, gap, now().isoformat(timespec="seconds"))
            for no, cell in cells.items():
                state["cells"].setdefault(no, {})[day.isoformat()] = cell
            for no, note in notes.items():
                m = state["meta"].setdefault(no, {"checks": 0, "seen": {}})
                if note["rooms_seen"]:
                    m["checks"] += 1
                for f in note["features"]:
                    m["seen"][f] = True
            fetched[dk] = now().isoformat(timespec="seconds")
            save_json(data_dir / "state.json", state)  # 1日ごとに保存(途中で止まっても無駄にならない)
            save_json(data_dir / "stats.json", stats)
            done += 1
            n_ok = sum(1 for c in cells.values() if c["s"] == "v")
            log(f"{cfg['areas'][area]['label']} {day} 完了: 対象{len(active)}軒中 空きあり{n_ok}軒 (累計API {client.requests}回)")

        prune_changes(state, now(), today)
        # その日のエリアごとのまとめ(月別レポートの材料)。同じ日のうちは、最後の更新の値で上書きする
        stats.setdefault("area_daily", {})[today.isoformat()] = areastat.snapshot(cfg, hotels, state, today)
        state["updated"] = now().isoformat(timespec="seconds")
        save_json(data_dir / "state.json", state)
        if done:
            stats["runs"] += 1
            prune_stats(stats, today)
            save_json(data_dir / "stats.json", stats)
        log(f"{done} 件(エリア×日付)を更新しました")
    except rk.AuthError as e:
        raise SystemExit(
            f"APIの認証エラーです: {e}\n"
            "→ アプリID/アクセスキーの貼り間違い、または「許可するWebサイト」の設定を確認してください。"
        )
    except rk.ApiError as e:
        raise SystemExit(f"楽天APIでエラーが続きました: {e}")


if __name__ == "__main__":
    main()
