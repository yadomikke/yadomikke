"""更新したページのURLを、IndexNow(Bingなど)に通知する。通知に失敗しても、サイトの更新には影響しません。

 - まだ通知していない新しいURL → すべて通知
 - トップ・日程別まとめ・エリアのページ(階層が浅いページ) → 1日1回だけ通知
 - 通知の記録は data/indexnow.json に保存
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
LIMIT = 300


def depth(url, base):
    rel = url[len(base):].strip("/")
    return len([x for x in rel.split("/") if x]) if rel else 0


def pick_urls(urls, sent, today, base):
    """通知するURLを選ぶ。sent は {URL: 最後に通知した日}"""
    new = [u for u in urls if u not in sent]
    # 毎日通知し直すのは浅いページだけ(宿ページ /hotel/番号/ は数が多いので、新しいときだけ通知する)
    shallow = [u for u in urls if u in sent and depth(u, base) <= 2 and sent[u] != today
               and not u[len(base):].startswith("/hotel/")]
    return (new + shallow)[:LIMIT]


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    base = cfg["site"]["url"].rstrip("/")
    key = str(cfg["site"].get("indexnow_key", "")).strip()
    sm = ROOT / "public" / "sitemap.xml"
    if not re.fullmatch(r"[0-9a-f]{8,128}", key) or not sm.exists():
        print("IndexNow: 鍵またはsitemapがないため、通知しません")
        return
    urls = re.findall(r"<loc>([^<]+)</loc>", sm.read_text(encoding="utf-8"))
    rec_path = ROOT / "data" / "indexnow.json"
    rec = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.exists() else {"sent": {}}
    today = datetime.now(JST).date().isoformat()
    todo = pick_urls(urls, rec["sent"], today, base)
    if not todo:
        print("IndexNow: 通知するURLはありません")
        return
    body = json.dumps({"host": base.split("//", 1)[1], "key": key, "keyLocation": f"{base}/{key}.txt", "urlList": todo}).encode()
    req = urllib.request.Request("https://api.indexnow.org/indexnow", data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"IndexNow: {len(todo)} 件を通知しました (HTTP {r.status})")
    except Exception as e:  # 通知の失敗でサイト更新を止めない
        print(f"IndexNow: 通知できませんでした({e})。次回やり直します")
        return
    for u in todo:
        rec["sent"][u] = today
    rec["sent"] = {u: d for u, d in rec["sent"].items() if u in set(urls)}
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    rec_path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
