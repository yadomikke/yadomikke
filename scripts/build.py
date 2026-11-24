"""data/ の空室データから、公開用のHTMLページ(public/)を作る。

使い方:
    python scripts/build.py
    python scripts/build.py --demo --data-dir preview_data --out-dir preview   # 見本(架空データ)

画面の流れ(旅行サイトで一般的な「日付を選ぶ → 宿カードが並ぶ」形):
    1. 日付を選ぶ(横スクロールのカレンダー帯 + 「次の土曜」「連休」ボタン)
    2. その日に泊まれる宿をカードで表示(写真・評価・料金・予約ボタン)
    3. 31日ぶんを一覧で比べるカレンダー表(下の方)
"""
import argparse
import html
import json
import math
import os
import re
import shutil
import unicodedata
import urllib.parse
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scenes import all_scenes, holiday_blocks, area_days  # noqa: E402
import hotelfeat  # noqa: E402
import areastat  # noqa: E402

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
WD = "月火水木金土日"


# ---- 小さな部品 -------------------------------------------------------

CUR_AUTO = False     # いま作っているのが「宿の条件ページ」(大浴場など)かどうか
HOTEL_HREF = {}      # 施設番号 -> 宿ページのパス(/hotel/番号/)。main() が最初に決める
LINK = None          # 日付つきプラン一覧リンクの材料 {"a": アフィリエイトID, "ad": 大人, "n": 泊数}。main() が決める
# 日付別ページを作る日程(「来週末」「クリスマス」は、似たページが増えすぎるので作らない。データの取得は続ける)
PAGE_SCENES = ("shumatsu", "renkyu", "nenmatsu")
DROPPED_SCENES = ("raishu", "xmas")
POPULAR_MIN_REVIEWS = 20   # 条件ページに載らない宿でも、口コミがこの件数以上なら宿ページを作る(update.py も空室を取り続ける)
ZEN = "zentai"             # 上の宿の「宿全体の空き」を表す、画面に出ない条件
TABLE_MAX = 25       # カレンダー表に最初から載せる宿の数(残りは「もっと見る」でブラウザ側が表示。ページを軽くするため)


def is_hotel_feat(cfg, feat):
    return bool(cfg["features"][feat].get("hotel"))


def fdesc(cfg, feat):
    """「◯◯」はどう判定しているか(ページに載せる説明の中身)"""
    f = cfg["features"][feat]
    if f.get("hotel") or f.get("def"):
        return f["def"]
    return "部屋名に「" + "」「".join(f["keywords"]) + "」を含む部屋"


REGIONS = [("北海道・東北", "北海道 青森 岩手 宮城 秋田 山形 福島"), ("関東", "茨城 栃木 群馬 埼玉 千葉 東京 神奈川"),
           ("甲信越", "山梨 長野 新潟"), ("北陸", "富山 石川 福井"), ("東海", "静岡 愛知 岐阜 三重"),
           ("近畿", "滋賀 京都 大阪 兵庫 奈良 和歌山"), ("中国・四国", "鳥取 島根 岡山 広島 山口 徳島 香川 愛媛 高知"),
           ("九州", "福岡 佐賀 長崎 熊本 大分 宮崎 鹿児島 沖縄")]


def region_of(area_cfg):
    pref = area_cfg.get("pref", "")
    return next((r for r, ps in REGIONS if pref and pref in ps.split()), "その他")


def by_region(cfg, keys):
    """エリアのキーを、地方ごとに(地方の順・エリアの並びはそのまま)。[(地方, [キー…])]"""
    out = {}
    for k in keys:
        out.setdefault(region_of(cfg["areas"][k]), []).append(k)
    return [(r, out[r]) for r, _ in REGIONS + [("その他", "")] if r in out]


def hotel_href(h):
    """宿のページへのリンク(サイト直下からのパス。なければ空文字)。宿ページは条件に関係なく1軒1ページ(/hotel/番号/)"""
    return HOTEL_HREF.get(h["no"], "")


def apply_hotel_features(cfg, hotels, state, detail):
    """宿の条件(大浴場など)を、既存の仕組み(特徴・セル)に載せる。
    ・cfg["features"] に条件を足す  ・条件に合う宿を「seen」にする
    ・空きのある日のセルに、宿全体の最安(any)を「その条件の◯」として入れる
    ・宿が3軒以上あるエリア×条件のページ定義を cfg["pages"] に足す"""
    nmin = int(cfg.get("hotel_page_min", 3))
    keys = list(hotelfeat.FEATURES)
    for k in keys:
        fd = hotelfeat.FEATURES[k]
        cfg["features"][k] = {"label": fd["label"], "keywords": [], "hotel": True, "def": fd["def"], "short": fd["short"]}
    added = 0
    for area, lst in hotels.get("areas", {}).items():
        if area not in cfg["areas"]:
            continue
        alabel = cfg["areas"][area]["label"]
        for k in keys:
            hit = [h for h in lst if hotelfeat.matches(k, h, detail.get(h["no"]))]
            n_cells = 0
            for h in hit:
                state.setdefault("meta", {}).setdefault(h["no"], {"checks": 0, "seen": {}}).setdefault("seen", {})[k] = True
                for d, c in state.get("cells", {}).get(h["no"], {}).items():
                    if c.get("s") == "v" and c.get("any") and c["any"][0]:
                        c.setdefault("f", {})[k] = [c["any"][0], c["any"][1], "宿全体の最安プラン"]
                        n_cells += 1
            if len(hit) >= nmin:
                fd = hotelfeat.FEATURES[k]
                cfg["pages"].append({
                    "slug": f"{area}-{k}", "area": area, "feature": k, "auto": True,
                    "title": f"{alabel}で今空いている{fd['label']}",
                    "lead": f"{alabel}で、{fd['short']}の条件に当てはまる宿の空きを、日付ごとにまとめています。"})
                added += 1
    return added


DEFAULT_THEMES = [
    {"slug": "high-rated", "name": "口コミ高評価(4.5以上)", "kind": "rating", "min": 4.5},
    {"slug": "under-30000", "name": "3万円以下で泊まれる", "kind": "price_max", "max": 30000},
    {"slug": "over-50000", "name": "5万円以上の特別な宿", "kind": "price_min", "min": 50000},
    {"slug": "many-reviews", "name": "口コミ500件以上の人気宿", "kind": "reviews", "min": 500},
]

REL_JS = r"""(function(){var ls=document.querySelectorAll('label.dinput');for(var i=0;i<ls.length;i++){(function(l){l.addEventListener('click',function(e){var x=l.querySelector('input');if(!x||e.target===x)return;e.preventDefault();try{x.showPicker()}catch(_){x.focus()}});})(ls[i]);}})();window.relt=function(){var ts=document.querySelectorAll('time.rel');for(var i=0;i<ts.length;i++){var t=ts[i],d=new Date(t.getAttribute('datetime')),m=Math.round((Date.now()-d.getTime())/60000);if(isNaN(m)||m<0)continue;if(!t.getAttribute('data-a')){t.setAttribute('data-a',t.textContent);}var r=m<60?'約'+Math.max(m,1)+'分前':(m<1440?'約'+Math.round(m/60)+'時間前':'約'+Math.round(m/1440)+'日前');t.textContent=r+'('+t.getAttribute('data-a')+')';}};relt();"""


# 流入元(ChatGPTなど)と、楽天へのリンクのクリックを、Googleタグマネージャー(dataLayer)に知らせる。
# リンクのURL自体は変えない(アフィリエイトの計測を壊さないため)
TRACK_JS = r"""(function(){try{var w=window,dl=w.dataLayer=w.dataLayer||[],k='ym_src',s=null,ss=null;try{ss=w.sessionStorage;s=ss.getItem(k)}catch(e){}
if(!s){var u='',h='';try{u=(new URLSearchParams(location.search).get('utm_source')||'').toLowerCase()}catch(e){}
try{h=document.referrer?new URL(document.referrer).hostname:''}catch(e){}if(h===location.hostname)h='';var t=u+' '+h;
s=/chatgpt|openai/.test(t)?'chatgpt':/perplexity/.test(t)?'perplexity':/claude\.ai|anthropic/.test(t)?'claude':/gemini/.test(t)?'gemini':/copilot/.test(t)?'copilot':/google\./.test(t)?'google':/bing\./.test(t)?'bing':/yahoo\./.test(t)?'yahoo':(u||h)?'other':'direct';
try{ss&&ss.setItem(k,s)}catch(e){}dl.push({event:'ym_landing',ym_src:s});}
w.ymSrc=s;
w.ymAff=function(){dl.push({event:'aff_click',ym_src:s,ym_page:location.pathname})};
document.addEventListener('click',function(e){if(e.defaultPrevented)return;var a=e.target.closest?e.target.closest('a[href*="hb.afl.rakuten.co.jp"],a[href*="travel.rakuten.co.jp"]'):null;if(a)w.ymAff()},false);}catch(e){}})();"""


# iPhone の ChatGPT アプリの中で開いた人に、Safari で開き直す案内を出す(アプリ内のブラウザでは楽天のログインが引き継がれないため)
IOS_JS = r"""(function(){try{var w=window,d=document,dl=w.dataLayer=w.dataLayer||[],ss=null,k='ym_ios';try{ss=w.sessionStorage}catch(e){}
var q=new URLSearchParams(location.search),test=q.get('iabtest')==='1';
// Safari に移ったあと(ボタンで開いたページには ym_ext=1 が付く)は何も出さない
if(q.get('ym_ext')==='1'){try{ss&&ss.setItem('ym_ext','1')}catch(e){}dl.push({event:'iab_ext_open',ym_src:w.ymSrc});return;}
try{if(ss&&ss.getItem('ym_ext'))return;}catch(e){}
// 🧭で Safari に移った場合: アドレスには目印(ym_v=c)があるのに、このブラウザでは案内を出した記録がない → もう Safari にいる
var seen=null;try{seen=ss&&ss.getItem('ym_iab')}catch(e){}
if(!test&&q.get('ym_v')==='c'&&!seen){try{ss&&ss.setItem('ym_ext','1')}catch(e){}dl.push({event:'iab_ext_open',ym_src:w.ymSrc,ym_via:'compass'});return;}
if(!test&&(w.ymSrc!=='chatgpt'||!/iPhone|iPad|iPod/.test(navigator.userAgent)))return;
// アプリ内のブラウザにいる: 記録を残し、アドレスに目印を付ける(🧭で開いた先の Safari で見分けるため)
try{ss&&ss.setItem('ym_iab','1')}catch(e){}
try{if(q.get('ym_v')!=='c'){var u0=new URL(location.href);u0.searchParams.set('ym_v','c');history.replaceState(history.state,'',u0.toString());}}catch(e){}
var BTN=true;   // 「Safariで開く」ボタン(iPhoneのChatGPTアプリで動作を確認済み。押すと「Safariで開きますか?」の確認が出る)
function extUrl(){var u=new URL(location.href);u.searchParams.delete('iabtest');u.searchParams.delete('ym_v');u.searchParams.set('ym_ext','1');return 'x-safari-'+u.toString();}
function guide(box){if(box.querySelector('.ifb'))return;var g=d.createElement('div');g.innerHTML='<p class="ifb">「開く」を押しても切り替わらない場合は、画面右下の<b>🧭</b>を押すと Safari で開けます。</p><button type="button" class="icopy">リンクをコピー</button>';box.appendChild(g);
 g.querySelector('.icopy').addEventListener('click',function(){var u=new URL(location.href);u.searchParams.delete('iabtest');try{navigator.clipboard.writeText(u.toString());this.textContent='コピーしました(Safariに貼り付け)';}catch(e){}dl.push({event:'iab_copy'});});}
function btn(where){var box=d.createElement('div');box.className='iabb';var a=d.createElement('a');a.className='cta iabtn';a.href=extUrl();a.textContent='🧭 Safariで開く';
 // 押すと「Safariで開きますか?」の確認が出るので、すぐには「開けなかった」と判断しない。8秒たってもこの画面のままなら、ボタンは残したまま🧭の案内を足す
 a.addEventListener('click',function(){dl.push({event:'iab_btn_click',ym_place:where});var t0=Date.now();setTimeout(function(){if(d.visibilityState==='visible'&&Date.now()-t0<12000){dl.push({event:'iab_btn_fail',ym_place:where});guide(box);}},8000);});
 box.appendChild(a);return box;}
var wr=d.querySelector('.wrap');if(!wr)return;
var closed=false;try{closed=!!(ss&&ss.getItem(k))}catch(e){}
if(!closed){var b=d.createElement('div');b.className='iosn';b.setAttribute('role','note');
 b.innerHTML='<div class="iosb"><p><b>ChatGPTアプリの中で開いています</b><br>楽天にログインしたまま予約して、ポイントも使えるように、<b>予約ボタンを押す前に</b>Safariで開いてください。'+(BTN?'':'画面右下の<b>🧭</b>を押すと Safari で開けます。')+'</p></div><button type="button" class="iclose" aria-label="閉じる">×</button>';
 if(BTN)b.querySelector('.iosb').appendChild(btn('top'));
 b.querySelector('.iclose').addEventListener('click',function(){b.remove();try{ss&&ss.setItem(k,'1')}catch(e){}});wr.insertBefore(b,wr.firstChild);}
// 予約ボタンのすぐ上にも、短い一言(とボタン)を出す
var spot=d.getElementById('dpick')||d.getElementById('bookpanel');
if(spot){var n=d.createElement('div');n.className='iosn2';n.innerHTML='<p>楽天にログインしたまま予約するなら、<b>予約ボタンを押す前に</b>Safariで開いてください'+(BTN?'':'(画面右下の🧭)')+'。</p>';
 if(BTN)n.appendChild(btn('book'));spot.insertBefore(n,spot.firstChild);}
dl.push({event:'iab_show',ym_btn:BTN?1:0});}catch(e){}})();"""


# 条件ページの固定バー: 一覧の先頭(最安)の宿の予約ボタンと同じリンク
SBAR2_JS = r"""(function(){var sb=document.getElementById('sbar2');if(!sb)return;var hd=document.getElementById('rhead');
function chk(){var show=sb.getAttribute('data-ok')==='1'&&hd&&hd.getBoundingClientRect().bottom<0;sb.hidden=!show;document.body.classList.toggle('hasbar',show);}
window.ymSbar=function(){var a=document.querySelector('#cards article.hc:not(.part) a.cta');if(!a){sb.setAttribute('data-ok','');chk();return;}
var art=a.closest('article'),nm=art.querySelector('h3'),pv=art.querySelector('.pv'),t=document.getElementById('rt');
sb.querySelector('b').textContent=(t&&t.firstChild?t.firstChild.textContent.replace('に泊まれる宿','').trim()+' ':'')+'最安'+(pv?pv.textContent.replace(/\s+/g,''):'');
sb.querySelector('small').textContent=(nm?nm.textContent.trim():'');sb.querySelector('a').href=a.href;sb.setAttribute('data-ok','1');chk();};
window.addEventListener('scroll',chk,{passive:true});window.ymSbar();})();"""

# 予約リンク(日付つきプラン一覧・アフィリエイト経由)を、宿番号と日付からブラウザ側で組み立てる。stay_link() と同じ形
SL_JS = r"""(function(){var L=window.__LT;if(!L)return;var as=document.querySelectorAll('a[data-sl]');
for(var i=0;i<as.length;i++){var a=as[i],x=a.getAttribute('data-sl').split('|'),p=x[1].split('-'),
d=new Date(Date.UTC(+p[0],+p[1]-1,+p[2])),c=new Date(d.getTime()+L.n*864e5),
t='https://hotel.travel.rakuten.co.jp/hotelinfo/plan/'+x[0]+'?f_teikei=&f_nen1='+d.getUTCFullYear()+'&f_tuki1='+(d.getUTCMonth()+1)+'&f_hi1='+d.getUTCDate()
+'&f_nen2='+c.getUTCFullYear()+'&f_tuki2='+(c.getUTCMonth()+1)+'&f_hi2='+c.getUTCDate()+'&f_heya_su=1&f_otona_su='+L.ad+'&f_s1=0&f_s2=0&f_y1=0&f_y2=0&f_y3=0&f_y4=0',
q=encodeURIComponent(t);a.href=L.a?'https://hb.afl.rakuten.co.jp/hgc/'+L.a+'/?pc='+q+'&m='+q:t;}
window.ymSL=function(no,iso){var p=iso.split('-'),d=new Date(Date.UTC(+p[0],+p[1]-1,+p[2])),c=new Date(d.getTime()+L.n*864e5),
t='https://hotel.travel.rakuten.co.jp/hotelinfo/plan/'+no+'?f_teikei=&f_nen1='+d.getUTCFullYear()+'&f_tuki1='+(d.getUTCMonth()+1)+'&f_hi1='+d.getUTCDate()
+'&f_nen2='+c.getUTCFullYear()+'&f_tuki2='+(c.getUTCMonth()+1)+'&f_hi2='+c.getUTCDate()+'&f_heya_su=1&f_otona_su='+L.ad+'&f_s1=0&f_s2=0&f_y1=0&f_y2=0&f_y3=0&f_y4=0',
q=encodeURIComponent(t);return L.a?'https://hb.afl.rakuten.co.jp/hgc/'+L.a+'/?pc='+q+'&m='+q:t;};})();"""

# エリアページ: 日付(?d= または日付の入力)を選ぶと、その日の空き数・上位3軒の予約ボタン・条件ページへのリンクを出す
AREA_JS = r"""(function(){var A=window.__AD,box=document.getElementById('apick');if(!A||!box)return;
var out=document.getElementById('aout'),inp=document.getElementById('ainput'),WD='日月火水木金土';
function lab(iso){var p=iso.split('-');return (+p[1])+'/'+(+p[2])+'('+WD[new Date(Date.UTC(+p[0],+p[1]-1,+p[2])).getUTCDay()]+((A.hol||[]).indexOf(iso)>=0?'祝':'')+')';}
function yen(n){return '¥'+(+n).toLocaleString('ja-JP');}
function mk(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e;}
function link(no,iso){return window.ymSL?ymSL(no,iso):'#';}
function show(iso,push){out.textContent='';inp.value=iso;var x=A.d[iso];
 var bs=box.querySelectorAll('.quick button');for(var i=0;i<bs.length;i++)bs[i].classList.toggle('on',bs[i].getAttribute('data-d')===iso);
 if(push){try{var u=new URL(location.href);u.searchParams.set('d',iso);u.hash='';history.replaceState(null,'',u.toString());}catch(e){}}
 if(!x){out.appendChild(mk('p','qh',lab(iso)+'は、当サイトの取得範囲外です('+lab(A.min)+'〜'+lab(A.max)+'から選んでください)。'));return;}
 if(!x[0]){out.appendChild(mk('p','qh',lab(iso)+'は、まだ空室を取得できていません。'));return;}
 out.appendChild(mk('p','qh',lab(iso)+':'+A.l+'で空きのある宿 '+x[1]+'軒(確認できた'+x[0]+'軒中)'+(x[2]?'・最安 '+yen(x[2])+'〜':'')));
 x[3].forEach(function(e){var no=e[0],pr=e[1],I=A.H[no]||['','','',''],row=mk('div','qr'),nm=mk('div','qn');
   if(I[3]){var im=mk('img','qimg');im.src=I[3];im.alt='';im.loading='lazy';im.onerror=function(){this.remove();};row.appendChild(im);}
   var a=mk('a',null,I[0]);a.href=I[1]||link(no,iso);if(!I[1]){a.target='_blank';a.rel='sponsored nofollow noopener';}nm.appendChild(a);
   nm.appendChild(mk('small',null,(I[2]?'★'+I[2]+' ・ ':'')+(pr?yen(pr)+'〜':'')));row.appendChild(nm);
   var b=mk('a','cta qcta','予約する ›');b.href=link(no,iso);b.target='_blank';b.rel='sponsored nofollow noopener';row.appendChild(b);out.appendChild(row);});
 if(!x[3].length&&x[1])out.appendChild(mk('p','qm','口コミの多い宿の空きはありませんでした。条件ページで、ほかの宿を確認できます。'));
 if(A.f.length){var c=mk('p','qm','条件で絞る: ');A.f.forEach(function(f,i){var fa=mk('a',null,f[1]);fa.href='../'+f[0]+'/?d='+iso;if(i)c.appendChild(document.createTextNode(' ・ '));c.appendChild(fa);});out.appendChild(c);}}
var q=null;try{q=new URLSearchParams(location.search).get('d');}catch(e){}
if(!q&&/^#\d{4}-\d{2}-\d{2}$/.test(location.hash))q=location.hash.slice(1);
box.hidden=false;show(q&&/^\d{4}-\d{2}-\d{2}$/.test(q)?q:A.def,false);
inp.addEventListener('change',function(){if(inp.value)show(inp.value,true);});
var bs=box.querySelectorAll('.quick button');for(var i=0;i<bs.length;i++){(function(b){b.addEventListener('click',function(){show(b.getAttribute('data-d'),true);});})(bs[i]);}
})();"""


# 宿ページ: アドレスで指定された日付(?d=YYYY-MM-DD)の空きを、ページの上に表示し、カレンダーのその日に印を付ける
DPICK_JS = r"""(function(){var box=document.getElementById('dpick');if(!box)return;
var out=document.getElementById('dout'),inp=document.getElementById('hdinput'),fl=box.getAttribute('data-fl'),no=box.getAttribute('data-no'),
mn=box.getAttribute('data-min'),mx=box.getAttribute('data-max'),WD='日月火水木金土',
sb=document.getElementById('sbar'),sl=sb&&sb.querySelector('b'),sa=sb&&sb.querySelector('a'),sm=sb&&sb.querySelector('small');
function lab(q){var p=q.split('-');return (+p[1])+'月'+(+p[2])+'日('+WD[new Date(Date.UTC(+p[0],+p[1]-1,+p[2])).getUTCDay()]+')';}
function mk(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e;}
function show(q,push){out.textContent='';inp.value=q;
 var old=document.querySelectorAll('.mini .sel');for(var i=0;i<old.length;i++)old[i].classList.remove('sel');
 var bs=box.querySelectorAll('.quick button');for(var i=0;i<bs.length;i++)bs[i].classList.toggle('on',bs[i].getAttribute('data-d')===q);
 if(push){try{var u=new URL(location.href);u.searchParams.set('d',q);u.hash='';history.replaceState(null,'',u.toString());}catch(e){}}
 var L=lab(q),c=document.querySelector('.mini [data-d="'+q+'"]'),msg,link=window.ymSL?ymSL(no,q):null,ok=false;
 if(!c){msg=L+'は、当サイトがまだ空室を確認していない日付です。楽天トラベルで直接ご確認ください。';}
 else{c.classList.add('sel');var k=c.getAttribute('data-s'),pr=c.getAttribute('data-p');
  if(k==='ok'){ok=true;msg=L+':◯ '+fl+'の空きあり'+(pr?' ¥'+(+pr).toLocaleString('ja-JP')+'〜':'');link=c.getAttribute('data-u')||c.getAttribute('href')||link;}
  else if(k==='part'){msg=L+':宿には空きがありますが、'+fl+'の空きは確認できませんでした。';}
  else if(k==='full'){msg=L+':取得時点では満室です。キャンセルで空くこともあります。';
   var bk=(box.getAttribute('data-back')||'').split('|');if(bk.length===4){msg+='この宿の満室からの戻りやすさは「'+bk[1]+'」です(当サイトの観測で、満室だった'+bk[2]+'回のうち'+bk[3]+'回、次の確認で空きが出ました)。';}}
  else{msg=L+':まだ取得できていません。';}}
 out.appendChild(mk('p','qh',msg));
 if(ok){var rn=c.getAttribute('data-r');out.appendChild(mk('p','qm','1泊・大人2名・1室の最安(取得時点)'+(rn?' / '+rn:'')));
  try{var kp=JSON.parse(box.getAttribute('data-kp')||'null'),ld=Math.round((Date.UTC(+q.slice(0,4),+q.slice(5,7)-1,+q.slice(8,10))-Date.UTC(+mn.slice(0,4),+mn.slice(5,7)-1,+mn.slice(8,10)))/864e5);
   if(kp&&ld>0&&kp[ld]){out.appendChild(mk('p','qkeep','📊 このエリアで今空いている宿が、この日まで空いたまま残る見込み: 約'+kp[ld]+'%(当サイトの観測)。'+(kp[ld]<40?'早めの予約がおすすめです':(kp[ld]>=70?'まだ少し余裕がありそうです':'数日のうちに埋まる宿も出てきそうです'))));}}catch(e){}}
 if(link){var a=mk('a','cta'+(ok?'':' ghost'),L+'を楽天トラベルで'+(ok?'予約する ›':'最新の空きを確認する ›'));a.href=link;a.target='_blank';a.rel='sponsored nofollow noopener';var pp=mk('p');pp.appendChild(a);out.appendChild(pp);}
 if(!ok)alts(q);
 var mc=document.querySelector('#maincta a');if(mc&&link){mc.href=link;mc.textContent=L+'の空室・料金を楽天トラベルで確認 ›';}
 if(sb&&link){sl.textContent=L+(ok&&pr?' ¥'+(+pr).toLocaleString('ja-JP')+'〜':(c&&c.getAttribute('data-s')==='full'?' 取得時点で満室':''));sm.textContent=(ok&&pr?'1泊・大人2名の最安(取得時点)':'楽天トラベルで最新の空きを確認');sa.href=link;sa.textContent=(ok?'予約する ›':'空きを確認 ›');sb.hidden=false;document.body.classList.add('hasbar');}}
// この宿が空いていない日は、同じ条件で、その日に空いていた近くの宿を出す(取得時点の値)
function alts(q){var A=window.__ALT;if(!A)return;var i=A.d.indexOf(q);if(i<0)return;
 var r=A.h.filter(function(x){return x[2][i]>0;}).sort(function(a,b){return a[2][i]-b[2][i];}).slice(0,3);if(!r.length)return;
 var w=mk('div','qalt');w.appendChild(mk('p','qm',fl==='宿'?'同じ日に空きがあった近くの宿(取得時点):':'同じ日に'+fl+'の空きがあった近くの宿(取得時点):'));
 r.forEach(function(x){var row=mk('div','qr'),n=mk('div','qn'),na=mk('a',null,x[1]);na.href='../'+x[0]+'/?d='+q;n.appendChild(na);
  n.appendChild(mk('small',null,'¥'+x[2][i].toLocaleString('ja-JP')+'〜'));row.appendChild(n);
  if(window.ymSL){var b=mk('a','cta qcta','空室を見る ›');b.href=ymSL(x[0],q);b.target='_blank';b.rel='sponsored nofollow noopener';row.appendChild(b);}
  w.appendChild(row);});out.appendChild(w);}
var q=null;try{q=new URLSearchParams(location.search).get('d');}catch(e){}
if(!q&&/^#\d{4}-\d{2}-\d{2}$/.test(location.hash))q=location.hash.slice(1);
var valid=q&&/^\d{4}-\d{2}-\d{2}$/.test(q);
box.hidden=false;show(valid?q:box.getAttribute('data-def'),false);
inp.addEventListener('change',function(){if(inp.value)show(inp.value,true);});
var bs=box.querySelectorAll('.quick button');for(var i=0;i<bs.length;i++){(function(b){b.addEventListener('click',function(){show(b.getAttribute('data-d'),true);});})(bs[i]);}
// カレンダーの満室・△・未取得の日を押すと、上の「日付を選んで予約する」にその日を出す(楽天の最新確認・代わりの宿)
var cb=document.querySelectorAll('.mini button[data-d],.mini a[data-d]');for(var i=0;i<cb.length;i++){(function(b){b.addEventListener('click',function(e){e.preventDefault();show(b.getAttribute('data-d'),true);try{box.scrollIntoView({behavior:'smooth',block:'start'});}catch(e){box.scrollIntoView();}});})(cb[i]);}
// 下の予約バーは、上の予約ボタンが画面に見えている間は隠す(同じボタンが2つ並ばないように)
if(sb&&window.IntersectionObserver){var vis={},io=new IntersectionObserver(function(es){es.forEach(function(e){vis[e.target.id]=e.isIntersecting;});sb.classList.toggle('off',!!(vis.dpick||vis.maincta));});
 io.observe(box);var m2=document.getElementById('maincta');if(m2)io.observe(m2);}
})();"""


def norm_name(s):
    """宿名を読みやすくする: 全角英数字・記号を半角に、半角カナを全角に(NFKC)、連続する空白を1つに"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(s))).strip()


def esc(s):
    return html.escape(str(s), quote=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def md(d):
    return f"{d.month}/{d.day}"


def yen(p):
    return f"{p / 10000:.1f}万" if p >= 10000 else f"{p:,}"


def yen_full(p):
    return f"¥{p:,}"


def parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def safe_url(u):
    """リンク先は http(s) だけ許可する(画像は見本用のdata:imageも許可)"""
    u = str(u or "")
    if u.startswith(("https://", "http://", "data:image/")):
        return u
    return "#"


def aff_wrap(aff, url):
    """楽天トラベルのURLが、アフィリエイト経由になっていなければ、経由するURLに付け替える(念のための安全策)"""
    u = str(url or "")
    if not aff or not u.startswith("https://hotel.travel.rakuten.co.jp/"):
        return u
    q = urllib.parse.quote(u, safe="")
    return f"https://hb.afl.rakuten.co.jp/hgc/{aff}/?pc={q}&m={q}"


def stay_link(aff, no, day, adults, nights):
    """宿のプラン一覧(日付・人数つき)へ行く、楽天アフィリエイトのリンク。ログインなしで開けるページです"""
    co = day + timedelta(days=nights)
    target = (f"https://hotel.travel.rakuten.co.jp/hotelinfo/plan/{no}?f_teikei="
              f"&f_nen1={day.year}&f_tuki1={day.month}&f_hi1={day.day}"
              f"&f_nen2={co.year}&f_tuki2={co.month}&f_hi2={co.day}"
              f"&f_heya_su=1&f_otona_su={adults}&f_s1=0&f_s2=0&f_y1=0&f_y2=0&f_y3=0&f_y4=0")
    if not aff:
        return target
    q = urllib.parse.quote(target, safe="")
    return f"https://hb.afl.rakuten.co.jp/hgc/{aff}/?pc={q}&m={q}"


def link_or_blank(no, dk, url):
    """ブラウザ側で同じリンクを組み立てられる場合は空文字を返す(ページに長いURLを何千個も載せないため)"""
    if not LINK or not url:
        return url
    try:
        day = datetime.strptime(dk, "%Y-%m-%d").date()
    except ValueError:
        return url
    return "" if url == stay_link(LINK["a"], no, day, LINK["ad"], LINK["n"]) else url


def rewrite_links(cfg, state, aff):
    """空室の行き先を、予約手続きのページ(ログインが必要)から、日付つきのプラン一覧に付け替える"""
    adults = int(cfg["search"].get("adults", 2))
    nights = int(cfg["search"].get("nights", 1))
    n = 0
    for no, days in state.get("cells", {}).items():
        if not re.fullmatch(r"[0-9A-Za-z_-]+", str(no)):
            continue
        for dk, c in days.items():
            try:
                day = datetime.strptime(dk, "%Y-%m-%d").date()
            except ValueError:
                continue
            url = stay_link(aff, no, day, adults, nights)
            for f in c.get("f", {}).values():
                if len(f) > 1:
                    f[1] = url
                    n += 1
            if c.get("any"):
                c["any"][1] = url
    return n


def btn(url, cls, text, ext=True):
    u = safe_url(url)
    if u == "#":
        return f'<span class="{cls}">{text}</span>'
    return f'<a class="{cls}" href="{esc(u)}" target="_blank" rel="sponsored nofollow noopener">{text}</a>'


def day_label(d, holidays):
    mark = "祝" if d.isoformat() in holidays else ""
    return f"{md(d)}({WD[d.weekday()]}{mark})"


def day_class(d, holidays):
    if d.isoformat() in holidays or d.weekday() == 6:
        return "sun"
    return "sat" if d.weekday() == 5 else ""


# ---- 集計 -------------------------------------------------------------

def hotels_for_page(cfg, hotels, state, page):
    feat = page["feature"]
    force = {str(x) for x in cfg["force_include"]}
    out = []
    for h in hotels.get("areas", {}).get(page["area"], []):
        seen = state.get("meta", {}).get(h["no"], {}).get("seen", {})
        if seen.get(feat) or h["no"] in force:
            out.append(h)
    return out


def status(cell, feat):
    """セルの状態: none(未取得) / full(満室) / ok(◯) / part(△)"""
    if not cell:
        return "none"
    if cell["s"] == "f":
        return "full"
    return "ok" if feat in cell.get("f", {}) else "part"


def summarize(rows, dates, feat, state):
    """日付ごとの ◯ の軒数と最安"""
    info = {}
    for d in dates:
        k = d.isoformat()
        n, low = 0, None
        for h in rows:
            c = state["cells"].get(h["no"], {}).get(k)
            if status(c, feat) == "ok":
                n += 1
                p = c["f"][feat][0]
                low = p if low is None else min(low, p)
        info[k] = (n, low)
    return info


def url_template(nos, urls):
    """どの宿のURLも「前の部分 + 施設番号 + 後ろの部分」で同じ形なら、その (前, 後ろ) を返す。半分以上が当てはまらなければ None"""
    cnt = {}
    for no, u in zip(nos, urls):
        if u and u.count(no) == 1:
            a, b = u.split(no)
            cnt[(a, b)] = cnt.get((a, b), 0) + 1
    if not cnt:
        return None
    best, n = max(cnt.items(), key=lambda x: x[1])
    return list(best) if n * 2 >= len(nos) else None


def embed_data(cfg, rows, dates, info, state, feat, holidays, default, now):
    """ブラウザ側(JavaScript)で日付を切り替えるためのデータ"""
    # ページを軽くするため、マスは日付の並び順の配列にし、部屋名は一覧(rn)の番号で持つ。
    # 宿のURLが決まった形(番号だけ違う)なら、形(ut)だけを載せて省く。ブラウザ側(JS)で元の形に戻す
    hs, rn, rni = [], [], {}
    urls = {k: [] for k in ("u", "pu", "im")}
    for h in rows:
        urls["u"].append(safe_url(h.get("url")))
        urls["pu"].append(safe_url(h.get("plan_url") or h.get("url")))
        urls["im"].append(safe_url(h.get("image")) if h.get("image") else "")
    ut = {k: url_template([h["no"] for h in rows], v) for k, v in urls.items()}
    for i, h in enumerate(rows):
        cc = []
        for d in dates:
            c = state["cells"].get(h["no"], {}).get(d.isoformat())
            st = status(c, feat)
            if st == "ok":
                p, url, name = c["f"][feat]
                n_d, low_d = info[d.isoformat()]
                if name not in rni:
                    rni[name] = len(rn)
                    rn.append(name)
                v = ["o", p, rni[name], link_or_blank(h["no"], d.isoformat(), safe_url(url or h.get("url"))),
                     badges_for(cfg, h, p, low_d, n_d), badges_for(cfg, h, p, low_d, n_d, limit=False)]
                while len(v) > 3 and not v[-1]:
                    v.pop()
                cc.append(v)
            elif st == "part":
                a = c.get("any") or [0, ""]
                lk_ = link_or_blank(h["no"], d.isoformat(), safe_url(a[1] or h.get("plan_url") or h.get("url")))
                cc.append(["p", a[0]] + ([lk_] if lk_ else []))
            elif st == "full":
                cc.append("f")
            else:
                cc.append(0)
        while cc and cc[-1] == 0:
            cc.pop()
        e = {"no": h["no"], "n": h["name"], "rv": h.get("review"), "rc": h.get("reviews"), "st": h.get("station", ""),
             "hp": hotel_href(h), "ad": h.get("address", ""), "cc": cc}
        for k in ("u", "pu", "im"):
            if not ut[k] or urls[k][i] != ut[k][0] + h["no"] + ut[k][1]:
                e[k] = urls[k][i]
        hs.append(e)
    ren = set()
    for a, b in holiday_blocks(dates, holidays):
        for i in range((b - a).days + 1):
            ren.add((a + timedelta(days=i)).isoformat())
    mx = max([info[d.isoformat()][0] for d in dates] or [0])
    crown = {d.isoformat() for d in dates if mx >= 2 and info[d.isoformat()][0] == mx}
    if len(crown) > 3:
        crown = set()   # 同じ軒数の日が多いときは、目印の意味がないので付けない
    ds = []
    for d in dates:
        k = d.isoformat()
        n, low = info[k]
        ds.append({"d": k, "l": day_label(d, holidays), "k": day_class(d, holidays), "n": n, "p": low,
                   "r": 1 if k in ren else 0, "c": 1 if k in crown else 0,
                   "u": 0 if any(status(state["cells"].get(h["no"], {}).get(k), feat) != "none" for h in rows) else 1,
                   "g": (parse_dt(state.get("fetched", {}).get(k, "")) or now).isoformat(timespec="seconds") if state.get("fetched", {}).get(k) else ""})
    return {"dates": ds, "hotels": hs, "rn": rn, "ut": {k: v for k, v in ut.items() if v}, "def": default.isoformat(),
            "badges": badge_defs(cfg), "lt": LINK, "tm": TABLE_MAX,
            "owls": {"sleepy": owl_html("sleepy")}}


def badges_for(cfg, h, price, day_low, day_n, limit=True):
    """当サイトの基準(config.json の badges)に合うバッジの id を、優先順に最大 badge_max 個返す"""
    try:
        rv = float(h.get("review"))
    except (TypeError, ValueError):
        rv = None
    rc = h.get("reviews") or 0
    out = []
    for b in cfg.get("badges", []):
        if b.get("rule") == "day_cheapest":
            ok = day_low is not None and day_n >= 2 and price == day_low
        else:
            ok = (rv is not None and rv >= b.get("min_review", 0) and rc >= b.get("min_reviews", 0)
                  and b.get("min_price", 0) <= price < b.get("max_price", 10 ** 9))
        if ok:
            out.append(b["id"])
    return out[:cfg.get("badge_max", 2)] if limit else out


def badge_defs(cfg):
    return {b["id"]: {"l": b["label"], "i": b.get("icon", "")} for b in cfg.get("badges", [])}


def badge_html(cfg, ids):
    d = badge_defs(cfg)
    if not ids:
        return ""
    return '<p class="bdg">' + "".join(f'<span class="bd b-{esc(i)}">{esc(d[i]["i"])} {esc(d[i]["l"]).replace("★", "<span class=st>★</span>")}</span>' for i in ids if i in d) + "</p>"


# ---- カード(サーバー側の初期表示。JavaScriptが無くても見える) ---------

def card_html(h, cell, feat, flabel, kind, bd="", flags="", got=None, day=None):
    img = safe_url(h.get("image")) if h.get("image") else "#"
    imgtag = ""
    if img != "#":
        imgtag = f'<a class="himg" href="{esc(safe_url(h.get("url")))}" target="_blank" rel="sponsored nofollow noopener" tabindex="-1"><img src="{esc(img)}" alt="" loading="lazy"></a>'
    rv = ""
    if h.get("review"):
        rv = f'<p class="rv"><span class="star">★</span><b>{esc(h["review"])}</b>'
        if h.get("reviews"):
            rv += f' <span>({h["reviews"]:,}件)</span>'
        rv += "</p>"
    loc = " / ".join(x for x in (h.get("station", ""), h.get("address", "")) if x)
    if kind == "ok":
        p, url, name = cell["f"][feat]
        tag = f'<p class="tags"><span class="tag">{esc(flabel)}</span> <span class="rn">{esc(name)}</span></p>'
        price = (bd + f'<p class="pl">1泊2名・1部屋</p><p class="pv">{yen_full(p)}<span>〜</span></p>'
                 + btn(url or h.get("url"), "cta", (f"{md(day)}({WD[day.weekday()]})の空室を見る ›" if day else "空室・プランを見る ›")))
        cls = "hc"
    else:
        a = cell.get("any") or [0, ""]
        tag = f'<p class="tags"><span class="tag gray">{esc(flabel)}の空きは未確認</span></p>'
        price = (f'<p class="pl">{esc(flabel)}以外も含む最安(参考)</p>' + (f'<p class="pv sm">{yen_full(a[0])}<span>〜</span></p>' if a[0] else "")
                 + btn(a[1] or h.get("plan_url") or h.get("url"), "cta ghost", "プラン一覧を見る ›"))
        cls = "hc part"
    return (f'<article class="{cls}">{imgtag}<div class="hb"><h3>{btn(h.get("url"), "hn", esc(h["name"]))}</h3>'
            f'{rv}<p class="loc">{esc(loc)}</p>{tag}{flags}{('<p class="gt">取得: ' + time_html(got) + '</p>') if got else ''}{('<p class="il"><a href="' + esc(hotel_href(h)) + '">この宿の空室カレンダー・料金 ›</a></p>') if hotel_href(h) else ''}</div><div class="hp">{price}</div></article>')


def results_html(cfg, rows, state, feat, flabel, day, dates, info, holidays, now):
    k = day.isoformat()
    oks, parts, fulls = [], [], []
    for h in rows:
        c = state["cells"].get(h["no"], {}).get(k)
        st = status(c, feat)
        if st == "ok":
            oks.append((c["f"][feat][0], h, c))
        elif st == "part":
            parts.append((h, c))
        elif st == "full":
            fulls.append(h)
    oks.sort(key=lambda x: x[0])
    got_k = parse_dt(state.get("fetched", {}).get(k, ""))
    out = ""
    if oks:
        n_d, low_d = info[k]
        out += "".join(card_html(h, c, feat, flabel, "ok", badge_html(cfg, badges_for(cfg, h, p, low_d, n_d)),
                                 flags_html(False, n_d == 1), got_k, day) for p, h, c in oks)
    else:
        near = [d for d in dates if info[d.isoformat()][0] > 0][:4]
        near.sort(key=lambda d: abs((d - day).days))
        hint = "".join(f'<span class="nb">{esc(day_label(d, holidays))}に空きあり</span>' for d in near[:3])
        out += (f'<div class="none">{owl_html("sleepy")}<div class="nt"><b>この日は「{esc(flabel)}」の空きが見つかりませんでした。</b>'
                f"<p>前後の日付や、空きのある日を選んでみてください。</p>{hint}</div></div>")
    if parts:
        out += (f'<details class="more" data-nosnippet><summary>参考: {esc(flabel)}の空きは確認できない宿({len(parts)}軒・ほかの部屋・プランなら空きあり)</summary>'
                f'<p class="pnote">※この欄の料金は「{esc(flabel)}」ではない部屋も含む、宿全体の最安の目安です。</p>'
                + "".join(card_html(h, c, feat, flabel, "part") for h, c in parts) + "</details>")
    if fulls:
        out += (f'<details class="more"><summary>満室の宿({len(fulls)}軒)</summary><ul class="fl">'
                + "".join(f"<li>{esc(h['name'])}</li>" for h in fulls) + "</ul></details>")
    return out, len(oks)


# ---- スタイル・スクリプト ---------------------------------------------

OWL_SVG = "<path d='M5 9L9 2.5L14 7.5Z M27 9L23 2.5L18 7.5Z' fill='#b9772f'/><ellipse cx='16' cy='18.5' rx='12.5' ry='11.5' fill='#eaa44c'/><ellipse cx='16' cy='22.5' rx='7' ry='6' fill='#fbe6bd'/><circle cx='10.8' cy='15' r='4.6' fill='#fff'/><circle cx='21.2' cy='15' r='4.6' fill='#fff'/><circle cx='11.3' cy='15.5' r='2.3' fill='#3b2a1a'/><circle cx='20.7' cy='15.5' r='2.3' fill='#3b2a1a'/><circle cx='12.1' cy='14.6' r='.9' fill='#fff'/><circle cx='21.5' cy='14.6' r='.9' fill='#fff'/><path d='M14.3 18.6L16 21.4L17.7 18.6Z' fill='#e2672b'/><ellipse cx='7.5' cy='22' rx='1.8' ry='1' fill='#f08a7a' opacity='.6'/><ellipse cx='24.5' cy='22' rx='1.8' ry='1' fill='#f08a7a' opacity='.6'/>"
OWL_URI = 'data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Cpath d=%27M5 9L9 2.5L14 7.5Z M27 9L23 2.5L18 7.5Z%27 fill=%27%23b9772f%27/%3E%3Cellipse cx=%2716%27 cy=%2718.5%27 rx=%2712.5%27 ry=%2711.5%27 fill=%27%23eaa44c%27/%3E%3Cellipse cx=%2716%27 cy=%2722.5%27 rx=%277%27 ry=%276%27 fill=%27%23fbe6bd%27/%3E%3Ccircle cx=%2710.8%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.2%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2711.3%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2720.7%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2712.1%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.5%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Cpath d=%27M14.3 18.6L16 21.4L17.7 18.6Z%27 fill=%27%23e2672b%27/%3E%3Cellipse cx=%277.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08a7a%27 opacity=%27.6%27/%3E%3Cellipse cx=%2724.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08a7a%27 opacity=%27.6%27/%3E%3C/svg%3E'

_EYE_FROM = OWL_SVG.index("<circle cx='11.3'")
_EYE_TO = OWL_SVG.index("<path d='M14.3 18.6")


def owl_inner(mood="normal"):
    """フクロウの表情違い。normal=ふつう / happy=にっこり / sleepy=眠そう"""
    if mood == "normal":
        return OWL_SVG
    st = "stroke='#3b2a1a' stroke-width='1.9' fill='none' stroke-linecap='round'"
    if mood == "happy":
        mid = f"<path d='M7.6 15.8Q10.8 11 14 15.8' {st}/><path d='M18 15.8Q21.2 11 24.4 15.8' {st}/>"
    else:
        mid = (f"<path d='M7.6 14.4Q10.8 18.2 14 14.4' {st}/><path d='M18 14.4Q21.2 18.2 24.4 14.4' {st}/>"
               "<text x='22.5' y='8' font-size='7' font-weight='700' fill='#8a6d3b'>z</text>"
               "<text x='26.5' y='5' font-size='5' font-weight='700' fill='#8a6d3b'>z</text>")
    return OWL_SVG[:_EYE_FROM] + mid + OWL_SVG[_EYE_TO:]


def hero_banner(thin=False):
    """ページ上部のイラスト(山と湯けむり・フクロウ)。写真ではなく図形なので軽い(約2KB)。thin=細い帯(エリア・条件ページ)"""
    st = ('<g fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".85">'
          '<path d="M{x} {y}c-6-8 6-12 0-20s6-12 0-20"/><path d="M{x2} {y}c-6-8 6-12 0-20s6-12 0-20"/><path d="M{x3} {y}c-6-8 6-12 0-20"/></g>')
    # 横長(960×150)で描き、右寄せで切り取る。スマホは右側の390だけ(山・太陽・フクロウ)、パソコンは全体が見える
    steam = (st.format(x=640, y=128, x2=654, x3=668) + st.format(x=820, y=130, x2=834, x3=848)
             + st.format(x=300, y=126, x2=314, x3=328))
    svg = ('<svg viewBox="0 0 960 150" preserveAspectRatio="xMaxYMax slice" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
           '<defs><linearGradient id="hbsky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#cfe6f5"/><stop offset="1" stop-color="#f4f9fc"/></linearGradient></defs>'
           '<rect width="960" height="150" fill="url(#hbsky)"/><circle cx="890" cy="38" r="16" fill="#ffe08a"/>'
           '<path d="M0 120L70 70l60 34 90-52 80 50 60-26 90 46 70-30 50 26v32H0z" fill="#9cc3dd"/>'
           '<path d="M570 118l60-56 40 30 50-52 60 56 40-28 70 56 70-40v66H570z" fill="#9cc3dd"/>'
           '<path d="M220 52l14 13-8-2-6 7-6-7-8 2z" fill="#fff"/><path d="M720 40l18 17-10-3-8 9-8-9-10 3z" fill="#fff"/>'
           '<path d="M0 128l100-28 90 26 110-30 120 34 150 2v18H0z" fill="#6f9fc4"/>'
           '<path d="M570 132l80-36 60 22 70-32 90 40 90-26v50H570z" fill="#6f9fc4"/>'
           '<path d="M0 150v-12c120-14 240-14 360-6s240 8 330 0 180-10 270-6v24z" fill="#1d3557"/>'
           + steam + f'<g transform="translate(900 96) scale(1.4)">{owl_inner("happy")}</g></svg>')
    return f'<div class="hbanner{" thin" if thin else ""}">{svg}</div>'


def owl_html(mood, cls="mood"):
    return f'<svg class="{cls}" viewBox="0 0 32 32" aria-hidden="true">{owl_inner(mood)}</svg>'


def flags_html(new, only):
    out = ""
    if only:
        out += '<span class="fg-only">この日はここだけ</span>'
    return f'<p class="flags">{out}</p>' if out else ""


def share_html(url, text):
    u = urllib.parse.quote(url, safe="")
    t = urllib.parse.quote(text, safe="")
    return ('<div class="share"><b>この空室を送る</b>'
            f'<a class="sh line" href="https://social-plugins.line.me/lineit/share?url={u}" target="_blank" rel="noopener nofollow">LINEで送る</a>'
            f'<a class="sh x" href="https://twitter.com/intent/tweet?url={u}&amp;text={t}" target="_blank" rel="noopener nofollow">Xでポスト</a></div>')


CSS = """.mc{background:#fff;border:1px solid var(--line);border-radius:14px;padding:10px 8px 12px;box-shadow:0 2px 6px rgba(0,0,0,.06)}
.mc-h{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 2px 8px;background:#1f3a5f;color:#fff;border-radius:10px;padding:6px 8px}
.mc-t{font-size:20px;font-weight:900;letter-spacing:.04em}
.mc-nav{font:inherit;font-size:22px;font-weight:900;line-height:1;width:44px;height:40px;border:0;border-radius:8px;background:#fff;color:#1f3a5f;cursor:pointer}
.mc-nav:disabled{opacity:.3;cursor:default}
.mc-w,.mc-g{display:grid;grid-template-columns:repeat(7,1fr);gap:3px}
.mc-w span{text-align:center;font-size:12px;font-weight:700;color:#5b6168;padding:2px 0}
.mc-w .sat{color:#1e62b0}.mc-w .sun{color:#c8102e}
.mc-c{font:inherit;border:2px solid transparent;border-radius:8px;background:#f1f2f4;min-height:62px;padding:3px 0;text-align:center;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;gap:1px;line-height:1.2;color:#555;text-decoration:none;cursor:default}
.mc-c.pad{background:transparent}
.mc-c.off{background:transparent;color:#c3c7cc}
.mc-d{font-size:13px;font-weight:800}
.mc-c.sat .mc-d{color:#1e62b0}.mc-c.sun .mc-d{color:#c8102e}
.mc-l{font-size:11px;font-weight:700;white-space:nowrap}
.mc-l.pr{color:var(--price);font-size:11px}
.mc-c.ok{background:#e7f4ea;color:#256b3a;cursor:pointer}
.mc-c.ok .mc-l{color:#256b3a}.mc-c.ok .mc-l.pr{color:var(--price)}
.mc-c.part{background:#fff1d1;color:#7a5200}
.mc-c.full,.mc-c.zero{background:#eceef2;color:#6b7078}
.mc-c.wait{background:#fff;border-color:#e3e5e8;color:#a0a5ab}
.mc-c.on{border-color:var(--cta);box-shadow:0 0 0 2px rgba(200,56,40,.25)}
.mc-c.ok:hover{border-color:#256b3a}
.mc-note{font-size:12px;color:#5b6168;margin:8px 4px 0;line-height:1.6}
.calbtn{white-space:nowrap;font:inherit;font-size:14px;font-weight:700;border:2px solid #1f3a5f;color:#1f3a5f;background:#fff;border-radius:999px;padding:8px 14px;min-height:40px;cursor:pointer}
.calbtn[aria-expanded=true]{background:#1f3a5f;color:#fff}
.hcal{grid-column:1/-1;margin-top:6px}
.mstack .mh{background:#1f3a5f;color:#fff;font-size:18px;font-weight:900;border-radius:10px;padding:6px 12px;margin:14px 0 8px}
@media (max-width:420px){.mc-c{min-height:58px}.mc-t{font-size:18px}.mc-l{font-size:10.5px}}
.bul{margin:6px 0 4px;padding-left:20px;line-height:1.9}

:root{--ink:#222;--sub:#5b6168;--bg:#f2f2f2;--card:#fff;--line:#dedede;--cta:#d9261c;--cta2:#b81d14;--price:#c8102e;--navy:#1d3557;--ok:#17703f;--okbg:#e3f4ea;--part:#7a4e00;--partbg:#fff1d0;--star:#f2a100;--link:#0a58a8}
*{box-sizing:border-box;margin:0;padding:0}
html{font-size:16px;-webkit-text-size-adjust:100%}
body{font-family:"Zen Maru Gothic","Hiragino Maru Gothic ProN","Hiragino Sans","Noto Sans JP","Yu Gothic",Meiryo,sans-serif;background:var(--bg);color:var(--ink);line-height:1.7;font-size:16px}
a{color:var(--link)}
.hdr{background:#fff;border-bottom:3px solid var(--cta);padding:10px 16px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.logo{font-size:24px;font-weight:900;color:var(--cta);text-decoration:none;letter-spacing:.06em;display:inline-flex;align-items:center;gap:6px}
.logo svg{width:38px;height:38px;flex:none}
.hdr small{font-size:13px;color:var(--sub)}
.disc{background:#fff7dd;border-bottom:1px solid #efd88a;font-size:12px;line-height:1.5;padding:3px 16px;color:#53430a;text-align:right}
.demo{background:#b42318;color:#fff;text-align:center;padding:10px 12px;font-size:14px;font-weight:700}
.wrap{max-width:960px;margin:0 auto;padding:0 12px}
.hero{padding:18px 4px 6px}
.hbanner{margin:0 -12px;height:150px;overflow:hidden;border-radius:0 0 18px 18px;background:#e6f1f8}.hbanner svg{display:block;width:100%;height:100%}
.hbanner.thin{height:84px}
@media (min-width:984px){.hbanner{margin:12px 0 0;border-radius:18px;height:160px}.hbanner.thin{height:110px}}
.trust{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 2px}.trust span{font-size:12.5px;font-weight:700;color:var(--navy);background:#fff;border:1px solid var(--line);border-radius:999px;padding:4px 10px}
.crumb{font-size:13px;color:var(--sub);margin-bottom:6px}.crumb a{color:var(--sub)}
h1{font-size:24px;line-height:1.4;font-weight:800;color:var(--navy);text-wrap:balance}
.lead{font-size:15px;color:#3b4148;margin-top:6px}
.quick{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 10px}
.quick button{font:inherit;font-size:15px;font-weight:700;padding:10px 16px;min-height:44px;border:2px solid var(--cta);color:var(--cta);background:#fff;border-radius:999px;cursor:pointer}
.quick button:active,.quick button.on{background:var(--cta);color:#fff}
.finder{margin-top:6px}
.dstrip{background:transparent}
.dscroll{padding:6px}
.dchip{flex:0 0 100px;scroll-snap-align:start;font:inherit;border:2px solid var(--line);border-radius:12px;background:#fff;text-align:center;padding:7px 2px;cursor:pointer;line-height:1.35;min-height:92px;position:relative}
.dchip .l{display:block;font-size:15px;font-weight:800}
.dchip .n{display:block;font-size:15px;font-weight:700;color:var(--ok)}
.dchip .p{display:block;font-size:17px;font-weight:800;color:var(--price);line-height:1.3}
.dchip.sat .l{color:#1e62b0}.dchip.sun .l{color:#c8102e}
.dchip.zero .n{color:#8a9097;font-weight:500}
.dchip.on{border-color:var(--cta);background:#fff1ef}
.rhead{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;margin:18px 2px 4px}
.rhead h2{font-size:20px;line-height:1.4;color:var(--navy)}
.rhead h2 b{color:var(--cta)}
.sort{font-size:14px;color:var(--sub)}
.sort select{font:inherit;font-size:15px;padding:8px 10px;min-height:42px;border:1px solid #bbb;border-radius:8px;background:#fff;color:var(--ink)}
.rsub{font-size:13px;color:var(--sub);margin:0 2px 10px}
.hc{display:grid;grid-template-columns:112px 1fr;gap:4px 12px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:10px 0}
.hc .himg{grid-row:1;display:block}
.hc img{width:112px;height:112px;object-fit:cover;border-radius:8px;display:block;background:#e9e9e9}
.hc:not(:has(.himg)){grid-template-columns:1fr}
.hb{min-width:0}
.hb h3{font-size:17px;line-height:1.4}
.hb h3 .hn,.hb h3 a{color:var(--ink);font-weight:800;text-decoration:none}
.hb h3 a:hover{text-decoration:underline}
.rv{font-size:15px;margin-top:2px}.rv .star{color:var(--star);margin-right:2px}.rv b{font-size:16px}.rv span{color:var(--sub);font-size:13px}
.gt{font-size:12px;color:var(--sub);margin:2px 0}
.loc{font-size:13px;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tags{margin-top:4px;font-size:13px}
.tag{display:inline-block;background:#fdeceb;color:var(--cta2);border:1px solid #f4b8b3;border-radius:4px;padding:1px 8px;font-weight:700;font-size:13px}
.tag.gray{background:#f1f2f4;color:#555;border-color:#d4d7db}
.rn{color:#444}
.hp{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px 12px;border-top:1px dashed var(--line);padding-top:10px;margin-top:6px}
.pl{font-size:13px;color:var(--sub);width:100%}
.pv{font-size:28px;font-weight:800;color:var(--price);line-height:1.2}.pv span{font-size:15px}
.pv.sm{font-size:22px}
.cta{display:inline-flex;align-items:center;justify-content:center;min-height:48px;padding:10px 22px;border-radius:10px;background:var(--cta);color:#fff;font-size:16px;font-weight:800;text-decoration:none;box-shadow:0 2px 0 var(--cta2)}
.cta:hover{background:var(--cta2)}
.cta.ghost{background:#fff;color:var(--cta);border:2px solid var(--cta);box-shadow:none}
.none{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin:10px 0}
.none p{font-size:14px;color:var(--sub);margin:4px 0 8px}
.nb{display:inline-block;margin:2px 6px 2px 0;background:var(--okbg);color:var(--ok);font-weight:700;border-radius:999px;padding:6px 14px;font-size:14px}
.more{background:#fff;border:1px solid var(--line);border-radius:12px;padding:4px 12px;margin:10px 0}
.more summary{cursor:pointer;font-weight:700;font-size:15px;padding:10px 0;min-height:44px}
.more .hc{box-shadow:none;margin:8px 0}
.fl{padding:0 0 10px 20px;font-size:14px;color:#444}
.sec{margin:26px 0 8px}
.sec h2{font-size:20px;color:var(--navy);margin-bottom:4px}
.sec p.sub{font-size:14px;color:var(--sub);margin-bottom:8px}
.tabs{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.tabs button{font:inherit;font-size:14px;padding:8px 14px;min-height:42px;border:1px solid #bbb;border-radius:999px;background:#fff;color:var(--navy);cursor:pointer}
.tabs button[aria-pressed=true]{background:var(--navy);border-color:var(--navy);color:#fff;font-weight:700}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line);border-radius:12px;background:#fff}
table{border-collapse:separate;border-spacing:0;font-size:14px}
th,td{border-bottom:1px solid var(--line)}
th.hn{position:sticky;left:0;z-index:2;background:#fff;text-align:left;min-width:128px;max-width:128px;padding:8px 8px 8px 10px;border-right:1px solid var(--line);font-size:14px;line-height:1.4}
th.hn a,th.hn span.nm{font-weight:800;text-decoration:none;color:var(--ink)}
thead th small.ft{display:block;font-size:10px;font-weight:400;color:var(--sub);font-style:normal}
th.hn small{display:block;color:var(--sub);font-weight:400;font-size:12px;margin-top:2px}
thead th{background:#efefef;color:var(--navy);font-size:14px;text-align:center;padding:6px 0;min-width:50px;cursor:pointer;line-height:1.35}
thead th.hn{background:#efefef;z-index:3;cursor:default}
thead th i{display:block;font-style:normal;font-weight:400;font-size:12px}
thead th.sat{color:#1e62b0}thead th.sun{color:#c8102e}
thead th.sel{background:#ffe3df}
td.c{text-align:center;min-width:50px;height:54px;padding:0}
td.c a,td.c span{display:flex;flex-direction:column;align-items:center;justify-content:center;height:54px;text-decoration:none;font-weight:800;font-size:17px;line-height:1.15}
td.c i{font-style:normal;font-size:12px;font-weight:700;color:var(--ink)}
td.c.ok,td.c.part,td.c.full{cursor:pointer}
.tmore{text-align:center;margin:10px 0 0}.tmore button{font:inherit;font-weight:800;padding:10px 18px;border-radius:10px;border:1px solid var(--line);background:#fff;cursor:pointer}
td.ok a,td.ok span{background:var(--okbg);color:var(--ok)}
td.part a,td.part span{background:var(--partbg);color:var(--part)}
td.full a,td.full span{color:#7d848c;font-weight:500;font-size:14px}
td.nd span{color:#aab0b7;font-weight:400}
.legend{font-size:13px;color:var(--sub);margin:10px 2px;line-height:1.8}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:6px 16px 10px;margin:22px 0}
.card h2{font-size:18px;margin:10px 0 4px;color:var(--navy)}
details.q{padding:8px 0;border-bottom:1px dashed var(--line)}details.q:last-child{border:0}
details.q summary{cursor:pointer;font-weight:700;font-size:15px;min-height:40px;padding:6px 0}
details.q p{font-size:15px;color:#41474e;margin-top:2px}
footer{border-top:1px solid var(--line);padding:20px 14px 44px;text-align:center;font-size:13px;color:var(--sub);line-height:1.9;background:#fff;margin-top:24px}
.empty{background:#fff6ec;border:1px solid #e3cfa8;border-radius:12px;padding:16px;margin:14px 0;font-size:15px}
.list{display:grid;gap:12px;margin:16px 0}
.list a{display:block;background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;text-decoration:none;color:var(--ink);font-weight:800;font-size:18px}
.list a span{display:block;font-weight:400;font-size:14px;color:var(--sub);margin-top:2px}
.answer{background:#fff;border:1px solid var(--line);border-left:6px solid var(--cta);border-radius:12px;padding:12px 16px 6px;margin:12px 0}
.answer h2{font-size:18px;color:var(--navy);margin-bottom:6px}.answer h2 small{font-size:13px;font-weight:400;color:var(--sub);margin-left:6px}
.answer p{font-size:16px;margin:0 0 8px;color:#222}.answer p.lead2{font-size:17px;font-weight:700;line-height:1.7}
.answer ul.ab{padding-left:20px;margin:0 0 8px;font-size:16px}.answer ul.ab li{margin:2px 0}
.amore{margin:0 0 6px}.amore summary{cursor:pointer;font-size:15px;font-weight:700;color:var(--link);min-height:40px;padding:8px 0}.amore ul.ab{margin-top:2px}
.adet{margin:4px 0 8px}.adet summary{cursor:pointer;font-size:14px;color:var(--link);min-height:36px;padding:6px 0}.adet p{font-size:14px;color:var(--sub);margin:4px 0}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 4px;align-items:center}
.chips b{font-size:14px;color:var(--sub);font-weight:600}
.chips a{display:inline-block;padding:9px 16px;min-height:42px;border:1px solid #bbb;border-radius:999px;background:#fff;color:var(--navy);text-decoration:none;font-weight:700;font-size:15px}
.chips a:hover{background:#eef3fa}
.chips .cur{display:inline-block;padding:9px 16px;min-height:42px;border:1px solid var(--navy);border-radius:999px;background:var(--navy);color:#fff;font-weight:700;font-size:15px}
.sw{margin:10px 0 2px}.sw .chips{margin:6px 0}
.ftabs{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.ftabs button{min-height:46px;padding:10px 18px;border:2px solid var(--navy);border-radius:999px;background:#fff;color:var(--navy);font:inherit;font-weight:700;font-size:16px;cursor:pointer}
.ftabs button[aria-pressed=true]{background:var(--navy);color:#fff}
.finder h2{margin-bottom:4px}.fstep{margin:12px 0 6px;font-weight:700;color:var(--navy);font-size:15px}
.fstep b{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;background:var(--navy);color:#fff;font-size:13px;margin-right:6px}
.fstep small{font-weight:400;color:var(--sub);font-size:12px;margin-left:4px}
.fchips{display:flex;flex-wrap:wrap;gap:6px}
.fchips a{display:inline-flex;align-items:center;min-height:38px;padding:0 12px;border:1px solid #bbb;border-radius:999px;background:#fff;color:var(--navy);font-weight:700;font-size:13.5px;text-decoration:none;white-space:nowrap}
.fchips a.on{background:var(--navy);border-color:var(--navy);color:#fff}
.fgrp{margin:6px 0 10px}.fgrp h3{font-size:13px;color:var(--sub);margin:4px 0 6px;font-weight:400}.fgrp h3 b{color:var(--navy);font-weight:700}
.agrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}@media (min-width:720px){.agrid{grid-template-columns:repeat(6,minmax(0,1fr))}}
.agrid a,.agrid span{display:flex;flex-direction:column;justify-content:flex-start;min-height:52px;overflow:hidden;padding:6px 8px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--navy);text-decoration:none;line-height:1.3}
.agrid a:hover{background:#eef3fa}.agrid b{font-size:15px}.agrid small{font-size:11px;color:var(--sub);font-weight:400}
.agrid .na{background:#f4f4f4;color:#aaa}.agrid .na small{color:#aaa}
.fmore{margin:8px 0 0;font-size:14px}
.achips a{display:inline-flex;flex-direction:column;gap:2px;padding:10px 18px;line-height:1.3}
.achips a small{font-weight:400;font-size:12px;color:var(--sub)}
.il{margin-top:4px;font-size:14px}
.hc{position:relative}.hb h3{padding-right:42px}
.fav{position:absolute;top:6px;right:6px;width:44px;height:44px;border:0;background:transparent;font-size:27px;line-height:1;color:#c7a3b1;cursor:pointer;border-radius:50%}
.fav.on{color:#e0457b}
.favtog{font:inherit;font-size:14px;padding:8px 14px;min-height:42px;border:1px solid #e3a5bb;border-radius:999px;background:#fff;color:#b6325f;cursor:pointer;font-weight:700}
.favtog.on{background:#ffeef4}
.bdnote{width:100%;font-size:12px;color:var(--sub);margin:0 0 2px}
.rctl{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.snum{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 14px 4px;margin:12px 0}
.snum h2{font-size:17px;color:var(--navy);margin-bottom:8px}.snum h2 small{display:block;font-size:12px;font-weight:400;color:var(--sub);margin-top:2px}
.stiles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-bottom:8px}@media (min-width:720px){.stiles{grid-template-columns:repeat(4,minmax(0,1fr))}}
.stile{display:flex;flex-direction:column;align-items:flex-start;padding:10px 12px;border-radius:10px;background:#f6f9fd;border:1px solid #dbe5f1;color:var(--navy);text-decoration:none}
.stile:hover{background:#eaf1fa}.stile .sl{font-size:13px;font-weight:700}.stile .sd{font-size:13px;color:#3b4148;line-height:1.25}.stile .sd b{font-size:26px;color:var(--cta);margin:0 2px;font-weight:800}
.stile .sp{font-size:11px;color:var(--sub);margin-top:2px}.snum .amore summary{font-size:13px}
.agrid img{display:block;width:calc(100% + 16px);height:44px;object-fit:cover;margin:-6px -8px 5px;border-radius:9px 9px 0 0;background:#e9edf2}
@media (min-width:720px){.agrid img{height:60px}}.agrid .na img{filter:grayscale(1);opacity:.5}
.picks{margin:16px 0}.picks .sub{font-size:12px;color:var(--sub);margin-top:8px}.picks h2{font-size:18px;color:var(--navy);margin-bottom:8px}
.pgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}@media (min-width:720px){.pgrid{grid-template-columns:repeat(3,minmax(0,1fr))}}
.pcard{display:flex;flex-direction:column;background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden;color:var(--ink);text-decoration:none}
.pcard:hover{box-shadow:0 2px 10px rgba(0,0,0,.08)}.pcard img{width:100%;aspect-ratio:4/3;object-fit:cover;background:#e9edf2}
.pcard .pb{display:flex;flex-direction:column;gap:2px;padding:8px 10px 10px}.pcard .pa{font-size:11px;color:#fff;background:var(--navy);border-radius:4px;padding:1px 6px;align-self:flex-start}
.pcard b{font-size:14px;line-height:1.35;color:var(--navy)}.pcard .pv{font-size:12.5px;color:#333}.pcard .pv small{color:var(--sub);margin-left:2px}
.pcard .pp{font-size:12px;color:#3b4148}.pcard .pp em{font-style:normal;font-weight:800;color:var(--price);font-size:15px}.pcard .pp small{color:var(--sub)}
table.stat tr.sub th,table.stat tr.sub td{font-size:12.5px;color:#4a5058;background:#fafbfc}
.seenr{font-size:13px;color:#3b4148;margin:4px 0 6px}
.adh{margin:8px 0 2px;font-size:14px}.adl{margin:0 0 6px}
#basics th{white-space:nowrap;width:6.5em}#basics td{text-align:left}
.qlinks{font-size:13px;margin:6px 0 0;line-height:1.7}.qlinks .ql{font-weight:700}
.rtbl .tb{display:inline-block;white-space:nowrap;font-size:12px;font-weight:700;color:#fff;background:var(--cta);border-radius:6px;padding:3px 8px;text-decoration:none}
@media (max-width:640px){.rtbl td:nth-child(6){grid-column:1/-1;margin-top:4px}}
.pcard .pin{display:flex;flex-direction:column;color:var(--ink);text-decoration:none;flex:1}
.pcard .pbtn{display:block;margin:0 10px 10px;padding:8px 6px;border-radius:8px;background:var(--cta);color:#fff;font-weight:700;font-size:12.5px;text-align:center;text-decoration:none;line-height:1.3}
.pcard .pbtn:hover{background:var(--cta2)}
.adnote{margin:0;padding:3px max(16px,env(safe-area-inset-right,0px)) 3px max(16px,env(safe-area-inset-left,0px));font-size:11px;color:var(--sub);text-align:right}
.afind{margin:2px 0 8px}.afind input{width:100%;box-sizing:border-box;min-height:42px;padding:8px 12px;border:1px solid #bbb;border-radius:10px;font-size:15px;background:#fff;color:var(--ink)}
.rchips{display:flex;gap:6px;overflow-x:auto;padding:8px 0 2px;scrollbar-width:none}.rchips::-webkit-scrollbar{display:none}
.rchips button{flex:none;min-height:34px;padding:0 12px;border:1px solid #ccc;border-radius:999px;background:#fff;color:var(--navy);font-weight:700;font-size:13px;cursor:pointer}
.rchips button.on{background:var(--navy);border-color:var(--navy);color:#fff}.anone{font-size:13px;color:var(--sub)}
.rg h4{font-size:13px;color:var(--navy);margin:10px 0 6px;padding-left:6px;border-left:3px solid var(--cta)}
.chips .rgl{flex-basis:100%;font-size:11.5px;color:var(--sub);margin-top:4px}
[hidden]{display:none!important}
.rdef{font-size:12.5px;color:#3b4148;background:#f6f9fd;border-radius:8px;padding:6px 10px;margin:4px 0 6px}
.hero .qans{font-size:13px;line-height:1.6;color:#2a3138;background:#fff;border:1px solid var(--line);border-left:4px solid var(--ok);border-radius:8px;padding:8px 10px;margin-top:8px}
.rtab{margin:0 0 12px}.rtbl{border-collapse:collapse;font-size:13px;width:100%}.rtbl caption{text-align:left;font-size:12px;color:var(--sub);padding:4px 0}
.rtbl th,.rtbl td{border-bottom:1px solid var(--line);padding:6px 6px;text-align:left;vertical-align:top}.rtbl td.pr{white-space:nowrap;font-weight:700;color:var(--price)}.rtbl td:first-child,.rtbl td:last-child{white-space:nowrap}
.rtbl caption{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
@media (max-width:640px){.rtbl thead{display:none}.rtbl,.rtbl tbody{display:block}.rtbl tr{display:grid;grid-template-columns:auto 1fr auto;column-gap:8px;border-bottom:1px solid var(--line);padding:7px 2px}
.rtbl td{border:0;padding:0}.rtbl td:nth-child(3){grid-column:1/-1;font-size:12.5px;color:#3b4148;margin-top:2px}.rtbl td:nth-child(4){grid-column:3;grid-row:1}.rtbl td:nth-child(5){grid-column:1/-1;font-size:11.5px;color:var(--sub)}.rtbl td:nth-child(5)::before{content:"取得 "}}
.more .pnote{margin:6px 2px 8px;font-size:12.5px;color:var(--sub,#666)}
.qbook .qkeep{margin:6px 0 0;font-size:13.5px;font-weight:700;color:var(--navy)}
.rkeep{margin:2px 0 8px;font-size:14px;font-weight:700;color:var(--navy);background:#eef3fa;border-radius:10px;padding:8px 10px}
.rctl .favtog{min-height:38px;padding:4px 12px;font-size:13px}.rctl .sort{font-size:13px}.rctl .sort select{min-height:38px;font-size:14px;padding:4px 6px;max-width:10em}
.flags{margin-top:4px;display:flex;flex-wrap:wrap;gap:6px;font-size:13px}
.fg-only{background:#fff7dd;color:#7a4e00;border:1px solid #efd88a;border-radius:4px;padding:1px 8px;font-weight:700}
.dchip .mk{position:absolute;top:-9px;left:50%;transform:translateX(-50%);white-space:nowrap;font-size:11px;font-weight:800;background:#d9261c;color:#fff;border-radius:999px;padding:2px 8px;line-height:1.4}
.dchip .n.last{color:#c8102e}
.none{display:flex;gap:12px;align-items:center}.none .mood{width:64px;height:64px;flex:none}.none .nt{min-width:0}
.answer{position:relative}.answer .mood{position:absolute;right:12px;top:10px;width:46px;height:46px}.answer h2{padding-right:58px}
.share{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0}.share b{font-size:14px;color:var(--sub)}
.sh{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:8px 18px;border-radius:999px;color:#fff;font-weight:800;font-size:15px;text-decoration:none}
.sh.line{background:#058a3c}.sh.x{background:#111}
.bdg{width:100%;display:flex;flex-wrap:wrap;gap:6px;margin:0 0 2px}
.bd{display:inline-flex;align-items:center;gap:6px;font-size:14px;font-weight:800;line-height:1;padding:4px 12px 4px 6px;border-radius:999px;background:#fff1c9;color:#7a4e00;border:1px solid #f0c75e}
.bd.b-ichido{background:#ffeef4;color:#b6325f;border-color:#f6b3c8}
.bd .st{color:#f2a100;margin-left:1px}
.bd.b-many{background:#e4f6e8;color:#17703f;border-color:#9bd3ab}
.bd::before{content:"";width:28px;height:28px;flex:none;background:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Cpath d=%27M5 9L9 2.5L14 7.5Z M27 9L23 2.5L18 7.5Z%27 fill=%27%23b9772f%27/%3E%3Cellipse cx=%2716%27 cy=%2718.5%27 rx=%2712.5%27 ry=%2711.5%27 fill=%27%23eaa44c%27/%3E%3Cellipse cx=%2716%27 cy=%2722.5%27 rx=%277%27 ry=%276%27 fill=%27%23fbe6bd%27/%3E%3Ccircle cx=%2710.8%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.2%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2711.3%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2720.7%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2712.1%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.5%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Cpath d=%27M14.3 18.6L16 21.4L17.7 18.6Z%27 fill=%27%23e2672b%27/%3E%3Cellipse cx=%277.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08a7a%27 opacity=%27.6%27/%3E%3Cellipse cx=%2724.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08a7a%27 opacity=%27.6%27/%3E%3C/svg%3E") center/contain no-repeat}
.bd.b-ichido::before{background-image:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Cpath d=%27M5 9L9 2.5L14 7.5Z M27 9L23 2.5L18 7.5Z%27 fill=%27%23e0709b%27/%3E%3Cellipse cx=%2716%27 cy=%2718.5%27 rx=%2712.5%27 ry=%2711.5%27 fill=%27%23f8a9c4%27/%3E%3Cellipse cx=%2716%27 cy=%2722.5%27 rx=%277%27 ry=%276%27 fill=%27%23fff0f5%27/%3E%3Ccircle cx=%2710.8%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.2%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2711.3%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2720.7%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2712.1%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.5%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Cpath d=%27M14.3 18.6L16 21.4L17.7 18.6Z%27 fill=%27%23e2672b%27/%3E%3Cellipse cx=%277.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08aa8%27 opacity=%27.6%27/%3E%3Cellipse cx=%2724.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f08aa8%27 opacity=%27.6%27/%3E%3C/svg%3E")}
.bd.b-many::before{background-image:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Cpath d=%27M5 9L9 2.5L14 7.5Z M27 9L23 2.5L18 7.5Z%27 fill=%27%233f9a5a%27/%3E%3Cellipse cx=%2716%27 cy=%2718.5%27 rx=%2712.5%27 ry=%2711.5%27 fill=%27%237fcf8f%27/%3E%3Cellipse cx=%2716%27 cy=%2722.5%27 rx=%277%27 ry=%276%27 fill=%27%23e9f8ec%27/%3E%3Ccircle cx=%2710.8%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.2%27 cy=%2715%27 r=%274.6%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2711.3%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2720.7%27 cy=%2715.5%27 r=%272.3%27 fill=%27%233b2a1a%27/%3E%3Ccircle cx=%2712.1%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Ccircle cx=%2721.5%27 cy=%2714.6%27 r=%27.9%27 fill=%27%23fff%27/%3E%3Cpath d=%27M14.3 18.6L16 21.4L17.7 18.6Z%27 fill=%27%23e2672b%27/%3E%3Cellipse cx=%277.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f4a0a0%27 opacity=%27.6%27/%3E%3Cellipse cx=%2724.5%27 cy=%2722%27 rx=%271.8%27 ry=%271%27 fill=%27%23f4a0a0%27 opacity=%27.6%27/%3E%3C/svg%3E")}
.facts{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;font-size:15px;margin:6px 0 10px}
.facts dt{color:var(--sub);font-weight:700}.facts dd{margin:0}
.dsec{background:#fff;border:1px solid var(--line);border-radius:12px;padding:6px 14px 10px;margin:14px 0}
.dsec h2{font-size:19px;color:var(--navy);margin:8px 0 2px}.dsec h2 b{color:var(--cta)}
.dsec .meta{font-size:13px;color:var(--sub);margin-bottom:6px}
.rowl{list-style:none;padding:0}
.rowl li{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:6px 12px;padding:10px 0;border-top:1px dashed var(--line)}
.rowl li:first-child{border-top:0}
.rowl .t{min-width:0;flex:1 1 220px}.rowl .t a.n,.rowl .t .n{font-weight:800;color:var(--ink);text-decoration:none;font-size:16px}
.rowl .t small{display:block;color:var(--sub);font-size:13px;line-height:1.5}
.rowl .pr{font-size:20px;font-weight:800;color:var(--price);white-space:nowrap}.rowl .pr span{font-size:13px}
.rowl .cta{min-height:44px;padding:8px 16px;font-size:15px}
.mini{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin:8px 0}
.mini .h{font-size:12px;color:var(--sub);text-align:center}
.mini a,.mini span{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:56px;border-radius:8px;text-decoration:none;font-size:13px;line-height:1.25;background:#f4f4f4;color:#888}
.mini b{font-size:13px;font-weight:700}.mini i{font-style:normal;font-size:16px;font-weight:800}
.mini .ok{background:var(--okbg);color:var(--ok)}.mini .part{background:var(--partbg);color:var(--part)}.mini .full{background:#eceff2;color:#6b7280}
.mini .pad{background:transparent}
.hhero{padding-top:12px}.hhero .crumb{font-size:12px;margin-bottom:4px}.hhero h1{font-size:21px}.hhero .lead{font-size:13.5px;margin-top:2px}
.hhead{display:flex;gap:10px;align-items:flex-start}.hthumb{flex:none;width:96px;height:72px;object-fit:cover;border-radius:8px;background:#eee;margin-top:4px}
.hthumb,.hphoto{font-size:0;color:transparent}.hphoto{display:block;width:100%;max-height:210px;object-fit:cover;border-radius:12px;margin:4px 0 8px;background:#eee}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.dpick .qbook{margin-top:4px}.dchg{margin:8px 0 0}.dlab{display:block;font-size:13px;font-weight:700;color:var(--sub);margin:0 0 2px}.dchg .quick{margin:2px 0 6px}
.dchg .quick button{min-height:40px;padding:6px 14px;font-size:14px}
.hcal{margin-top:6px}.hcal .sub{font-size:12.5px}
.fyes{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 8px}.fyes a,.fyes span{display:inline-flex;align-items:center;min-height:34px;padding:0 12px;border-radius:999px;background:var(--okbg);color:var(--ok);font-weight:700;font-size:13.5px;text-decoration:none}
#features details summary{cursor:pointer;color:var(--sub);font-size:14px;margin:4px 0}
.mini button{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:56px;border-radius:8px;border:0;font:inherit;font-size:13px;line-height:1.25;background:#f4f4f4;color:#888;cursor:pointer;padding:0}
.iosn{display:flex;gap:8px;align-items:flex-start;background:#fff8e1;border:1px solid #f0c36d;border-radius:10px;padding:10px 12px;margin:10px 0;font-size:14px;line-height:1.55}.iosb{flex:1}.iosn p{margin:0}.iosn .iclose{flex:none;border:0;background:transparent;font-size:20px;line-height:1;color:#8a6d3b;cursor:pointer;padding:0 4px}
.iabb{margin:8px 0 0}.iabtn{width:100%;min-height:46px;font-size:16px;background:#0a58a8;box-shadow:0 2px 0 #08467f}.ifb{margin:0 0 6px;font-size:13.5px}.icopy{font:inherit;font-size:14px;font-weight:700;border:2px solid #0a58a8;color:#0a58a8;background:#fff;border-radius:10px;min-height:42px;padding:6px 12px;cursor:pointer;width:100%}
.iosn2{background:#fff8e1;border:1px solid #f0c36d;border-radius:10px;padding:8px 10px;margin:0 0 8px;font-size:13.5px;line-height:1.55}.iosn2 p{margin:0}
.qalt{margin:8px 0 0;padding-top:6px;border-top:1px solid var(--line)}.qalt .qm{margin:0 0 2px}
.sbar{display:none}
@media (max-width:760px){.sbar{position:fixed;left:0;right:0;bottom:0;z-index:50;display:flex;align-items:center;justify-content:space-between;gap:10px;background:#fff;border-top:1px solid var(--line);box-shadow:0 -2px 10px rgba(0,0,0,.08);padding:8px 12px calc(8px + env(safe-area-inset-bottom))}.sbar[hidden],.sbar.off{display:none}.sbar .st{min-width:0;font-size:13px;line-height:1.35}.sbar .st b{display:block;font-size:15px;color:var(--navy)}.sbar .st small{color:var(--sub)}.sbar .cta{flex:none;padding:10px 14px;font-size:15px;margin:0}body.hasbar{padding-bottom:76px}}
.hl{list-style:none;padding:0}
.hl li{padding:10px 0;border-top:1px dashed var(--line);font-size:15px}
.hl li:first-child{border-top:0}.hl a{font-weight:800}.hl small{display:block;color:var(--sub);font-size:13px}
.picks b.ok{color:var(--ok)}
.qbook{background:#fff;border:2px solid var(--cta);border-radius:12px;padding:10px 12px;margin:8px 0 4px}
.bookpanel{margin:8px 0 12px}.bookpanel h2{font-size:18px;color:var(--navy);margin:0 0 6px}.bookpanel .quick{margin:4px 0 8px}
.dinput{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;max-width:440px;margin:2px 0 8px;padding:6px 8px 6px 14px;min-height:52px;background:#fff;border:2px solid var(--navy);border-radius:999px;cursor:pointer;box-sizing:border-box}
.dinput .dlab2{flex:none;font-size:15px;font-weight:800;color:var(--navy)}
.dinput input{flex:1;min-width:0;font:inherit;font-size:16px;font-weight:700;color:var(--navy);padding:6px 10px;border:0;border-radius:999px;background:#eef3fa;min-height:38px;text-align:center}
.qbook .qh{font-weight:700;color:var(--navy);margin:0 0 6px;font-size:15px}.qbook .qm{margin:6px 0 0;font-size:13px;color:var(--sub)}
.qbook .qr{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 0;border-top:1px dashed var(--line)}
.qbook .qn{min-width:0;font-weight:700}.qbook .qn a{color:var(--ink)}.qbook .qn small{display:block;font-weight:400;color:var(--price)}
.qbook .qcta{flex:none;padding:10px 14px;font-size:15px}
.qbook .qimg{flex:none;width:64px;height:64px;object-fit:cover;border-radius:8px;background:#eee}.qbook .qn{flex:1}
.rowl .limg,.hl .limg{flex:none;width:72px;height:72px;object-fit:cover;border-radius:8px;background:#eee}
.hl li.wimg{display:flex;gap:10px;align-items:flex-start}
.hc>.il{grid-column:1/-1;margin:6px 0 0}
@media (min-width:720px){.hc>.il{grid-column:2;grid-row:2}}
.mini .sel{outline:3px solid var(--cta);outline-offset:-2px;border-radius:6px}
.stat{border-collapse:collapse;font-size:14px;margin:8px 0;width:100%}
.stat th,.stat td{border:1px solid var(--line);padding:6px 8px;text-align:center;background:#fff}
.stat thead th{background:#efefef;position:static;cursor:default;min-width:0;font-size:13px}
.prose{background:#fff;border:1px solid var(--line);border-radius:12px;padding:8px 18px 16px;margin:14px 0}
.prose h2{font-size:19px;color:var(--navy);margin:18px 0 6px}.prose p,.prose li{font-size:16px;margin:6px 0}
.prose ul{padding-left:22px}
.prose table{margin:8px 0}.prose th,.prose td{padding:6px 10px;text-align:left;font-size:15px;background:#fff;border:1px solid var(--line)}
.fnav{margin-bottom:8px;font-size:14px}
.note{background:#fff7dd;border:1px solid #efd88a;border-radius:10px;padding:10px 14px;font-size:14px;margin:10px 0}
@media(min-width:720px){
  h1{font-size:28px}
  .hc{grid-template-columns:168px 1fr 230px;padding:14px;gap:0 16px}
  .hc img{width:168px;height:126px}
  .hc .hp{grid-column:3;grid-row:1;border-top:0;border-left:1px dashed var(--line);padding:0 0 0 16px;margin:0;flex-direction:column;align-items:flex-end;justify-content:center;text-align:right}
  .hc:not(:has(.himg)){grid-template-columns:1fr 230px}
  .hc:not(:has(.himg)) .hp{grid-column:2}
  .pl{text-align:right}
  .hc .bdg{justify-content:flex-end}
  .cta{width:100%}
}
"""

JS = r"""
(function(){
var D=window.__V;if(!D||!D.hotels.length)return;
// 軽くして送ったデータを、元の形(h.c[日付], h.u など)に戻す
(function(){var ut=D.ut||{},rn=D.rn||[];D.hotels.forEach(function(h){
  ['u','pu','im'].forEach(function(k){if(h[k]===undefined)h[k]=ut[k]?ut[k][0]+h.no+ut[k][1]:'';});
  if(h.cc){var c={};h.cc.forEach(function(v,i){if(!v||!D.dates[i])return;var d=D.dates[i].d;
    if(v==='f')c[d]=['f'];else if(v[0]==='p')c[d]=['p',v[1],v[2]||''];else c[d]=['o',v[1],v[3]||'',rn[v[2]],v[4]||[],v[5]||[]];});
   h.c=c;delete h.cc;}});})();
var F=D.feat,FL=D.flabel;
var cards=document.getElementById('cards'),rt=document.getElementById('rt'),strip=document.getElementById('dscroll'),
    sortSel=document.getElementById('sortsel'),finder=document.getElementById('finder');
var cur=D.def,mode='p',favOnly=false,filterId='',FK='yadomikke_fav',fav={};
// アドレスで日付を指定して開ける(?d=2026-11-28 または #2026-11-28)。AIや共有のリンクから、その日付で開くため
var urlDate=(function(){var q=null;try{q=new URLSearchParams(location.search).get('d');}catch(e){}
  if(!q&&/^#\d{4}-\d{2}-\d{2}$/.test(location.hash))q=location.hash.slice(1);
  return q&&D.dates.some(function(x){return x.d===q;})?q:null;})();
if(urlDate)cur=urlDate;
try{var raw=localStorage.getItem(FK);if(raw){JSON.parse(raw).forEach(function(n){fav[n]=1;});}}catch(e){}
function saveFav(){try{localStorage.setItem(FK,JSON.stringify(Object.keys(fav)));}catch(e){}}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e;}
function yen(p){return '¥'+String(p).replace(/\B(?=(\d{3})+(?!\d))/g,',');}
function btn(url,cls,text){
  if(!url||url==='#'){return el('span',cls,text);}
  var a=el('a',cls,text);a.href=url;a.target='_blank';a.rel='sponsored nofollow noopener';return a;}
function sl(no,iso){var L=D.lt,p=iso.split('-'),y=+p[0],m=+p[1],d=+p[2],b=new Date(Date.UTC(y,m-1,d)+L.n*864e5);
  var t='https://hotel.travel.rakuten.co.jp/hotelinfo/plan/'+no+'?f_teikei=&f_nen1='+y+'&f_tuki1='+m+'&f_hi1='+d+'&f_nen2='+b.getUTCFullYear()+'&f_tuki2='+(b.getUTCMonth()+1)+'&f_hi2='+b.getUTCDate()+'&f_heya_su=1&f_otona_su='+L.ad+'&f_s1=0&f_s2=0&f_y1=0&f_y2=0&f_y3=0&f_y4=0';
  if(!L.a)return t;var q=encodeURIComponent(t);return 'https://hb.afl.rakuten.co.jp/hgc/'+L.a+'/?pc='+q+'&m='+q;}
function lk(h,iso,c){if(!c||c[0]==='f')return h.u;if(c[2])return c[2];if(D.lt)return sl(h.no,iso);return c[0]==='p'?h.pu:h.u;}
function dinfo(d){for(var i=0;i<D.dates.length;i++){if(D.dates[i].d===d)return D.dates[i];}return null;}
function mcal(ids,cellFn){
  var root=el('div','mc'),ms=[],map={},mi=0;
  ids.forEach(function(d){var m=d.slice(0,7);if(ms.indexOf(m)<0)ms.push(m);map[d]=1;});
  var head=el('div','mc-h'),prev=el('button','mc-nav','‹'),next=el('button','mc-nav','›'),ttl=el('div','mc-t');
  prev.type=next.type='button';prev.setAttribute('aria-label','前の月');next.setAttribute('aria-label','次の月');
  head.appendChild(prev);head.appendChild(ttl);head.appendChild(next);root.appendChild(head);
  var wk=el('div','mc-w');'月火水木金土日'.split('').forEach(function(w,i){wk.appendChild(el('span',i===5?'sat':(i===6?'sun':''),w));});
  var grid=el('div','mc-g');root.appendChild(wk);root.appendChild(grid);
  function two(n){return n<10?'0'+n:''+n;}
  function draw(){
    var ym=ms[mi].split('-'),y=+ym[0],m=+ym[1];
    ttl.textContent=y+'年'+m+'月';prev.disabled=(mi===0);next.disabled=(mi===ms.length-1);
    grid.textContent='';
    var pad=(new Date(y,m-1,1).getDay()+6)%7,nd=new Date(y,m,0).getDate(),i;
    for(i=0;i<pad;i++){grid.appendChild(el('span','mc-c pad'));}
    for(var d=1;d<=nd;d++){
      var iso=ms[mi]+'-'+two(d),dow=(pad+d-1)%7,wc=dow===5?' sat':(dow===6?' sun':''),inf=map[iso]?cellFn(iso):null,c;
      if(!inf){c=el('span','mc-c off'+wc);c.appendChild(el('span','mc-d',String(d)));grid.appendChild(c);continue;}
      if(inf.href){c=btn(inf.href,'mc-c '+inf.cls+wc,'');}
      else if(inf.click){c=el('button','mc-c '+inf.cls+wc);c.type='button';c.addEventListener('click',inf.click);}
      else{c=el('span','mc-c '+inf.cls+wc);}
      c.textContent='';c.appendChild(el('span','mc-d',(inf.mark||'')+d));
      inf.lines.forEach(function(t){var ln=el('span','mc-l'+(t.charAt(0)==='¥'||/万/.test(t)?' pr':''),t);c.appendChild(ln);});
      if(inf.label){c.setAttribute('aria-label',inf.label);}
      grid.appendChild(c);
    }
  }
  prev.addEventListener('click',function(){if(mi>0){mi--;draw();}});
  next.addEventListener('click',function(){if(mi<ms.length-1){mi++;draw();}});
  root.refresh=draw;
  root.goto=function(iso){var k=ms.indexOf(iso.slice(0,7));if(k>=0&&k!==mi){mi=k;}draw();};
  draw();return root;}
function shortYen(p){return (p>=10000?(Math.round(p/1000)/10)+'万':String(p).replace(/\B(?=(\d{3})+(?!\d))/g,','))+'〜';}

function card(h,c,kind){
  var art=el('article',kind==='o'?'hc':'hc part');
  if(h.im){var a=btn(h.u,'himg','');if(a.tagName==='A'){a.tabIndex=-1;}var im=el('img');im.src=h.im;im.alt='';im.loading='lazy';a.appendChild(im);art.appendChild(a);}
  var b=el('div','hb'),h3=el('h3');h3.appendChild(btn(h.u,'hn',h.n));b.appendChild(h3);
  if(h.rv){var rv=el('p','rv');rv.appendChild(el('span','star','★'));rv.appendChild(el('b',null,String(h.rv)));
    if(h.rc){rv.appendChild(document.createTextNode(' '));rv.appendChild(el('span',null,'('+h.rc.toLocaleString()+'件)'));}b.appendChild(rv);}
  var loc=[h.st,h.ad].filter(Boolean).join(' / ');b.appendChild(el('p','loc',loc));
  var di0=dinfo(cur);if(di0&&di0.g){var gt=el('p','gt','取得: ');var tm=el('time','rel');tm.setAttribute('datetime',di0.g);var gd=new Date(di0.g);tm.textContent=(gd.getMonth()+1)+'/'+gd.getDate()+' '+('0'+gd.getHours()).slice(-2)+':'+('0'+gd.getMinutes()).slice(-2);gt.appendChild(tm);b.appendChild(gt);}
  var tg=el('p','tags');
  if(kind==='o'){tg.appendChild(el('span','tag',FL));tg.appendChild(document.createTextNode(' '));tg.appendChild(el('span','rn',c[3]));}
  else{tg.appendChild(el('span','tag gray',FL+'の空きは未確認'));}
  b.appendChild(tg);
  if(kind==='o'){var fg=el('p','flags');
    var di0=dinfo(cur);if(di0&&di0.n===1){fg.appendChild(el('span','fg-only','この日はここだけ'));}
    if(fg.childNodes.length){b.appendChild(fg);}}
  var il=el('p','il');
  var cb=el('button','calbtn','📅 空室カレンダー');cb.type='button';cb.setAttribute('aria-expanded','false');
  cb.addEventListener('click',function(){
    var open=cb.getAttribute('aria-expanded')==='true';
    if(!art._cal){var box=el('div','hcal');
      var cal=mcal(D.dates.map(function(x){return x.d;}),function(iso){var cc=h.c[iso];
        if(!cc)return {cls:'wait',lines:['—'],label:iso+' 未取得'};
        if(cc[0]==='o')return {cls:'ok',lines:['◯',shortYen(cc[1])],href:lk(h,iso,cc),label:iso+' 空きあり'};
        if(cc[0]==='p')return {cls:'part',lines:['△'],label:iso+' 対象の部屋は未確認'};
        return {cls:'full',lines:['満'],label:iso+' 満室'};});
      cal.goto(cur);box.appendChild(cal);
      box.appendChild(el('p','mc-note','◯=空きあり(数字は最安) ／ △=宿に空きはあるが該当の部屋は確認できない ／ 満=空室なし ／ —=未取得(満室ではありません)。◯を押すと、その日の楽天トラベルのページが開きます。'));
      art._cal=box;art.appendChild(box);}
    art._cal.hidden=open;cb.setAttribute('aria-expanded',open?'false':'true');
    if(!open){var cc2=art._cal.firstChild;if(cc2&&cc2.goto)cc2.goto(cur);}});
  il.appendChild(cb);
  if(h.hp){il.appendChild(document.createTextNode('　'));var ia=el('a',null,'宿の詳細ページ ›');ia.href=h.hp;il.appendChild(ia);}
  art.appendChild(b);
  var hp=el('div','hp');
  if(kind==='o'&&c[4]&&c[4].length){var bg=el('p','bdg');
    c[4].forEach(function(id){var m=D.badges[id];if(m){var sp=el('span','bd b-'+id),parts=((m.i?m.i+' ':'')+m.l).split('★');
      parts.forEach(function(t,k){if(k>0){sp.appendChild(el('span','st','★'));}sp.appendChild(document.createTextNode(t));});bg.appendChild(sp);}});hp.appendChild(bg);}
  if(kind==='o'&&filterId&&c[4].indexOf(filterId)<0&&c[5]&&c[5].indexOf(filterId)>=0&&D.badges[filterId]){hp.appendChild(el('p','bdnote','「'+D.badges[filterId].l.replace('★','')+'」の条件にも当てはまります'));}
  if(kind==='o'){hp.appendChild(el('p','pl','1泊2名・1部屋'));
    var pv=el('p','pv',yen(c[1]));pv.appendChild(el('span',null,'〜'));hp.appendChild(pv);
    var _di=dinfo(cur);hp.appendChild(btn(lk(h,cur,c),'cta',_di?_di.l.replace(/\((.).*\)$/,'($1)')+'の空室を見る ›':'空室・プランを見る ›'));}
  else{hp.appendChild(el('p','pl',FL+'以外も含む最安(参考)'));
    if(c[1]){var pv2=el('p','pv sm',yen(c[1]));pv2.appendChild(el('span',null,'〜'));hp.appendChild(pv2);}
    hp.appendChild(btn(lk(h,cur,c),'cta ghost','プラン一覧を見る ›'));}
  art.appendChild(hp);
  art.appendChild(il);   // 空室カレンダー・宿の詳細は、予約ボタンのあとに置く(予約ボタンを先に見せる)
  var on=!!fav[h.no],fb=el('button','fav'+(on?' on':''),on?'♥':'♡');fb.type='button';
  fb.setAttribute('aria-pressed',on?'true':'false');fb.setAttribute('aria-label',h.n+'をお気に入りに入れる');
  fb.addEventListener('click',function(){if(fav[h.no]){delete fav[h.no];}else{fav[h.no]=1;}saveFav();
    if(favOnly){render();}else{var o=!!fav[h.no];fb.classList.toggle('on',o);fb.textContent=o?'♥':'♡';fb.setAttribute('aria-pressed',o?'true':'false');}updFav();});
  art.appendChild(fb);return art;}
function render(){
  var oks=[],parts=[],fulls=[],i,h,c;
  for(i=0;i<D.hotels.length;i++){h=D.hotels[i];c=h.c[cur];if(!c)continue;if(favOnly&&!fav[h.no])continue;
    if(filterId&&!(c[0]==='o'&&c[5]&&c[5].indexOf(filterId)>=0))continue;
    if(c[0]==='o')oks.push([h,c]);else if(c[0]==='p')parts.push([h,c]);else fulls.push(h);}
  if(mode==='p'){oks.sort(function(a,b){return a[1][1]-b[1][1];});}
  else{oks.sort(function(a,b){return (b[0].rv||0)-(a[0].rv||0);});}
  var di=dinfo(cur);
  var rk=document.getElementById('rkeep');
  if(rk){var ix=-1;for(var j=0;j<D.dates.length;j++){if(D.dates[j].d===cur){ix=j;break;}}var kv=(D.kp&&ix>0)?D.kp[ix]:null;
    if(kv&&oks.length){rk.textContent='📊 今空いている宿のうち、'+di.l.replace(/\(.*$/,'')+'まで空いたまま残るのは約'+kv+'%の見込み(当サイトの観測)。'+(kv<40?'早めの予約がおすすめです':(kv>=70?'まだ少し余裕がありそうです':'数日のうちに埋まる宿も出てきそうです'));rk.hidden=false;}else{rk.hidden=true;}}
  rt.textContent='';rt.appendChild(document.createTextNode(di.l+'に泊まれる宿 '));
  rt.appendChild(el('b',null,oks.length+'軒'));
  if(favOnly){rt.appendChild(document.createTextNode(' (お気に入りのみ)'));}
  if(filterId&&D.badges[filterId]){rt.appendChild(document.createTextNode(' (絞り込み: '+D.badges[filterId].l+')'));}
  // すぐ予約できるように、この日の上位3軒の予約ボタンを、見出しのすぐ下に並べる
  var qk=document.getElementById('qbook');
  if(qk){qk.textContent='';
    var dI=document.getElementById('dinput');if(dI)dI.value=cur;
    if(!oks.length){qk.appendChild(el('p','qh',di.l+(di.u?'はまだ空室を取得できていません。':'は「'+FL+'」の空きが見つかりませんでした。')));
      qk.appendChild(el('p','qm','ほかの日付を選んでください。'));}
    if(oks.length){qk.appendChild(el('p','qh',di.l+'に予約できる宿('+(mode==='p'?'安い順':'口コミ評価順')+'・上位'+Math.min(3,oks.length)+'軒)'));
      oks.slice(0,3).forEach(function(x){var hh=x[0],cc=x[1],row=el('div','qr'),nm=el('div','qn');
        if(hh.hp){var na=el('a',null,hh.n);na.href=hh.hp;nm.appendChild(na);}else nm.appendChild(el('span',null,hh.n));
        nm.appendChild(el('small',null,(hh.rv?'★'+hh.rv+' ・ ':'')+yen(cc[1])+'〜'));row.appendChild(nm);
        row.appendChild(btn(lk(hh,cur,cc),'cta qcta','予約する ›'));qk.appendChild(row);});
      if(oks.length>3)qk.appendChild(el('p','qm','ほか'+(oks.length-3)+'軒は下の一覧へ'));}}
  cards.textContent='';
  if(oks.length){for(i=0;i<oks.length;i++)cards.appendChild(card(oks[i][0],oks[i][1],'o'));}
  else{
    var n=el('div','none');n.innerHTML=D.owls.sleepy;var nt=el('div','nt');n.appendChild(nt);
    if(favOnly&&!Object.keys(fav).length){nt.appendChild(el('b',null,'お気に入りの宿がまだありません。'));nt.appendChild(el('p',null,'カードの♡を押すと、この端末に保存されます。'));}
    else{nt.appendChild(el('b',null,di.u?'この日はまだ空室を取得できていません(満室ではありません)。':((favOnly||filterId)?'この日は、選んだ条件に合う宿が見つかりませんでした。':'この日は「'+FL+'」の空きが見つかりませんでした。')));
    nt.appendChild(el('p',null,di.u?'自動更新で順番に取得しています。取得済みの日を選んでみてください。':'空きのある日を選んでみてください。'));}
    var near=D.dates.filter(function(x){return x.n>0;});
    var ci=D.dates.indexOf(di);
    near.sort(function(a,b){return Math.abs(D.dates.indexOf(a)-ci)-Math.abs(D.dates.indexOf(b)-ci);});
    near.slice(0,3).forEach(function(x){var s=el('button','nb',x.l+'に空きあり');s.type='button';s.style.border='0';s.style.cursor='pointer';
      s.addEventListener('click',function(){pick(x.d,true);});nt.appendChild(s);});
    cards.appendChild(n);}
  if(parts.length){var dp=el('details','more');dp.setAttribute('data-nosnippet','');dp.appendChild(el('summary',null,'参考: '+FL+'の空きは確認できない宿('+parts.length+'軒・ほかの部屋・プランなら空きあり)'));
    dp.appendChild(el('p','pnote','※この欄の料金は「'+FL+'」ではない部屋も含む、宿全体の最安の目安です。'));
    for(i=0;i<parts.length;i++)dp.appendChild(card(parts[i][0],parts[i][1],'p'));cards.appendChild(dp);}
  if(fulls.length){var df=el('details','more');df.appendChild(el('summary',null,'満室の宿('+fulls.length+'軒)'));
    var ul=el('ul','fl');fulls.forEach(function(x){ul.appendChild(el('li',null,x.n));});df.appendChild(ul);cards.appendChild(df);}
  if(areaCal){areaCal.goto(cur);}
  var ths=document.querySelectorAll('thead th[data-d]');
  for(i=0;i<ths.length;i++){ths[i].classList.toggle('sel',ths[i].getAttribute('data-d')===cur);}
  var qb=document.querySelectorAll('.quick button');
  for(i=0;i<qb.length;i++){qb[i].classList.toggle('on',qb[i].getAttribute('data-d')===cur);}
  if(window.relt){window.relt();}
  if(window.ymSbar){window.ymSbar();}
}
var areaCal=null;
function pick(d,scroll){cur=d;render();
  try{var u=new URL(location.href);u.searchParams.set('d',d);u.hash='';history.replaceState(null,'',u.toString());}catch(e){}
  if(scroll&&finder){var r=document.getElementById('rhead').getBoundingClientRect();
    if(r.top<0||r.top>window.innerHeight*0.6){window.scrollTo({top:window.pageYOffset+r.top-90,behavior:'smooth'});}}}
areaCal=mcal(D.dates.map(function(x){return x.d;}),function(iso){var x=dinfo(iso);
  var on=iso===cur?' on':'',mk=x.c?'👑':'';
  if(x.n>0){return {cls:'ok'+on,mark:mk,lines:['空'+x.n+'軒',x.p?shortYen(x.p):''].filter(Boolean),click:function(){pick(iso,true);},label:x.l+' 空き'+x.n+'軒'};}
  if(x.u){return {cls:'wait'+on,lines:['—'],click:function(){pick(iso,true);},label:x.l+' 取得待ち'};}
  return {cls:'zero'+on,lines:['空きなし'],click:function(){pick(iso,true);},label:x.l+' 空きなし'};});
strip.appendChild(areaCal);
strip.appendChild(el('p','mc-note','数字は、その日に空きのある宿の軒数と最安値です。👑=空きがいちばん多い日 ／ —=まだ取得できていない日(満室ではありません)。日付を押すと、下に宿の一覧が出ます。'));
areaCal.goto(cur);
// 空き軒数の月カレンダーは、ボタンで開く(最初から開くと、宿の一覧が画面の下に押し出されるため)
var dsb=document.getElementById('dcalbtn'),dst=document.getElementById('dstrip');
if(dsb&&dst){dsb.hidden=false;dsb.addEventListener('click',function(){var op=dst.hidden;dst.hidden=!op;dsb.setAttribute('aria-expanded',op?'true':'false');dsb.textContent=op?'🗓 カレンダーを閉じる':'🗓 空き軒数のカレンダーから選ぶ';if(op){areaCal.goto(cur);}});}else if(dst){dst.hidden=false;}
sortSel.addEventListener('change',function(){mode=sortSel.value;render();});
var fs=document.getElementById('fltsel');if(fs){fs.addEventListener('change',function(){filterId=fs.value;render();});}
var ft=document.getElementById('favtog');
function updFav(){if(!ft)return;var n=Object.keys(fav).length;ft.textContent=(favOnly?'♥':'♡')+' お気に入りだけ('+n+')';ft.setAttribute('aria-pressed',favOnly?'true':'false');ft.classList.toggle('on',favOnly);}
if(ft){ft.hidden=false;ft.addEventListener('click',function(){favOnly=!favOnly;updFav();render();});updFav();}
var qb=document.querySelectorAll('.quick button');
for(var q=0;q<qb.length;q++){(function(b){b.addEventListener('click',function(){pick(b.getAttribute('data-d'),false);});})(qb[q]);}
var dIn=document.getElementById('dinput');
if(dIn){dIn.addEventListener('change',function(){var v=dIn.value;if(D.dates.some(function(x){return x.d===v;}))pick(v,false);
  else{var qk=document.getElementById('qbook');if(qk){qk.textContent='';qk.appendChild(el('p','qh','この日付は、当サイトの取得範囲外です('+D.dates[0].l+'〜'+D.dates[D.dates.length-1].l+'から選んでください)。'));}}});}
var ths=document.querySelectorAll('thead th[data-d]');
for(var t=0;t<ths.length;t++){(function(th){th.addEventListener('click',function(){pick(th.getAttribute('data-d'),true);});})(ths[t]);}
// カレンダー表の週タブ
var tb=document.getElementById('tabs');
var curTab='0';
if(tb){var bt=tb.querySelectorAll('button');
  function show(w){curTab=w;var cells=document.querySelectorAll('[data-w]');for(var i=0;i<cells.length;i++){cells[i].hidden=(w!=='all'&&cells[i].getAttribute('data-w')!==w);}
    for(var j=0;j<bt.length;j++){bt[j].setAttribute('aria-pressed',bt[j].getAttribute('data-t')===w?'true':'false');}}
  for(var k=0;k<bt.length;k++){bt[k].addEventListener('click',function(){show(this.getAttribute('data-t'));});}
  tb.hidden=false;show('0');}
// カレンダー表: マスを押すと、その日の楽天トラベルのページを開く / 残りの宿を表に足す
var tbl=document.querySelector('#calendar table'),HM={};
D.hotels.forEach(function(h){HM[h.no]=h;});
if(tbl){tbl.addEventListener('click',function(e){var td=e.target.closest?e.target.closest('td.c'):null;
    if(!td||td.classList.contains('nd'))return;var tr=td.parentNode,h=HM[tr.getAttribute('data-h')];if(!h)return;
    var th=ths[Array.prototype.indexOf.call(tr.children,td)-1];if(!th)return;var iso=th.getAttribute('data-d'),u=lk(h,iso,h.c[iso]);
    if(u&&u!=='#'){if(window.ymAff)ymAff();window.open(u,'_blank','noopener');}});
  var tm=document.getElementById('tmore');
  if(tm){tm.hidden=false;tm.addEventListener('click',function(){var tbody=tbl.tBodies[0];
    for(var i=D.tm;i<D.hotels.length;i++){var h=D.hotels[i],tr=el('tr');tr.setAttribute('data-h',h.no);
      var hn=el('th','hn');hn.scope='row';if(h.hp){var na=el('a','nm',h.n);na.href=h.hp;hn.appendChild(na);}else hn.appendChild(btn(h.u,'nm',h.n));hn.appendChild(el('small',null,h.rv?'★'+h.rv:''));tr.appendChild(hn);
      D.dates.forEach(function(x,j){var c=h.c[x.d],td=el('td'),sp=el('span');td.setAttribute('data-w',String(Math.floor(j/7)));
        if(!c){td.className='c nd';sp.textContent='—';}
        else if(c[0]==='f'){td.className='c full';sp.textContent='満';}
        else if(c[0]==='p'){td.className='c part';sp.textContent='△';}
        else{td.className='c ok';sp.textContent='◯';sp.appendChild(el('i',null,c[1]>=10000?(c[1]/10000).toFixed(1)+'万':String(c[1]).replace(/\B(?=(\d{3})+(?!\d))/g,',')));}
        td.appendChild(sp);tr.appendChild(td);});
      tbody.appendChild(tr);}
    tm.parentNode.removeChild(tm);if(tb)show(curTab);});}}
render();
if(urlDate)pick(urlDate,false);
})();
"""


def head(cfg, title, desc, canonical, demo, extra="", noindex=False):
    ga = cfg["site"].get("analytics_id", "")
    gtag = ""
    if ga and not demo and ga.startswith("GTM-"):
        gtag = (f'<script async src="https://www.googletagmanager.com/gtm.js?id={esc(ga)}"></script>'
                "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}"
                "gtag('js',new Date());</script>")
    elif ga and not demo:
        gtag = (f'<script async src="https://www.googletagmanager.com/gtag/js?id={esc(ga)}"></script>'
                f"<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments)}}"
                f"gtag('js',new Date());gtag('config','{esc(ga)}');</script>")
    robots = '<meta name="robots" content="noindex,nofollow">' if (demo or noindex) else ""
    return (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title><meta name="description" content="{esc(desc)}">'
            f'<link rel="canonical" href="{esc(canonical)}">{robots}'
            f'<link rel="alternate" type="application/json" title="空室・料金データ(JSON)" href="{esc(cfg["site"]["url"].rstrip("/"))}/data/latest.json">'
            f'<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc)}">'
            f'<meta property="og:type" content="website"><meta property="og:url" content="{esc(canonical)}">'
            f'<meta property="og:image" content="{esc(cfg["site"]["url"].rstrip("/"))}/ogp.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
            f'<meta property="og:site_name" content="{esc(cfg["site"]["name"])}"><meta name="twitter:card" content="summary_large_image">'
            f'<link rel="icon" href="{OWL_URI}">'
            '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Maru+Gothic:wght@500;700;900&display=swap">'
            f"{extra}<style>{CSS}</style>{gtag}</head><body>")


def header_html(cfg, demo, root="../"):
    out = ""
    if demo:
        out += '<div class="demo">これは見本です。宿・料金・空室はすべて架空のデータです(公開されません)</div>'
    out += (f'<header class="hdr"><a class="logo" href="{root}">{esc(cfg["site"]["name"])}<svg viewBox="0 0 32 32" role="img" aria-label="フクロウのキャラクター">{OWL_SVG}</svg></a>'
            "<small>宿の空室をまとめてチェック</small></header>"
            # ステルスマーケティング規制への対応: ボタンごとの【PR】の代わりに、どのページも最初に1か所だけ広告であることを示す
            '<p class="adnote">当サイトは楽天トラベルのアフィリエイト広告を利用しています</p>'
            )
    return out


def footer_html(cfg, root="../"):
    return (f'<nav class="fnav"><a href="{root}">トップ</a> ・ <a href="{root}about/">データの取得方法と注意点</a> ・ '
            f'<a href="{root}stats/">空室データ統計</a> ・ <a href="{root}souba/">料金の相場</a> ・ <a href="{root}report/">空室レポート</a> ・ <a href="{root}privacy/">プライバシーポリシー</a></nav>'
            f'© {esc(cfg["site"]["name"])}<br>'
            "<b>【PR】当サイトは広告(アフィリエイト)を含みます。</b><br>"
            "当サイトは楽天トラベルのアフィリエイトプログラムに参加しており、掲載リンクから予約が行われると運営者に報酬が発生する場合があります。<br>"
            "楽天トラベル公式サイトではありません。掲載の空室・料金は取得時点の情報で、変更される場合があります。最終的な内容は楽天トラベルの予約ページでご確認ください。<br>"
            '<a href="https://webservice.rakuten.co.jp/" target="_blank" rel="noopener">Supported by Rakuten Developers</a>')


# ---- 要約・事実・履歴(AIにも人にも「答え」が先に見える部品) ----------------

def fmt_dt(x):
    return f"{x.month}/{x.day} {x:%H:%M}"


def when_txt(x):
    """AIや検索結果に引用されても意味が通るよう、年と出どころを入れた「いつの数字か」"""
    return f"{x.year}年{x.month}月{x.day}日{x:%H:%M}時点(楽天トラベルの空室データ)で、" if x else ""


def freq_text(cfg):
    r = cfg["refresh"]
    soon = f"今日・明日の日程は約{r['soon_hours']}時間ごと、" if r.get("soon_hours") else ""
    return (f"{soon}{r['near_days']}日以内は約{r['near_hours']}時間ごと、{r['mid_days']}日以内は約{r['mid_hours']}時間ごと、"
            f"それ以降は約{r['far_hours']}時間ごと")


def cond_text(cfg):
    s = cfg["search"]
    return f"{s['nights']}泊・大人{s['adults']}名・1部屋"


def ld_script(objs):
    return '<script type="application/ld+json">' + json.dumps(objs, ensure_ascii=False).replace("</", "<\\/") + "</script>"


def shell(cfg, demo, root, title, desc, canonical, inner, ld=None, noindex=False, js=False):
    extra = ld_script(ld) if ld else ""
    sl = (f"<script>window.__LT={json.dumps(LINK)};{SL_JS}</script>"
          if LINK and ('data-sl="' in inner or 'id="dpick"' in inner or 'id="apick"' in inner) else "")
    if 'id="dpick"' in inner:
        sl += f"<script>{DPICK_JS}</script>"
    if 'id="apick"' in inner:
        sl += f"<script>{AREA_JS}</script>"
    return (head(cfg, title, desc, canonical, demo, extra, noindex) + header_html(cfg, demo, root)
            + '<div class="wrap">' + inner + "</div>" + f"<footer>{footer_html(cfg, root)}</footer>"
            + (f"<script>{JS}</script>" if js else "") + sl + "<script>" + REL_JS + TRACK_JS + IOS_JS + "</script></body></html>")


def breadcrumb_ld(items):
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": n, "item": u} for i, (n, u) in enumerate(items)]}


def faq_ld(faq):
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]}


def faq_html(faq, title="よくある質問"):
    return (f'<div class="card"><h2>{esc(title)}</h2>'
            + "".join(f'<details class="q"><summary>{esc(q)}</summary><p>{esc(a)}</p></details>' for q, a in faq) + "</div>")


def scene_stats(rows, state, feat, ds):
    """日付のまとまり(今週末など)の集計"""
    per, anyh, low, got = [], set(), None, 0
    for d in ds:
        k = d.isoformat()
        fetched = k in state.get("fetched", {})
        n, l = 0, None
        for h in rows:
            c = state["cells"].get(h["no"], {}).get(k)
            if status(c, feat) == "ok":
                n += 1
                p = c["f"][feat][0]
                l = p if l is None else min(l, p)
                anyh.add(h["no"])
        got += 1 if fetched else 0
        per.append((d, fetched, n, l))
        if l is not None:
            low = l if low is None else min(low, l)
    return {"per": per, "hotels": len(anyh), "low": low, "got": got, "n": len(ds)}


def price_txt(low):
    return f"(最安{yen_full(low)}〜)" if low else ""


def _rv(h):
    try:
        return float(h.get("review") or 0)
    except (TypeError, ValueError):
        return 0.0


def top_picks(rows, state, feat, ds, n=3):
    """対象の日程で空きがある宿を、口コミ評価の高い順に(同点は安い順)。(宿, 最安値, その日) の一覧"""
    out = []
    for h in rows:
        best = None
        for d in ds:
            c = state["cells"].get(h["no"], {}).get(d.isoformat())
            if status(c, feat) == "ok":
                p = c["f"][feat][0]
                if best is None or p < best[0]:
                    best = (p, d)
        if best:
            out.append((h, best[0], best[1]))
    out.sort(key=lambda x: (-_rv(x[0]), x[1], x[0]["name"]))
    return out[:n]


def per_day_txt(per, price=None):
    """今週末など2〜3日のまとまりを「金曜9軒(¥…〜)・土曜1軒(¥…〜)」のように曜日ごとに
    (合計の軒数・最安だと、土曜の数や料金と読み違えやすいため)。price="paren" で(¥〜)、"tight" で直後に¥〜。2〜3日でなければ空文字"""
    if not (2 <= len(per) <= 3) or not all(f for _, f, _, _ in per):
        return ""
    def one(d, n, l):
        t = f"{md(d)}({WD[d.weekday()]}){n}軒"
        if price and n and l:
            t += f"({yen_full(l)}〜)" if price == "paren" else f"{yen_full(l)}〜"
        return t
    return "・".join(one(d, n, l) for d, _, n, l in per)


def room_noun(flabel):
    return flabel if flabel.endswith(("客室", "部屋", "プラン")) else f"{flabel}の部屋"


def short_room(rn, n=28):
    rn = (rn or "").strip()
    if rn.startswith("宿全体"):
        return ""
    return rn if len(rn) <= n else rn[:n - 1] + "…"


def day_split(rows, state, feat, d):
    """その日の宿を (空きあり[(最安, 宿, 部屋名)], 条件の部屋は未確認, 満室, 未取得) に分ける"""
    ok, part, full, none = [], 0, 0, 0
    for h in rows:
        c = state["cells"].get(h["no"], {}).get(d.isoformat())
        st = status(c, feat)
        if st == "ok":
            p, _, rn = c["f"][feat]
            ok.append((p, h, rn or ""))
        elif st == "part":
            part += 1
        elif st == "full":
            full += 1
        else:
            none += 1
    ok.sort(key=lambda x: (x[0], x[1]["name"]))
    return ok, part, full, none


def quick_answer(cfg, feat, flabel, alabel, rows, state, d, holidays, dates, info):
    """ページ冒頭の「即答の1文」。指定日・人数・確認できた軒数・部屋名・料金・取得時刻・未確認/満室の数を、データから作る"""
    ok, part, full, none = day_split(rows, state, feat, d)
    ft = parse_dt(state.get("fetched", {}).get(d.isoformat(), ""))
    hotel_feat = is_hotel_feat(cfg, feat)
    head = f"{day_label(d, holidays)}・1泊大人2名1室" + (f"({ft.month}/{ft.day} {ft:%H:%M}取得)" if ft else "") + ": "
    if ok:
        what = f"{alabel}で{flabel}に空きを確認できたのは{len(ok)}軒" if hotel_feat else f"{alabel}で{room_noun(flabel)}に空きを確認できた宿は{len(ok)}軒"
        ex = []
        for pr, h, rn in ok[:2]:
            r = "" if hotel_feat else short_room(rn, 18)
            ex.append(f"{h['name']}" + (f"「{r}」" if r else "") + f"{yen_full(pr)}〜")
        txt = head + what + "(安い順に " + "、".join(ex) + (" など" if len(ok) > 2 else "") + ")。"
    else:
        txt = head + (f"{alabel}で{flabel}の空きは確認できませんでした。" if hotel_feat else f"{alabel}で{room_noun(flabel)}の空きは確認できませんでした。")
        near = [x for x in dates if x != d and info[x.isoformat()][0] > 0]
        near.sort(key=lambda x: abs((x - d).days))
        if near:
            txt += f"近い日では{day_label(near[0], holidays)}に{info[near[0].isoformat()][0]}軒の空きがあります。"
    rest = []
    if part and not hotel_feat:
        rest.append(f"宿に空きはあるが{flabel}は未確認{part}軒")
    if full:
        rest.append(f"満室{full}軒")
    if none:
        rest.append(f"未取得{none}軒")
    if rest:
        txt += "ほかに" + "、".join(rest) + "。"
    return txt


def room_table_html(cfg, feat, flabel, rows, state, ds, holidays, label, limit=80):
    """今週末など直近の日について、空いている宿と部屋(各宿の最安の部屋)を表で。AIが予算などで絞り込んで、部屋名つきで答えられるように"""
    hotel_feat = is_hotel_feat(cfg, feat)
    trs, n = [], 0
    for d in ds:
        ok, _, _, _ = day_split(rows, state, feat, d)
        ft = parse_dt(state.get("fetched", {}).get(d.isoformat(), ""))
        for pr, h, rn in ok:
            if n >= limit:
                break
            n += 1
            hh = hotel_href(h)
            nm = (f'<a href="{esc(hh)}?d={d.isoformat()}">{esc(h["name"])}</a>' if hh else esc(h["name"]))
            room = "宿全体の最安プラン" if (hotel_feat or not short_room(rn, 60)) else short_room(rn, 60)
            _u = ((state["cells"].get(h["no"], {}).get(d.isoformat()) or {}).get("f") or {}).get(feat, [0, ""])[1]
            trs.append(f"<tr><td>{esc(day_label(d, holidays))}</td><td>{nm}</td><td>{esc(room)}</td>"
                       f"<td class=\"pr\">{yen_full(pr)}〜</td><td>{(f'{ft.month}/{ft.day} {ft:%H:%M}') if ft else ''}</td>"
                       f"<td>{rk_btn(h, d, '予約', 'tb', _u)}</td></tr>")
    if not trs:
        return ""
    cap = f"{label}に空いている{esc(flabel)}" + ("" if hotel_feat or room_noun(flabel) == flabel else "の部屋") + f"(1泊・大人2名・1室、{n}件、料金が安い順)"
    return (f'<details class="amore rtab"><summary>📋 {cap}を表で見る</summary>'
            '<div class="scroll"><table class="rtbl"><caption>' + cap + '</caption><thead><tr><th scope="col">宿泊日</th><th scope="col">宿名</th><th scope="col">'
            + ("プラン" if hotel_feat else "部屋(各宿の最安の部屋)") + '</th><th scope="col">料金</th><th scope="col">取得</th><th scope="col">予約</th></tr></thead><tbody>'
            + "".join(trs) + '</tbody></table></div><p class="sub">取得時点の楽天トラベルの掲載額です。宿名を押すと、その日を選んだ宿のページが開きます。</p></details>')


def day_examples(rows, state, feat, d, n=3):
    """その日に空いている宿の例(口コミ評価の高い順)。「宿名(部屋名 ¥〜)、…(MM/DD HH:MM取得)」の1文"""
    got = []
    for h in rows:
        c = state["cells"].get(h["no"], {}).get(d.isoformat())
        if status(c, feat) == "ok":
            p, _, rn = c["f"][feat]
            got.append((h, p, rn or ""))
    if not got:
        return ""
    got.sort(key=lambda x: (-_rv(x[0]), x[1], x[0]["name"]))
    parts = []
    for h, p, rn in got[:n]:
        rn = "" if rn.startswith("宿全体") else (rn if len(rn) <= 30 else rn[:29] + "…")
        parts.append(f"{h['name']}(" + (f"{rn} " if rn else "") + f"{yen_full(p)}〜)")
    ft = parse_dt(state.get("fetched", {}).get(d.isoformat(), ""))
    return "、".join(parts) + (f"({ft:%m/%d %H:%M}取得)" if ft else "")


def picks_txt(picks, label="口コミ上位の空き宿"):
    if not picks:
        return ""
    parts = []
    for h, p, d in picks:
        rv = f"★{h['review']}・" if h.get("review") else ""
        parts.append(f"{h['name']}({rv}{yen_full(p)}〜)")
    return f"{label}: " + "、".join(parts)


def offer_ld(p, d, got, url, desc="1泊・大人2名・1部屋の最安値"):
    o = {"@type": "Offer", "price": p, "priceCurrency": "JPY", "availability": "https://schema.org/InStock",
         "availabilityStarts": d.isoformat(), "url": url, "description": desc}
    if got:
        o["validFrom"] = got.isoformat(timespec="seconds")
    return o


def time_html(dt):
    """取得時刻。JavaScriptが動けば「約〇時間前」を添える(動かなくても、時刻は読める)"""
    if not dt:
        return ""
    return f'<time class="rel" datetime="{dt.isoformat(timespec="seconds")}">{fmt_dt(dt)}</time>'


def status_any(state, h, feat, ds):
    return any(status(state["cells"].get(h["no"], {}).get(d.isoformat()), feat) == "ok" for d in ds)


def item_list_ld(rows, state, feat, ds, canonical, limit=20):
    """一覧の各宿を、料金・在庫つき(Offer)で機械に伝える。canonical はこのページのURL(宿ページは ./hotel/番号/)"""
    out = []
    for i, h in enumerate(rows):
        it = {"@type": "ListItem", "position": i + 1, "name": h["name"]}
        pk = top_picks([h], state, feat, ds, 1)
        if pk and i < limit:
            _, p, d = pk[0]
            hu = canonical.split("/", 3)[0] + "//" + canonical.split("/", 3)[2] + f"/hotel/{h['no']}/"
            of = offer_ld(p, d, parse_dt(state.get("fetched", {}).get(d.isoformat(), "")), hu)
            # 部屋の条件(客室露天風呂など)のページでは、どの部屋の料金かを伝える(宿全体の料金と読み違えないように)
            rn = ((state["cells"].get(h["no"], {}).get(d.isoformat()) or {}).get("f") or {}).get(feat, [0, "", ""])[2]
            if rn and not str(rn).startswith("宿全体"):
                of["itemOffered"] = {"@type": "HotelRoom", "name": rn}
                of["description"] = f"1泊・大人2名・1部屋の最安値({rn})"
            hotel = {"@type": "Hotel", "name": h["name"], "url": hu, "makesOffer": of}
            if h.get("review") and h.get("reviews"):
                try:
                    hotel["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": float(h["review"]), "bestRating": 5, "ratingCount": int(h["reviews"])}
                except (TypeError, ValueError):
                    pass
            it["item"] = hotel
        out.append(it)
    return out


def answer_parts(cfg, page, rows, state, today, dates, info, scene_list, newest, holidays, now):
    """冒頭の「答え」。(時刻, 先頭の1〜2文, 箇条書き, 折りたたむ補足) を返す。数字はすべてデータから作る。"""
    feat = page["feature"]
    flabel = cfg["features"][feat]["label"]
    alabel = cfg["areas"][page["area"]]["label"]
    asof = fmt_dt(newest) if newest else ""
    n0, l0 = info[today.isoformat()]
    items = [(f"今日({day_label(today, holidays)})", n0, l0)]
    for sc in scene_list:
        if sc["slug"] not in ("shumatsu", "raishu", "renkyu") or not sc["dates"]:
            continue
        st = scene_stats(rows, state, feat, sc["dates"])
        if st["got"]:
            items.append((f"{sc.get('label', sc['name'])}({sc['span']})", st["hotels"], st["low"], per_day_txt(st["per"], "paren")))
    head_item = next((it for it in items if it[0].startswith("今週末")), items[0])

    def phrase(it):
        return it[3] if len(it) > 3 and it[3] else f"{it[1]}軒{price_txt(it[2])}"

    noun = flabel if is_hotel_feat(cfg, feat) else f"{flabel}が確認できた宿"
    lead = (f"{when_txt(newest)}{alabel}の{noun}{len(rows)}軒のうち、"
            f"{head_item[0]}に空きがあるのは{phrase(head_item)}です(1泊・大人2名)。")
    bullets = [f"{it[0]}: {phrase(it)}" for it in items if it[0] != head_item[0]]
    cands = [(d, *info[d.isoformat()]) for d in dates if info[d.isoformat()][0] > 0]
    if cands:
        best = max(cands, key=lambda x: (x[1], -x[2]))
        cheap = min(cands, key=lambda x: (x[2], x[0]))
        bullets.append(f"向こう{len(dates)}日間で空きが最も多い日: {day_label(best[0], holidays)}の{best[1]}軒")
        bullets.append(f"向こう{len(dates)}日間で最安の日: {day_label(cheap[0], holidays)}の{yen_full(cheap[2])}〜")
    else:
        bullets.append(f"向こう{len(dates)}日間のうち、取得済みの日程では該当する空きは見つかっていません(未取得の日を除く)")
    pk = picks_txt(top_picks(rows, state, feat, dates))
    if pk:
        bullets.append(pk)
    # その日に空いている宿を、宿名・部屋名・料金・取得時刻の1文で(AIが確かめて引用できるように)
    wk = next((sc["dates"] for sc in scene_list if sc["slug"] == "shumatsu"), [])
    for d in [today] + [x for x in wk if x != today]:
        ex = day_examples(rows, state, feat, d)
        if ex:
            bullets.append(f"{day_label(d, holidays)}に空いている宿の例: {ex}")
    bullets.append(f"確認できた宿: {len(rows)}軒")
    details = [(f"「{flabel}」は、{fdesc(cfg, feat)}のことです。軒数は、その条件の宿のうち、その日に空きのある宿の数です。料金は、宿全体で空いているプランの{cond_text(cfg)}の最安値です。"
                if is_hotel_feat(cfg, feat) else
                f"「{flabel}」は、{fdesc(cfg, feat)}のことです。軒数は、その部屋に空きがある宿の数です。料金は{cond_text(cfg)}の最安値です。"),
               f"楽天トラベルの空室検索APIから{freq_text(cfg)}に取得した値で、リアルタイムの在庫ではありません。"
               "予約の前に、楽天トラベルの予約ページで空室と料金をご確認ください。"]
    return asof, lead, bullets, details


def answer_html(asof, lead, bullets, details, title="いまの空室まとめ", more="数字の意味と注意点", mood="happy", keep=None):
    """まとめの欄。箇条書きは最初の keep 個だけ見せ、残りは「くわしい数字を見る」に折りたたむ
    (折りたたんだ部分もページの中に残るので、AIや検索エンジンには今まで通り読まれる)"""
    small = f"<small>{esc(asof)} 時点</small>" if asof else ""
    if keep is None:
        keep = 3 if len(bullets) > 4 else len(bullets)
    shown, rest = bullets[:keep], bullets[keep:]
    ul = ("<ul class='ab'>" + "".join(f"<li>{esc(b)}</li>" for b in shown) + "</ul>") if shown else ""
    if rest:
        ul += (f'<details class="amore"><summary>くわしい数字を見る({len(rest)}項目)</summary><ul class="ab">'
               + "".join(f"<li>{esc(b)}</li>" for b in rest) + "</ul></details>")
    det = (f'<details class="adet"><summary>{esc(more)}</summary>' + "".join(f"<p>{esc(d)}</p>" for d in details) + "</details>") if details else ""
    return (f'<section class="answer" id="answer">{owl_html(mood)}<h2>{esc(title)}{small}</h2><p class="lead2">{esc(lead)}</p>{ul}{det}</section>')


LIVE = []   # 実際にページを作る(エリア, 条件)の一覧。main() が決める
AUTO_PAGE_MIN = 3   # プラン名の条件(記念日・カップルなど)のページを作る、当てはまる宿の最低数


def switcher_html(cfg, page, root):
    """エリアと条件を切り替えるリンク(ページがあるものだけ)"""
    same_f = [p for p in LIVE if p["feature"] == page["feature"]]
    same_a = [p for p in LIVE if p["area"] == page["area"]]
    rows = []
    if len(same_f) > 1:
        byp = {p["area"]: p for p in same_f}
        links = "".join(f'<small class="rgl">{esc(r)}</small>' + "".join(
            f'<span class="cur">{esc(cfg["areas"][a]["label"])}</span>' if byp[a]["slug"] == page["slug"]
            else f'<a href="{root}{esc(byp[a]["slug"])}/">{esc(cfg["areas"][a]["label"])}</a>' for a in ks)
            for r, ks in by_region(cfg, [p["area"] for p in same_f]))
        links += f'<a href="{root}{esc(hub_slug(cfg, page["feature"]))}/">全エリアまとめ</a>'
        rows.append(f'<nav class="chips" aria-label="エリアを選ぶ"><b>エリア</b>{links}</nav>')
    if len(same_a) > 1:
        links = "".join(f'<span class="cur">{esc(cfg["features"][p["feature"]]["label"])}</span>' if p["slug"] == page["slug"]
                        else f'<a href="{root}{esc(p["slug"])}/">{esc(cfg["features"][p["feature"]]["label"])}</a>' for p in same_a)
        rows.append(f'<nav class="chips" aria-label="条件を選ぶ"><b>条件</b>{links}</nav>')
    return f'<div class="sw">{"".join(rows)}</div>' if rows else ""


def chips_html(scene_list, root, current=None):
    links = "".join(f'<a href="{root}{esc(sc["slug"])}/">{esc(sc["name"])}</a>' for sc in scene_list if sc["slug"] != current)
    return f'<nav class="chips" aria-label="日程から探す"><b>日程から探す</b>{links}</nav>'


def facts_html(cfg, page, nrows, newest, oldest):
    feat = page["feature"]
    flabel = cfg["features"][feat]["label"]
    rows = [
        ("対象エリア", cfg["areas"][page["area"]]["label"]),
        ("対象の宿" if is_hotel_feat(cfg, feat) else "対象の部屋", f"{flabel}({fdesc(cfg, feat)})"),
        ("検索条件", cond_text(cfg)),
        ("掲載している宿", f"{nrows}軒"),
        ("最新の取得", f"{fmt_dt(newest)}(日本時間)" if newest else "取得前"),
        ("最も古い取得", f"{fmt_dt(oldest)}(日本時間)" if oldest else "取得前"),
        ("更新の頻度(目安)", freq_text(cfg)),
        ("データの出どころ", "楽天ウェブサービス(楽天トラベルの空室検索API・施設検索API)"),
    ]
    dl = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
    return (f'<section class="card" id="facts"><h2>このページのデータの概要</h2><dl class="facts">{dl}</dl>'
            '<p><a href="../about/">取得方法・判定方法・注意点の詳細</a></p></section>')


BUCKET_LABEL = ["0〜6日先", "7〜13日先", "14〜20日先", "21〜27日先", "28〜34日先", "35日以上先"]
MIN_N = 30        # 1区分あたりの観測数がこれ未満なら、割合は出さない
READY_DAYS = 14   # 観測を始めてからの日数
READY_N = 200     # 全体の観測数


def pct(a, b):
    return f"{a / b * 100:.0f}%" if b >= MIN_N else "—"


def stats_block(stats, cfg, feat, flabel, today, heading="当サイトの観測履歴"):
    since = stats.get("since")
    tr = stats.get("trans", {}).get(feat, {})
    tot_n = sum(v[0] for v in tr.values())
    obs = stats.get("obs", 0)
    try:
        days = (today - datetime.fromisoformat(since).date()).days + 1
    except Exception:
        days = 0
    if not since or days < READY_DAYS or tot_n < READY_N:
        return (f'<section class="card" id="history"><h2>{esc(heading)}</h2>'
                f"<p>空きが出やすい時期や、空きが消えるまでの早さを、当サイト自身の観測から集計しています。"
                f"現在は蓄積中です(観測開始: {esc(since or '—')}、{days}日目、累計{obs:,}件)。"
                f"{READY_DAYS}日分・{READY_N}件の比較がたまるまでは、数字を公開しません。</p></section>")
    rows = ""
    for i, lab in enumerate(BUCKET_LABEL):
        v = tr.get(str(i), [0, 0, 0, 0])
        rows += (f"<tr><th>{lab}</th><td>{v[0]:,}</td><td>{pct(v[1], v[0])}</td><td>{v[2]:,}</td><td>{pct(v[3], v[2])}</td></tr>")
    return (f'<section class="card" id="history"><h2>{esc(heading)}</h2>'
            f"<p>{esc(since)}から{days}日間、累計{obs:,}件の観測にもとづく、当サイトの集計です。"
            f"前回の確認と今回の確認を比べ、「{esc(flabel)}」の空きがどう変わったかを数えています"
            f"(確認の間隔は{esc(freq_text(cfg))}で、同じ日程・同じ宿の組み合わせを繰り返し数えています)。</p>"
            '<div class="scroll"><table class="stat"><thead><tr><th>宿泊日までの日数</th><th>前回◯でなかった件数</th>'
            '<th>今回◯になった割合</th><th>前回◯だった件数</th><th>今回◯でなくなった割合</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
            f"<p class='legend'>件数が{MIN_N}件に満たない区分は「—」にしています。一般的な傾向ではなく、当サイトが観測した範囲の数字です。"
            "「◯でなくなった」は、予約された場合のほか、楽天側の掲載の変更も含みます。</p></section>")


# ---- 宿ごとの集計 -----------------------------------------------------

def hotel_facts(h, state, feat, dates):
    ok, part, full, none = [], 0, 0, 0
    for d in dates:
        c = state["cells"].get(h["no"], {}).get(d.isoformat())
        st = status(c, feat)
        if st == "ok":
            p, url, name = c["f"][feat]
            ok.append((d, p, url, name))
        elif st == "part":
            part += 1
        elif st == "full":
            full += 1
        else:
            none += 1
    return {"ok": ok, "part": part, "full": full, "none": none}


def hotel_sub(h):
    bits = []
    if h.get("review"):
        bits.append(f"★{h['review']}" + (f"({h['reviews']:,}件)" if h.get("reviews") else ""))
    for k in ("station", "address"):
        if h.get(k):
            bits.append(h[k])
    return " ・ ".join(bits)


def thumb_html(h, cls="limg"):
    """宿の小さな写真(楽天トラベルの施設写真)。読み込めないときは消す"""
    u = safe_url(h.get("image")) if str(h.get("image", "")).startswith("https://") else "#"
    return (f'<img class="{cls}" src="{esc(u)}" alt="" width="72" height="72" loading="lazy" decoding="async" onerror="this.remove()">'
            if u != "#" else "")


def row_html(h, d, p, url, name, root_hotel, bd=""):
    sub = hotel_sub(h)
    return (f'<li>{thumb_html(h)}<div class="t"><a class="n" href="{esc(hotel_href(h) or "/hotel/" + h["no"] + "/")}">{esc(h["name"])}</a>'
            f"<small>{esc(sub)}<br>{esc(name)}</small>{bd}</div>"
            f'<div class="pr">{yen_full(p)}<span>〜</span></div>'
            + btn(url or h.get("url"), "cta", "空室を見る ›") + "</li>")


def page_ctx(cfg, hotels, state, page, now):
    feat = page["feature"]
    today = now.date()
    dates = [today + timedelta(days=i) for i in range(area_days(cfg, page["area"]))]
    rows = hotels_for_page(cfg, hotels, state, page)
    info = summarize(rows, dates, feat, state)
    fetched = [parse_dt(v) for v in state.get("fetched", {}).values()]
    fetched = [x for x in fetched if x]
    newest, oldest = (max(fetched), min(fetched)) if fetched else (None, None)
    return {"feat": feat, "flabel": cfg["features"][feat]["label"], "alabel": cfg["areas"][page["area"]]["label"],
            "today": today, "dates": dates, "rows": rows, "info": info, "newest": newest, "oldest": oldest,
            "holidays": set(cfg.get("holidays", [])), "base": cfg["site"]["url"].rstrip("/"), "state": state, "page": page}


# ---- 日付別ページ(今週末・来週末・連休・年末年始・クリスマス) ----------------

def render_scene(cfg, page, sc, scene_list, ctx, state, stats, demo):
    site, feat, flabel, alabel = cfg["site"], ctx["feat"], ctx["flabel"], ctx["alabel"]
    holidays, rows, newest = ctx["holidays"], ctx["rows"], ctx["newest"]
    canonical = f"{ctx['base']}/{page['slug']}/{sc['slug']}/"
    scn = sc.get("label", sc["name"])
    name = f"{alabel}の{flabel} {scn}の空室"
    st = scene_stats(rows, state, feat, sc["dates"])
    asof = fmt_dt(newest) if newest else ""

    bullets, details = [], []
    if not sc["dates"]:
        lead = f"今年(今回)の{sc['name']}の日程は終了しました。次の日程が近づくと、このページに空室がまとまります。"
    else:
        if st["hotels"]:
            lead = (f"{when_txt(newest)}{alabel}の{flabel}は、{scn}のどこか1日でも空きがある宿が{st['hotels']}軒、"
                    f"最安は{yen_full(st['low'])}〜です。")
        elif st["got"]:
            lead = f"{when_txt(newest)}{alabel}の{flabel}は、{sc['name']}の取得済みの日程では空きが見つかっていません。"
        else:
            lead = f"{sc['name']}の日程は、まだ楽天トラベルから取得できていません。自動更新で順次取得します。"
        extra = []
        pk = picks_txt(top_picks(rows, state, feat, sc["dates"]))
        if pk:
            extra.append(pk)
        for d, fetched, n, low in st["per"]:
            bullets.append(f"{day_label(d, holidays)}: " + (f"{n}軒{price_txt(low)}" if fetched else "取得待ち"))
            ex = day_examples(rows, state, feat, d) if fetched and n else ""
            if ex:
                extra.append(f"{day_label(d, holidays)}に空いている宿の例: {ex}")
        n_vis = len(bullets)
        bullets += extra
        if st["got"] < st["n"]:
            details.append("「取得待ち」の日は、まだ楽天トラベルから取得できていない日です(満室という意味ではありません)。")
    details += [f"{sc['note']}「{flabel}」は、{fdesc(cfg, feat)}のことです。料金は{cond_text(cfg)}の最安値です。",
                f"楽天トラベルの空室検索APIから{freq_text(cfg)}に取得した値で、リアルタイムの在庫ではありません。"]
    parts = [lead, " / ".join(bullets)]

    title_for_share = f"{name}" + (f"【{sc['span']}】" if sc["span"] else "") + f" | {site['name']}"
    body = [f'<div class="hero"><div class="crumb"><a href="../../">{esc(site["name"])}</a> › <a href="../../{esc(page["area"])}/">{esc(cfg["areas"][page["area"]]["label"])}</a> › <a href="../">{esc(page["title"])}</a> › {esc(sc["name"])}</div>'
            f"<h1>{esc(name)}" + (f"<br><small style='font-size:.6em;color:var(--sub)'>{esc(sc['span'])}</small>" if sc["span"] else "") + "</h1></div>",
            ]
    summary = [answer_html(asof, lead, bullets, details, f"{scn}の空室まとめ", mood=("happy" if st["hotels"] else "sleepy"),
                           keep=(n_vis if sc["dates"] else None)), chips_html(scene_list, "../", sc["slug"]), share_html(canonical, title_for_share)]
    if not sc["dates"]:
        body += summary
        summary = []
    for d, fetched, n, low in st["per"]:
        k = d.isoformat()
        sec = f'<section class="dsec" id="d-{k}"><h2>{esc(day_label(d, holidays))}に泊まれる宿 '
        if not fetched:
            body.append(sec + '</h2><p class="meta">まだ取得できていません。</p></section>')
            continue
        oks = []
        for h in rows:
            c = state["cells"].get(h["no"], {}).get(k)
            if status(c, feat) == "ok":
                p, url, rn = c["f"][feat]
                oks.append((p, h, url, rn, False))
        oks.sort(key=lambda x: x[0])
        got = parse_dt(state["fetched"].get(k, ""))
        hist = ""
        days_obs = sorted(od for od, v in stats.get("daily", {}).items() if k in v.get(feat, {}))
        if len(days_obs) >= 2:
            first = stats["daily"][days_obs[0]][feat][k]
            hist = f" ・ 初回観測({days_obs[0][5:].replace('-', '/')})は{first[0]}軒 → 現在{n}軒"
        meta = f'<p class="meta">{("取得: " + time_html(got)) if got else ""}{esc(hist)}</p>'
        if oks:
            body.append(sec + f"<b>{n}軒</b></h2>{meta}<ul class='rowl'>"
                        + "".join(row_html(h, d, p, url, rn, "../", flags_html(nwf, False) + badge_html(cfg, badges_for(cfg, h, p, ctx["info"][k][1] if k in ctx["info"] else None, ctx["info"][k][0] if k in ctx["info"] else 0)))
                                  for p, h, url, rn, nwf in oks) + "</ul></section>")
        else:
            body.append(sec + f"<b>0軒</b></h2>{meta}<p>この日は「{esc(flabel)}」の空きが見つかっていません。</p></section>")
    body.append(f'<p><a href="../#calendar">31日分のカレンダーで比べる</a></p>')
    body += summary

    faq = [
        (f"{sc.get('label', sc['name'])}に空いている{alabel}の{flabel}はありますか?",
         lead + " " + " / ".join(bullets)),
        ("この情報はいつ取得したものですか?",
         (f"最新の取得は{newest:%Y-%m-%d %H:%M}(日本時間)です。" if newest else "取得前です。") + f"更新は{freq_text(cfg)}です。リアルタイムではありません。"),
    ]
    body.append(faq_html(faq))
    ld = [breadcrumb_ld([(site["name"], ctx["base"] + "/"), (cfg["areas"][page["area"]]["label"], f"{ctx['base']}/{page['area']}/"),
                         (page["title"], f"{ctx['base']}/{page['slug']}/"), (sc["name"], canonical)]),
          {"@context": "https://schema.org", "@type": "CollectionPage", "name": name, "url": canonical, "inLanguage": "ja",
           "dateModified": (newest or datetime.now(JST)).isoformat(timespec="seconds"),
           "isPartOf": {"@type": "WebSite", "name": site["name"], "url": ctx["base"] + "/"}},
          faq_ld(faq)]
    sc_rows = [h for h in rows if status_any(state, h, feat, sc["dates"])]
    if sc_rows:
        ld.append({"@context": "https://schema.org", "@type": "ItemList", "numberOfItems": len(sc_rows),
                   "itemListElement": item_list_ld(sc_rows, state, feat, sc["dates"], f"{ctx['base']}/{page['slug']}/")})
    # 検索結果で最初に見える部分を「地域・条件・日付・軒数・最安」にする(取得日時は後ろ)
    _pd = per_day_txt(st["per"], "tight") if sc["dates"] else ""
    _num = _pd or (f"{st['hotels']}軒に空き" + (f"・最安{yen_full(st['low'])}〜" if st["low"] else "") if sc["dates"] and st["got"] else "")
    title = (f"{name}|{_num}【" + ("" if _pd else f"{sc['span']}・") + f"{md((newest or datetime.now(JST)).date())}更新】" if _num else
             f"{name}" + (f"【{sc['span']}】" if sc["span"] else "")) + f" | {site['name']}"
    if sc["dates"] and st["got"]:
        _pdp = per_day_txt(st["per"], "paren")
        desc = (f"{alabel}の{flabel}、{scn}({sc['span']})は" + (f"{_pdp}に空き" if _pdp else f"{st['hotels']}軒に空き{price_txt(st['low'])}")
                + f"({cond_text(cfg)})。" + " / ".join(extra[:2]) + (f"({fmt_dt(newest)}時点・楽天トラベルの空室データ)" if newest else ""))[:260]
    else:
        desc = (lead + " " + " / ".join(bullets))[:260]
    return shell(cfg, demo, "../../", title, desc, canonical, "".join(body), ld)


# ---- 全エリアをまたぐ「日程別まとめ」 --------------------------------------

def hub_slug(cfg, fkey):
    return cfg["features"][fkey].get("slug") or fkey


def render_hub(cfg, fkey, sc, scene_list, area_ctxs, now, demo):
    """条件(例: 客室露天風呂)×日程(例: 今週末)で、全エリアをまとめて見せるページ"""
    site = cfg["site"]
    base = site["url"].rstrip("/")
    flabel = cfg["features"][fkey]["label"]
    hs = hub_slug(cfg, fkey)
    canonical = f"{base}/{hs}/{sc['slug']}/"
    holidays = set(cfg.get("holidays", []))
    newest = max([c["newest"] for _, c in area_ctxs if c["newest"]], default=None)
    asof = fmt_dt(newest) if newest else ""
    name = f"{sc.get('label', sc['name'])}に空いている{flabel}" + ("の宿" if flabel.endswith("プラン") else "") + "【全エリア】"

    per_area, hot = [], []
    for page, ctx in area_ctxs:
        st = scene_stats(ctx["rows"], ctx["state"], fkey, sc["dates"])
        per_area.append((page, ctx, st))
        for h in ctx["rows"]:
            best = None
            for d in sc["dates"]:
                c = ctx["state"]["cells"].get(h["no"], {}).get(d.isoformat())
                if status(c, fkey) == "ok":
                    p = c["f"][fkey][0]
                    if best is None or p < best[0]:
                        best = (p, d)
            if best:
                hot.append((best[0], best[1], h, page, ctx))
    hot.sort(key=lambda x: (x[0], x[2]["name"]))
    got_any = any(st["got"] for _, _, st in per_area)
    n_hotels = sum(st["hotels"] for _, _, st in per_area)
    n_areas = sum(1 for _, _, st in per_area if st["hotels"])
    lows = [st["low"] for _, _, st in per_area if st["low"]]

    bullets, details = [], []
    if not sc["dates"]:
        lead = f"今年(今回)の{sc['name']}の日程は終了しました。次の日程が近づくと、このページに空室がまとまります。"
    elif n_hotels:
        top = min((st["low"], c["alabel"]) for _, c, st in per_area if st["low"])
        lead = (f"{when_txt(newest)}{sc.get('label', sc['name'])}({sc['span']})に{flabel}の空きがあるのは、{n_areas}エリア・計{n_hotels}軒です。"
                f"最安は{top[1]}の{yen_full(top[0])}〜です。")
    elif got_any:
        lead = f"{when_txt(newest)}{sc['name']}の取得済みの日程では、どのエリアでも{flabel}の空きが見つかっていません。"
    else:
        lead = f"{sc['name']}の日程は、まだ楽天トラベルから取得できていません。自動更新で順次取得します。"
    if sc["dates"]:
        for page, ctx, st in per_area:
            bullets.append(f"{ctx['alabel']}: " + (f"{st['hotels']}軒{price_txt(st['low'])}" if st["got"] else "取得待ち"))
    details += [f"{sc['note']}「{flabel}」は、{fdesc(cfg, fkey)}のことです。料金は{cond_text(cfg)}の最安値です。",
                f"楽天トラベルの空室検索APIから{freq_text(cfg)}に取得した値で、リアルタイムの在庫ではありません。"]

    body = [f'<div class="hero"><div class="crumb"><a href="../../">{esc(site["name"])}</a> › {esc(flabel)} › {esc(sc["name"])}</div>'
            f"<h1>{esc(name)}" + (f"<br><small style='font-size:.6em;color:var(--sub)'>{esc(sc['span'])}</small>" if sc["span"] else "") + "</h1></div>",
            answer_html(asof, lead, bullets, details, f"{sc['name']}の空室まとめ(全エリア)", mood=("happy" if n_hotels else "sleepy")),
            chips_html([s for s in scene_list], "../", sc["slug"]), share_html(canonical, name)]
    if sc["dates"]:
        lis = []
        for page, ctx, st in per_area:
            sub = (f"{st['hotels']}軒に空き{price_txt(st['low'])}" if st["got"] else "取得待ち")
            lis.append(f'<li><a href="../../{esc(page["slug"])}/{esc(sc["slug"])}/">{esc(ctx["alabel"])}の{esc(sc["name"])}</a><small>{esc(sub)}</small></li>')
        body.append(f'<section class="card"><h2>エリア別の空き</h2><ul class="hl">{"".join(lis)}</ul></section>')
        if hot:
            lis = []
            for p, d, h, page, ctx in hot[:12]:
                lis.append(f'<li><a href="{esc(hotel_href(h) or "/hotel/" + h["no"] + "/")}">{esc(h["name"])}</a>'
                           f'<small>{esc(ctx["alabel"])} ・ {esc(day_label(d, holidays))}に{yen_full(p)}〜</small></li>')
            body.append(f'<section class="card"><h2>{esc(sc["name"])}に泊まれる宿(料金が安い順・全エリア)</h2><ul class="hl">{"".join(lis)}</ul></section>')
    faq = [
        (f"{sc.get('label', sc['name'])}に空いている{flabel}の宿は、どのエリアにありますか?", lead + (" " + " / ".join(bullets) if bullets else "")),
        ("この情報はいつ取得したものですか?",
         (f"最新の取得は{newest:%Y-%m-%d %H:%M}(日本時間)です。" if newest else "取得前です。") + f"更新は{freq_text(cfg)}です。リアルタイムではありません。"),
    ]
    body.append(faq_html(faq))
    items = [{"@type": "ListItem", "position": i + 1, "name": h["name"], "url": f"{base}/hotel/{h['no']}/"}
             for i, (p, d, h, page, ctx) in enumerate(hot[:12])]
    ld = [breadcrumb_ld([(site["name"], base + "/"), (f"{flabel} {sc['name']}", canonical)]),
          {"@context": "https://schema.org", "@type": "CollectionPage", "name": name, "url": canonical, "inLanguage": "ja",
           "dateModified": (newest or datetime.now(JST)).isoformat(timespec="seconds"),
           "isPartOf": {"@type": "WebSite", "name": site["name"], "url": base + "/"}},
          faq_ld(faq)]
    if items:
        ld.append({"@context": "https://schema.org", "@type": "ItemList", "itemListElement": items})
    if sc["dates"] and n_hotels:
        top = min((st["low"], c["alabel"]) for _, c, st in per_area if st["low"])
        title = f"{name}|{n_areas}エリア計{n_hotels}軒・最安¥{top[0]:,}〜【{sc['span']}】 | {site['name']}"
        desc = (f"{sc.get('label', sc['name'])}({sc['span']})に{flabel}の空きがあるのは{n_areas}エリア・計{n_hotels}軒、最安は{top[1]}の{yen_full(top[0])}〜({cond_text(cfg)})。"
                + " / ".join(bullets) + (f"({fmt_dt(newest)}時点)" if newest else ""))[:220]
    else:
        title = f"{name}" + (f"【{sc['span']}】" if sc["span"] else "") + f" | {site['name']}"
        desc = (lead + " " + " / ".join(bullets))[:220]
    return shell(cfg, demo, "../../", title, desc, canonical, "".join(body), ld)


# ---- テーマ別ページ(口コミ高評価・価格帯など。取得済みの数字だけで判定する) ----------------

def themes_for(cfg, page, rows, state, dates):
    """条件に合う宿が、設定した軒数(既定3軒)以上あるテーマだけを返す。[(テーマ, [(宿, 最安値, その日)])]"""
    feat = page["feature"]
    need = int(cfg.get("theme_min_hotels", 3))
    out = []
    for th in cfg.get("themes", []):
        got = []
        for h in rows:
            pk = top_picks([h], state, feat, dates, 1)
            if not pk:
                continue
            _, p, d = pk[0]
            kind = th["kind"]
            if kind == "rating":
                good = _rv(h) >= float(th["min"])
            elif kind == "reviews":
                good = int(h.get("reviews") or 0) >= int(th["min"])
            elif kind == "price_max":
                good = p <= int(th["max"])
            elif kind == "price_min":
                good = p >= int(th["min"])
            else:
                good = False
            if good:
                got.append((h, p, d))
        if len(got) >= need:
            if th["kind"] in ("rating", "reviews"):
                got.sort(key=lambda x: (-_rv(x[0]) if th["kind"] == "rating" else -int(x[0].get("reviews") or 0), x[1]))
            else:
                got.sort(key=lambda x: (x[1], x[0]["name"]))
            out.append((th, got))
    return out


def theme_rule_text(th):
    k = th["kind"]
    if k == "rating":
        return f"楽天トラベルの口コミ評価が{th['min']}以上の宿"
    if k == "reviews":
        return f"楽天トラベルの口コミが{int(th['min']):,}件以上ある宿"
    if k == "price_max":
        return f"取得済みの日程のうち、1泊2名の最安値が{int(th['max']):,}円以下になる日がある宿(最安の日の料金で判定)"
    return f"取得済みの日程でも、1泊2名の最安値が{int(th['min']):,}円以上の宿(最安の日の料金で判定)"


def theme_nav_html(avail, root):
    if not avail:
        return ""
    links = "".join(f'<a href="{root}{esc(th["slug"])}/">{esc(th["name"])}</a>' for th, _ in avail)
    return f'<nav class="chips" aria-label="条件から探す"><b>条件から探す</b>{links}</nav>'


def render_theme(cfg, page, th, got, avail, scene_list, ctx, state, demo):
    site, feat, flabel, alabel = cfg["site"], ctx["feat"], ctx["flabel"], ctx["alabel"]
    holidays, newest, dates = ctx["holidays"], ctx["newest"], ctx["dates"]
    canonical = f"{ctx['base']}/{page['slug']}/{th['slug']}/"
    name = f"{alabel}の{flabel}【{th['name']}】空室"
    asof = fmt_dt(newest) if newest else ""
    picks = [(h, p, d) for h, p, d in got[:3]]
    lead = (f"{when_txt(newest)}{alabel}の{flabel}のうち「{th['name']}」に当てはまる宿で、"
            f"向こう{len(dates)}日間に空きがあるのは{len(got)}軒です。最安は{yen_full(min(p for _, p, _ in got))}〜です。")
    bullets = [picks_txt(picks, "上位3軒")]
    bullets.append(f"当てはまる宿: {len(got)}軒(条件: {theme_rule_text(th)})")
    details = [f"このページは、{theme_rule_text(th)}を、空きのある日がある宿だけ並べたものです。判定には、楽天トラベルの掲載値と、当サイトが取得した料金だけを使っています。",
               f"楽天トラベルの空室検索APIから{freq_text(cfg)}に取得した値で、リアルタイムの在庫ではありません。料金は{cond_text(cfg)}の最安値です。"]
    ps = state
    body = [f'<div class="hero"><div class="crumb"><a href="../../">{esc(site["name"])}</a> › <a href="../">{esc(page["title"])}</a> › {esc(th["name"])}</div>'
            f"<h1>{esc(name)}</h1></div>",
            answer_html(asof, lead, bullets, details, f"{th['name']}の空室まとめ", mood="happy"),
            theme_nav_html([a for a in avail if a[0]["slug"] != th["slug"]], "../"),
            chips_html(scene_list, "../"),
            share_html(canonical, name)]
    lis = []
    for h, p, d in got:
        c = state["cells"].get(h["no"], {}).get(d.isoformat())
        url, rn = (c["f"][feat][1], c["f"][feat][2]) if c else ("", "")
        ok_days = sum(1 for x in dates if status(state["cells"].get(h["no"], {}).get(x.isoformat()), feat) == "ok")
        lis.append(row_html(h, d, p, url, f"{day_label(d, holidays)}が最安 ・ {rn} ・ 空きのある日: {ok_days}日", "../"))
    body.append(f'<section class="sec"><h2>{esc(th["name"])}の宿(空きのある宿のみ)</h2><ul class="rowl">{"".join(lis)}</ul></section>')
    body.append(f'<p><a href="../#calendar">31日分のカレンダーで比べる</a></p>')
    faq = [
        (f"{alabel}で{th['name']}の{flabel}は、今空いていますか?", lead + " " + " ".join(bullets)),
        ("この条件は、どう判定していますか?", f"{theme_rule_text(th)}が対象です。口コミ評価・件数は楽天トラベルの掲載値、料金は取得した最安値で、当サイトが個別に審査したものではありません。"),
        ("この情報はいつ取得したものですか?",
         (f"最新の取得は{newest:%Y-%m-%d %H:%M}(日本時間)です。" if newest else "取得前です。") + f"更新は{freq_text(cfg)}です。リアルタイムではありません。"),
    ]
    body.append(faq_html(faq))
    ld = [breadcrumb_ld([(site["name"], ctx["base"] + "/"), (page["title"], f"{ctx['base']}/{page['slug']}/"), (th["name"], canonical)]),
          {"@context": "https://schema.org", "@type": "CollectionPage", "name": name, "url": canonical, "inLanguage": "ja",
           "dateModified": (newest or datetime.now(JST)).isoformat(timespec="seconds"),
           "isPartOf": {"@type": "WebSite", "name": site["name"], "url": ctx["base"] + "/"}},
          {"@context": "https://schema.org", "@type": "ItemList", "numberOfItems": len(got),
           "itemListElement": item_list_ld([h for h, _, _ in got], state, feat, dates, f"{ctx['base']}/{page['slug']}/")},
          faq_ld(faq)]
    title = f"{name}【{md(ctx['today'])}時点】 | {site['name']}"
    desc = (lead + " " + " ".join(bullets))[:260]
    return shell(cfg, demo, "../../", title, desc, canonical, "".join(body), ld)


# ---- 宿ごとのページ ---------------------------------------------------

def scene_lines(cfg, h, state, feat, today, holidays):
    """今夜・明日・今週末・連休・年末年始・クリスマスごとの、この宿の状態(文章と、(名前, 状態, 最安) の一覧)"""
    items = [("今夜", [today]), ("明日", [today + timedelta(days=1)])]
    for sc in all_scenes(cfg, today):
        if sc["slug"] in ("shumatsu", "renkyu", "nenmatsu", "xmas") and sc["dates"]:
            items.append((sc["name"], sc["dates"]))
    out = []
    for name, ds in items:
        sts, best = [], None
        for d in ds:
            c = state["cells"].get(h["no"], {}).get(d.isoformat())
            st = status(c, feat)
            sts.append(st)
            if st == "ok":
                p = c["f"][feat][0]
                if best is None or p < best[1]:
                    best = (d, p)
        if best:
            state_txt = f"空室あり({day_label(best[0], holidays)} {yen_full(best[1])}〜)"
            kind = "ok"
        elif sts and all(x == "full" for x in sts):
            state_txt, kind = "満室", "full"
        elif sts and all(x == "none" for x in sts):
            state_txt, kind = "取得待ち", "none"
        elif "part" in sts:
            state_txt, kind = "対象の部屋は見つかっていません", "part"
        else:
            state_txt, kind = "満室または取得待ち", "part"
        out.append((name, kind, state_txt))
    return out


def geo_ok(h):
    try:
        la, ln = float(h.get("lat")), float(h.get("lng"))
    except (TypeError, ValueError):
        return None
    return (la, ln) if 20 <= la <= 46 and 122 <= ln <= 154 else None


def hotel_pick_html(h, fl_txt, dates, ok, holidays, back=None, kps=None):
    """宿ページの「日付を選んで予約する」。表示はブラウザ側(DPICK_JS)。最初は ?d= の日付、なければ直近の空きの日"""
    today = dates[0]
    sats = [d for d in dates if d.weekday() == 5]
    quick = [("今夜", today), ("明日", today + timedelta(days=1))] + ([(f"次の土曜 {md(sats[0])}", sats[0])] if sats else [])
    qb = "".join(f'<button type="button" data-d="{d.isoformat()}">{esc(t)}</button>' for t, d in quick)
    dflt = (ok[0][0] if ok else today).isoformat()
    extra = (f' data-back="{back[2]}|{esc(back[3])}|{back[0]}|{back[1]}"' if back else "") + (f' data-kp="{esc(json.dumps(kps, separators=(",", ":")))}"' if kps else "")
    return (f'<section class="bookpanel dpick" id="dpick"{extra} data-no="{esc(h["no"])}" data-fl="{esc(fl_txt)}" data-def="{dflt}" '
            f'data-min="{dates[0].isoformat()}" data-max="{dates[-1].isoformat()}" hidden><h2 class="vh">日付を選んで予約する</h2>'
            '<div class="qbook" id="dout"></div>'
            f'<div class="dchg"><span class="dlab">日付を変える</span><div class="quick" aria-label="日付をすばやく選ぶ">{qb}</div>'
            f'<label class="dinput"><span class="dlab2">📅 ほかの日付を選ぶ</span><input type="date" id="hdinput" min="{dates[0].isoformat()}" max="{dates[-1].isoformat()}"></label></div></section>')


ALT_MAX = 12   # 宿ページに載せる「代わりの宿」の候補の数(日付ごとに、安い順に3軒まで表示)


def alt_data(h, ctx, feat, dates):
    """この宿が空いていない日に出す「同じ日に空いていた宿」の候補。日付ごとの最安(空きなしは0)を、日付の並びにそろえて埋め込む"""
    st = ctx["state"]["cells"]
    mine = st.get(h["no"], {})
    miss = [i for i, d in enumerate(dates) if status(mine.get(d.isoformat()), feat) != "ok"]
    if not miss:
        return ""
    cand = []
    for o in ctx["rows"]:
        if o["no"] == h["no"]:
            continue
        cs = st.get(o["no"], {})
        ps = []
        for d in dates:
            c = cs.get(d.isoformat())
            ps.append(int(c["f"][feat][0]) if status(c, feat) == "ok" and c["f"][feat][0] else 0)
        hit = sum(1 for i in miss if ps[i])
        if hit:
            cand.append((-hit, -(o.get("reviews") or 0), o["no"], o["name"], ps))
    if not cand:
        return ""
    cand.sort()
    data = {"d": [d.isoformat() for d in dates], "h": [[no, nm, ps] for _, _, no, nm, ps in cand[:ALT_MAX]]}
    return "<script>window.__ALT=" + json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + ";</script>"


BATH_WORDS = ["天然温泉", "温泉", "源泉かけ流し", "かけ流し", "掛け流し", "露天風呂", "大浴場", "貸切風呂", "家族風呂", "サウナ", "岩盤浴", "硫黄"]


def hotel_basics(h, d):
    """宿の基本情報(楽天トラベルの施設情報より)。[(見出し, 文)]。ないものは出さない"""
    rows = []
    if h.get("address"):
        rows.append(("住所", h["address"]))
    if h.get("station"):
        rows.append(("最寄り駅", h["station"]))
    if h.get("access"):
        rows.append(("アクセス", re.sub(r"\s+", " ", h["access"]).strip()[:160]))
    if h.get("parking"):
        rows.append(("駐車場", re.sub(r"\s+", " ", h["parking"]).strip()[:120]))
    d = d or {}
    ci = (f"{d['in']}から" + (f"(最終{d['last_in']})" if d.get("last_in") else "")) if d.get("in") else (f"最終{d['last_in']}まで" if d.get("last_in") else "")
    if ci:
        rows.append(("チェックイン", ci))
    if d.get("out"):
        rows.append(("チェックアウト", f"{d['out']}まで"))
    if d.get("rooms"):
        rows.append(("客室数", f"{d['rooms']}室"))
    bw = []
    for w in BATH_WORDS:
        if w in (d.get("bt") or "") and not any(w in x for x in bw):
            bw.append(w)
    if bw:
        rows.append(("お風呂", "・".join(bw[:6])))
    rv = [(lab, d.get(k)) for lab, k in (("お風呂", "bath"), ("夕食", "dinner"), ("清潔さ", "clean")) if d.get(k)]
    if h.get("review") or rv:
        rows.append(("口コミ評価", (f"総合★{h['review']}" + (f"({h['reviews']:,}件)" if h.get("reviews") else "") if h.get("review") else "")
                     + ("・" if h.get("review") and rv else "") + "・".join(f"{lab}{v}" for lab, v in rv)))
    return rows


def hotel_basic_faq(h, rows):
    """基本情報から、宿名で聞かれやすい質問と答え"""
    r = dict(rows)
    nm, src = h["name"], "(楽天トラベルの施設情報より。最新の情報は宿にご確認ください)"
    out = []
    if r.get("チェックイン") or r.get("チェックアウト"):
        out.append((f"{nm}のチェックイン・チェックアウトは何時ですか?",
                    "、".join(x for x in ((r.get("チェックイン") and f"チェックインは{r['チェックイン']}"), (r.get("チェックアウト") and f"チェックアウトは{r['チェックアウト']}")) if x) + "です" + src))
    if r.get("駐車場"):
        out.append((f"{nm}に駐車場はありますか?", f"駐車場: {r['駐車場']}" + src))
    if r.get("アクセス"):
        out.append((f"{nm}へのアクセスは?", f"{r['アクセス']}" + (f"(最寄り駅: {r['最寄り駅']})" if r.get("最寄り駅") else "") + src))
    if r.get("お風呂"):
        out.append((f"{nm}のお風呂の特徴は?", f"施設情報には「{r['お風呂']}」の記載があります" + src))
    return out


def seen_rooms(cfg, state, h, area, n=3):
    """部屋の条件(客室露天風呂など)ごとに、当サイトが空室データで実際に見た部屋名と、最後に見た取得日時。
    [(条件名, [部屋名…], 最後に見た取得日時, 見た日数)]"""
    cells = state["cells"].get(h["no"], {})
    fb = state.get("fetched_by", {}).get(area, {})
    out = []
    for fk, f in cfg["features"].items():
        if is_hotel_feat(cfg, fk):
            continue
        seen, last, days = {}, None, 0
        for iso, c in cells.items():
            v = (c.get("f") or {}).get(fk)
            if not v or not v[2] or str(v[2]).startswith("宿全体"):
                continue
            days += 1
            t = parse_dt(fb.get(iso, "")) or parse_dt(state.get("fetched", {}).get(iso, ""))
            if t and (seen.get(v[2]) is None or t > seen[v[2]]):
                seen[v[2]] = t
            elif v[2] not in seen:
                seen[v[2]] = None
            if t and (last is None or t > last):
                last = t
        if seen:
            names = sorted(seen, key=lambda x: (seen[x] is None, -(seen[x].timestamp() if seen[x] else 0)))[:n]
            out.append((f["label"], names, last, days))
    return out


def render_hotel(cfg, page, h, ctx, state, stats, demo):
    """宿ページ(1軒1ページ)。page は、その宿が載っている代表のページ(部屋の条件のページを優先)"""
    site, feat, flabel, alabel = cfg["site"], ctx["feat"], ctx["flabel"], ctx["alabel"]
    room = not is_hotel_feat(cfg, feat)
    nm_f = f"{h['name']}の{flabel}" if room else h["name"]
    fl_txt = flabel if room else "宿"
    dates, holidays, newest = ctx["dates"], ctx["holidays"], ctx["newest"]
    hf = hotel_facts(h, state, feat, dates)
    ok = hf["ok"]
    canonical = f"{ctx['base']}/hotel/{h['no']}/"
    asof = fmt_dt(newest) if newest else ""
    where = h.get("station") or h.get("address") or alabel
    prices = [p for _, p, _, _ in ok]
    lead = (f"{h['name']}({where})" + (f"の{flabel}" if room else "") + "は、" + when_txt(newest)
            + f"{day_label(dates[0], holidays)}〜{day_label(dates[-1], holidays)}のうち{len(ok)}日に空きがあります。"
            + "このページで日付を選ぶと、その日の空室と、楽天トラベルの予約ページを確認できます。")
    bullets = []
    if ok:
        d0, p0, _, rn0 = ok[0]
        lowd = min(ok, key=lambda x: x[1])
        highd = max(ok, key=lambda x: x[1])
        bullets += [f"直近の空き: {day_label(d0, holidays)} {yen_full(p0)}〜({rn0})",
                    f"この期間の最安: {day_label(lowd[0], holidays)} {yen_full(lowd[1])}〜",
                    f"この期間の最高: {day_label(highd[0], holidays)} {yen_full(highd[1])}〜",
                    f"中央値: {yen_full(int(statistics.median(prices)))}〜"]
    else:
        bullets.append(f"取得済みの日程では、{fl_txt}の空きは見つかっていません")
    bullets.append(f"宿全体で満室の日: {hf['full']}日 / 未取得の日: {hf['none']}日")
    # 独自の観測データ: 満室から空きに戻りやすいか(キャンセル待ちの目安)
    back_inf = back_info(stats, h["no"])
    hz_area = fill_hazard(stats, page["area"])
    if back_inf:
        bullets.append(f"満室からの戻りやすさ: {back_inf[3]}(当サイトの観測で、満室だった{back_inf[0]}回のうち{back_inf[1]}回、次の確認で空きが出ました・{back_inf[2]}%)")
    hist = stats.get("price", {}).get(h["no"], {}).get(feat)
    details = ["未取得の日は、まだ取得できていない日で、満室という意味ではありません。"]
    if hist and "min" in hist:
        bullets.append(f"当サイトの観測での最安: {yen_full(hist['min'][0])}〜({hist['min'][1].replace('-', '/')}泊)")
        details.append(f"「観測での最安」は、当サイトが観測を始めてからの記録です(観測日 {hist['min'][2].replace('-', '/')})。過去の全期間の価格ではありません。")
    details.append(f"料金は{cond_text(cfg)}の最安値で、楽天トラベルの空室検索APIから取得した値です。リアルタイムではないため、最終確認は楽天トラベルでお願いします。")
    s1, s2 = lead, " ".join(b + "。" for b in bullets[:4])
    scl = scene_lines(cfg, h, state, feat, ctx["today"], holidays)
    scene_txt = "。".join(f"{n}は{t}" for n, k, t in scl if k != "none") + ("。" if any(k != "none" for _, k, _ in scl) else "")
    # 検索結果で「このページで何ができるか」が先に伝わるように、説明文の最初に書く
    desc = (f"{nm_f}の今夜・今週末の空室と最安値を毎日更新。日付を選ぶと、楽天トラベルのその日の予約ページへ進めます。"
            + (f"{asof}時点で、" if asof else "") + scene_txt + (" " + s2 if s2 else ""))[:300]

    # 月ごとのカレンダー(月の見出しつき・曜日を揃える)
    def _cell(d):
        c = state["cells"].get(h["no"], {}).get(d.isoformat())
        s_ = status(c, feat)
        lab = f"<b>{d.day}</b>"
        if s_ == "ok":
            p, url, _ = c["f"][feat]
            inner_c = f"{lab}<i>◯</i><b>{yen(p)}</b>"
            u = safe_url(url or h.get("url"))
            dd = f'data-d="{d.isoformat()}" data-s="ok" data-p="{p}" data-r="{esc(c["f"][feat][2] or "")}"'
            return (f'<a class="ok" {dd} href="{esc(u)}" target="_blank" rel="sponsored nofollow noopener" aria-label="{d.month}月{d.day}日 空きあり">{inner_c}</a>'
                    if u != "#" else f'<span class="ok" {dd}>{inner_c}</span>')
        if s_ == "part":
            return f'<button type="button" class="part" data-d="{d.isoformat()}" data-s="part" aria-label="{d.month}月{d.day}日 条件の空きなし">{lab}<i>△</i></button>'
        if s_ == "full":
            return f'<button type="button" class="full" data-d="{d.isoformat()}" data-s="full" aria-label="{d.month}月{d.day}日 満室(最新を確認)">{lab}<i>満</i></button>'
        return f'<button type="button" data-d="{d.isoformat()}" data-s="none" aria-label="{d.month}月{d.day}日 未取得">{lab}<i>—</i></button>'

    cal = ""
    months = []
    for d in dates:
        if not months or months[-1][0] != (d.year, d.month):
            months.append(((d.year, d.month), []))
        months[-1][1].append(d)
    for (yy, mm), ds_ in months:
        cal += f'<div class="mstack"><div class="mh">{yy}年{mm}月</div><div class="mini">'
        cal += "".join(f'<div class="h">{w}</div>' for w in "月火水木金土日")
        cal += '<span class="pad"></span>' * ds_[0].weekday()
        cal += "".join(_cell(d) for d in ds_)
        cal += "</div></div>"

    mid_crumb = "" if page.get("virtual") else f' › <a href="../../{esc(page["slug"])}/">{esc(page["title"])}</a>'
    # 写真: 大きい写真(image_l)があれば横長で、なければ小さな写真を宿名の横に出す(楽天トラベルの施設写真)
    big = safe_url(h.get("image_l")) if str(h.get("image_l", "")).startswith("https://") else "#"
    small = safe_url(h.get("image")) if str(h.get("image", "")).startswith("https://") else "#"
    thumb = (f'<img class="hthumb" src="{esc(small)}" alt="{esc(h["name"])}の写真" width="96" height="72" decoding="async" onerror="this.remove()">'
             if big == "#" and small != "#" else "")
    hero = (f'<div class="hero hhero"><div class="crumb"><a href="../../">{esc(site["name"])}</a> › <a href="../../{esc(page["area"])}/">{esc(cfg["areas"][page["area"]]["label"])}</a>{mid_crumb} › {esc(h["name"])}</div>'
            + (f'<img class="hphoto" src="{esc(big)}" alt="{esc(h["name"])}の写真" decoding="async" onerror="this.remove()">' if big != "#" else "")
            + f'<div class="hhead">{thumb}<div><h1>{esc(nm_f)} 空室状況</h1><p class="lead">{esc(hotel_sub(h))}</p></div></div></div>')
    cta_url = (ok[0][2] if ok else None) or h.get("plan_url") or h.get("url")
    # ボタンには、どの日付のページが開くかを書く(日付が指定されていれば、ブラウザ側でその日付に差し替える)
    cta_txt = (f"{day_label(ok[0][0], holidays)}の空室・料金を楽天トラベルで確認 ›" if ok else "楽天トラベルで空室・料金を確認 ›")
    frs = hotel_feature_rows(cfg, h, state, page["area"])
    yes = [lab for lab, mark, _ in frs if mark.startswith("◯")]
    yes_links = [(lab, sl) for lab, mark, sl in frs if mark.startswith("◯")]
    # 並び: 宿名・写真 → その日の結果と予約ボタン → 日付を変える → カレンダー → 当てはまる条件 → 文章のまとめ・よくある質問
    body = [hero,
            # アドレスで日付が指定されたとき(?d=)は、その日の空きと予約ボタンを、宿名のすぐ下に出す
            hotel_pick_html(h, fl_txt, dates, ok, holidays, back_inf, [keep_pct(hz_area, i) for i in range(len(dates))]),
            f'<section class="sec hcal" id="calendar"><h2>空室カレンダー({len(dates)}日間)</h2>'
            '<p class="sub">◯=空きあり(数字は最安) ／ △=宿に空きはあるが該当の部屋は確認できない ／ 満=空室なし ／ —=未取得。日付を押すと、上にその日の料金と予約ボタンが出ます。</p>'
            f'{cal}</section>',
            '<p style="margin:10px 0" id="maincta">' + btn(cta_url, "cta", cta_txt) + "</p>"]
    body.append('<div class="sbar" id="sbar" hidden><div class="st"><b></b><small>1泊・大人2名・1室の最安</small></div>'
                '<a class="cta" target="_blank" rel="sponsored nofollow noopener">予約する ›</a></div>')
    body.append(alt_data(h, ctx, feat, dates))
    _bas = hotel_basics(h, DETAIL.get(h["no"]))
    if _bas:
        body.append(f'<section class="card" id="basics"><h2>{esc(h["name"])}の基本情報</h2><div class="scroll"><table class="stat"><tbody>'
                    + "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in _bas)
                    + '</tbody></table></div><p class="legend">楽天トラベルの施設情報より(当サイトで自動取得)。最新の情報は宿の公式案内でご確認ください。</p></section>')
    _sr = seen_rooms(cfg, state, h, page["area"])
    body.append('<section class="card" id="features"><h2>この宿に当てはまる条件</h2>'
                + "".join(f'<p class="seenr">当サイトが空室データで確認した「{esc(lab)}」の部屋: {esc("、".join(nm))}'
                          + (f"(最終確認 {t.month}/{t.day} {t:%H:%M}・{dn}日分のデータ)" if t else f"({dn}日分のデータ)") + "</p>" for lab, nm, t, dn in _sr)
                + ('<p class="fyes">' + "".join((f'<a href="../../{esc(sl)}/">{esc(lab)}</a>' if sl else f"<span>{esc(lab)}</span>") for lab, sl in yes_links) + "</p>"
                   if yes_links else "<p class='sub'>自動判定で当てはまる条件は、まだ見つかっていません。</p>")
                + f'<details><summary>すべての条件の判定を見る({len(frs)}項目)</summary><div class="scroll"><table class="stat"><tbody>'
                + "".join(f"<tr><th>{esc(lab)}</th><td>" + (f'<a href="../../{esc(sl)}/">{esc(mark)}</a>' if sl else esc(mark)) + "</td></tr>" for lab, mark, sl in frs)
                + "</tbody></table></div><p class='legend'>部屋の条件(客室露天風呂など)は部屋名、宿の条件(大浴場など)は楽天トラベルの施設情報から機械的に判定しています。"
                "「記載なし」「確認できず」は、設備がないという意味ではありません。最終確認は宿のページでお願いします。</p></details></section>")
    body.append(answer_html(asof, lead, bullets, details, "この宿の空室まとめ", mood=("happy" if ok else "sleepy"), keep=2))
    if scl:
        body.append('<section class="sec"><h2>日付別の空き状況</h2><ul class="bul">'
                    + "".join(f"<li><b>{esc(n)}</b>: {esc(t)}</li>" for n, k, t in scl) + "</ul>"
                    '<p class="sub">「取得待ち」は、まだ取得できていない日で、満室という意味ではありません。</p></section>')
    if ok:
        body.append('<section class="dsec"><details><summary><b>空きのある日と料金を一覧で見る</b>(' + str(len(ok)) + '日)</summary><ul class="rowl">'
                    + "".join(f'<li><div class="t"><span class="n">{esc(day_label(d, holidays))}</span><small>{esc(rn)}</small></div>'
                              f'<div class="pr">{yen_full(p)}<span>〜</span></div>' + btn(url or h.get("url"), "cta", "予約ページへ ›") + "</li>"
                              for d, p, url, rn in ok) + "</ul></details></section>")
    if page.get("virtual"):
        body.append(f'<p><a href="../../{esc(page["area"])}/">{esc(alabel)}の宿の空室(今夜・明日・今週末・日付指定)を見る</a></p>')
    else:
        body.append(f'<p><a href="../../{esc(page["slug"])}/">{esc(alabel)}の{esc(flabel)} 全{len(ctx["rows"])}軒の空室表を見る</a></p>')

    # 宿名×日付の質問(「〇〇 11/21 空いてる?」)に答える: 今週末の各日の、宿全体の空きと(部屋の条件なら)その部屋の空き
    _wk_ds = next((sc["dates"] for sc in all_scenes(cfg, ctx["today"]) if sc["slug"] == "shumatsu"), [])
    _wk_short, _wk_faq = [], []
    for d in _wk_ds:
        c = state["cells"].get(h["no"], {}).get(d.isoformat())
        if not c:
            continue
        ft = parse_dt(state.get("fetched_by", {}).get(page["area"], {}).get(d.isoformat(), "")) or parse_dt(state.get("fetched", {}).get(d.isoformat(), ""))
        whole = (c.get("any") or [None])[0] if c.get("s") == "v" else None
        if c.get("s") == "v":
            _wk_short.append(f"{md(d)}({WD[d.weekday()]})空きあり" + (f"¥{whole:,}〜" if whole else ""))
            a = f"宿全体で空きあり(2名1室の最安{yen_full(whole)}〜)" if whole else "宿全体で空きあり"
        else:
            _wk_short.append(f"{md(d)}({WD[d.weekday()]})満室")
            a = "宿全体で満室(大人2名1室の条件。キャンセルで空くことがあります)"
        if room and c.get("s") == "v":
            st_ = status(c, feat)
            a += (f"。{flabel}の部屋も空きあり「{short_room(c['f'][feat][2], 30)}」{yen_full(c['f'][feat][0])}〜" if st_ == "ok"
                  else f"。ただし{flabel}の部屋の空きは確認できませんでした")
        _ft3, _v3 = adults_day(state, page["area"], d)
        if _v3 is not None and c.get("s") == "v":
            _m3 = next((x for x in _v3 if x[1] == h["no"]), None)
            a += (f"。大人3名1室でも空きあり({yen_full(_m3[0])}〜・3名1室の合計)" if _m3 else "。大人3名1室では空きが見つかりませんでした")
        _wk_faq.append((f"{day_label(d, holidays)}に{h['name']}は空いていますか?",
                        f"{day_label(d, holidays)}は{a}" + (f"({ft.month}/{ft.day} {ft:%H:%M}取得・1泊)。" if ft else "(1泊)。")
                        + "予約前に楽天トラベルで最新の空室をご確認ください。"))
    _wk_t = "・".join(_wk_short)
    if _wk_short:
        desc = (f"{h['name']}は今週末 " + "、".join(_wk_short) + f"({cond_text(cfg)})。" + desc)[:300]
    faq = _wk_faq + hotel_basic_faq(h, hotel_basics(h, DETAIL.get(h["no"]))) + [
        (f"{nm_f}は今空いていますか?", s1 + s2),
        (f"{h['name']}にはどんな特徴がありますか?",
         (f"楽天トラベルの情報からの自動判定では、{'、'.join(yes)}に当てはまります。" if yes else "自動判定で当てはまる条件は、まだ見つかっていません。")
         + "設備の最終確認は宿のページでお願いします。"),
        (f"{nm_f}の料金はいくらですか?",
         (f"取得済みの空き日の最安は{yen_full(min(prices))}〜、最高は{yen_full(max(prices))}〜です({cond_text(cfg)})。" if prices
          else "現在、空きが見つかっていないため、料金は取得できていません。")),
        ("この情報はいつのものですか?",
         (f"最新の取得は{newest:%Y-%m-%d %H:%M}(日本時間)です。" if newest else "取得前です。") + "リアルタイムではないため、予約前に楽天トラベルでご確認ください。"),
    ]
    body.append(faq_html(faq))
    body.append(share_html(canonical, f"{nm_f} 空室状況 | {site['name']}"))
    hotel_ld = {"@type": "Hotel", "name": h["name"], "url": canonical}
    if h.get("address"):
        addr = {"@type": "PostalAddress", "addressCountry": "JP", "streetAddress": h["address"]}
        if h.get("zip"):
            addr["postalCode"] = str(h["zip"])
        hotel_ld["address"] = addr
    g = geo_ok(h)
    if g:
        hotel_ld["geo"] = {"@type": "GeoCoordinates", "latitude": g[0], "longitude": g[1]}
    try:
        rv = float(h.get("review"))
        if rv > 0 and h.get("reviews"):
            hotel_ld["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": rv, "bestRating": 5, "ratingCount": int(h["reviews"])}
    except (TypeError, ValueError):
        pass
    if prices:
        hotel_ld["priceRange"] = f"{yen_full(min(prices))}〜{yen_full(max(prices))}"
    if ok:
        cheap = min(ok, key=lambda x: x[1])
        hotel_ld["makesOffer"] = offer_ld(cheap[1], cheap[0], parse_dt(state.get("fetched", {}).get(cheap[0].isoformat(), "")), canonical,
                                          (f"{flabel}・" if room else "") + "1泊・大人2名・1部屋の最安値")
    if h.get("image", "").startswith("https://"):
        hotel_ld["image"] = h["image"]
    if yes:
        hotel_ld["amenityFeature"] = [{"@type": "LocationFeatureSpecification", "name": y, "value": True} for y in yes]
    ld = [breadcrumb_ld([(site["name"], ctx["base"] + "/"), (cfg["areas"][page["area"]]["label"], f"{ctx['base']}/{page['area']}/")]
                        + ([] if page.get("virtual") else [(page["title"], f"{ctx['base']}/{page['slug']}/")]) + [(h["name"], canonical)]),
          {"@context": "https://schema.org", "@type": "WebPage", "name": f"{nm_f} 空室状況", "url": canonical, "inLanguage": "ja",
           "dateModified": (newest or datetime.now(JST)).isoformat(timespec="seconds"), "about": hotel_ld,
           "isPartOf": {"@type": "WebSite", "name": site["name"], "url": ctx["base"] + "/"}},
          faq_ld(faq)]
    # 宿名で検索する人が多いので、宿名+「空室・予約」+いまの空き状況を見出しにする
    nm_t = f"{h['name']}の空室・予約" + (f"({flabel})" if room else "")
    upd = f"【{md(ctx['today'])}更新】"
    if _wk_t:
        title = f"{nm_t}|今週末 {_wk_t}{upd} | {site['name']}"
    elif ok and ok[0][0] == ctx["today"]:
        title = f"{nm_t}|今夜空きあり {yen_full(ok[0][1])}〜{upd} | {site['name']}"
    elif ok:
        title = f"{nm_t}|{md(ok[0][0])}から空きあり {yen_full(min(prices))}〜{upd} | {site['name']}"
    else:
        title = f"{nm_t}|空き状況と料金{upd} | {site['name']}"
    return shell(cfg, demo, "../../", title, desc, canonical, "".join(body), ld)


# ---- 運営方法 -------------------------------------------

# ---- 空室データ統計ページ(/stats/)と、AI向けの案内(llms.txt) ----------------
# 統計の数字は、すべて当サイトが楽天トラベルAPIから取得した値の集計です(推測はしません)。

STAT_MIN_HOTELS = 5     # 対象の宿がこれ未満の組み合わせは、統計の表に載せない(数字がぶれるため)
DAY_GROUPS = [("wd", "平日(日〜木)"), ("fri", "金曜"), ("sat", "土曜・祝前日")]


def day_group(d, holidays):
    """宿泊日の区分。祝日の前日は、土曜と同じ「休前日」として扱う"""
    if d.weekday() == 5 or (d + timedelta(days=1)).isoformat() in holidays:
        return "sat"
    return "fri" if d.weekday() == 4 else "wd"


def page_snapshot(cfg, hotels, pstate, page, today):
    """いま持っている空室データから、(エリア, 条件)ごとの「空き率」と「最安値の中央値」を、宿泊日の区分ごとに出す"""
    feat = page["feature"]
    rows = hotels_for_page(cfg, hotels, pstate, page)
    holidays = set(cfg.get("holidays", []))
    dates = [today + timedelta(days=i) for i in range(area_days(cfg, page["area"]))]
    fetched = pstate.get("fetched", {})
    info = summarize(rows, dates, feat, pstate)
    groups = {g: [] for g, _ in DAY_GROUPS}
    for d in dates:
        if d.isoformat() in fetched and rows:
            groups[day_group(d, holidays)].append(info[d.isoformat()])
    out = {"slug": page["slug"], "area": page["area"], "feat": feat,
           "alabel": cfg["areas"][page["area"]]["label"], "flabel": cfg["features"][feat]["label"],
           "hotels": len(rows), "days": sum(len(v) for v in groups.values())}
    for g, lst in groups.items():
        lows = sorted(low for _, low in lst if low)
        out[g] = {"days": len(lst),
                  "rate": (sum(n for n, _ in lst) / (len(rows) * len(lst))) if lst and rows else None,
                  "low": lows[len(lows) // 2] if lows else None}
    return out


def pct1(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def stat_findings(snaps):
    """表から、目立つ点を文章にする(宿が10軒以上・平日と休前日が2日以上ずつある組み合わせだけ)"""
    ok = [x for x in snaps if x["hotels"] >= 10 and x["wd"]["days"] >= 2 and x["sat"]["days"] >= 2
          and x["wd"]["rate"] is not None and x["sat"]["rate"] is not None]
    out = []
    if not ok:
        return out
    tight = min(ok, key=lambda x: x["sat"]["rate"])
    out.append(f"土曜・祝前日に最も空きが少ないのは{tight['alabel']}の「{tight['flabel']}」で、空きがある宿は平均{pct1(tight['sat']['rate'])}"
               f"(平日は{pct1(tight['wd']['rate'])})です。")
    loose = max(ok, key=lambda x: x["sat"]["rate"])
    if loose is not tight:
        out.append(f"土曜・祝前日でも空きが多いのは{loose['alabel']}の「{loose['flabel']}」で、平均{pct1(loose['sat']['rate'])}の宿に空きがあります。")
    gap = [x for x in ok if x["wd"]["rate"] > 0]
    if gap:
        g = min(gap, key=lambda x: x["sat"]["rate"] / x["wd"]["rate"])
        if g is not tight:
            out.append(f"平日との差が最も大きいのは{g['alabel']}の「{g['flabel']}」で、土曜・祝前日の空き率は平日の約{g['sat']['rate'] / g['wd']['rate'] * 10:.0f}割です。")
    pr = [x for x in ok if x["wd"]["low"] and x["sat"]["low"]]
    if pr:
        r = max(pr, key=lambda x: x["sat"]["low"] / x["wd"]["low"])
        out.append(f"料金の差が最も大きいのは{r['alabel']}の「{r['flabel']}」で、最安値の中央値は平日{yen_full(r['wd']['low'])}、"
                   f"土曜・祝前日{yen_full(r['sat']['low'])}(約{r['sat']['low'] / r['wd']['low']:.1f}倍)です。")
    return out


def stats_csv(snaps, asof):
    cols = ["area", "area_label", "condition", "condition_label", "hotels"]
    for g, _ in DAY_GROUPS:
        cols += [f"{g}_nights", f"{g}_vacancy_rate", f"{g}_median_lowest_price_jpy"]
    cols.append("as_of_jst")
    lines = [",".join(cols)]
    for x in snaps:
        vals = [x["area"], x["alabel"], x["feat"], x["flabel"], str(x["hotels"])]
        for g, _ in DAY_GROUPS:
            v = x[g]
            vals += [str(v["days"]), "" if v["rate"] is None else f"{v['rate']:.3f}", "" if v["low"] is None else str(v["low"])]
        vals.append(asof)
        lines.append(",".join('"' + str(c).replace('"', '""') + '"' if ("," in str(c) or '"' in str(c)) else str(c) for c in vals))
    return "\n".join(lines) + "\n"


def trans_html(cfg, stats, snaps, today):
    """「空きの動き」(前回の確認から今回までに、空きが埋まった/出た割合)。十分たまるまでは数字を出さない"""
    since = stats.get("since")
    try:
        days = (today - datetime.fromisoformat(since).date()).days + 1
    except Exception:
        days = 0
    tot = sum(v[0] + v[2] for t in stats.get("trans", {}).values() for v in t.values())
    head = '<section class="card" id="movement"><h2>空きの動き(埋まる早さ・空きが出る割合)</h2>'
    if not since or days < READY_DAYS or tot < READY_N:
        ready = (datetime.fromisoformat(since).date() + timedelta(days=READY_DAYS - 1)).strftime("%Y年%-m月%-d日") if since else "—"
        return (head + f"<p>同じ宿・同じ日程を繰り返し確認して、「空いていた部屋が次の確認までに埋まった割合」と「満室だった日に空きが出た割合」を集計しています。"
                f"現在は蓄積中です(観測開始: {esc(since or '—')}、{days}日目)。数字がぶれないよう、{READY_DAYS}日分たまってから公開します"
                f"(公開予定: {esc(ready)}ごろ)。</p></section>")
    body = ""
    for x in snaps:
        t = stats.get("trans", {}).get(f"{x['area']}/{x['feat']}")
        if not t:
            continue
        cells = ""
        for b in ("0", "1"):
            v = t.get(b, [0, 0, 0, 0])
            cells += f"<td>{pct(v[3], v[2])}</td><td>{pct(v[1], v[0])}</td>"
        body += f"<tr><th>{esc(x['alabel'])}・{esc(x['flabel'])}</th>{cells}</tr>"
    if not body:
        return ""
    return (head + f"<p>{esc(since)}から{days}日間の観測です。前回の確認と今回の確認を比べています(確認の間隔は{esc(freq_text(cfg))})。</p>"
            '<div class="scroll"><table class="stat"><thead><tr><th rowspan="2">エリア・条件</th><th colspan="2">0〜6日先の日程</th><th colspan="2">7〜13日先の日程</th></tr>'
            "<tr><th>空き→埋まった</th><th>満・未確認→空きが出た</th><th>空き→埋まった</th><th>満・未確認→空きが出た</th></tr></thead>"
            f"<tbody>{body}</tbody></table></div>"
            f"<p class='legend'>件数が{MIN_N}件に満たない区分は「—」です。「埋まった」には、予約のほか楽天側の掲載の変更も含みます。</p></section>")


def render_stats(cfg, snaps, stats, newest, now, demo):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = base + "/stats/"
    today = now.date()
    asof_dt = newest or now
    snaps = [x for x in snaps if x["hotels"] >= STAT_MIN_HOTELS and x["days"]]
    snaps.sort(key=lambda x: (list(cfg["areas"]).index(x["area"]), list(cfg["features"]).index(x["feat"])))
    finds = stat_findings(snaps)
    lead = (f"{when_txt(asof_dt)}{len({x['area'] for x in snaps})}エリア・{len(snaps)}通りの条件について、"
            "宿泊日の曜日ごとに「空きがある宿の割合」と「最安値の中央値」をまとめました。")
    details = ["空き率は、その条件に当てはまる宿のうち、その日に空きがある宿の割合を、区分ごとに平均したものです。"
               f"最安値の中央値は、日ごとの最安値({cond_text(cfg)})を並べた真ん中の値です。",
               f"対象は向こう約1か月の日程のうち、取得済みの日だけです。宿が{STAT_MIN_HOTELS}軒未満の組み合わせは載せていません。"
               "楽天トラベルの空室検索APIから自動で取得した値の集計で、リアルタイムの在庫や将来の予測ではありません。"]
    rows = ""
    for x in snaps:
        cells = "".join(f"<td>{pct1(x[g]['rate'])}</td>" for g, _ in DAY_GROUPS)
        cells += "".join(f"<td>{yen_full(x[g]['low']) if x[g]['low'] else '—'}</td>" for g in ("wd", "sat"))
        rows += (f'<tr><th><a href="../{esc(x["slug"])}/">{esc(x["alabel"])}・{esc(x["flabel"])}</a></th>'
                 f"<td>{x['hotels']}</td>{cells}</tr>")
    table = ('<section class="card" id="snapshot"><h2>曜日別の空き率と最安値</h2>'
             f'<p class="sub">{esc(fmt_dt(asof_dt))}時点。向こう約1か月の取得済みの日程が対象です。</p>'
             '<div class="scroll"><table class="stat"><thead><tr><th>エリア・条件</th><th>宿の数</th>'
             + "".join(f"<th>空き率<br>{esc(l)}</th>" for _, l in DAY_GROUPS)
             + "<th>最安値の中央値<br>平日</th><th>最安値の中央値<br>土曜・祝前日</th></tr></thead>"
             f"<tbody>{rows}</tbody></table></div>"
             '<p class="legend">空き率=その条件の宿のうち、その日に空きがある宿の割合(区分ごとの平均)。料金は1泊・大人2名・1部屋。'
             '<a href="latest.csv">この表をCSVでダウンロード</a>(引用・再利用の際は「ヤドミッケ調べ」と出典の記載をお願いします)。</p></section>')
    inner = ('<div class="hero"><div class="crumb"><a href="../">' + esc(site["name"]) + '</a> › 空室データ統計</div>'
             '<h1>温泉地の空室データ統計</h1><p class="lead">当サイトが楽天トラベルから自動で集めている空室データを、エリア・条件・曜日ごとに集計しています。自動で更新されます。</p></div>'
             + answer_html(fmt_dt(asof_dt), lead, finds, details, "いまの傾向", "集計方法と注意点")
             + table + trans_html(cfg, stats, snaps, today)
             + '<section class="card"><h2>このデータについて</h2><p>出典: 楽天トラベル 空室検索API(楽天ウェブサービス)をもとにヤドミッケが集計。'
               '取得方法・判定方法・限界は<a href="../about/">データの取得方法と注意点</a>をご覧ください。</p></section>')
    desc = lead + (finds[0] if finds else "")
    ld = [breadcrumb_ld([(site["name"], base + "/"), ("空室データ統計", canonical)]),
          {"@context": "https://schema.org", "@type": "Dataset", "name": "温泉地の曜日別 空室率・最安値(ヤドミッケ調べ)",
           "description": lead + " " + " ".join(finds), "url": canonical, "inLanguage": "ja",
           "dateModified": asof_dt.isoformat(timespec="seconds"),
           "temporalCoverage": f"{today.isoformat()}/{(today + timedelta(days=31)).isoformat()}",
           "spatialCoverage": [cfg["areas"][a]["label"] for a in dict.fromkeys(x["area"] for x in snaps)],
           "creator": {"@type": "Organization", "name": site["name"], "url": base + "/"},
           "isBasedOn": "楽天トラベル 空室検索API(楽天ウェブサービス)",
           "variableMeasured": ["空き率(空きがある宿の割合)", "最安値の中央値(1泊・大人2名・1部屋)"],
           "distribution": [{"@type": "DataDownload", "encodingFormat": "text/csv", "contentUrl": canonical + "latest.csv"},
                            {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": f"{base}/{DATA_JSON}"}]}]
    return shell(cfg, demo, "../", f"温泉地の空室データ統計(曜日別の空き率・最安値) | {site['name']}", desc, canonical, inner, ld)


def llms_txt(cfg, digests, snaps_ok, now, wl=()):
    """AIアシスタント向けの、サイトの短い案内(https://llmstxt.org/ の書式)"""
    site = cfg["site"]
    base = site["url"].rstrip("/")
    lines = [f"# {site['name']}", "",
             f"> 温泉地の宿の空室を、条件(客室露天風呂・ペット可・大浴場など)と日付ごとにまとめた日本語サイトです。"
             f"楽天トラベルの空室検索APIから自動で取得し、{freq_text(cfg)}を目安に更新しています。"
             f"料金は{cond_text(cfg)}の最安値です。リアルタイムの在庫ではないため、予約前に楽天トラベルでの確認が必要です。", "",
             f"最終更新: {now:%Y-%m-%d %H:%M}(日本時間)", "",
             "## 空室ページ(エリア×条件)", ""]
    for page, n_rows, today_n, digest in digests:
        alabel = cfg["areas"][page["area"]]["label"]
        flabel = cfg["features"][page["feature"]]["label"]
        lines.append(f"- [{page['title']}]({base}/{page['slug']}/): {alabel}の{flabel} {n_rows}軒の空室表(本日空き{today_n}軒)")
    hubs = list(dict.fromkeys(page["feature"] for page, *_ in digests))
    lines += ["", "## 条件ごとの全エリアまとめ(おすすめ・エリア比較)", ""]
    lines += [f"- [{gnoun(cfg, f)} 全エリアの空室とおすすめ]({base}/{hub_slug(cfg, f)}/)" for f in hubs]
    if wl:
        lines += ["", "## 日程ごとの全エリアまとめ(条件を問わない空き)", ""]
        lines += [f"- [{when_title(k, n)}]({base}/{k}/): 全エリアの空きのある宿の数・最安値・口コミ上位の空き宿" for k, n, *_ in wl]
        lines.append(f"- [きのうから空きが出た宿]({base}/ugoki/): 前回満室で、今回空きが見つかった宿と日付(直近24時間)")
        lines += ["", "## エリアごとのページ(条件を問わない空きと料金相場)", ""]
        lines += [f"- [{a['label']}の宿の空室]({base}/{k}/): 今夜・明日・今週末などの空き数、曜日別の料金相場" for k, a in cfg["areas"].items()]
        lines += ["", "## 料金と月別レポート", "",
                  f"- [温泉地の宿の料金相場]({base}/souba/): エリア・曜日別の最安値の中央値",
                  f"- [温泉地の空室レポート(月別)]({base}/report/): 毎日の観測を月ごとにまとめたレポート",
                  "", "## 機械向けのデータ", "",
                  f"- [空室・料金データ(JSON)]({base}/data/latest.json): 全エリアの今夜・明日・今週末などの空き数・最安値・取得日時と、曜日別の料金相場。ページと同じ頻度で更新"]
    lines += ["", "## 日付を指定してページを開く", "",
              "ユーザーが日付を指定して質問した場合は、次の形のリンクを案内すると、その日付を選んだ状態でページが開きます(チェックイン日、YYYY-MM-DD)。",
              "",
              f"- エリアのページ: {base}/(エリア名)/?d=YYYY-MM-DD  例: {base}/hakone/?d=2026-11-28(条件を問わず、その日に空きのある宿の数・口コミ上位の空き宿・予約リンク)",
              f"- エリア×条件のページ: {base}/(ページ名)/?d=YYYY-MM-DD  例: {base}/hakone-roten/?d=2026-11-28(箱根の客室露天風呂付きの宿の、その日の空室と料金の一覧)",
              f"- 宿ごとのページ: {base}/hotel/(施設番号)/?d=YYYY-MM-DD (その宿の、その日の空き・最安値と、楽天トラベルの予約ページへのリンク)",
              f"- 今夜・明日・今週末などは、日付つきのページがあります: {base}/konya/ 、{base}/ashita/ 、{base}/shumatsu/",
              "- 取得している範囲は、おおむね今日から3〜4週間先までです(年末年始など一部の日程はそれ以上先も取得)。範囲外の日付は、ページ側で既定の日付に戻ります。"]
    lines += ["", "## 宿ごとのページ", "", f"- {base}/hotel/(施設番号)/ : 宿ごとの空室カレンダー・料金と、条件(客室露天風呂・大浴場など)の判定結果", ""]
    lines += ["", "## データと方法", "",
              f"- [温泉地の空室データ統計]({base}/stats/): エリア・条件・曜日ごとの空き率と最安値の中央値" + ("(CSVあり)" if snaps_ok else ""),
              f"- [データの取得方法と注意点]({base}/about/): 取得元・更新頻度・判定方法・限界・広告について", "",
              "## 引用の際のお願い", "",
              "- 数字には取得日時を添えてください(各ページの「いまの空室まとめ」に記載)。",
              "- 出典は「ヤドミッケ(楽天トラベルの空室データをもとに集計)」としてください。", ""]
    return "\n".join(lines)


# ---- 選び方ガイド(条件ごとの説明 + データにもとづくおすすめ) ----------------
# 説明文は一般的な目安だけを書き、宿ごとの事実は楽天トラベルの掲載値と当サイトの取得データだけから作ります。

GUIDES = {
    "kinenbi": {"noun": "記念日・カップル向けプランのある宿",
                "what": "記念日・カップル向けプランは、ケーキや花束、スパークリングワインなどのお祝いの特典や、貸切風呂・客室露天風呂などを組み合わせた、2人旅向けのプランです。",
                "who": "誕生日や結婚記念日、プロポーズなどの特別な日の旅行や、カップル・夫婦でゆっくり過ごしたい方に向いています。",
                "tips": ["特典(ケーキ・花束など)は事前の予約が必要なことが多いので、プランの説明を確認しましょう。", "記念日プランは数が限られ、週末から先に埋まりやすい傾向があります。", "客室露天風呂や貸切風呂と組み合わせると、より特別な時間になります。"]},
    "roten": {"noun": "客室露天風呂付きの宿",
              "what": "客室露天風呂は、部屋のベランダやテラスに専用の露天風呂がある客室です。大浴場に行かなくても、好きな時間に人目を気にせず温泉を楽しめます。",
              "who": "カップル・夫婦の記念日旅行や、大浴場が苦手な方、小さな子ども連れで部屋でゆっくりしたい方に向いています。",
              "tips": ["同じ宿でも、露天風呂付きの部屋は数が少なく、週末から先に埋まりやすい傾向があります。", "「温泉」か「沸かし湯」か、半露天(屋根つき)かどうかは部屋ごとに違うので、プランの説明で確認しましょう。", "料金は一般の客室より高めです。平日や連休を外した日程だと選択肢が広がります。"]},
    "pet": {"noun": "ペットと泊まれる宿",
            "what": "ペット可の客室は、犬などのペットと同じ部屋に泊まれる客室です。ドッグラン付きの宿や、食事処にも同伴できる宿もあります。",
            "who": "愛犬と一緒に旅行したい方や、ペットを預けずに家族全員で出かけたい方に向いています。",
            "tips": ["泊まれるペットの種類・大きさ・頭数は宿ごとに決まっています。予約前に必ず条件を確認しましょう。", "ワクチン接種の証明書や、ケージ・トイレ用品の持参を求められることがあります。", "ペット可の部屋は数が少ないため、週末や連休は早めの予約がおすすめです。"]},
    "daiyoku": {"noun": "大浴場のある宿",
                "what": "大浴場は、宿泊客がみんなで使う広い浴場です。露天風呂やサウナを併設している宿もあります。",
                "who": "広いお風呂でのびのび入浴したい方や、料金をおさえつつ温泉を楽しみたい方に向いています。",
                "tips": ["入浴できる時間帯や、男女の入れ替えの有無は宿ごとに違います。", "夕食前後は混みやすいので、早朝や夜遅めの時間がねらい目です。"]},
    "kashikiri": {"noun": "貸切風呂のある宿",
                  "what": "貸切風呂(家族風呂)は、決まった時間だけ家族やカップルで貸し切って使えるお風呂です。",
                  "who": "家族やカップルで一緒に入りたい方、小さな子ども連れや、人目が気になる方に向いています。",
                  "tips": ["予約制か、空いていれば使える方式か、有料か無料かは宿ごとに違います。", "1回あたりの利用時間が決まっていることが多いので、チェックイン時に確認しましょう。"]},
    "kakenagashi": {"noun": "源泉かけ流しの宿",
                    "what": "源泉かけ流しは、湧き出た温泉を循環させずに浴槽へ注ぎ続ける方式です。",
                    "who": "温泉そのものの質にこだわりたい方に向いています。",
                    "tips": ["加水・加温の有無は宿ごとに違います。宿の温泉の説明を確認しましょう。"]},
    "chiisana": {"noun": "客室10室以下の小さな宿",
                 "what": "客室数が10室以下の小さな宿は、ほかの宿泊客が少なく、静かに過ごしやすいのが特徴です。",
                 "who": "落ち着いた雰囲気で過ごしたい方や、行き届いたもてなしを求める方に向いています。",
                 "tips": ["部屋数が少ないぶん、週末はすぐに満室になりやすいので、早めの予約が安心です。"]},
    "osoitsuki": {"noun": "夜遅くでもチェックインできる宿",
                  "what": "最終チェックインが22時以降の宿です。仕事のあとや、遅い時間の到着でも泊まれます。",
                  "who": "仕事帰りに出発する方や、観光・移動で到着が遅くなる方に向いています。",
                  "tips": ["遅い到着だと夕食が付かないプランもあります。食事の有無をプランで確認しましょう。"]},
    "ekichika": {"noun": "駅から徒歩5分以内の宿",
                 "what": "最寄り駅から歩いて5分以内で着ける宿です。",
                 "who": "電車で旅行する方や、荷物が多い方、車を使わない方に向いています。",
                 "tips": ["徒歩の分数は宿の掲載値です。坂道や階段がある場合もあります。"]},
    "yushoku": {"noun": "夕食の口コミ評価が高い宿",
                "what": "楽天トラベルの口コミで、夕食の評価が平均4.5以上の宿です。",
                "who": "食事を旅の楽しみの中心にしたい方に向いています。",
                "tips": ["夕食付きのプランを選びましょう。素泊まりや朝食のみのプランもあります。"]},
    "furohyoka": {"noun": "お風呂の口コミ評価が高い宿",
                  "what": "楽天トラベルの口コミで、お風呂の評価が平均4.5以上の宿です。",
                  "who": "温泉やお風呂の満足度を重視する方に向いています。",
                  "tips": ["大浴場・貸切風呂・客室露天風呂のどれが評価されているかは、宿のページで確認しましょう。"]},
    "seiketsu": {"noun": "清潔さの口コミ評価が高い宿",
                 "what": "楽天トラベルの口コミで、清潔感の評価が平均4.5以上の宿です。",
                 "who": "部屋や水回りのきれいさを重視する方に向いています。",
                 "tips": ["改装の時期や部屋のタイプによって印象が変わることがあります。"]},
    "kids": {"noun": "子連れ向けの宿",
             "what": "キッズ・ベビー向けの設備やサービス(子ども用の食事・アメニティ・添い寝など)の記載がある宿です。",
             "who": "乳幼児や小学生の子ども連れの家族旅行に向いています。",
             "tips": ["添い寝の年齢や子ども料金の扱いは宿ごとに違うので、人数の入力に注意しましょう。", "貸切風呂や客室露天風呂がある宿だと、子どもと一緒に入浴しやすくなります。"]},
    "sauna": {"noun": "サウナのある宿",
              "what": "大浴場などにサウナを備えた宿です。温泉とサウナを交互に楽しめます。",
              "who": "サウナ好きの方や、温泉で体をしっかり温めたい方に向いています。",
              "tips": ["男湯だけ・女湯だけにサウナがある宿や、時間帯で使えない宿もあります。宿の説明で確認しましょう。", "水風呂や外気浴スペースの有無は宿ごとに違います。"]},
    "iou": {"noun": "硫黄泉の宿",
            "what": "硫黄泉は、硫黄の成分を含む温泉で、独特のにおいや白くにごったお湯が特徴のことが多い泉質です。",
            "who": "「温泉らしい」においやにごり湯を楽しみたい方に向いています。",
            "tips": ["硫黄は銀や銅のアクセサリーを変色させることがあるので、入浴前に外しましょう。", "宿によって源泉が複数あり、浴場ごとに泉質が違う場合があります。"]},
    "ganbanyoku": {"noun": "岩盤浴のある宿",
                   "what": "岩盤浴は、温めた石の上に横になって、じんわり汗をかく入浴法です。",
                   "who": "熱いお風呂が苦手な方や、ゆっくり体を温めたい方に向いています。",
                   "tips": ["有料・予約制・女性専用などの決まりがある宿もあります。", "汗をかくので、水分を多めにとりましょう。"]},
    "sougei": {"noun": "送迎のある宿",
               "what": "最寄り駅などから、宿の送迎バスや送迎車を利用できる宿です。",
               "who": "車を使わずに旅行する方や、荷物が多い方、坂道の多い温泉地に泊まる方に向いています。",
               "tips": ["送迎は予約制・時間指定のことが多いです。予約後に宿へ連絡しておきましょう。", "有料の場合や、共同運行のバスの場合もあります。"]},
    "barrierfree": {"noun": "バリアフリー対応の宿",
                    "what": "バリアフリー設備や、車椅子で使える客室・トイレなどの記載がある宿です。",
                    "who": "車椅子を使う方や、高齢の家族と一緒に旅行する方に向いています。",
                    "tips": ["対応の範囲(段差・浴場の手すり・客室の広さなど)は宿ごとに大きく違います。必要な設備は予約前に宿へ確認しましょう。", "車椅子利用者用の客室は数が少ないため、早めの予約がおすすめです。"]},
}
PICK_MIN_REVIEWS = 20   # おすすめに載せるのは、口コミがこの件数以上の宿だけ
DETAIL = {}             # 宿の詳細(main() が入れる)
SNAPS = {}              # ページごとの曜日別集計(main() が入れる)


def gnoun(cfg, feat):
    g = GUIDES.get(feat)
    return g["noun"] if g else cfg["features"][feat]["label"]


def pick_reason(cfg, feat, h, cell):
    """おすすめの理由に添える、その条件ならではの事実(掲載値・取得データのみ)"""
    d = DETAIL.get(h["no"]) or {}
    if not is_hotel_feat(cfg, feat) and cell and feat in cell.get("f", {}):
        return f"空いていた部屋: {cell['f'][feat][2]}"
    if feat == "furohyoka" and d.get("bath"):
        return f"お風呂の口コミ {d['bath']:.2f}"
    if feat == "yushoku" and d.get("dinner"):
        return f"夕食の口コミ {d['dinner']:.2f}"
    if feat == "seiketsu" and d.get("clean"):
        return f"清潔感の口コミ {d['clean']:.2f}"
    if feat == "chiisana" and d.get("rooms"):
        return f"客室 {d['rooms']}室"
    if feat == "osoitsuki" and d.get("last_in"):
        return f"最終チェックイン {d['last_in']}"
    if feat == "ekichika":
        w = hotelfeat.walk_minutes(h)
        if w is not None:
            return f"駅から徒歩{w}分"
    return ""


def guide_picks(cfg, rows, state, feat, dates, n=5):
    """口コミ評価の高い順(件数が少ない宿は除く)。空きのある日がある宿だけ。(宿, 最安, その日, 空き日数, セル)"""
    out = []
    for h in rows:
        if (h.get("reviews") or 0) < PICK_MIN_REVIEWS or not _rv(h):
            continue
        oks = []
        for d in dates:
            c = state["cells"].get(h["no"], {}).get(d.isoformat())
            if status(c, feat) == "ok":
                oks.append((c["f"][feat][0], d, c))
        if oks:
            p, d, c = min(oks, key=lambda x: (x[0], x[1]))
            out.append((h, p, d, len(oks), c))
    out.sort(key=lambda x: (-_rv(x[0]), -(x[0].get("reviews") or 0), x[0]["name"]))
    return out[:n]


def snap_sentence(alabel, noun, snap):
    if not snap or snap["wd"]["rate"] is None or snap["sat"]["rate"] is None or snap["wd"]["days"] < 2 or snap["sat"]["days"] < 2:
        return ""
    t = (f"ヤドミッケの集計では、{alabel}の{noun}のうち空きがある宿は、平日(日〜木)が平均{pct1(snap['wd']['rate'])}、"
         f"土曜・祝前日が平均{pct1(snap['sat']['rate'])}です")
    if snap["wd"]["low"] and snap["sat"]["low"]:
        t += f"。最安値の中央値は、平日{yen_full(snap['wd']['low'])}、土曜・祝前日{yen_full(snap['sat']['low'])}です"
    t += "。"
    if snap["wd"]["rate"] >= snap["sat"]["rate"] * 1.5:
        t += "選択肢を広げたいなら、平日の宿泊がねらい目です。"
    return t


def picks_list_html(cfg, feat, picks, holidays, ndays, show_area=False):
    lis = []
    for h, p, d, n_ok, c in picks:
        why = pick_reason(cfg, feat, h, c)
        area = (cfg["areas"].get(h.get("_area", ""), {}).get("label", "") + " ・ ") if show_area else ""
        href = hotel_href(h)
        name = f'<a href="{esc(href)}">{esc(h["name"])}</a>' if href else btn_plain(h.get("url"), esc(h["name"]))
        lis.append(f"<li>{name}<small>{esc(area)}★{esc(h['review'])}(口コミ{(h.get('reviews') or 0):,}件)"
                   + (f" ・ {ndays}日中{n_ok}日に空き" if ndays else f" ・ 空きのある日{n_ok}日")
                   + f" ・ 最安{yen_full(p)}〜({esc(day_label(d, holidays))})"
                   + (f" ・ {esc(why)}" if why else "") + "</small></li>")
    return '<ol class="hl picks">' + "".join(lis) + "</ol>"


def picks_answer(picks, holidays):
    return "、".join(f"{h['name']}(★{h['review']}・{yen_full(p)}〜)" for h, p, d, n_ok, c in picks)


def guide_html(cfg, feat, alabel, rows, state, dates, holidays, snap, newest):
    """エリア×条件ページの「選び方とおすすめ」。(HTML, FAQに足す項目) を返す"""
    g = GUIDES.get(feat)
    noun = gnoun(cfg, feat)
    picks = guide_picks(cfg, rows, state, feat, dates)
    faq = []
    parts = [f'<section class="card guide" id="guide"><h2>{esc(alabel)}で{esc(noun)}を選ぶポイントとおすすめ</h2>']
    if g:
        parts.append(f"<p>{esc(g['what'])}{esc(g['who'])}</p>")
    st = snap_sentence(alabel, noun, snap)
    if st:
        parts.append(f"<p><b>{esc(st)}</b>(<a href=\"../stats/\">集計の詳細</a>)</p>")
        faq.append((f"{alabel}の{noun}は、何曜日が空いていますか?", st + "楽天トラベルの空室データをもとにした、向こう約1か月の集計です。"))
    if g and g.get("tips"):
        parts.append("<h3>予約のコツ</h3><ul class='bul'>" + "".join(f"<li>{esc(t)}</li>" for t in g["tips"]) + "</ul>")
    if picks:
        parts.append(f"<h3>口コミ評価の高い{esc(noun)}(空きのある日がある宿)</h3>" + picks_list_html(cfg, feat, picks, holidays, len(dates)))
        faq.append((f"{alabel}で{noun}のおすすめはどこですか?",
                    f"{when_txt(newest)}空きのある日がある宿のうち、口コミ評価が高いのは{picks_answer(picks, holidays)}です。"
                    f"順番は楽天トラベルの口コミ評価と件数({PICK_MIN_REVIEWS}件以上)だけで機械的に決めています。"))
    parts.append("<p class='legend'>選び方は一般的な目安です。設備や条件は宿ごとに違うため、楽天トラベルの宿のページでご確認ください。"
                 f"おすすめの順番は、口コミ評価と件数({PICK_MIN_REVIEWS}件以上)だけで機械的に決めており、広告料や報酬とは無関係です。</p></section>")
    return "".join(parts), faq


def render_feature_hub(cfg, fkey, items, scene_list, now, demo):
    """条件ごとの全エリアまとめ(/roten/ など)。items: [(page, rows, pstate, dates, info, snap)]"""
    site = cfg["site"]
    base = site["url"].rstrip("/")
    hs = hub_slug(cfg, fkey)
    canonical = f"{base}/{hs}/"
    flabel = cfg["features"][fkey]["label"]
    noun = gnoun(cfg, fkey)
    g = GUIDES.get(fkey)
    holidays = set(cfg.get("holidays", []))
    wk = next((sc for sc in scene_list if sc["slug"] == "shumatsu"), None)
    newest = max([x for it in items for x in (parse_dt(v) for v in it[2].get("fetched", {}).values()) if x], default=None)
    rows_html, bullets, tot, tot_w, all_picks = "", [], 0, 0, []
    for page, rows, pstate, dates, info, snap in items:
        alabel = cfg["areas"][page["area"]]["label"]
        st = scene_stats(rows, pstate, fkey, wk["dates"]) if wk and wk["dates"] else None
        tot += len(rows)
        wtxt = "—"
        if st and st["got"]:
            tot_w += st["hotels"]
            wtxt = f"{st['hotels']}軒" + (f"(最安{yen_full(st['low'])}〜)" if st["low"] else "")
            bullets.append(f"{alabel}: {len(rows)}軒中{st['hotels']}軒{price_txt(st['low'])}")
        rows_html += (f'<tr><th><a href="../{esc(page["slug"])}/">{esc(alabel)}</a></th><td>{len(rows)}</td><td>{esc(wtxt)}</td>'
                      f"<td>{pct1(snap['wd']['rate']) if snap else '—'}</td><td>{pct1(snap['sat']['rate']) if snap else '—'}</td></tr>")
        for pk in guide_picks(cfg, rows, pstate, fkey, dates, 10):
            pk[0]["_area"] = page["area"]
            all_picks.append(pk)
    all_picks.sort(key=lambda x: (-_rv(x[0]), -(x[0].get("reviews") or 0), x[0]["name"]))
    all_picks = all_picks[:10]
    span = f"今週末({wk['span']})" if wk and wk["dates"] else "今週末"
    lead = (f"{when_txt(newest)}{len(items)}エリアの{noun}計{tot}軒のうち、{span}に空きがあるのは{tot_w}軒です(1泊・大人2名)。")
    details = [f"「{flabel}」は、{fdesc(cfg, fkey)}のことです。楽天トラベルの情報から機械的に判定しています。",
               f"楽天トラベルの空室検索APIから{freq_text(cfg)}に取得した値で、リアルタイムではありません。"]
    inner = [f'<div class="hero"><div class="crumb"><a href="../">{esc(site["name"])}</a> › {esc(flabel)}(全エリア)</div>'
             f"<h1>{esc(noun)} 全エリアの空室とおすすめ</h1>"
             f'<p class="lead">{esc(noun)}を、エリアをまたいで比べられます。空室は自動で更新しています。</p></div>',
             answer_html(fmt_dt(newest) if newest else "", lead, bullets, details, f"{flabel}の空室まとめ(全エリア)",
                         mood=("happy" if tot_w else "sleepy"))]
    inner.append('<section class="card"><h2>エリア別の空き</h2><div class="scroll"><table class="stat"><thead><tr><th>エリア</th><th>宿の数</th>'
                 f"<th>{esc(span)}の空き</th><th>空き率 平日</th><th>空き率 土曜・祝前日</th></tr></thead><tbody>{rows_html}</tbody></table></div>"
                 '<p class="legend">空き率=その条件の宿のうち、その日に空きがある宿の割合(向こう約1か月の平均)。エリア名を押すと、日付ごとの空室表が開きます。</p></section>')
    faq = [(f"{noun}は、どのエリアが空いていますか?", lead + (" " + " / ".join(bullets) if bullets else ""))]
    if g:
        inner.append(f'<section class="card guide"><h2>{esc(noun)}の選び方</h2><p>{esc(g["what"])}{esc(g["who"])}</p>'
                     + "<h3>予約のコツ</h3><ul class='bul'>" + "".join(f"<li>{esc(t)}</li>" for t in g["tips"]) + "</ul></section>")
        faq.append((f"{noun}とは何ですか?", g["what"] + g["who"]))
    if all_picks:
        inner.append(f'<section class="card"><h2>口コミ評価の高い{esc(noun)}(全エリア・空きのある日がある宿)</h2>'
                     + picks_list_html(cfg, fkey, all_picks, holidays, None, show_area=True)
                     + f"<p class='legend'>順番は楽天トラベルの口コミ評価と件数({PICK_MIN_REVIEWS}件以上)だけで機械的に決めています。広告料や報酬とは無関係です。</p></section>")
        faq.append((f"{noun}のおすすめはどこですか?",
                    f"{when_txt(newest)}空きのある日がある宿のうち、口コミ評価が高いのは"
                    + "、".join(f"{h['name']}({cfg['areas'][h['_area']]['label']}・★{h['review']}・{yen_full(p)}〜)" for h, p, d, n, c in all_picks[:5]) + "です。"))
    if not is_hotel_feat(cfg, fkey):
        inner.append('<nav class="chips" aria-label="日程から全エリアを探す"><b>日程から探す</b>'
                     + "".join(f'<a href="{esc(sc["slug"])}/">{esc(sc["name"])}</a>' for sc in scene_list) + "</nav>")
    inner.append(faq_html(faq))
    ld = [breadcrumb_ld([(site["name"], base + "/"), (f"{flabel}(全エリア)", canonical)]),
          {"@context": "https://schema.org", "@type": "CollectionPage", "name": f"{noun} 全エリアの空室とおすすめ", "url": canonical,
           "inLanguage": "ja", "description": lead, "dateModified": (newest or now).isoformat(timespec="seconds"),
           "mainEntity": {"@type": "ItemList", "itemListElement": [
               {"@type": "ListItem", "position": i + 1, "name": h["name"], "url": base + (HOTEL_HREF.get(h["no"]) or f"/hotel/{h['no']}/")}
               for i, (h, p, d, n, c) in enumerate(all_picks)]}},
          faq_ld(faq)]
    title = f"{noun} 全エリアの空室とおすすめ【{md(now.date())}時点】 | {site['name']}"
    return shell(cfg, demo, "../", title, lead, canonical, "".join(inner), ld)


def hotel_feature_rows(cfg, h, state, area):
    """宿ページの「この宿の条件」: (条件名, 判定, リンク先) の一覧"""
    seen = state.get("meta", {}).get(h["no"], {}).get("seen", {})
    checks = state.get("meta", {}).get(h["no"], {}).get("checks", 0)
    live = {p["feature"]: p["slug"] for p in LIVE if p["area"] == area}
    out = []
    for f, fd in cfg["features"].items():
        if fd.get("hidden"):
            continue
        if seen.get(f):
            mark = "◯ 当てはまる"
        elif not fd.get("hotel"):
            mark = "部屋名では確認できず" if checks else "判定中"
        elif f == "ekichika" or DETAIL.get(h["no"]):
            mark = "記載なし"
        else:
            mark = "判定中(施設情報を取得中)"
        out.append((fd["label"], mark, live.get(f) if seen.get(f) else None))
    return out


def render_about(cfg, ctxs, stats, now, demo):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = base + "/about/"
    op = cfg.get("operator", {})
    feats = [f for f in cfg["features"] if any(p["feature"] == f for p in cfg["pages"])] or list(cfg["features"])
    flabel = "」「".join(cfg["features"][f]["label"] for f in feats)
    r = cfg["refresh"]
    parts = [
        '<div class="hero"><h1>データの取得方法と注意点</h1>'
        f'<p class="lead">{esc(site["name"])}が、どのデータを・どう集め・どう判定しているかの説明です。</p></div><div class="prose">',
        "<h2>このサイトについて</h2>"
        "<p>宿の空室を日付ごとにまとめ、楽天トラベルの予約ページへご案内するサイトです。楽天トラベルの公式サイトではありません。"
        "予約・決済・キャンセルは、すべて楽天トラベル上で行われます。</p>",
        "<h2>データの取得元</h2>"
        "<p>楽天ウェブサービスが提供する「楽天トラベル 空室検索API」と「施設検索API」から取得しています。宿の名称・住所・口コミ評価・写真・料金・予約ページのリンクは、そのAPIが返した内容です。"
        "当サイトが数字を推測したり、書き換えたりすることはありません。</p>",
        "<h2>更新の頻度</h2>"
        f"<p>自動で更新しています。日程が近いほど頻繁に取得し、{esc(freq_text(cfg))}を目安にしています。"
        f"{r['near_days']}日以内の取得は約{r['near_hours']}時間ごとですが、取得の混み具合により遅れることがあります。"
        "各ページには、取得した日時を表示しています。リアルタイムの在庫ではありません。</p>",
        "<h2>表示の見方</h2><table><tbody>"
        "<tr><th>◯</th><td>そのページの条件(客室露天風呂など)に合う部屋に空きがある。大浴場・貸切風呂など<b>宿の条件</b>のページでは、その条件の宿に空室がある(客室の種類は問いません)</td></tr>"
        "<tr><th>△</th><td>宿には空きがあるが、条件に合う部屋の空きは確認できない</td></tr>"
        "<tr><th>満</th><td>その日は宿全体で空室が見つからない</td></tr>"
        "<tr><th>—</th><td>まだ取得していない(満室という意味ではありません)</td></tr></tbody></table>"
        f"<p>料金は{esc(cond_text(cfg))}の最安値です。人数・泊数・プランによって変わります。</p>",
        "<h2>条件(客室露天風呂など)の判定方法と限界</h2>"
        "<p>「客室露天風呂」「ペット可」などの部屋の条件は、楽天トラベルから届く<b>部屋の名前に、キーワードが含まれるか</b>で、機械的に判定しています。"
        "「大浴場」「貸切風呂」などの宿の条件は、楽天トラベルの<b>施設情報(設備・風呂・客室数・チェックイン時刻・アクセス・口コミ点数)</b>から機械的に判定しています。"
        "宿を1軒ずつ人が確認しているわけではありません。</p><ul>"
        + "".join(f"<li>「{esc(cfg['features'][f]['label'])}」: {esc(fdesc(cfg, f))}</li>" for f in feats)
        + "</ul><ul><li>部屋名に書かれていない設備(たとえば、部屋名に出ていない露天風呂)は、拾えません。</li>"
        "<li>宿の条件は、宿が楽天トラベルに登録している情報の書き方に左右されます。記載がない設備は拾えず、古い記載が残っていることもあります。</li>"
        "<li>キーワードを含む別の意味の部屋名を、誤って拾う可能性があります。</li>"
        "<li>設備の最終確認は、各宿・楽天トラベルのページでお願いします。</li></ul>",
        "<h2>AIの利用について</h2>"
        "<p>当サイトの制作と、データを自動で集めてページを作る仕組みには、AIツールを使っています。"
        "掲載している空室・料金の数字は、上記のとおりAPIから機械的に取得した値で、AIが推測して作った数値ではありません。</p>",
        "<h2>楽天トラベル由来の情報と、当サイトの判定の区別</h2><table><tbody>"
        "<tr><th>楽天トラベルの値をそのまま表示</th><td>宿の名前・住所・最寄り駅・口コミ評価と件数・写真・空室・料金・予約ページのリンク</td></tr>"
        "<tr><th>当サイトが機械的に判定</th><td>「客室露天風呂」「ペット可」などの条件(部屋名のキーワードによる)、バッジ、「満」と「未取得」の区別、日別の軒数・最安値の集計</td></tr>"
        "<tr><th>当サイトが推測しないもの</th><td>空室や料金の予測、口コミの内容の評価、設備の有無(部屋名に書かれていないもの)</td></tr></tbody></table>",
        "<h2>誤りの訂正・更新の方針</h2>"
        "<p>空室と料金は、取得のたびに最新の値で置き換わります。取得日時は各ページに表示しています。"
        "判定の誤りや、掲載内容の間違いが見つかった場合は、確認のうえ、速やかに修正します。"
        "宿・楽天トラベル側から掲載の停止を求められた場合も、対応します。</p>",
        "<h2 id=\"badges\">宿につけているバッジの基準</h2>"
        "<p>宿のカードに表示するバッジは、次の基準を機械的に当てはめて付けています。個別に審査したものではなく、広告料や報酬の有無とは無関係です。"
        "口コミ評価と件数は楽天トラベルの掲載値、料金は取得した最安値(" + esc(cond_text(cfg)) + ")です。</p><ul>"
        + "".join(f"<li><b>{esc(b.get('icon', ''))} {esc(b['label'])}</b>: {esc(b.get('desc', ''))}</li>" for b in cfg.get("badges", []))
        + f"</ul><p>1つの宿に付くバッジは最大{cfg.get('badge_max', 2)}個です。基準は変更することがあります。</p>",
        "<h2>広告・収益について</h2>"
        "<p>当サイトは楽天アフィリエイトに参加しています。掲載リンクから予約が行われると、運営者に報酬が発生する場合があります。報酬の有無によって、掲載する宿や順序を変えることはしていません"
        "(宿の並びは、空きのある日数・料金・口コミ件数などデータにもとづきます)。</p>",
    ]
    if op.get("name") or op.get("contact"):
        parts.append("<h2>運営者・お問い合わせ</h2><ul>"
                     + (f"<li>運営者: {esc(op['name'])}</li>" if op.get("name") else "")
                     + (f"<li>連絡先: {esc(op['contact'])}</li>" if op.get("contact") else "")
                     + "</ul>" + (f"<p>{esc(op['note'])}</p>" if op.get("note") else "")
                     + ("<p>掲載内容の誤りや、掲載の停止のご希望は、上記までご連絡ください。</p>" if op.get("contact") else ""))
    parts.append("</div>")
    parts.append(f'<p style="margin:16px 0"><a href="../">トップへ戻る</a></p>')
    desc = f"{site['name']}が使うデータ(楽天トラベルAPI)・更新頻度・判定方法・限界・広告についての説明です。"
    ld = [breadcrumb_ld([(site["name"], base + "/"), ("データの取得方法と注意点", canonical)]),
          {"@context": "https://schema.org", "@type": "AboutPage", "name": "データの取得方法と注意点", "url": canonical, "inLanguage": "ja",
           "dateModified": now.isoformat(timespec="seconds"),
           "isPartOf": {"@type": "WebSite", "name": site["name"], "url": base + "/"}}]
    return shell(cfg, demo, "../", f"データの取得方法と注意点 | {site['name']}", desc, canonical, "".join(parts), ld)


def render_privacy(cfg, now, demo):
    """最低限のプライバシーポリシー(個人情報・Cookie・外部サービス・免責)"""
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = base + "/privacy/"
    op = cfg.get("operator", {})
    ga = site.get("analytics_id", "")
    contact = op.get("contact") or "運営者情報に記載の連絡先"
    ga_txt = ("当サイトは、アクセス状況を把握するために「Google アナリティクス」を利用しています。Cookieを使って匿名の利用状況を収集しますが、個人を特定するものではありません。"
              "ブラウザの設定でCookieを無効にすることで、収集を拒否できます。"
              if ga else "現在、アクセス解析ツールは使用していません。")
    inner = (
        '<div class="hero"><h1>プライバシーポリシー</h1></div><div class="prose">'
        "<h2>個人情報の取得</h2><p>当サイトは、会員登録・コメント・問い合わせフォームなど、閲覧者の個人情報を入力していただく機能を設けておらず、個人情報を取得しません。</p>"
        f"<h2>アクセス解析</h2><p>{esc(ga_txt)}</p>"
        "<h2>広告(アフィリエイト)</h2><p>当サイトは楽天アフィリエイトのリンクを掲載しています。リンクをクリックすると、楽天グループ側でCookie等が利用され、予約などの成果が計測されます。"
        "その取り扱いは、楽天グループのプライバシーポリシーに従います。</p>"
        "<h2>外部サービス</h2><p>文字の表示にGoogle Fontsを利用しており、読み込み時に閲覧者のIPアドレス等がGoogleに送信されることがあります。"
        "また、お気に入り(♡)は閲覧者の端末(ブラウザ)内にだけ保存され、当サイトのサーバーには送信されません。</p>"
        "<h2>免責</h2><p>掲載している空室・料金・宿の情報は、楽天ウェブサービスのAPIから取得した時点のものです。正確性・最新性を保証するものではありません。"
        "最終的な内容は、楽天トラベルの予約ページでご確認ください。当サイトの利用により生じた損害について、責任を負いかねます。</p>"
        f"<h2>お問い合わせ</h2><p>本ポリシーや掲載内容に関するお問い合わせは、{esc(contact)}までお願いします。</p>"
        f"<h2>改定</h2><p>必要に応じて、本ポリシーを予告なく改定することがあります。最終更新: {now:%Y年%m月%d日}</p></div>"
        '<p style="margin:16px 0"><a href="../">トップへ戻る</a></p>')
    return shell(cfg, demo, "../", f"プライバシーポリシー | {site['name']}", f"{site['name']}のプライバシーポリシー(個人情報・Cookie・広告・免責)。",
                 canonical, inner, [breadcrumb_ld([(site["name"], base + "/"), ("プライバシーポリシー", canonical)])])


# ---- ページ本体 -------------------------------------------------------

def page_scenes(cfg, today):
    return [sc for sc in all_scenes(cfg, today) if sc["slug"] in PAGE_SCENES]


def render_page(cfg, hotels, state, stats, page, now, demo):
    site = cfg["site"]
    feat = page["feature"]
    flabel = cfg["features"][feat]["label"]
    alabel = cfg["areas"][page["area"]]["label"]
    today = now.date()
    n_days = area_days(cfg, page["area"])
    dates = [today + timedelta(days=i) for i in range(n_days)]
    holidays = set(cfg.get("holidays", []))
    canonical = f"{site['url'].rstrip('/')}/{page['slug']}/"

    rows = hotels_for_page(cfg, hotels, state, page)
    info = summarize(rows, dates, feat, state)

    def n_ok(h):
        return sum(1 for d in dates if status(state["cells"].get(h["no"], {}).get(d.isoformat()), feat) == "ok")

    rows.sort(key=lambda h: (-n_ok(h), -(h.get("reviews") or 0)))

    fetched = [parse_dt(state["fetched"][d.isoformat()]) for d in dates if d.isoformat() in state.get("fetched", {})]
    fetched = [x for x in fetched if x]
    newest, oldest = (max(fetched), min(fetched)) if fetched else (None, None)

    sats = [d for d in dates if d.weekday() == 5]
    default = dates[0]
    if sats and info[sats[0].isoformat()][0] > 0:
        default = sats[0]
    else:
        default = next((d for d in dates if info[d.isoformat()][0] > 0), sats[0] if sats else dates[0])

    # 「次の土曜」「連休」ボタン
    quick = []
    if sats:
        quick.append((f"次の土曜 {md(sats[0])}", sats[0]))
    for a, b in holiday_blocks(dates, holidays):
        quick.append((f"{md(a)}〜の{(b - a).days + 1}連休", a))
        if len(quick) >= 3:
            break
    quick.insert(0, ("今日", today))

    t0 = info[today.isoformat()] if rows else (0, None)
    scene_list = page_scenes(cfg, today)
    ans = (answer_parts(cfg, page, rows, state, today, dates, info, scene_list, newest, holidays, now)
           if rows else ("", "", [], []))
    if rows:
        # 独自の観測データ: この一覧の宿が「満室→空き」に変わった回数(キャンセルなどで空きが出る目安)
        nos = {h["no"] for h in rows}
        since = stats.get("moves_since")
        tot = sum((stats.get("hotel_moves", {}).get(no) or [0, 0])[1] for no in nos)
        recent = sum(1 for c in state.get("changes", []) if c.get("no") in nos)
        if since and tot:
            ans[2].insert(len(ans[2]) - 1, f"満室から空きに戻った回数: 当サイトの観測({since[5:].replace('-', '/')}〜)で計{tot:,}回、直近3日で{recent:,}回"
                          "(この一覧の宿・宿全体の空き。キャンセルなどで空きが出ることがあります)")
    # 独自の観測データ: 今空いている宿が、宿泊日まで空いたまま残る見込み(何日前に埋まるか)
    hz = fill_hazard(stats, page["area"])
    kps = [(lab, keep_pct(hz, ld)) for lab, ld in (("1週間前", 7), ("3日前", 3), ("前日", 1))]
    kps = [(lab, k) for lab, k in kps if k is not None]
    if rows and kps:
        ans[2].insert(len(ans[2]) - 1, f"空きが埋まる早さ({alabel}・当サイトの観測): 宿泊日の" + "、".join(f"{lab}に空いていた宿が当日まで残るのは約{k}%" for lab, k in kps)
                      + "(宿全体の空きで集計。予約の目安です)")
    # 検索結果に表示される最初の部分に「地域・条件・空き数・最安値」が来るように(日時は後ろへ)
    noun = flabel if is_hotel_feat(cfg, feat) else f"{flabel}の宿"
    wk = next((x for x in scene_list if x["slug"] == "shumatsu" and x["dates"]), None)
    wst = scene_stats(rows, state, feat, wk["dates"]) if (rows and wk) else None
    head_bits = []
    if wst and wst["got"]:
        head_bits.append(f"{wk.get('label', wk['name'])}({wk['span']})は{per_day_txt(wst['per'], 'paren') + 'に空き' if per_day_txt(wst['per']) else str(wst['hotels']) + '軒に空き' + (f"・最安{yen_full(wst['low'])}〜" if wst["low"] else "")}")
    if rows:
        head_bits.append(f"今日({md(today)})は{t0[0]}軒" + (f"・最安{yen_full(t0[1])}〜" if t0[0] and t0[1] else ""))
    desc_head = (f"{alabel}の{noun}、" + "、".join(head_bits) + "(1泊・大人2名)。") if head_bits else ""
    desc = ((desc_head + f"{n_days}日先までの空室表と料金を、楽天トラベルの空室データから自動更新({fmt_dt(newest) if newest else ''}時点・リアルタイムではありません)。") if rows else
            f"{alabel}の{flabel}の空室を{n_days}日先まで確認できます。"
            f"{md(today)}({WD[today.weekday()]})は{t0[0]}軒に空き"
            + (f"(取得: {newest:%m/%d %H:%M})" if newest else "")
            + "。" + (picks_txt(top_picks(rows, state, feat, dates)) + "。" if top_picks(rows, state, feat, dates) else "")
            + "1泊2名・楽天トラベルの空室データを自動更新しています(リアルタイムではありません)。")
    # タイトルにも、いまの空き数と最安値を入れる(検索結果で一目で分かるように)
    if wst and wst["got"] and wst["hotels"]:
        _pd = per_day_txt(wst["per"], "tight")
        tnum = (f"{wk['name']}の空き {_pd}" if _pd else f"{wk['name']}{wst['hotels']}軒に空き" + (f"・最安{yen_full(wst['low'])}〜" if wst["low"] else ""))
    elif rows and t0[0]:
        tnum = f"今日{t0[0]}軒に空き" + (f"・最安{yen_full(t0[1])}〜" if t0[1] else "")
    else:
        tnum = f"{md(today)}〜{md(dates[-1])}の空室表"
    title = f"{page['title']}|{tnum}【{md(today)}更新】 | {site['name']}"

    faq = [
        (f"{alabel}で今日空いている{flabel}の宿はどこですか?",
         f"{when_txt(newest)}今日({today.year}年{today.month}月{today.day}日)の「{flabel}」の空きは{len(rows)}軒中{t0[0]}軒です。日付を選ぶと、その日に泊まれる宿が一覧で表示されます。"
         "データは楽天トラベルの空室情報を定期的に取得したもので、リアルタイムではありません。"),
        ("表の◯・△・満・—は何ですか?",
         (f"◯は「{flabel}」の宿に空室があること(客室の種類は問いません)、満は空室なし、—はまだ取得していない日です(満室ではありません)。判定は1泊2名の条件です。"
          if is_hotel_feat(cfg, feat) else
          f"◯は「{flabel}」と判定した部屋に空きがあること、△は宿に空きはあるが該当の部屋の空きは確認できないこと、"
          "満は空室なし、—はまだ取得していない日です(満室ではありません)。判定は1泊2名の条件です。")),
        ("料金はいつのものですか?",
         (f"{newest:%Y-%m-%d %H:%M}(日本時間)までに取得した、1泊2名・楽天トラベル掲載料金の最安値です。" if newest else "取得後に表示されます。")
         + "人数や泊数を変えると変わります。最終確認は楽天トラベルでお願いします。"),
        (f"「{flabel}」はどう判定していますか?",
         (f"{fdesc(cfg, feat)}を、楽天トラベルの施設情報から自動で判定しています。" if is_hotel_feat(cfg, feat)
          else f"楽天トラベルから取得した{fdesc(cfg, feat)}を自動で判定しています。")
         + "個別に確認した情報ではないため、実際の設備は宿のページでご確認ください。"),
        ("このサイトの利用料金は?", "閲覧は無料です。予約は楽天トラベルで行います。"),
    ]
    # その日の質問(AIに聞かれる形)と答えを、データから自動で作る(今週末の各日と、初期表示の日)
    _qd = [d for d in ((wk["dates"] if wk else []) + [default]) if d.isoformat() in state.get("fetched", {})]
    _qd = sorted(set(_qd))[:3]
    if rows:
        faq = faq[:1] + [(f"{day_label(d, holidays)}に{alabel}で{flabel}の宿は空いていますか?",
                          quick_answer(cfg, feat, flabel, alabel, rows, state, d, holidays, dates, info)
                          + "データは楽天トラベルの空室情報を定期的に取得したもので、リアルタイムではありません。") for d in _qd] + faq[1:]
        # 予算別(土曜を優先)
        _bd = next((d for d in sorted(_qd, key=lambda x: x.weekday() != 5) if len(day_split(rows, state, feat, d)[0]) >= 2), None)
        if _bd:
            _bt = budget_txt([(pr, h) for pr, h, _ in day_split(rows, state, feat, _bd)[0]])
            if _bt:
                faq.insert(1 + len(_qd), (f"{day_label(_bd, holidays)}に{alabel}で{flabel}の宿に予算内(2万・3万・5万・10万円以内)で泊まれますか?",
                                          f"{day_label(_bd, holidays)}・{cond_text(cfg)}の最安で、{_bt}です。料金は取得時点の楽天トラベルの掲載額です。"))
        # 空いている日(日付が決まっていない質問)
        _fd = free_days_txt([(d, *info[d.isoformat()]) for d in dates if d.isoformat() in state.get("fetched", {})], holidays)
        if _fd:
            faq.insert(1 + len(_qd), (f"{alabel}で{flabel}の宿が空いている日はいつですか?",
                                      f"向こう{len(dates)}日間(取得済みの日)で、{_fd}({cond_text(cfg)})"))
        # 土曜を優先。土曜の空きが2軒未満なら、今週末のほかの日
        _cd = next((d for d in sorted(_qd, key=lambda x: x.weekday() != 5) if len(day_split(rows, state, feat, d)[0]) >= 2), None)
        _ok = day_split(rows, state, feat, _cd)[0] if _cd else []
        if len(_ok) >= 2:
            _ft = parse_dt(state.get("fetched", {}).get(_cd.isoformat(), ""))
            faq.insert(1 + len(_qd), (f"{day_label(_cd, holidays)}に{alabel}で{flabel}の宿に安く泊まれるのはどこですか?",
                       "安い順に " + "、".join(f"{h['name']}" + (f"「{short_room(rn, 24)}」" if short_room(rn, 24) and not is_hotel_feat(cfg, feat) else "") + f"{yen_full(pr)}〜"
                                               for pr, h, rn in _ok[:3])
                       + f"です({cond_text(cfg)}" + (f"・{_ft.month}/{_ft.day} {_ft:%H:%M}取得" if _ft else "") + ")。料金はプランや人数で変わります。予約前に楽天トラベルでご確認ください。"))
    g_html, g_faq = (guide_html(cfg, feat, alabel, rows, state, dates, holidays, SNAPS.get(page["slug"]), newest)
                     if rows else ("", []))
    faq = faq[:1] + g_faq + faq[1:]

    # 日付カレンダー表
    weeks = sorted({i // 7 for i in range(n_days)})
    thead = ['<th class="hn" scope="col">宿名</th>']
    for i, d in enumerate(dates):
        _ft = parse_dt(state.get("fetched", {}).get(d.isoformat(), ""))
        thead.append(f'<th class="{day_class(d, holidays)}" data-w="{i // 7}" data-d="{d.isoformat()}" scope="col">{d.day}<i>{WD[d.weekday()]}</i>'
                     + (f'<small class="ft">{_ft:%H:%M}</small>' if _ft and _ft.date() == now.date() else (f'<small class="ft">{_ft.month}/{_ft.day}</small>' if _ft else '<small class="ft">未取得</small>'))
                     + '</th>')
    # 表のマスには長い予約リンクを載せない(押したときにブラウザ側で組み立てる)。宿も上位 TABLE_MAX 軒だけ
    body = []
    for h in rows[:TABLE_MAX]:
        cs = []
        for i, d in enumerate(dates):
            c = state["cells"].get(h["no"], {}).get(d.isoformat())
            st = status(c, feat)
            w = i // 7
            if st == "none":
                cs.append(f'<td class="c nd" data-w="{w}"><span>—</span></td>')
            elif st == "full":
                cs.append(f'<td class="c full" data-w="{w}"><span>満</span></td>')
            elif st == "ok":
                p, url, name = c["f"][feat]
                cs.append(f'<td class="c ok" data-w="{w}"><span>◯<i>{yen(p)}</i></span></td>')
            else:
                cs.append(f'<td class="c part" data-w="{w}"><span>△</span></td>')
        rv = f"★{h['review']}" if h.get("review") else ""
        # 宿名は、宿ページがあればそちらへ(サイト内のつながりを強くする)。なければ楽天の宿情報へ
        nm = (f'<a class="nm" href="{esc(hotel_href(h))}">{esc(h["name"])}</a>' if hotel_href(h)
              else btn_plain(h.get("url"), esc(h["name"]), "nm"))
        body.append(f'<tr data-h="{esc(h["no"])}"><th class="hn" scope="row">{nm}'
                    f"<small>{esc(rv)}</small></th>{''.join(cs)}</tr>")
    rest = len(rows) - TABLE_MAX
    more_btn = (f'<p class="tmore"><button type="button" id="tmore" hidden>残り{rest}軒も表に表示する</button></p>'
                f'<p class="sub">表は空きの多い順に上位{TABLE_MAX}軒です。全{len(rows)}軒は下の「宿 一覧」でも確認できます。</p>'
                if rest > 0 else "")
    tabs = "".join(
        f'<button type="button" data-t="{w}" aria-pressed="false">{md(dates[w * 7])}〜{md(dates[min(w * 7 + 6, n_days - 1)])}</button>'
        for w in weeks) + '<button type="button" data-t="all" aria-pressed="false">全日程</button>'

    ld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": site["name"], "item": site["url"].rstrip("/") + "/"},
            {"@type": "ListItem", "position": 2, "name": alabel, "item": site["url"].rstrip("/") + f"/{page['area']}/"},
            {"@type": "ListItem", "position": 3, "name": page["title"], "item": canonical}]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq]},
        {"@context": "https://schema.org", "@type": "CollectionPage", "name": page["title"], "url": canonical, "description": desc,
         "inLanguage": "ja", "dateModified": (newest or now).isoformat(timespec="seconds"),
         "mainEntity": {"@type": "ItemList", "numberOfItems": len(rows), "itemListElement": item_list_ld(rows, state, feat, dates, canonical)}},
    ]
    ld_html = '<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + "</script>"

    out = [head(cfg, title, desc, canonical, demo, ld_html), header_html(cfg, demo), '<div class="wrap">']
    out.append(hero_banner(thin=True) + f'<div class="hero"><div class="crumb"><a href="../">{esc(site["name"])}</a> › <a href="../{esc(page["area"])}/">{esc(alabel)}</a> › {esc(flabel)}</div>'
               f'<h1>{esc(page["title"])}</h1>'
               + (f'<p class="qans" id="qans">{esc(quick_answer(cfg, feat, flabel, alabel, rows, state, default, holidays, dates, info))}</p>'
                  + qlinks_html([(h, ((state["cells"].get(h["no"], {}).get(default.isoformat()) or {}).get("f") or {}).get(feat, [0, ""])[1])
                                 for _, h, _ in day_split(rows, state, feat, default)[0][:3]], default, holidays) if rows else "")
               + f'<p class="lead">{esc(page.get("lead", ""))} <a href="#switch">エリア・条件を変える ↓</a></p></div>')

    if rows:
        # 最初の画面に「日付を選ぶ → その日の上位3軒の予約ボタン」を出す(ChatGPTなどから来た人が、すぐ予約に進めるように)
        qb = "".join(f'<button type="button" data-d="{d.isoformat()}">{esc(t)}</button>' for t, d in quick)
        out.append('<section class="bookpanel" id="bookpanel"><h2>日付を選んで予約する</h2>'
                   f'<div class="quick" aria-label="日付をすばやく選ぶ">{qb}</div>'
                   f'<label class="dinput"><span class="dlab2">📅 ほかの日付を選ぶ</span><input type="date" id="dinput" min="{dates[0].isoformat()}" max="{dates[-1].isoformat()}" value="{default.isoformat()}"></label>'
                   '<button type="button" class="calbtn" id="dcalbtn" aria-expanded="false" hidden>🗓 空き軒数のカレンダーから選ぶ</button></section>')
    if not rows:
        out.append('<div class="empty"><b>データを準備中です。</b><br>初回の自動更新が終わると、ここに空室が表示されます。'
                   '(初回は宿の判定のため、数回の更新が必要です)</div>')
    else:
        out.append('<div class="finder" id="finder"><div class="dstrip" id="dstrip" hidden><div class="dscroll" id="dscroll"></div></div>')
        res, n_def = results_html(cfg, rows, state, feat, flabel, default, dates, info, holidays, now)
        _k0 = keep_pct(hz, (default - today).days)
        out.append(f'<div class="rhead" id="rhead"><h2 id="rt">{esc(day_label(default, holidays))}に泊まれる宿 <b>{n_def}軒</b></h2>'
                   + (f'<p class="rkeep" id="rkeep">📊 今空いている宿のうち、{esc(md(default))}まで空いたまま残るのは約{_k0}%の見込み(当サイトの観測)。{keep_advice(_k0)}</p>' if _k0 and n_def and default > today else '<p class="rkeep" id="rkeep" hidden></p>')
                   + '<div class="rctl"><button type="button" id="favtog" class="favtog" hidden aria-pressed="false">♡ お気に入りだけ</button><label class="sort">絞り込み <select id="fltsel"><option value="">すべて</option>' + "".join(f'<option value="{esc(b["id"])}">{esc(b["label"])}</option>' for b in cfg.get("badges", [])) + '</select></label><label class="sort">並び替え <select id="sortsel"><option value="p">料金が安い順</option>'
                   '<option value="r">口コミ評価が高い順</option></select></label></div></div>'
                   + ('' if is_hotel_feat(cfg, feat) else f'<p class="rdef">この一覧は「{esc(room_noun(flabel))}に空きを確認できた宿」です。宿に空きはあるが{esc(flabel)}は未確認の宿は「参考」、空室なしは「満室の宿」として下に分けています(まだ取得していない日は満室ではありません)。</p>')
                   + '<p class="rsub">1泊・大人2名・1部屋の料金です(取得時点の掲載額)。ボタンを押すと楽天トラベルの予約ページが開きます。バッジは当サイト独自の基準で付けています(<a href="../about/#badges">基準を見る</a>)。</p>')
        out.append(f'<div id="cards">{res}</div></div>')
        # スマホ: 一覧の見出しを過ぎたら、画面の下に「その日の最安の宿を楽天で見る」を出す(日付を変えると中身も変わる)
        out.append('<div class="sbar" id="sbar2" hidden><div class="st"><b></b><small></small></div>'
                   '<a class="cta" target="_blank" rel="sponsored nofollow noopener">楽天で見る ›</a></div><script>' + SBAR2_JS + '</script>')
        out.append('<section class="sec" id="calendar"><h2>カレンダーで比べる</h2>'
                   '<p class="sub">日付(数字)をタップすると、その日の宿が上の一覧に表示されます。◯・△・満のマスを押すと、その日の楽天トラベルのページが開きます。'
                   'このページは、アドレスの最後に「?d=2026-11-28」のように日付を付けると、その日付を選んだ状態で開きます。</p>'
                   f'<div class="tabs" id="tabs" hidden>{tabs}</div>'
                   '<div class="scroll"><table><thead><tr>' + "".join(thead) + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>" + more_btn +
                   f'<p class="legend"><b>◯</b>=' + (f"「{esc(flabel)}」の宿に空きあり(数字は宿全体の1泊2名の最安) ／ " if is_hotel_feat(cfg, feat) else f"「{esc(flabel)}」の部屋に空きあり(数字は1泊2名の最安) ／ <b>△</b>=宿に空きはあるが該当の部屋は確認できない ／ ") + ""
                   "<b>満</b>=空室なし ／ <b>—</b>=未取得(満室ではありません)。日付の下の時刻は、その日の空室を取得した時刻です(今日以外に取得した日は日付)"
                   + (f"<br>取得: 最新 {newest:%m/%d %H:%M} ・ 最古 {oldest:%m/%d %H:%M}(日本時間)。リアルタイムではありません。最終確認は楽天トラベルでお願いします。" if newest else "")
                   + "</p></section>")
        # 全体の数字(まとめ)は、宿の一覧とカレンダーの後に置く(AIはページ全体を読むので、位置が下でも引用される)
        out.append(answer_html(*ans, mood=("happy" if any(info[d.isoformat()][0] for d in dates) else "sleepy"), keep=2))
        _tds = (wk["dates"] if wk else []) or [default]
        if default not in _tds and len(_tds) < 3:
            _tds = sorted(set(_tds) | {default})
        out.append(room_table_html(cfg, feat, flabel, rows, state, _tds, holidays,
                                   (f"{wk.get('label', wk['name'])}({wk['span']})" if wk and set(_tds) == set(wk["dates"]) else "・".join(day_label(x, holidays) for x in _tds))))
        if not page.get("auto"):
            out.append(chips_html(scene_list, "./"))
        out.append(share_html(canonical, title))
        out.append(g_html)
        out.append(facts_html(cfg, page, len(rows), newest, oldest))
        out.append(stats_block(stats, cfg, feat, flabel, today))
        hl = []
        for h in rows:
            hf = hotel_facts(h, state, feat, dates)
            nxt = (f" ・ 直近の空き {day_label(hf['ok'][0][0], holidays)} {yen_full(hf['ok'][0][1])}〜" if hf["ok"] else "")
            hl.append(f'<li>{('<a href="' + esc(hotel_href(h)) + '">' + esc(h["name"]) + "</a>") if hotel_href(h) else btn_plain(h.get("url"), esc(h["name"]))}'
                      f"<small>{esc(hotel_sub(h))}<br>{len(dates)}日中 {len(hf['ok'])}日に空き{esc(nxt)}</small></li>")
        out.append(f'<section class="card" id="hotels"><h2>{esc(alabel)}の{esc(flabel)}の宿 一覧({len(rows)}軒)</h2><ul class="hl">{"".join(hl)}</ul></section>')
        data = embed_data(cfg, rows, dates, info, state, feat, holidays, default, now)
        data["kp"] = [keep_pct(hz, (d - today).days) for d in dates]
        data["al"] = alabel
        data["feat"], data["flabel"] = feat, flabel
        out.append("<script>window.__V=" + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>")

    out.append('<div class="card"><h2>よくある質問</h2>'
               + "".join(f'<details class="q"><summary>{esc(q)}</summary><p>{esc(a)}</p></details>' for q, a in faq) + "</div>")
    out.append(f'<section class="card" id="switch"><h2>エリア・条件を変える</h2>{switcher_html(cfg, page, "../")}</section>')
    out.append(f'</div><footer>{footer_html(cfg, "../")}</footer><script>{JS}</script><script>{REL_JS}{TRACK_JS}{IOS_JS}</script></body></html>')
    digest = (ans[1] + " " + " / ".join(ans[2][:3])) if rows else ""
    return "".join(out), len(rows), t0[0], digest


# ---- 全エリア・日程別(今夜・明日・今週末・次の連休・年末年始)と、空きの動き ----------------
# 条件(露天など)を問わず、「その日に1部屋でも空きがある宿」を数えます。

def when_list(cfg, today):
    """(slug, 呼び名, 日付の一覧, 説明) の一覧。トップページの答えと、/konya/ などのページに使う"""
    sc = {x["slug"]: x for x in all_scenes(cfg, today)}
    for x in sc.values():
        if x["dates"] and x["span"]:
            SCENE_SPANS[tuple(x["dates"])] = x["span"]
    out = [("konya", "今夜", [today], "今日チェックインする場合(今夜泊まる場合)の空室です。"),
           ("ashita", "明日", [today + timedelta(days=1)], "明日チェックインする場合の空室です。")]
    for k in ("shumatsu", "renkyu", "nenmatsu"):
        if k in sc:
            out.append((k, sc[k].get("label", sc[k]["name"]), sc[k]["dates"], sc[k]["note"]))
    return out


def when_title(slug, name):
    return f"{name}泊まれる温泉地の宿" if slug in ("konya", "ashita") else f"{name}に空いている温泉地の宿"


SCENE_SPANS = {}   # 日付の並び → 見出し用の言い方(連休は「11/21(土)〜11/23(月祝)の3連休・泊まる日…」)。when_list() が入れる


def span_txt(ds, holidays):
    if not ds:
        return ""
    if tuple(ds) in SCENE_SPANS:
        return SCENE_SPANS[tuple(ds)]
    return day_label(ds[0], holidays) + (f"〜{day_label(ds[-1], holidays)}" if len(ds) > 1 else "")


def area_vacancy(hotels, state, area, ds):
    """その日程で、確認できた宿の数・空きのある宿・最安値。空きのある宿は [(宿, 最安値, その日)]"""
    seen, got, low = set(), [], None
    for h in hotels.get("areas", {}).get(area, []):
        no = h["no"]
        if no in seen:
            continue
        cs = state["cells"].get(no, {})
        cells = [(d, cs.get(d.isoformat())) for d in ds if cs.get(d.isoformat())]
        if not cells:
            continue
        seen.add(no)
        vs = [((c.get("any") or [None])[0], d) for d, c in cells if c.get("s") == "v"]
        if not vs:
            continue
        priced = [x for x in vs if x[0]]
        p, d = min(priced, key=lambda x: x[0]) if priced else (None, vs[0][1])
        got.append((h, p, d))
        if p:
            low = p if low is None else min(low, p)
    fb = state.get("fetched_by", {}).get(area, {})
    ts = [x for x in (parse_dt(fb.get(d.isoformat(), "")) for d in ds) if x]
    return {"checked": seen, "vacant": got, "low": low, "got": min(ts) if ts else None}


def area_pages_links(cfg, area, root):
    return " ".join(f'<a href="{root}{esc(p["slug"])}/">{esc(cfg["features"][p["feature"]]["label"])}</a>'
                    for p in LIVE if p["area"] == area)


def book_link(h, d, cell_url=""):
    """日付つきのプラン一覧(アフィリエイト経由)。デモでは取得時のURL"""
    if LINK:
        return stay_link(LINK["a"], h["no"], d, LINK["ad"], LINK["n"])
    return cell_url or h.get("url", "")


def book_a(h, d, text):
    """日付つきプラン一覧(アフィリエイト経由)へのリンク。長いURLはページに載せず、ブラウザ側(SL_JS)で組み立てる。
    JavaScriptが動かないときは、宿ページ(なければ楽天の宿情報)へ行く"""
    if not LINK:
        return btn_plain(book_link(h, d), text)
    fb = hotel_href(h) or safe_url(h.get("url"))
    return (f'<a href="{esc(fb)}" data-sl="{esc(h["no"])}|{d.isoformat()}" target="_blank" '
            f'rel="sponsored nofollow noopener">{text}</a>')


def rk_btn(h, d, text, cls="rkb", url=""):
    """楽天トラベルへ直接行く予約ボタン(アフィリエイト経由)。url があればそれを、なければ日付つきプラン一覧"""
    u = url or book_link(h, d)
    return (f'<a class="{cls}" href="{esc(safe_url(u))}" target="_blank" rel="sponsored nofollow noopener">{text}</a>'
            if safe_url(u) != "#" else "")


def qlinks_html(items, d, holidays):
    """冒頭の即答文の下に置く、例に挙げた宿の予約リンク。items は [(宿, URL)]"""
    links = [rk_btn(h, d, esc(h["name"]), "ql", u) for h, u in items]
    links = [x for x in links if x]
    if not links:
        return ""
    return f'<p class="qlinks">{esc(day_label(d, holidays))}の空室を楽天で見る: ' + " ・ ".join(links) + "</p>"


def name_link(h, d):
    href = hotel_href(h)
    return f'<a href="{esc(href)}">{esc(h["name"])}</a>' if href else book_a(h, d, esc(h["name"]))


def when_summary(cfg, hotels, state, ds):
    per, tot_c, tot_v, low, got = [], set(), set(), None, []
    for area in cfg["areas"]:
        v = area_vacancy(hotels, state, area, ds)
        per.append((area, v))
        tot_c |= v["checked"]
        tot_v |= {h["no"] for h, p, d in v["vacant"]}
        if v["low"]:
            low = v["low"] if low is None else min(low, v["low"])
        if v["got"]:
            got.append(v["got"])
    return {"per": per, "checked": len(tot_c), "vacant": len(tot_v), "low": low, "oldest": min(got) if got else None,
            "newest": max(got) if got else None}


LATE_MIN = 22 * 60    # 「今から泊まれる」: 最終チェックインがこの時刻以降の宿


def late_tonight(hotels, state, area, today):
    """今夜空きがあって、夜遅く(22時以降)までチェックインできる宿。(取得時刻, [(最安, 宿, 最終チェックイン)] 口コミの多い順)"""
    out = []
    for h in hotels.get("areas", {}).get(area, []):
        c = state["cells"].get(h["no"], {}).get(today.isoformat())
        if not c or c.get("s") != "v":
            continue
        li = (DETAIL.get(h["no"]) or {}).get("last_in")
        m = hotelfeat.clock_minutes(li)
        if m is None or m < LATE_MIN:
            continue
        out.append(((c.get("any") or [None])[0], h, li))
    out.sort(key=lambda x: (-(1 if (x[1].get("reviews") or 0) >= PICK_MIN_REVIEWS else 0), -_rv(x[1]), x[0] or 9e9))
    return parse_dt(state.get("fetched_by", {}).get(area, {}).get(today.isoformat(), "")), out


def late_txt(alabel, ft, lst, holidays, today, n=3):
    if not lst:
        return f"今夜({day_label(today, holidays)})、{alabel}で夜遅く(22時以降)までチェックインできて空きのある宿は見つかりませんでした" + (f"({ft.month}/{ft.day} {ft:%H:%M}取得)。" if ft else "。")
    return (f"今夜({day_label(today, holidays)})、{alabel}で空きがあり夜遅く(22時以降)までチェックインできる宿は{len(lst)}軒"
            + (f"({ft.month}/{ft.day} {ft:%H:%M}取得)" if ft else "") + "。"
            + "口コミ評価の高い順に " + "、".join(f"{h['name']}(最終チェックイン{li}" + (f"・¥{p:,}〜" if p else "") + ")" for p, h, li in lst[:n])
            + "。料金は1泊・大人2名・1室の最安。当日は満室になりやすいので、予約前に楽天トラベルで最新の空室をご確認ください。")


def render_imakara(cfg, hotels, state, now, demo):
    """今から泊まれる温泉宿(今夜空きあり×夜遅くチェックインOK)・全エリア"""
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = f"{base}/imakara/"
    holidays = set(cfg.get("holidays", []))
    today = now.date()
    per = []
    for a, v in cfg["areas"].items():
        ft, lst = late_tonight(hotels, state, a, today)
        per.append((a, v["label"], ft, lst))
    total = sum(len(x[3]) for x in per)
    per.sort(key=lambda x: -len(x[3]))
    newest = max([x[2] for x in per if x[2]], default=None)
    top = [x for x in per if x[3]][:5]
    lead = (f"今夜({day_label(today, holidays)})空きがあり、夜遅く(22時以降)までチェックインできる温泉宿は、{len(cfg['areas'])}温泉地で計{total}軒です"
            + (f"(最新の取得 {newest.month}/{newest.day} {newest:%H:%M})" if newest else "") + "。"
            + ("多いのは " + "、".join(f"{lab}{len(l)}軒" for _, lab, _, l in top) + "。" if top else "")
            + "料金は1泊・大人2名・1室の最安。当日は空室が変わりやすいため、予約前に楽天トラベルでご確認ください。")
    secs = []
    for a, lab, ft, lst in per:
        if not lst:
            continue
        lis = "".join(
            f"<li class='wimg'>{thumb_html(h)}<div>{name_link(h, today)}<small>"
            + (f"★{esc(h.get('review'))} ・ " if h.get("review") else "") + f"最終チェックイン {esc(li)}"
            + (f" ・ 最安{yen_full(p)}〜" if p else "") + f' ・ {book_a(h, today, "楽天トラベルで空室を見る")}</small></div></li>'
            for p, h, li in lst[:6])
        secs.append(f'<section class="card"><h2>{esc(lab)}で今から泊まれる宿({len(lst)}軒)</h2>'
                    + (f'<p class="sub">取得: {esc(fmt_dt(ft))}</p>' if ft else "")
                    + f'<ol class="hl picks">{lis}</ol><p class="legend"><a href="../{esc(a)}/">{esc(lab)}の空室まとめ ›</a></p></section>')
    faq = [("今から泊まれる温泉宿はありますか?", lead),
           ("何時までにチェックインすればよいですか?", "宿ごとに最終チェックインの時刻が決まっています。このページは、楽天トラベルの施設情報で最終チェックインが22時以降の宿だけを載せています。遅れる場合は必ず宿に連絡しましょう。")]
    for a, lab, ft, lst in top[:3]:
        faq.append((f"今から泊まれる{lab}の温泉宿は?", late_txt(lab, ft, lst, holidays, today)))
    inner = [f'<div class="hero"><div class="crumb"><a href="../">{esc(site["name"])}</a> › 今から泊まれる宿</div>'
             f"<h1>今から泊まれる温泉宿【今夜・夜遅くチェックインOK】</h1>"
             f'<p class="qans" id="qans">{esc(lead)}</p></div>'] + secs + [faq_html(faq)]
    if not secs:
        inner.insert(1, '<div class="empty">今夜、夜遅くまでチェックインできて空きのある宿は、取得済みのデータでは見つかりませんでした。</div>')
    ld = [breadcrumb_ld([(site["name"], base + "/"), ("今から泊まれる宿", canonical)]), faq_ld(faq)]
    title = (f"今から泊まれる温泉宿|今夜空きあり・夜遅くチェックインOK 計{total}軒【{md(today)} " + (f"{newest:%H:%M}" if newest else "") + f"更新】 | {site['name']}")
    return shell(cfg, demo, "../", title, lead[:250], canonical, "".join(inner), ld)


def render_when(cfg, hotels, state, slug, name, ds, note, wl, now, demo):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = f"{base}/{slug}/"
    holidays = set(cfg.get("holidays", []))
    t = when_title(slug, name)
    sp = span_txt(ds, holidays)
    sm = when_summary(cfg, hotels, state, ds)
    nav = ('<nav class="chips" aria-label="日程を選ぶ"><b>日程から探す</b>'
           + "".join(f'<a href="../{esc(k)}/"' + (' aria-current="page"' if k == slug else "") + f">{esc(n)}</a>" for k, n, *_ in wl)
           + '<a href="../imakara/">今から泊まれる宿</a><a href="../ugoki/">空きが出た宿</a></nav>')
    bullets, rows, picks_html, ld_items = [], "", [], []
    for area, v in sm["per"]:
        alabel = cfg["areas"][area]["label"]
        nv = len(v["vacant"])
        if v["checked"]:
            bullets.append(f"{alabel}: {len(v['checked'])}軒中{nv}軒{price_txt(v['low'])}")
        rows += (f"<tr><th><a href=\"../{esc(area)}/\">{esc(alabel)}</a></th><td>{len(v['checked']) or '取得中'}</td><td>{nv if v['checked'] else '—'}</td>"
                 f"<td>{esc(yen_full(v['low'])) + '〜' if v['low'] else '—'}</td><td>{time_html(v['got']) or '—'}</td>"
                 f"<td>{area_pages_links(cfg, area, '../')}</td></tr>")
        top = sorted([x for x in v["vacant"] if (x[0].get("reviews") or 0) >= PICK_MIN_REVIEWS],
                     key=lambda x: (-_rv(x[0]), x[1] or 9e9, x[0]["name"]))[:WHEN_PICKS]
        if top:
            lis = "".join(
                f"<li>{name_link(h, d)}<small>★{esc(h.get('review'))}(口コミ{(h.get('reviews') or 0):,}件)"
                + (f" ・ 最安{yen_full(p)}〜" if p else "") + (f"({esc(day_label(d, holidays))})" if len(ds) > 1 else "")
                + f' ・ {book_a(h, d, "楽天トラベルで空室を見る")}</small></li>' for h, p, d in top)
            picks_html.append(f'<section class="card"><h2>{esc(alabel)}で{esc(name)}空いている、口コミ評価の高い宿</h2><ol class="hl picks">{lis}</ol>'
                              f'<p class="legend"><a href="../{esc(area)}/">{esc(alabel)}の空室まとめ(ほかの宿・料金相場) ›</a></p></section>')
            ld_items += [h for h, p, d in top]
    if not ds:
        lead = f"{name}の対象になる日程は、いまはありません(次の対象日が近づくと、自動で表示します)。"
    elif sm["checked"]:
        lead = (f"{when_txt(sm['newest'])}{name}({sp})に空きがある宿は、{len(cfg['areas'])}エリアで計{sm['vacant']}軒です"
                f"(確認できた{sm['checked']}軒中)。" + (f"最安は{yen_full(sm['low'])}〜({cond_text(cfg)})です。" if sm["low"] else ""))
    else:
        lead = f"{name}({sp})の空室は、まだ取得中です。"
    details = [note, f"条件(客室露天風呂など)は問わず、楽天トラベルでその日に1部屋でも空きがある宿を数えています。",
               f"楽天トラベルの空室検索APIから{freq_text(cfg)}に取得した値で、リアルタイムではありません。直前の日程ほど変わりやすいので、予約前に楽天トラベルでご確認ください。"]
    inner = [f'<div class="hero"><div class="crumb"><a href="../">{esc(site["name"])}</a> › {esc(name)}</div>'
             f"<h1>{esc(t)}【全エリア】</h1>"
             f'<p class="lead">{esc(sp)} ・ 温泉地{len(cfg["areas"])}エリアの空きを、まとめて自動で数えています。</p></div>',
             answer_html(fmt_dt(sm["newest"]) if sm["newest"] else "", lead, bullets, details, f"{name}の空室まとめ",
                         mood=("happy" if sm["vacant"] else "sleepy")), nav]
    inner.append('<section class="card"><h2>エリア別の空き</h2><div class="scroll"><table class="stat"><thead><tr><th>エリア</th><th>確認できた宿</th>'
                 '<th>空きのある宿</th><th>最安値</th><th>取得</th><th>条件別に見る</th></tr></thead>'
                 f"<tbody>{rows}</tbody></table></div><p class='legend'>最安値は{esc(cond_text(cfg))}の、取得時点の値です。</p></section>")
    inner += picks_html
    if picks_html:
        inner.append(f"<p class='legend'>口コミ評価の高い順(口コミ{PICK_MIN_REVIEWS}件以上)に、機械的に並べています。広告料や報酬とは無関係です。</p>")
    faq = [(f"{name}、温泉地の宿に空きはありますか?", lead + (" " + " / ".join(bullets) if bullets else "")),
           ("この数字はいつのものですか?", f"各エリアの「取得」の時刻の値です。{freq_text(cfg)}に更新しています。")]
    inner.append(faq_html(faq))
    ld = [breadcrumb_ld([(site["name"], base + "/"), (name, canonical)]),
          {"@context": "https://schema.org", "@type": "CollectionPage", "name": t, "url": canonical, "inLanguage": "ja",
           "description": lead, "dateModified": (sm["newest"] or now).isoformat(timespec="seconds"),
           "mainEntity": {"@type": "ItemList", "itemListElement": [
               {"@type": "ListItem", "position": i + 1, "name": h["name"], "url": base + (HOTEL_HREF.get(h["no"]) or f"/hotel/{h['no']}/")}
               for i, h in enumerate(x for x in ld_items if HOTEL_HREF.get(x["no"]))]}},
          faq_ld(faq)]
    if ds and sm["checked"]:
        title = f"{t}|{len(cfg['areas'])}温泉地で計{sm['vacant']:,}軒に空き【{sp}・{md(now.date())}更新】 | {site['name']}"
        desc = (f"{name}({sp})に空きがある温泉地の宿は、{len(cfg['areas'])}エリアで計{sm['vacant']:,}軒(確認できた{sm['checked']:,}軒中・{cond_text(cfg)}・条件を問わない数)。"
                + " / ".join(bullets[:6]) + (f"({fmt_dt(sm['newest'])}時点)" if sm["newest"] else ""))[:240]
    else:
        title = f"{t}【{md(now.date())}時点・全エリア】 | {site['name']}"
        desc = lead
    return shell(cfg, demo, "../", title, desc, canonical, "".join(inner), ld), sm


# ---- エリアページ(/hakone/ など)・料金の相場(/souba/)・月別レポート(/report/) -----------------

def price_cells(cfg, hotels, state, area, today):
    """{区分: 相場 or None}"""
    holidays = set(cfg.get("holidays", []))
    return {g: areastat.summary(ps) for g, ps in areastat.prices(hotels, state, area, today, holidays).items()}


def souba_cell(sm):
    """表のマス用: 中央値を大きく、範囲を小さく(スマホで縦長にならないように)"""
    if not sm:
        return "—"
    return f"<b>{esc(yen_full(sm['med']))}</b><br><small>{esc(yen_full(sm['p25']))}〜{esc(yen_full(sm['p75']))}</small>"


def sat_diff(pc):
    """平日と比べた、土曜・祝前日の料金の差(%)"""
    if not (pc.get("wd") and pc.get("sat")):
        return "—"
    d = round((pc["sat"]["med"] / pc["wd"]["med"] - 1) * 100)
    return "±0%" if d == 0 else f"{d:+d}%"


def souba_txt(sm):
    return f"{yen_full(sm['med'])}(多くは{yen_full(sm['p25'])}〜{yen_full(sm['p75'])})" if sm else "—"


AREA_PICKS = 8   # エリアページで、選んだ日に空いている宿を何軒まで写真つきで出すか(口コミ評価の高い順)


def area_pick_html(cfg, hotels, state, area, today, holidays, feats):
    """エリアページの「日付を選んで予約する」。日付ごとの空き数と上位3軒を、小さなデータとして埋め込む(AREA_JSが表示)"""
    days = [today + timedelta(days=i) for i in range(area_days(cfg, area))]
    d_, H = {}, {}
    for d in days:
        v = area_vacancy(hotels, state, area, [d])
        top = sorted([x for x in v["vacant"] if (x[0].get("reviews") or 0) >= PICK_MIN_REVIEWS],
                     key=lambda x: (-_rv(x[0]), x[1] or 9e9, x[0]["name"]))[:AREA_PICKS]
        for h, _, _ in top:
            if h["no"] not in H:
                img = safe_url(h.get("image")) if str(h.get("image", "")).startswith("https://") else ""
                H[h["no"]] = [h["name"], hotel_href(h), h.get("review"), img if img != "#" else ""]
        d_[d.isoformat()] = [len(v["checked"]), len(v["vacant"]), v["low"], [[h["no"], p] for h, p, _ in top]]
    sats = [d for d in days if d.weekday() == 5]
    quick = [("今夜", today), ("明日", today + timedelta(days=1))] + ([(f"次の土曜 {md(sats[0])}", sats[0])] if sats else [])
    data = {"l": cfg["areas"][area]["label"], "def": today.isoformat(), "min": days[0].isoformat(), "max": days[-1].isoformat(),
            "d": d_, "H": H, "f": [[p["slug"], cfg["features"][p["feature"]]["label"]] for p in feats],
            "hol": sorted(x for x in holidays if days[0].isoformat() <= x <= days[-1].isoformat())}
    qb = "".join(f'<button type="button" data-d="{d.isoformat()}">{esc(t)}</button>' for t, d in quick)
    return ('<section class="bookpanel" id="apick" hidden><h2>日付を選んで予約する</h2>'
            f'<div class="quick" aria-label="日付をすばやく選ぶ">{qb}</div>'
            f'<label class="dinput"><span class="dlab2">📅 ほかの日付を選ぶ</span><input type="date" id="ainput" min="{days[0].isoformat()}" max="{days[-1].isoformat()}"></label>'
            '<div class="qbook" id="aout"></div></section>'
            "<script>window.__AD=" + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>")


EXTRA_ADULTS = 3


def adults_day(state, area, d, a=EXTRA_ADULTS):
    """人数を変えた空き(update.py の fetch_adults)。(取得日時, [(1室の最安, 施設番号, 部屋名)] 安い順) 。未取得なら (None, None)"""
    e = state.get("adults", {}).get(str(a), {}).get(area, {}).get(d.isoformat())
    if not e:
        return None, None
    v = sorted([(x[0], no, x[1]) for no, x in (e.get("v") or {}).items() if x and x[0]], key=lambda y: (y[0], y[1]))
    return parse_dt(e.get("t", "")), v


def adults_link(no, d, a=EXTRA_ADULTS):
    """大人a名・1室の条件で開く、日付つきプラン一覧(アフィリエイト経由)"""
    if LINK:
        return stay_link(LINK["a"], no, d, a, LINK["n"])
    return ""


def adults_area_html(hotels, state, area, ds, holidays, alabel):
    """エリアページ: 3人で泊まれる宿(今週末の各日・安い順に5軒まで)。予約ボタンは3名1室の条件で楽天を開く"""
    byno = {h["no"]: h for h in hotels.get("areas", {}).get(area, [])}
    blocks, faqs = [], []
    for d in ds:
        ft, v = adults_day(state, area, d)
        if v is None:
            continue
        v = [x for x in v if x[1] in byno]
        lab = day_label(d, holidays)
        tm = f"({ft.month}/{ft.day} {ft:%H:%M}取得)" if ft else ""
        if not v:
            blocks.append(f'<p class="adh"><b>{esc(lab)}</b>: 3人で空きのある宿は見つかりませんでした{esc(tm)}</p>')
            faqs.append((f"{lab}に{alabel}で3人で泊まれる宿はありますか?", f"{lab}は、大人3名1室で空きのある宿は見つかりませんでした{tm}。キャンセルで空くことがあります。"))
            continue
        lis = []
        for p, no, rn in v[:5]:
            h = byno[no]
            u = adults_link(no, d)
            rv = f"★{h['review']} " if h.get("review") else ""
            nm = (f'<a href="{esc(safe_url(u))}" target="_blank" rel="sponsored nofollow noopener">{esc(h["name"])}</a>' if u else esc(h["name"]))
            lis.append(f"<li>{nm}<small>{esc(rv)}3名1室 {yen_full(p)}〜</small></li>")
        blocks.append(f'<p class="adh"><b>{esc(lab)}</b>: 3人で泊まれる宿 <b>{len(v)}軒</b>・3名1室の最安{yen_full(v[0][0])}〜{esc(tm)}</p>'
                      f'<details class="amore"><summary>安い順に{len(lis)}軒を見る(3名1室で予約ページへ)</summary><ol class="hl adl">{"".join(lis)}</ol></details>')
        faqs.append((f"{lab}に{alabel}で3人で泊まれる宿はありますか?",
                     f"{lab}に大人3名1室で空きのある宿は{len(v)}軒です{tm}。安い順に "
                     + "、".join(f"{byno[no]['name']}{yen_full(p)}〜" for p, no, _ in v[:3])
                     + "(料金は3名1室の合計・1泊)。予約前に楽天トラベルで最新の空室をご確認ください。"))
    if not blocks:
        return "", []
    html = (f'<section class="card" id="adults3"><h2>3人で泊まれる宿(今週末)</h2>{"".join(blocks)}'
            '<p class="legend">大人3名・1室の条件で楽天トラベルから取得した空室と、3名1室の合計の最安です(約8時間ごと)。'
            '宿名を押すと、3名1室の条件で楽天トラベルのプラン一覧が開きます。</p></section>')
    return html, faqs


BUDGETS = (20000, 30000, 50000, 100000)


def budget_txt(items):
    """予算別の軒数。items は [(1泊2名1室の最安, 宿)]。「2万円以内3軒(口コミ最高: 〇〇★4.6・¥18,000〜)、3万円以内…」"""
    items = [(p, h) for p, h in items if p]
    parts, prev = [], 0
    for b in BUDGETS:
        inb = [(p, h) for p, h in items if p <= b]
        if not inb or len(inb) == prev:
            continue
        prev = len(inb)
        best = max(inb, key=lambda x: (_rv(x[1]), -(x[0])))
        ex = f"(口コミ最高: {best[1]['name']}" + (f"★{best[1]['review']}" if best[1].get("review") else "") + f"・{yen_full(best[0])}〜)"
        parts.append(f"{b // 10000}万円以内{len(inb)}軒{ex}")
        if len(inb) == len(items):
            break
    return "、".join(parts)


def free_days_txt(per, holidays, n=3):
    """空いている日の答え。per は [(日付, 空きのある宿の数, 最安)](取得済みの日だけ)。土曜・祝前日と、全日の上位"""
    per = [x for x in per if x[1]]
    if not per:
        return ""
    def lab(x):
        return f"{day_label(x[0], holidays)}{x[1]}軒" + (f"(¥{x[2]:,}〜)" if x[2] else "")
    sat = [x for x in per if x[0].weekday() == 5 or (x[0] + timedelta(days=1)).isoformat() in holidays]
    top_sat = sorted(sat, key=lambda x: (-x[1], x[0]))[:n]
    top_all = sorted(per, key=lambda x: (-x[1], x[0]))[:n]
    txt = ""
    if top_sat:
        txt += "空きが多い土曜・祝前日は " + "、".join(lab(x) for x in sorted(top_sat, key=lambda x: x[0])) + "。"
    txt += "全日では " + "、".join(lab(x) for x in sorted(top_all, key=lambda x: x[0])) + "。"
    return txt


def area_day_info(hotels, state, area, d):
    """エリアのその日の空き: (確認した宿の数, [(最安, 宿)] 安い順, 取得時刻)"""
    v = area_vacancy(hotels, state, area, [d])
    ok = sorted([(p, h) for h, p, _ in v["vacant"] if p], key=lambda x: (x[0], x[1]["name"]))
    ok += [(None, h) for h, p, _ in v["vacant"] if not p]
    return len(v["checked"]), ok, parse_dt(state.get("fetched_by", {}).get(area, {}).get(d.isoformat(), ""))


def area_quick_answer(alabel, hotels, state, area, d, holidays, cfg):
    """エリアページ冒頭の即答文(条件を問わない)。その日の空き軒数・最安・口コミの高い宿の例・取得時刻"""
    n_chk, ok, ft = area_day_info(hotels, state, area, d)
    head = f"{day_label(d, holidays)}・1泊大人2名1室" + (f"({ft.month}/{ft.day} {ft:%H:%M}取得)" if ft else "") + ": "
    if not n_chk:
        return head + f"{alabel}の空室は、まだ取得していません。"
    if not ok:
        return head + f"{alabel}で空きを確認できた宿はありませんでした(確認{n_chk}軒・すべて満室)。"
    low = next((p for p, _ in ok if p), None)
    good = sorted([(p, h) for p, h in ok if p and (h.get("reviews") or 0) >= PICK_MIN_REVIEWS],
                  key=lambda x: (-_rv(x[1]), x[0]))[:2]
    txt = head + f"{alabel}で空きを確認できた宿は{len(ok)}軒(確認{n_chk}軒中)" + (f"、最安{yen_full(low)}〜" if low else "") + "。"
    if good:
        txt += "口コミ評価の高い宿では " + "、".join(f"{h['name']}(★{h.get('review')}・{yen_full(p)}〜)" for p, h in good) + " など。"
    full = n_chk - len(ok)
    if full:
        txt += f"満室{full}軒。"
    return txt


def area_table_html(hotels, state, area, ds, holidays, label, per_day=50):
    """今週末など直近の日に空いている宿の一覧(安い順)。AIが予算などで絞り込めるように。普段は閉じておく"""
    trs, n = [], 0
    for d in ds:
        _, ok, ft = area_day_info(hotels, state, area, d)
        for p, h in [x for x in ok if x[0]][:per_day]:
            n += 1
            hh = hotel_href(h)
            nm = f'<a href="{esc(hh)}?d={d.isoformat()}">{esc(h["name"])}</a>' if hh else esc(h["name"])
            rv = f"★{h['review']}" if h.get("review") else ""
            trs.append(f"<tr><td>{esc(day_label(d, holidays))}</td><td>{nm}</td><td>{esc(rv)}</td>"
                       f"<td class=\"pr\">{yen_full(p)}〜</td><td>{(f'{ft.month}/{ft.day} {ft:%H:%M}') if ft else ''}</td>"
                       f"<td>{rk_btn(h, d, '予約', 'tb')}</td></tr>")
    if not trs:
        return ""
    cap = f"{label}に空いている宿(1泊・大人2名・1室、料金が安い順に各日{per_day}軒まで・計{n}件)"
    return (f'<details class="amore rtab"><summary>📋 {esc(cap)}を表で見る</summary>'
            '<div class="scroll"><table class="rtbl"><caption>' + esc(cap) + '</caption><thead><tr><th scope="col">宿泊日</th><th scope="col">宿名</th>'
            '<th scope="col">口コミ</th><th scope="col">料金</th><th scope="col">取得</th><th scope="col">予約</th></tr></thead><tbody>'
            + "".join(trs) + '</tbody></table></div><p class="sub">取得時点の楽天トラベルの掲載額(宿全体の最安プラン)です。宿名を押すと、その日を選んだ宿のページが開きます。</p></details>')


def render_area(cfg, hotels, state, area, wl, sums, now, demo):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = f"{base}/{area}/"
    holidays = set(cfg.get("holidays", []))
    alabel = cfg["areas"][area]["label"]
    today = now.date()
    rows, parts, bullets, picks_html, ld_items = "", [], [], [], []
    wk_per, wk_ds, wk_lbl = [], [], ""
    newest = None
    for slug, name, ds, note in wl:
        if not ds:
            continue
        v = dict(sums[slug]["per"])[area]
        if slug == "shumatsu":
            wk_ds, wk_lbl = ds, f"{name}({span_txt(ds, holidays)})"
        sp = span_txt(ds, holidays)
        lbl = name if slug in ("konya", "ashita") else f"{name}({sp})"
        if v["got"]:
            newest = v["got"] if newest is None else max(newest, v["got"])
        nv = len(v["vacant"])
        if v["checked"]:
            parts.append(f"{lbl}は{nv}軒")
        rows += (f'<tr><th><a href="../{esc(slug)}/">{esc(lbl)}</a></th><td>{len(v["checked"]) or "取得中"}</td><td>{nv if v["checked"] else "—"}</td>'
                 f"<td>{esc(yen_full(v['low'])) + '〜' if v['low'] else '—'}</td><td>{time_html(v['got']) or '—'}</td></tr>")
        if slug in ("shumatsu", "renkyu") and 2 <= len(ds) <= 3:
            # 合計だと「土曜の数」と読み違えやすいので、日ごとの行も出す
            for d in ds:
                c_, ok_, ft_ = area_day_info(hotels, state, area, d)
                lw = next((p for p, _ in ok_ if p), None)
                rows += (f'<tr class="sub"><th>└ {esc(day_label(d, holidays))}</th><td>{c_ or "取得中"}</td><td>{len(ok_) if c_ else "—"}</td>'
                         f"<td>{esc(yen_full(lw)) + '〜' if lw else '—'}</td><td>{time_html(ft_) or '—'}</td></tr>")
                if slug == "shumatsu":
                    wk_per.append((d, bool(c_), len(ok_), lw))
        if slug in ("konya", "shumatsu"):
            top = sorted([x for x in v["vacant"] if (x[0].get("reviews") or 0) >= PICK_MIN_REVIEWS],
                         key=lambda x: (-_rv(x[0]), x[1] or 9e9, x[0]["name"]))[:5]
            if top:
                lis = "".join(
                    f"<li class='wimg'>{thumb_html(h)}<div>{name_link(h, d)}<small>★{esc(h.get('review'))}(口コミ{(h.get('reviews') or 0):,}件)"
                    + (f" ・ 最安{yen_full(p)}〜" if p else "") + (f"({esc(day_label(d, holidays))})" if len(ds) > 1 else "")
                    + f' ・ {book_a(h, d, "楽天トラベルで空室を見る")}</small></div></li>' for h, p, d in top)
                picks_html.append(f'<section class="card"><h2>{esc(alabel)}で{esc(lbl)}空いている、口コミ評価の高い宿</h2><ol class="hl picks">{lis}</ol></section>')
                ld_items += [h for h, p, d in top]
    pc = price_cells(cfg, hotels, state, area, today)
    n_h = len(areastat.area_nos(hotels, area))
    _wpd = per_day_txt(wk_per, "paren") if wk_per else ""
    if _wpd:
        parts = [(f"{wk_lbl}は{_wpd}" if x.startswith(wk_lbl + "は") else x) for x in parts]
    if parts:
        lead = (f"{when_txt(newest)}{alabel}(楽天トラベル掲載{n_h}軒)で空きがある宿は、" + "、".join(parts)
                + f"です({cond_text(cfg)}、条件を問わない数)。")
    else:
        lead = f"{alabel}の空室は、いま取得を進めています。"
    souba_lead = ""
    if pc.get("wd") or pc.get("sat"):
        souba_lead = (f"空きのある宿の最安値の中央値は、" + "、".join(f"{lbl}{yen_full(pc[g]['med'])}" for g, lbl in areastat.GROUPS if pc.get(g))
                      + f"です(今後{areastat.PRICE_DAYS}日の宿泊日・{cond_text(cfg)})。")
        bullets.append(souba_lead)
    feats = [p for p in LIVE if p["area"] == area]
    # 即答文・FAQの日: 今週末の土曜(なければ今週末の最初の日)
    _qday = next((d for d in wk_ds if d.weekday() == 5), wk_ds[0] if wk_ds else None)
    inner = [hero_banner(thin=True) + f'<div class="hero"><div class="crumb"><a href="../">{esc(site["name"])}</a> › {esc(alabel)}</div>'
             f"<h1>{esc(alabel)}の宿の空室【今夜・明日・今週末】</h1>"
             + (f'<p class="qans" id="qans">{esc(area_quick_answer(alabel, hotels, state, area, _qday, holidays, cfg))}</p>'
                + qlinks_html([(h, "") for p_, h in sorted([(p_, h) for p_, h in area_day_info(hotels, state, area, _qday)[1] if p_ and (h.get("reviews") or 0) >= PICK_MIN_REVIEWS],
                                                       key=lambda x: (-_rv(x[1]), x[0]))[:3]], _qday, holidays) if _qday else "")
             + f'<p class="lead">{esc(alabel)}の宿の空きを、日程ごとに自動で数えています。条件(客室露天風呂など)で絞り込むこともできます。</p></div>']
    inner.append(area_pick_html(cfg, hotels, state, area, today, holidays, feats))
    # 宿の一覧(写真つき)を先に。全体の数字(まとめ・日程ごとの表)はその後(AIはページ全体を読むので位置は下でよい)
    inner += picks_html
    inner.append(answer_html(fmt_dt(newest) if newest else "", lead, bullets,
                         [f"楽天トラベルの空室検索APIから{freq_text(cfg)}に取得した値で、リアルタイムではありません。予約前に楽天トラベルでご確認ください。",
                          "「確認できた宿」は、当サイトがその日程の空室を確認した宿の数です。"],
                         f"{alabel}の空室まとめ", mood=("happy" if parts else "sleepy")))
    if wk_ds:
        inner.append(area_table_html(hotels, state, area, wk_ds, holidays, wk_lbl))
    _a3_html, _a3_faq = adults_area_html(hotels, state, area, wk_ds, holidays, alabel)
    inner.append(_a3_html)
    inner.append('<section class="card"><h2>日程ごとの空き</h2><div class="scroll"><table class="stat"><thead><tr><th>日程</th><th>確認できた宿</th>'
                 f"<th>空きのある宿</th><th>最安値</th><th>取得</th></tr></thead><tbody>{rows}</tbody></table></div></section>")
    if any(pc.values()):
        inner.append(f'<section class="card"><h2>{esc(alabel)}の宿の料金相場(曜日別)</h2><div class="scroll"><table class="stat"><thead><tr><th>宿泊日</th><th>最安値の中央値(多くの宿の範囲)</th><th>件数</th></tr></thead><tbody>'
                     + "".join(f"<tr><th>{esc(lbl)}</th><td>{souba_cell(pc.get(g))}</td><td>{pc[g]['n'] if pc.get(g) else '—'}</td></tr>" for g, lbl in areastat.GROUPS)
                     + f"</tbody></table></div><p class='legend'>今後{areastat.PRICE_DAYS}日の宿泊日について、空きのある宿ごとの最安値({esc(cond_text(cfg))})を集計。"
                     "「多くの宿の範囲」は中央の半分(25%〜75%)です。満室の宿は含みません。<a href=\"../souba/\">全エリアの相場を比べる ›</a></p></section>")
    if feats:
        inner.append('<nav class="chips" aria-label="条件で絞り込む"><b>条件で絞り込む</b>'
                     + "".join(f'<a href="../{esc(p["slug"])}/">{esc(cfg["features"][p["feature"]]["label"])}</a>' for p in feats) + "</nav>")
    faq = [(f"{alabel}で今夜泊まれる宿はありますか?", lead)]
    for _bd in sorted(wk_ds, key=lambda x: x.weekday() != 5):
        _c, _ok, _ft = area_day_info(hotels, state, area, _bd)
        _bt = budget_txt(_ok)
        if len([x for x in _ok if x[0]]) >= 2 and _bt:
            faq.append((f"{day_label(_bd, holidays)}に{alabel}で予算内(2万・3万・5万・10万円以内)で泊まれる宿は?",
                        f"{day_label(_bd, holidays)}・{cond_text(cfg)}の最安で、{_bt}です(条件を問わない・取得時点の掲載額)。"))
            break
    _per = []
    for i in range(area_days(cfg, area)):
        d = today + timedelta(days=i)
        c_, ok_, _ = area_day_info(hotels, state, area, d)
        if c_:
            _per.append((d, len(ok_), next((p for p, _ in ok_ if p), None)))
    _fd = free_days_txt(_per, holidays)
    if _fd:
        faq.append((f"{alabel}で宿が空いている日はいつですか?", f"向こう{len(_per)}日間(取得済みの日)で、{_fd}({cond_text(cfg)}・条件を問わない数)"))
    # 土曜を優先。土曜の空きが2軒未満なら、今週末のほかの日
    for _cd in sorted(wk_ds, key=lambda x: x.weekday() != 5):
        _c, _ok, _ft = area_day_info(hotels, state, area, _cd)
        _ok = [x for x in _ok if x[0]]
        if len(_ok) >= 2:
            faq.append((f"{day_label(_cd, holidays)}に{alabel}で安く泊まれる宿はどこですか?",
                        "安い順に " + "、".join(f"{h['name']}{yen_full(p)}〜" for p, h in _ok[:3])
                        + f"です({cond_text(cfg)}・条件を問わない" + (f"・{_ft.month}/{_ft.day} {_ft:%H:%M}取得" if _ft else "") + ")。料金はプランや人数で変わります。予約前に楽天トラベルでご確認ください。"))
            break
    faq += _a3_faq
    _lft, _ll = late_tonight(hotels, state, area, today)
    faq.append((f"今から(今夜)泊まれる{alabel}の宿はありますか?", late_txt(alabel, _lft, _ll, holidays, today) + (" 全エリアは「今から泊まれる宿」のページで確認できます。" if True else "")))
    for d in wk_ds:
        faq.append((f"{day_label(d, holidays)}に{alabel}で泊まれる宿はありますか?",
                    area_quick_answer(alabel, hotels, state, area, d, holidays, cfg)
                    + "データは楽天トラベルの空室情報を定期的に取得したもので、リアルタイムではありません。料金は大人2名1室の条件です。"))
    if souba_lead:
        faq.append((f"{alabel}の宿の相場はいくらですか?", souba_lead))
    inner.append(faq_html(faq))
    ld = [breadcrumb_ld([(site["name"], base + "/"), (alabel, canonical)]),
          {"@context": "https://schema.org", "@type": "CollectionPage", "name": f"{alabel}の宿の空室", "url": canonical, "inLanguage": "ja",
           "description": lead, "dateModified": (newest or now).isoformat(timespec="seconds"),
           "about": {"@type": "Place", "name": alabel},
           "mainEntity": {"@type": "ItemList", "itemListElement": [
               {"@type": "ListItem", "position": i + 1, "name": h["name"], "url": base + HOTEL_HREF[h["no"]]}
               for i, h in enumerate(x for x in dict((y["no"], y) for y in ld_items).values() if HOTEL_HREF.get(x["no"]))]}},
          faq_ld(faq)]
    _wpt = per_day_txt(wk_per, "tight") if wk_per else ""
    title = (f"{alabel}の宿の空室|今週末 {_wpt}【{md(today)}更新】 | {site['name']}" if _wpt else
             f"{alabel}の宿の空室【今夜・明日・今週末 {md(today)}時点】 | {site['name']}")
    # 説明文は「地域・日付・軒数・最安」から(取得日時は後ろ)
    desc = ((f"{alabel}の宿、{wk_lbl}は{_wpd}に空き(楽天トラベル掲載{n_h}軒・{cond_text(cfg)}・条件を問わない数)。"
             + "、".join(x for x in parts if not x.startswith(wk_lbl)) + "。" + (f"({fmt_dt(newest)}時点)" if newest else "")) if _wpd else lead)[:260]
    return shell(cfg, demo, "../", title, desc, canonical, "".join(inner), ld)


def render_souba(cfg, hotels, state, now, demo):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = f"{base}/souba/"
    today = now.date()
    data = [(a, price_cells(cfg, hotels, state, a, today)) for a in cfg["areas"]]
    ok = [(a, pc) for a, pc in data if pc.get("wd") and pc.get("sat")]
    rows = "".join(f'<tr><th><a href="../{esc(a)}/">{esc(cfg["areas"][a]["label"])}</a></th>'
                   + "".join(f"<td>{souba_cell(pc.get(g))}</td>" for g, _ in areastat.GROUPS)
                   + f"<td>{sat_diff(pc)}</td>"
                   + "</tr>" for a, pc in data)
    newest = max([x for x in (parse_dt(v) for f in state.get("fetched_by", {}).values() for v in f.values()) if x], default=None)
    bullets = []
    if ok:
        cheap = min(ok, key=lambda x: x[1]["sat"]["med"])
        dear = max(ok, key=lambda x: x[1]["sat"]["med"])
        gap = max(ok, key=lambda x: x[1]["sat"]["med"] / x[1]["wd"]["med"])
        lead = (f"{when_txt(newest)}今後{areastat.PRICE_DAYS}日の土曜・祝前日に泊まる場合、空きのある宿の最安値の中央値が最も安いのは"
                f"{cfg['areas'][cheap[0]]['label']}({yen_full(cheap[1]['sat']['med'])})、最も高いのは"
                f"{cfg['areas'][dear[0]]['label']}({yen_full(dear[1]['sat']['med'])})です({cond_text(cfg)})。")
        bullets = [f"平日との差が最も大きいのは{cfg['areas'][gap[0]]['label']}(平日{yen_full(gap[1]['wd']['med'])} → 土曜・祝前日{yen_full(gap[1]['sat']['med'])})。"]
        bullets += [f"{cfg['areas'][a]['label']}: 平日{yen_full(pc['wd']['med'])} / 土曜・祝前日{yen_full(pc['sat']['med'])}" for a, pc in ok]
    else:
        lead = "料金の相場は、データがそろいしだい表示します。"
    inner = [f'<div class="hero"><div class="crumb"><a href="../">{esc(site["name"])}</a> › 料金の相場</div>'
             "<h1>温泉地の宿の料金相場【エリア・曜日別】</h1>"
             '<p class="lead">楽天トラベルで空きのある宿の最安値を、エリアと曜日ごとにまとめています。毎日自動で更新します。</p></div>',
             answer_html(fmt_dt(newest) if newest else "", lead, bullets,
                         [f"今後{areastat.PRICE_DAYS}日の宿泊日について、空きのある宿ごと・日ごとの最安値({cond_text(cfg)})を集めた中央値です。",
                          "満室の宿は含まないため、人気の宿が埋まった日は低めに出ることがあります。",
                          f"件数が{areastat.PRICE_MIN_N}件未満の区分は「—」です。"],
                         "料金相場のまとめ")]
    inner.append('<section class="card"><h2>エリア別・曜日別の料金相場</h2><div class="scroll"><table class="stat"><thead><tr><th>エリア</th>'
                 + "".join(f"<th>{esc(lbl)}</th>" for _, lbl in areastat.GROUPS) + "<th>土曜の差</th></tr></thead>"
                 f"<tbody>{rows}</tbody></table></div><p class='legend'>大きい数字は最安値の中央値、小さい数字は中央の半分の宿が入る範囲(25%〜75%)。「土曜の差」は平日と比べた土曜・祝前日の差です。</p></section>")
    faq = [("温泉地の宿の相場はいくらですか?", lead + (" " + " ".join(bullets[:1]) if bullets else "")),
           ("土曜は平日よりどのくらい高いですか?", (bullets[0] if bullets else "データがそろいしだい表示します。"))]
    inner.append(faq_html(faq))
    ld = [breadcrumb_ld([(site["name"], base + "/"), ("料金の相場", canonical)]),
          {"@context": "https://schema.org", "@type": "Dataset", "name": "温泉地の宿の料金相場(エリア・曜日別)", "url": canonical,
           "description": lead, "inLanguage": "ja", "dateModified": (newest or now).isoformat(timespec="seconds"),
           "creator": {"@type": "Organization", "name": site["name"], "url": base + "/"},
           "isBasedOn": "https://travel.rakuten.co.jp/",
           "variableMeasured": ["最安値の中央値(1泊・大人2名・1部屋)", "曜日区分"],
           "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": f"{base}/{DATA_JSON}"}]},
          faq_ld(faq)]
    title = f"温泉地の宿の料金相場【エリア・曜日別 {md(today)}更新】 | {site['name']}"
    return shell(cfg, demo, "../", title, lead, canonical, "".join(inner), ld)


REPORT_MIN_DAYS = 3   # 観測がこの日数未満の月は、数字を出さない
RATE_MIN = 5          # 1日の確認数がこれ未満の日は、空き率の平均に入れない


def month_stats(cfg, days):
    """月の観測(days: [(日付, {エリア: 記録})])から、エリアごとの平均を出す"""
    out = {}
    for area in cfg["areas"]:
        recs = [r[area] for _, r in days if area in r]
        def rate(k):
            xs = [x[k][1] / x[k][0] for x in recs if x.get(k) and x[k][0] >= RATE_MIN]
            return (sum(xs) / len(xs), len(xs)) if xs else (None, 0)
        def med(k):
            xs = [x[k] for x in recs if x.get(k)]
            return int(statistics.median(xs)) if xs else None
        out[area] = {"t": rate("t"), "s": rate("s"), "p_wd": med("p_wd"), "p_sat": med("p_sat")}
    return out


def render_report(cfg, stats, ym, months, now, demo):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = f"{base}/report/{ym}/"
    y, m = int(ym[:4]), int(ym[5:])
    days = sorted((d, r) for d, r in stats.get("area_daily", {}).items() if d.startswith(ym))
    nd = len(days)
    cur = (now.year, now.month) == (y, m)
    period = f"{y}年{m}月" + (f"({int(days[0][0][8:])}日〜{int(days[-1][0][8:])}日の{nd}日分" + ("・月の途中" if cur else "") + ")" if nd else "")
    ms = month_stats(cfg, days)
    ok_t = [(a, v) for a, v in ms.items() if v["t"][0] is not None and v["t"][1] >= REPORT_MIN_DAYS]
    ok_p = [(a, v) for a, v in ms.items() if v["p_sat"] and v["p_wd"]]
    lab = lambda a: cfg["areas"][a]["label"]
    bullets = []
    if nd >= REPORT_MIN_DAYS and ok_t:
        avg = sum(v["t"][0] for _, v in ok_t) / len(ok_t)
        hi = max(ok_t, key=lambda x: x[1]["t"][0])
        lo = min(ok_t, key=lambda x: x[1]["t"][0])
        lead = (f"{period}の当サイトの観測では、温泉地{len(ok_t)}エリアで「今夜泊まれる宿」の割合は平均{avg * 100:.0f}%でした。"
                f"最も空きやすかったのは{lab(hi[0])}({hi[1]['t'][0] * 100:.0f}%)、最も埋まっていたのは{lab(lo[0])}({lo[1]['t'][0] * 100:.0f}%)です。")
        ok_s = [(a, v) for a, v in ms.items() if v["s"][0] is not None and v["s"][1] >= REPORT_MIN_DAYS]
        if ok_s:
            ls = min(ok_s, key=lambda x: x[1]["s"][0])
            bullets.append(f"直近の土曜泊が最も埋まっていたのは{lab(ls[0])}(空きのある宿{ls[1]['s'][0] * 100:.0f}%)。")
        if ok_p:
            ch = min(ok_p, key=lambda x: x[1]["p_sat"])
            dr = max(ok_p, key=lambda x: x[1]["p_sat"])
            bullets.append(f"土曜・祝前日の最安値の中央値が最も安かったのは{lab(ch[0])}({yen_full(ch[1]['p_sat'])})、最も高かったのは{lab(dr[0])}({yen_full(dr[1]['p_sat'])})。")
        state_txt = "ready"
    else:
        lead = f"{y}年{m}月の観測は{nd}日分です。{REPORT_MIN_DAYS}日分たまったら、数字を表示します。"
        state_txt = "wait"
    pct = lambda r: f"{r[0] * 100:.0f}%" if r[0] is not None and r[1] >= REPORT_MIN_DAYS else "—"
    rows = "".join(f'<tr><th><a href="../../{esc(a)}/">{esc(lab(a))}</a></th><td>{pct(v["t"])}</td><td>{pct(v["s"])}</td>'
                   f"<td>{yen_full(v['p_wd']) if v['p_wd'] else '—'}</td><td>{yen_full(v['p_sat']) if v['p_sat'] else '—'}</td></tr>" for a, v in ms.items())
    others = "".join(f'<a href="../{esc(x)}/"' + (' aria-current="page"' if x == ym else "") + f">{int(x[:4])}年{int(x[5:])}月</a>" for x in months)
    inner = [f'<div class="hero"><div class="crumb"><a href="../../">{esc(site["name"])}</a> › <a href="../">空室レポート</a> › {y}年{m}月</div>'
             f"<h1>温泉地の空室レポート {y}年{m}月</h1>"
             f'<p class="lead">楽天トラベルの空室データを、当サイトが毎日自動で記録してまとめた月別のレポートです。{esc(period)}</p></div>',
             answer_html("", lead, bullets,
                         ["「今夜泊まれる宿の割合」は、毎日の取得時点で、その日にチェックインできる空きがあった宿の割合の、月の平均です。",
                          "「土曜泊の空き」は、各日の時点で、次の土曜に空きがあった宿の割合の平均です。",
                          f"料金は、空きのある宿の最安値({cond_text(cfg)})の中央値です。リアルタイムの値ではありません。"],
                         f"{y}年{m}月のまとめ", mood=("happy" if state_txt == "ready" else "sleepy"))]
    if state_txt == "ready":
        inner.append('<section class="card"><h2>エリア別の数字</h2><div class="scroll"><table class="stat"><thead><tr><th>エリア</th><th>今夜泊まれる宿の割合</th>'
                     "<th>次の土曜に空きのある宿の割合</th><th>平日の最安値(中央値)</th><th>土曜・祝前日の最安値(中央値)</th></tr></thead>"
                     f"<tbody>{rows}</tbody></table></div><p class='legend'>観測が{REPORT_MIN_DAYS}日分未満のエリアは「—」です。</p></section>")
    inner.append(f'<nav class="chips" aria-label="月を選ぶ"><b>ほかの月</b>{others}</nav>')
    faq = [(f"{y}年{m}月、温泉地の宿は空いていましたか?", lead + (" " + " ".join(bullets) if bullets else ""))]
    inner.append(faq_html(faq))
    mod = (days[-1][0] if days else now.date().isoformat())
    ld = [breadcrumb_ld([(site["name"], base + "/"), ("空室レポート", base + "/report/"), (f"{y}年{m}月", canonical)]),
          {"@context": "https://schema.org", "@type": "Report", "headline": f"温泉地の空室レポート {y}年{m}月", "url": canonical,
           "inLanguage": "ja", "description": lead, "datePublished": f"{ym}-01", "dateModified": mod,
           "author": {"@type": "Organization", "name": cfg.get("operator", {}).get("name") or site["name"]},
           "publisher": {"@type": "Organization", "name": site["name"], "url": base + "/"}},
          faq_ld(faq)]
    title = f"温泉地の空室レポート {y}年{m}月 | {site['name']}"
    return shell(cfg, demo, "../../", title, lead, canonical, "".join(inner), ld, noindex=(state_txt != "ready"))


def render_report_index(cfg, stats, months, now, demo):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = f"{base}/report/"
    lis = "".join(f'<li><a href="{esc(x)}/">温泉地の空室レポート {int(x[:4])}年{int(x[5:])}月</a></li>' for x in reversed(months))
    lead = "楽天トラベルの空室データを、当サイトが毎日記録してまとめた、月ごとのレポートの一覧です。"
    inner = (f'<div class="hero"><div class="crumb"><a href="../">{esc(site["name"])}</a> › 空室レポート</div>'
             f'<h1>温泉地の空室レポート(月別)</h1><p class="lead">{esc(lead)}</p></div>'
             f'<section class="card"><ul class="bul">{lis or "<li>集計中です</li>"}</ul></section>')
    ld = [breadcrumb_ld([(site["name"], base + "/"), ("空室レポート", canonical)])]
    return shell(cfg, demo, "../", f"温泉地の空室レポート(月別) | {site['name']}", lead, canonical, inner, ld)


# ---- 機械向けのデータ(/data/latest.json) ----------------------------------------
# AIアシスタントやエージェントが、ページを読まずに数字だけを取り出せるようにする。
DATA_JSON = "data/latest.json"


def data_json(cfg, hotels, state, wl, sums, now):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    holidays = set(cfg.get("holidays", []))
    today = now.date()
    groups = dict(areastat.GROUPS)
    areas = []
    for area, a in cfg["areas"].items():
        avail = {}
        for slug, name, ds, note in wl:
            if not ds:
                continue
            v = dict(sums[slug]["per"])[area]
            avail[slug] = {"label": name, "checkin_dates": [d.isoformat() for d in ds],
                           "hotels_checked": len(v["checked"]), "hotels_with_vacancy": len(v["vacant"]),
                           "min_price_jpy": v["low"], "fetched_at": v["got"].isoformat(timespec="minutes") if v["got"] else None}
        price = {}
        for g, sm in price_cells(cfg, hotels, state, area, today).items():
            if sm:
                price[g] = {"label": groups[g], "median_jpy": sm["med"], "p25_jpy": sm["p25"], "p75_jpy": sm["p75"], "samples": sm["n"]}
        areas.append({"key": area, "label": a["label"], "page_url": f"{base}/{area}/", "page_url_with_date": f"{base}/{area}/?d=YYYY-MM-DD",
                      "hotels_listed": len(areastat.area_nos(hotels, area)),
                      "availability": avail, "price_next_30_days": price,
                      "condition_pages": [{"condition": cfg["features"][p["feature"]]["label"], "url": f"{base}/{p['slug']}/",
                                           "url_with_date": f"{base}/{p['slug']}/?d=YYYY-MM-DD"}
                                          for p in LIVE if p["area"] == area]})
    total = {slug: {"label": name, "checkin_dates": [d.isoformat() for d in ds],
                    "hotels_checked": sums[slug]["checked"], "hotels_with_vacancy": sums[slug]["vacant"],
                    "min_price_jpy": sums[slug]["low"], "page_url": f"{base}/{slug}/"}
             for slug, name, ds, note in wl if ds}
    return {
        "name": f"{site['name']} 温泉地の空室・料金データ",
        "description": "楽天トラベルの空室検索APIから自動で取得し、ヤドミッケが集計した、温泉地の宿の空き数と料金の最新値です。",
        "generated_at": now.isoformat(timespec="seconds"),
        "source": "楽天トラベル 空室検索API(楽天ウェブサービス)",
        "attribution": "ヤドミッケ(楽天トラベルの空室データをもとに集計)",
        "site_url": base + "/",
        "notes": [
            f"料金は{cond_text(cfg)}の、取得時点の最安値(円)です。",
            "hotels_with_vacancy は、その日程(複数日なら、どれか1日)に1部屋でも空きがあった宿の数です。条件(露天風呂など)は問いません。",
            f"更新の目安: {freq_text(cfg)}。リアルタイムの在庫ではありません。予約前に楽天トラベルでの確認が必要です。",
            "price_next_30_days は、今後30日の宿泊日で空きのある宿の最安値の中央値と、中央の半分の範囲(p25〜p75)です。満室の宿は含みません。",
            "引用の際は、数字に fetched_at(取得日時)を添えてください。",
        ],
        "date_links": {
            "description": "ユーザーが日付を指定した場合は、?d=YYYY-MM-DD(チェックイン日)を付けたURLを案内すると、その日付を選んだ状態でページが開きます。",
            "area_page": f"{base}/(エリア名)/?d=YYYY-MM-DD",
            "condition_page": f"{base}/(ページ名)/?d=YYYY-MM-DD",
            "hotel_page": f"{base}/hotel/(施設番号)/?d=YYYY-MM-DD",
            "example": f"{base}/hakone-roten/?d={(today + timedelta(days=14)).isoformat()}"},
        "all_areas": total,
        "areas": areas,
        "links": {"price_overview": f"{base}/souba/", "monthly_reports": f"{base}/report/", "new_vacancies": f"{base}/ugoki/",
                  "statistics": f"{base}/stats/", "methodology": f"{base}/about/"},
    }


def home_answer(cfg, sums, wl, holidays, wkd=None):
    """トップページの最初に出す「いま空いている宿の数」。大きな数字のタイル(今夜・明日・今週末・年末年始など)と、
    折りたたんだ文章(検索・AI向けに、HTMLには常に入れておく)。wkd は今週末の [(日付, 空きのある宿の数)]"""
    parts, items, tiles = [], [], []
    newest = max([s["newest"] for s in sums.values() if s["newest"]], default=None)
    wk_txt = ""
    if wkd and 2 <= len(wkd) <= 3:
        wk_txt = "・".join(f"{md(d)}({WD[d.weekday()]}){n:,}軒" for d, n in wkd)
    for slug, name, ds, note in wl:
        sm = sums.get(slug)
        if not ds or not sm or not sm["checked"]:
            continue
        sp = span_txt(ds, holidays)
        lbl = name if slug in ("konya", "ashita") else f"{name}({sp})"
        num = wk_txt if (slug == "shumatsu" and wk_txt) else f"{sm['vacant']:,}軒"
        parts.append(f"{lbl}は{num}")
        items.append(f'<li><a href="{esc(slug)}/">{esc(lbl)}</a>: {esc(num)}(確認{sm["checked"]:,}軒)</li>')
        if len(tiles) < 4 and (slug != "renkyu" or not any(x[0] == "nenmatsu" for x in wl)):
            if slug == "shumatsu" and wk_txt:
                big = "".join(f'<span class="sd">{WD[d.weekday()]}<b>{n:,}</b>軒</span>' for d, n in wkd)
            else:
                big = f'<span class="sd"><b>{sm["vacant"]:,}</b>軒</span>'
            tiles.append(f'<a class="stile" href="{esc(slug)}/"><span class="sl">{esc(name)}</span>{big}<span class="sp">{esc(sp)}</span></a>')
    if not parts:
        return "", ""
    lead = (f"{when_txt(newest)}温泉地{len(cfg['areas'])}エリアで空きがある宿は、" + "、".join(parts)
            + f"です({cond_text(cfg)}、条件を問わない数)。")
    html = (f'<section class="snum" id="answer"><h2>いま空いている宿の数<small>{esc(fmt_dt(newest))}時点・{len(cfg["areas"])}温泉地・1泊大人2名</small></h2>'
            f'<div class="stiles">{"".join(tiles)}</div>'
            # 文章と、ほかの日程・データへのリンクは折りたたむ(HTMLには入っているので、検索・AIには読まれる)
            f'<details class="amore"><summary>文章で読む・ほかのデータを見る</summary><p class="lead2">{esc(lead)}</p><ul class="ab">{"".join(items)}'
            '<li><a href="imakara/">今から泊まれる宿(今夜・夜遅くチェックインOK)</a></li><li><a href="ugoki/">きのうから空きが出た宿</a></li>'
            '<li><a href="souba/">エリア・曜日別の料金相場</a></li><li><a href="report/">月別の空室レポート</a></li></ul></details></section>')
    return html, lead


MOVE_READY_DAYS = 14      # 「空きが戻りやすい宿」などは、観測がこの日数たまってから出す
MOVE_MIN_FULL = 20        # 1軒あたり「前回満室」だった観測がこの回数以上の宿だけ、割合を出す
FILL_MIN = 50             # 区分ごとの観測がこの数未満なら、割合を出さない
FILL_BUCKETS = [(0, 1, "当日・前日"), (2, 6, "2〜6日前"), (7, 13, "1〜2週間前"), (14, 30, "2週間〜1か月前"), (31, 9999, "1か月以上前")]


def moves_ready(stats, today):
    since = stats.get("moves_since")
    try:
        return since and (today - date.fromisoformat(since)).days >= MOVE_READY_DAYS
    except ValueError:
        return False


def fill_hazard(stats, area):
    """エリアごと・宿泊日までの日数ごとに、空いていた宿が1時間あたりに埋まる割合(当サイトの観測)。観測が少ない区分は入れない"""
    fl = stats.get("fill_lead", {}).get(area, {})
    hz = {}
    for lo, hi, _ in FILL_BUCKETS:
        n = f = g = 0
        for lead_s, v in fl.items():
            if lo <= int(lead_s) <= hi:
                n, f, g = n + v[0], f + v[1], g + (v[2] if len(v) > 2 else 0)
        if n >= FILL_MIN and g > 0:
            for d in range(lo, min(hi, 60) + 1):
                hz[d] = f / g
    return hz


def keep_pct(hz, lead, today_hours=6):
    """いま空いている宿が、宿泊日(lead日後)まで空いたまま残る見込み(%)。観測が足りない日数を含むときは None"""
    if lead < 0 or any(d not in hz for d in range(0, lead + 1)):
        return None
    tot = today_hours * hz[0] + sum(24 * hz[d] for d in range(1, lead + 1))
    return max(1, min(99, round(100 * math.exp(-tot))))


def keep_advice(k):
    return "早めの予約がおすすめです" if k < 40 else ("まだ少し余裕がありそうです" if k >= 70 else "数日のうちに埋まる宿も出てきそうです")


def back_info(stats, no):
    """宿ごとの「満室から空きに戻った」実績。(満室だった回数, 空きが出た回数, 割合%, 目安) か None"""
    m = stats.get("hotel_moves", {}).get(no)
    if not m or m[0] < MOVE_MIN_FULL:
        return None
    r = m[1] / m[0] * 100
    lvl = "高め" if r >= 10 else ("ふつう" if r >= 3 else "低め")
    return m[0], m[1], round(r), lvl


UGOKI_MAX = 10      # 「空きが出た宿」に載せる、1エリアあたりの件数(今も空いている宿を優先)
WHEN_PICKS = 3      # 今夜・明日などのページに載せる、1エリアあたりの口コミ上位の宿の数(エリアページは5軒)


def render_ugoki(cfg, hotels, state, stats, wl, now, demo):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = f"{base}/ugoki/"
    holidays = set(cfg.get("holidays", []))
    hmap = {}
    for area, lst in hotels.get("areas", {}).items():
        for h in lst:
            hmap.setdefault(h["no"], h)
    cut = now - timedelta(hours=24)
    latest = {}
    for c in state.get("changes", []):
        at = parse_dt(c.get("at", ""))
        if not at or at < cut or c.get("a") not in cfg["areas"] or c.get("no") not in hmap:
            continue
        k = (c["no"], c["d"])
        if k not in latest or at > latest[k][0]:
            latest[k] = (at, c)
    by_area = {}
    for (no, dk), (at, c) in latest.items():
        cur = state["cells"].get(no, {}).get(dk) or {}
        still = cur.get("s") == "v"
        by_area.setdefault(c["a"], []).append((at, c, still))
    n_all = len(latest)
    n_h = len({no for no, dk in latest})
    n_still = sum(1 for lst in by_area.values() for x in lst if x[2])
    nav = ('<nav class="chips" aria-label="日程を選ぶ"><b>日程から探す</b>'
           + "".join(f'<a href="../{esc(k)}/">{esc(n)}</a>' for k, n, *_ in wl) + "</nav>")
    since = stats.get("moves_since")
    if n_all:
        lead = (f"{when_txt(now)}直近24時間に、満室から空きが出た宿泊日は{n_all}件({n_h}軒)です。"
                f"そのうち{n_still}件は、最新の取得でも空いています({cond_text(cfg)})。")
    elif since:
        lead = f"{when_txt(now)}直近24時間に、満室から空きが出た宿は見つかっていません。"
    else:
        lead = "空きの動きの記録を始めたところです。次の更新から、満室から空きが出た宿をここに表示します。"
    sections, bullets = [], []
    for area in cfg["areas"]:
        lst = by_area.get(area)
        if not lst:
            continue
        lst.sort(key=lambda x: (not x[2], x[1]["d"], -x[0].timestamp()))
        alabel = cfg["areas"][area]["label"]
        bullets.append(f"{alabel}: {len(lst)}件")
        lis = []
        for at, c, still in lst[:UGOKI_MAX]:
            h = hmap[c["no"]]
            d = date.fromisoformat(c["d"])
            st = '<b class="ok">今も空き</b>' if still else "その後また満室"
            lis.append(f"<li>{name_link(h, d)}<small>{esc(day_label(d, holidays))}泊"
                       + (f" ・ {yen_full(c['p'])}〜" if c.get("p") else "") + f" ・ {fmt_dt(at)}に空きを確認 ・ {st}"
                       + (f' ・ {book_a(h, d, "楽天トラベルで見る")}' if still else "") + "</small></li>")
        more = (f"<p class='legend'>ほか{len(lst) - UGOKI_MAX}件(<a href=\"../{esc(area)}/\">{esc(alabel)}の空室まとめ</a>で最新の空きを確認できます)</p>"
                if len(lst) > UGOKI_MAX else "")
        sections.append(f'<section class="card"><h2>{esc(alabel)}で空きが出た宿</h2><ul class="hl picks">{"".join(lis)}</ul>{more}</section>')
    inner = [f'<div class="hero"><div class="crumb"><a href="../">{esc(site["name"])}</a> › 空きが出た宿</div>'
             "<h1>きのうから空きが出た宿(キャンセル待ちの目安)</h1>"
             '<p class="lead">前回の取得では満室だったのに、今回の取得で空きが見つかった宿と日付です。キャンセルなどで出た空きの可能性があります。</p></div>',
             answer_html(fmt_dt(now), lead, bullets,
                         ["楽天トラベルの空室検索APIを、" + freq_text(cfg) + "に確認し、前回と比べています。確認の間に空いてすぐ埋まった空きは、記録できません。",
                          "「今も空き」は、最新の取得時点の状態です。予約前に楽天トラベルでご確認ください。"],
                         "空きの動き(直近24時間)", mood=("happy" if n_all else "sleepy")), nav]
    inner += sections
    # 観測がたまってから出す: 空きが戻りやすい宿 / 何日前に埋まるか
    today = now.date()
    ready = moves_ready(stats, today)
    faq = [("満室だった宿に空きが出ることはありますか?", lead)]
    if ready:
        hm = stats.get("hotel_moves", {})
        cand = []
        for no, m in hm.items():
            h = hmap.get(no)
            if h and m[0] >= MOVE_MIN_FULL and m[1]:
                cand.append((m[1] / m[0], m, h))
        cand.sort(key=lambda x: (-x[0], -x[1][0]))
        if cand:
            area_of = {}
            for area, lst in hotels.get("areas", {}).items():
                for h in lst:
                    area_of.setdefault(h["no"], area)
            lis = "".join(f"<li>{name_link(h, today)}<small>{esc(cfg['areas'].get(area_of.get(h['no']), {}).get('label', ''))} ・ "
                          f"満室だった{m[0]}回のうち{m[1]}回、次の確認で空きが出た({r * 100:.0f}%)</small></li>" for r, m, h in cand[:15])
            inner.append(f'<section class="card"><h2>満室でも空きが戻りやすい宿</h2><ol class="hl picks">{lis}</ol>'
                         f"<p class='legend'>{esc(since)}からの観測。満室だった回数が{MOVE_MIN_FULL}回以上の宿だけを、空きが出た割合の高い順に並べています。</p></section>")
            faq.append(("満室でも空きが戻りやすい宿はどこですか?",
                        f"{since}からの当サイトの観測では、" + "、".join(f"{h['name']}({r * 100:.0f}%)" for r, m, h in cand[:5]) + "です。"))
        fl = stats.get("fill_lead", {})
        trs = ""
        for area in cfg["areas"]:
            cells = ""
            any_ok = False
            for lo, hi, _ in FILL_BUCKETS:
                n = f = g = 0
                for lead_s, v in fl.get(area, {}).items():
                    if lo <= int(lead_s) <= hi:
                        n, f, g = n + v[0], f + v[1], g + (v[2] if len(v) > 2 else 0)
                if n >= FILL_MIN and g > 0:
                    cells += f"<td>{f / (g / 24) * 100:.1f}%</td>"
                    any_ok = True
                else:
                    cells += "<td>—</td>"
            if any_ok:
                trs += f"<tr><th>{esc(cfg['areas'][area]['label'])}</th>{cells}</tr>"
        if trs:
            inner.append('<section class="card"><h2>宿泊日の何日前に埋まりやすいか</h2><div class="scroll"><table class="stat"><thead><tr><th>エリア</th>'
                         + "".join(f"<th>{esc(lbl)}</th>" for *_, lbl in FILL_BUCKETS) + f"</tr></thead><tbody>{trs}</tbody></table></div>"
                         "<p class='legend'>空いていた宿が、1日のうちに満室になる割合(当サイトの観測値)。数字が大きいほど、その時期に埋まりやすいことを表します。"
                         f"観測数が{FILL_MIN}未満の区分は「—」です。</p></section>")
    elif since:
        inner.append(f'<section class="card"><h2>空きが戻りやすい宿・何日前に埋まるか</h2><p>{esc(since)}から観測を始めました。'
                     f"{MOVE_READY_DAYS}日分たまったら、このページに自動で表示します。</p></section>")
    inner.append(faq_html(faq))
    ld = [breadcrumb_ld([(site["name"], base + "/"), ("空きが出た宿", canonical)]),
          {"@context": "https://schema.org", "@type": "CollectionPage", "name": "きのうから空きが出た宿", "url": canonical,
           "inLanguage": "ja", "description": lead, "dateModified": now.isoformat(timespec="seconds")}, faq_ld(faq)]
    title = f"きのうから空きが出た温泉地の宿【{md(now.date())}更新】 | {site['name']}"
    return shell(cfg, demo, "../", title, lead, canonical, "".join(inner), ld)


def btn_plain(url, text, cls=""):
    """表の中のリンク(見た目はセルのCSSで決まる)"""
    u = safe_url(url)
    c = f' class="{cls}"' if cls else ""
    if u == "#":
        return f"<span{c}>{text}</span>"
    return f'<a{c} href="{esc(u)}" target="_blank" rel="sponsored nofollow noopener">{text}</a>'


FCHIP_SHORT = {"pet": "ペット可"}   # トップの条件ボタンに出す短い名前(ないものは short か label)


def in_area(h, label, loose=False):
    """トップで「そのエリアの宿」として見せてよいか(楽天のエリア区分が広く、街のホテルが入ることがあるため)。
    宿名か住所にエリア名があれば可。loose なら「温泉」を含む宿も可"""
    t = f"{h.get('name', '')} {h.get('address', '')}"
    return any(x in t for x in label.replace("・", " ").replace("(", " ").replace(")", " ").split()) or (loose and "温泉" in t)


def home_area_images(cfg, hotels):
    """エリアごとの写真(そのエリアで口コミの多い、評価の高い宿の写真)"""
    out = {}
    for a in cfg["areas"]:
        lab = cfg["areas"][a]["label"]
        withimg = [h for h in hotels.get("areas", {}).get(a, []) if str(h.get("image", "")).startswith("https://")]
        cands = [h for h in withimg if in_area(h, lab)] or [h for h in withimg if in_area(h, lab, True)]
        cands.sort(key=lambda h: (-(1 if _rv(h) >= 4.3 else 0), -(h.get("reviews") or 0)))
        if cands:
            out[a] = safe_url(cands[0]["image"])
    return out


def home_picks(cfg, hotels, state, d, n=6):
    """その日に空きがある、口コミ評価の高い宿(1エリア1軒まで)。[(宿, エリア名, 最安, 日付)]"""
    cands = []
    for a, v in cfg["areas"].items():
        best = None
        for h, p, dd in area_vacancy(hotels, state, a, [d])["vacant"]:
            if not p or (h.get("reviews") or 0) < PICK_MIN_REVIEWS or not hotel_href(h) or not in_area(h, v["label"]):
                continue
            if not str(h.get("image_l") or h.get("image") or "").startswith("https://"):
                continue
            r = h.get("reviews") or 0
            # 口コミが少ない宿の満点に引っぱられないよう、件数で重みをつけた評価(100件分の4.3を足して平均)
            k = (round((_rv(h) * r + 4.3 * 100) / (r + 100), 3), r)
            if best is None or k > best[0]:
                best = (k, h, p)
        if best:
            cands.append((best[0], best[1], v["label"], best[2]))
    cands.sort(key=lambda x: (-x[0][0], -x[0][1]))
    return [(h, al, p, d) for _, h, al, p in cands[:n]]


def picks_html(picks, holidays, title):
    if not picks:
        return ""
    cards = []
    for h, al, p, d in picks:
        img = h.get("image_l") if str(h.get("image_l", "")).startswith("https://") else h.get("image")
        rv = (f'<span class="pv">★{esc(h["review"])}' + (f'<small>({h["reviews"]:,}件)</small>' if h.get("reviews") else "") + "</span>") if h.get("review") else ""
        cards.append(f'<div class="pcard"><a class="pin" href="{esc(hotel_href(h))}?d={d.isoformat()}"><img src="{esc(safe_url(img))}" alt="" loading="lazy">'
                     f'<span class="pb"><span class="pa">{esc(al)}</span><b>{esc(h["name"])}</b>{rv}'
                     f'<span class="pp">{esc(day_label(d, holidays))} <em>{yen_full(p)}〜</em><small>(2名1室)</small></span></span></a>'
                     + rk_btn(h, d, "楽天で空室を見る", "pbtn") + "</div>")
    return (f'<section class="picks" id="picks"><h2>{esc(title)}</h2><div class="pgrid">{"".join(cards)}</div>'
            '<p class="sub">口コミの多い宿のうち、評価(口コミ件数も加味)の高い順に各エリア1軒まで。料金は取得時点の楽天トラベルの掲載額(1泊・大人2名・1室の最安)です。写真・宿名を押すとその日を選んだ宿のページ、ボタンを押すと楽天トラベルのその日のプラン一覧が開きます。</p></section>')


def render_home(cfg, digests, now, demo, scene_list=(), answer=("", ""), area_today=None, n_hotels=0, aimg=None, picks=""):
    site = cfg["site"]
    base = site["url"].rstrip("/")
    canonical = base + "/"
    area_today = area_today or {}
    aimg = aimg or {}

    def ai(a):
        return f'<img src="{esc(aimg[a])}" alt="" loading="lazy">' if a in aimg else ""
    # 誰が・どうやって集めた数字かを、最初の画面に小さく出す(安心して楽天へ進んでもらうため)
    soon_h = cfg.get("refresh", {}).get("soon_hours", 4)
    trust = ('<div class="trust"><span>楽天トラベルの空室データ</span>'
             f'<span>今日・明日は約{soon_h}時間ごとに更新</span>'
             f'<span>{len(cfg["areas"])}温泉地' + (f'・約{n_hotels:,}軒' if n_hotels else "") + '</span></div>')
    inner = [hero_banner(), '<div class="hero"><h1>宿の空室を、日付からさがす</h1>'
             '<p class="lead">条件(なくてもOK)とエリアを選ぶと、その空室表へ進めます。</p>' + trust + '</div>', answer[0]]
    feats = [(k, f) for k, f in cfg["features"].items() if any(d[0]["feature"] == k for d in digests)]
    # ①条件 → ②エリア の一方向。エリアの一覧は1つだけで、選んだ条件に合わせて中身(リンク先・空き数)を切り替える
    # (ブラウザの機能が使えないときは、条件ごとの一覧が順に並ぶ)
    def chip(k, label, href, on):
        return (f'<a href="{esc(href)}" data-f="{esc(k)}" role="button" aria-pressed="{"true" if on else "false"}"'
                + (' class="on"' if on else "") + f">{esc(label)}</a>")
    room = [(k, f) for k, f in feats if not is_hotel_feat(cfg, k)]
    hotelf = [(k, f) for k, f in feats if is_hotel_feat(cfg, k)]
    short = lambda k, f: FCHIP_SHORT.get(k) or f.get("short") or f["label"]
    rows = [chip("all", "指定なし", "#finder", True)] + [chip(k, short(k, f), hub_slug(cfg, k) + "/", False) for k, f in room + hotelf]
    grps = []

    def sdata(a):
        v = cfg["areas"][a]
        return f' data-s="{esc(v["label"])} {esc(v.get("pref", ""))} {esc(a)}"'

    def grouped(cellmap):
        """エリアのマスを地方ごとに並べる(地方の見出しつき)"""
        return "".join(f'<div class="rg" data-rg="{esc(r)}"><h4>{esc(r)}</h4><div class="agrid">' + "".join(cellmap[k] for k in ks) + "</div></div>"
                       for r, ks in by_region(cfg, list(cfg["areas"])))
    cellmap = {}
    for a, v in cfg["areas"].items():
        n = area_today.get(a)
        cellmap[a] = (f'<a href="{esc(a)}/"{sdata(a)}>{ai(a)}<b>{esc(v["label"])}</b><small>' + (f"今日の空き {n}軒" if n is not None else "空室を見る") + "</small></a>")
    grps.append('<div class="fgrp" data-f="all"><h3>選択中: <b>条件なし</b>(エリアのページへ)</h3>' + grouped(cellmap) + "</div>")
    for fkey, f in feats:
        bya = {d[0]["area"]: d for d in digests if d[0]["feature"] == fkey}
        cellmap = {}
        for a, v in cfg["areas"].items():
            d = bya.get(a)
            if d:
                p, n, today_n, _ = d
                cellmap[a] = (f'<a href="{esc(p["slug"])}/"{sdata(a)}>{ai(a)}<b>{esc(v["label"])}</b><small>'
                              + (f"今日の空き {today_n}軒" if n else "準備中") + "</small></a>")
            else:
                cellmap[a] = f'<span class="na"{sdata(a)}>{ai(a)}<b>{esc(v["label"])}</b><small>対象の宿なし</small></span>'
        hub = hub_slug(cfg, fkey)
        more = [f'<a href="{esc(hub)}/">{esc(short(fkey, f))}の全エリアまとめ ›</a>']
        if not is_hotel_feat(cfg, fkey):
            more += [f'<a href="{esc(hub)}/{esc(sc["slug"])}/">{esc(sc["name"])}</a>' for sc in scene_list]
        grps.append(f'<div class="fgrp" data-f="{esc(fkey)}"><h3>選択中: <b>{esc(f["label"])}</b></h3>' + grouped(cellmap)
                    + '<p class="fmore">' + " ・ ".join(more) + "</p></div>")
    inner.append('<section class="card finder" id="finder"><h2>エリアと条件でさがす</h2>'
                 '<p class="fstep"><b>1</b>条件をえらぶ(なくてもOK)</p><nav class="fchips" id="fchips" aria-label="条件を選ぶ">' + "".join(rows) + "</nav>"
                 f'<p class="fstep"><b>2</b>エリアをえらぶ <small>{len(cfg["areas"])}温泉地・数字は今日の空き軒数</small></p>'
                 '<div class="afind"><input type="search" id="aq" placeholder="温泉地名・県名で探す(例: 箱根、静岡)" aria-label="温泉地名・県名で探す" autocomplete="off">'
                 '<nav class="rchips" id="rchips" aria-label="地方で絞り込む"><button type="button" data-r="" class="on">すべて</button>'
                 + "".join(f'<button type="button" data-r="{esc(r)}">{esc(r)}</button>' for r, _ in by_region(cfg, list(cfg["areas"])))
                 + '</nav><p class="anone" id="anone" hidden>見つかりませんでした。ほかの名前や県名で探してみてください。</p></div>'
                 + "".join(grps) + "</section>")
    # 地方のボタンと、名前・県名での絞り込み(どの条件を選んでいても効く)
    inner.append("<script>(function(){var q=document.getElementById('aq'),rc=document.getElementById('rchips'),no=document.getElementById('anone');if(!q||!rc)return;var cur='';"
                 "function ap(){var t=(q.value||'').trim(),vis=0;var gs=document.querySelectorAll('.fgrp');for(var i=0;i<gs.length;i++){if(gs[i].hidden)continue;"
                 "var rs=gs[i].querySelectorAll('.rg');for(var j=0;j<rs.length;j++){var r=rs[j],okr=!cur||r.getAttribute('data-rg')===cur,n=0;"
                 "var cs=r.querySelectorAll('[data-s]');for(var k=0;k<cs.length;k++){var ok=okr&&(!t||cs[k].getAttribute('data-s').indexOf(t)>=0);cs[k].hidden=!ok;if(ok)n++;}"
                 "r.hidden=!n;vis+=n;}}no.hidden=vis>0;}"
                 "q.addEventListener('input',ap);rc.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;cur=b.getAttribute('data-r');"
                 "var bs=rc.querySelectorAll('button');for(var i=0;i<bs.length;i++)bs[i].classList.toggle('on',bs[i]===b);ap();});"
                 "var fc=document.getElementById('fchips');if(fc)fc.addEventListener('click',function(){setTimeout(ap,0);});})();</script>")
    inner.append("<script>(function(){var c=document.getElementById('fchips');if(!c)return;var b=c.querySelectorAll('a[data-f]'),g=document.querySelectorAll('.fgrp');"
                 "function show(f){for(var i=0;i<b.length;i++){var on=b[i].getAttribute('data-f')===f;b[i].classList.toggle('on',on);b[i].setAttribute('aria-pressed',on?'true':'false');}"
                 "for(var j=0;j<g.length;j++)g[j].hidden=g[j].getAttribute('data-f')!==f;}"
                 "for(var i=0;i<b.length;i++){b[i].addEventListener('click',function(e){e.preventDefault();show(this.getAttribute('data-f'));});}"
                 "document.getElementById('finder').classList.add('js');show('all');})();</script>")
    inner.append(picks)
    same = [u for u in site.get("same_as", []) if str(u).startswith("https://")]
    org = {"@type": "Organization", "name": site["name"], "url": canonical}
    if same:
        org["sameAs"] = same
    ld = [{"@context": "https://schema.org", "@type": "WebSite", "name": site["name"], "url": canonical, "inLanguage": "ja"},
          dict(org, **{"@context": "https://schema.org"})]
    title = f"{site['name']} | 宿の空室をまとめてチェック"
    desc = answer[1] or "楽天トラベルの空室を日付ごとにまとめた、非公式の空室チェックサイト。取得日時と判定方法を公開しています。"
    return shell(cfg, demo, "./", title, desc, canonical, "".join(inner), ld)


ARTIFICIAL_BOTS = ["OAI-SearchBot", "ChatGPT-User", "GPTBot", "PerplexityBot", "Perplexity-User", "Claude-SearchBot",
                   "Claude-User", "ClaudeBot", "Google-Extended", "Applebot", "bingbot", "Googlebot"]


def write_robots(out_dir, base):
    lines = ["# 検索・AIアシスタントの巡回を歓迎します(数字の出どころは /about/ に書いています)"]
    for bot in ARTIFICIAL_BOTS:
        lines += [f"User-agent: {bot}", "Allow: /", ""]
    lines += ["User-agent: *", "Allow: /", "", f"Sitemap: {base}/sitemap.xml", ""]
    (out_dir / "robots.txt").write_text("\n".join(lines), encoding="utf-8")


def write_page(out_dir, rel, text):
    d = out_dir / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--out-dir", default=str(ROOT / "public"))
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    cfg = load_json(args.config, None)
    if cfg is None:
        sys.exit("config.json が見つかりません")
    # config.json に書かれていない項目は、既定値で補う(設定ファイルを上書きしなくても、新機能が使えるように)
    op0 = cfg.setdefault("operator", {})
    if not op0.get("name"):
        op0["name"] = "ヤドミッケ運営"
        op0.setdefault("note", "連絡先は準備中です。")
    cfg.setdefault("themes", DEFAULT_THEMES)
    cfg.setdefault("theme_min_hotels", 3)
    data_dir, out_dir = Path(args.data_dir), Path(args.out_dir)
    hotels = load_json(data_dir / "hotels.json", {"areas": {}})
    # 宿名の全角英数字・全角スペースを読みやすい形にそろえる(例: 「ｈｏｔｅｌ」→「hotel」)。検索された言葉とも合いやすくなる
    for _lst in hotels.get("areas", {}).values():
        for _h in (_lst if isinstance(_lst, list) else []):
            if _h.get("name"):
                _h["name"] = norm_name(_h["name"])
    state = load_json(data_dir / "state.json", {"cells": {}, "fetched_by": {}, "meta": {}})
    state.setdefault("cells", {})
    # 宿がまだ1軒も見つかっていないエリア(追加したばかり、またはコード違い)は、ページを作らない
    have = {a for a, lst in hotels.get("areas", {}).items() if lst}
    if have:
        wait = [cfg["areas"][a]["label"] for a in cfg["areas"] if a not in have]
        if wait:
            print(f"宿の一覧がまだ無いため、ページを作らないエリア: {'、'.join(wait)}")
        cfg["areas"] = {k: v for k, v in cfg["areas"].items() if k in have}
        cfg["pages"] = [pg for pg in cfg["pages"] if pg["area"] in have]
    aff = os.environ.get("RAKUTEN_AFFILIATE_ID", "").strip() or str(load_json(data_dir / "site_meta.json", {}).get("affiliate_id", ""))
    global LINK
    LINK = None
    if not args.demo:
        LINK = {"a": aff, "ad": int(cfg["search"].get("adults", 2)), "n": int(cfg["search"].get("nights", 1))}
        rewrite_links(cfg, state, aff)
        for _lst in hotels.get("areas", {}).values():
            for _h in (_lst if isinstance(_lst, list) else []):
                for _k in ("url", "plan_url"):
                    if _h.get(_k):
                        _h[_k] = aff_wrap(aff, _h[_k])
        for _days in state.get("cells", {}).values():
            for _c in _days.values():
                if _c.get("any") and len(_c["any"]) > 1:
                    _c["any"][1] = aff_wrap(aff, _c["any"][1])
    state.setdefault("fetched_by", {})
    state.setdefault("meta", {})
    stats = load_json(data_dir / "stats.json", {}) or {}
    detail = load_json(data_dir / "hotel_detail.json", {})
    if detail:
        n_auto = apply_hotel_features(cfg, hotels, state, detail)
        print(f"宿の条件ページ: {n_auto} 枚(詳細取得済み {len(detail)} 軒)")
    # プラン名で判定する条件(記念日・カップルなど)は、当てはまる宿が AUTO_PAGE_MIN 軒以上のエリアだけ、ページを自動で足す
    have_slugs = {pg["slug"] for pg in cfg["pages"]}
    for fk, fv in cfg["features"].items():
        if not fv.get("auto_pages") or fv.get("hotel"):
            continue
        for ak, av in cfg["areas"].items():
            n = sum(1 for h in hotels.get("areas", {}).get(ak, []) if state.get("meta", {}).get(h["no"], {}).get("seen", {}).get(fk))
            slug = f"{ak}-{fk}"
            if n >= AUTO_PAGE_MIN and slug not in have_slugs:
                cfg["pages"].append({"slug": slug, "area": ak, "feature": fk,
                                     "title": f"{av['label']}で今空いている{fv['label']}の宿",
                                     "lead": f"{av['label']}で、{fdesc(cfg, fk)}の空きを、日付ごとにまとめています(プラン名からの自動判定)。"})
    # 部屋名ベースのページを先に作り、そのあと宿の条件ページを作る(宿ページへのリンクを共有するため)
    cfg["pages"].sort(key=lambda p: bool(p.get("auto")))
    now = datetime.now(JST)
    base = cfg["site"]["url"].rstrip("/")

    out_dir.mkdir(parents=True, exist_ok=True)
    digests, ctxs, paths, snaps = [], {}, ["", "about/", "privacy/"], []
    global LIVE, DETAIL
    DETAIL = detail or {}
    LIVE = []
    primary = {}   # 施設番号 -> 宿ページの代表にする (ページ, 宿)。部屋の条件のページが先に並んでいるので、そちらが優先される
    for page in cfg["pages"]:
        ps = dict(state, fetched=state.get("fetched_by", {}).get(page["area"], {}))
        prow = hotels_for_page(cfg, hotels, ps, page)
        if prow or not ps["fetched"]:
            LIVE.append(page)
        for h in prow:
            if re.fullmatch(r"[0-9A-Za-z_-]+", str(h["no"])) and h["no"] not in primary:
                primary[h["no"]] = (page, h)
    # 条件ページに載らない宿でも、口コミが多い宿には宿ページを作る(ChatGPTは宿ページを参考にすることが多いため)。
    # 空室は「宿全体の空き」(条件を問わない)で見せる。代表ページは画面に出ない仮のページ(エリアページへつなぐ)
    vpages = {}
    today_iso = now.date().isoformat()
    for area, lst in hotels.get("areas", {}).items():
        if area not in cfg["areas"]:
            continue
        for h in lst:
            no = h["no"]
            if no in primary or not re.fullmatch(r"[0-9A-Za-z_-]+", str(no)) or (h.get("reviews") or 0) < POPULAR_MIN_REVIEWS:
                continue
            cells = state["cells"].get(no, {})
            if sum(1 for d in cells if d >= today_iso) < 3:
                continue
            for c in cells.values():
                if c.get("s") == "v" and c.get("any") and c["any"][0]:
                    c.setdefault("f", {})[ZEN] = [c["any"][0], c["any"][1], "宿全体の最安プラン"]
            state.setdefault("meta", {}).setdefault(no, {"checks": 0, "seen": {}}).setdefault("seen", {})[ZEN] = True
            vp = vpages.setdefault(area, {"slug": area, "area": area, "feature": ZEN, "auto": True, "virtual": True,
                                          "title": f"{cfg['areas'][area]['label']}の宿の空室", "lead": ""})
            primary[no] = (vp, h)
    HOTEL_HREF.clear()
    HOTEL_HREF.update({no: f"/hotel/{no}/" for no in primary})
    hub_items = {}
    global CUR_AUTO
    for page in cfg["pages"]:
        area = page["area"]
        CUR_AUTO = bool(page.get("auto"))
        # このエリアだけの見え方(取得日時・履歴は、エリアごとに貯めている)
        pstate = dict(state, fetched=state.get("fetched_by", {}).get(area, {}))
        pre = f"{area}/"
        pstats = dict(stats,
                      trans={f: stats.get("trans", {}).get(pre + f, {}) for f in cfg["features"]},
                      daily={od: {f: v.get(pre + f, {}) for f in cfg["features"]} for od, v in stats.get("daily", {}).items()})
        n_rows_probe = len(hotels_for_page(cfg, hotels, pstate, page))
        if not n_rows_probe and pstate["fetched"]:
            # 取得は進んだのに、該当する宿が1軒もない組み合わせは、ページを作らない(中身のないページを公開しない)
            shutil.rmtree(out_dir / page["slug"], ignore_errors=True)
            print(f"{page['slug']}: 該当する宿がないため、ページを作りません")
            continue
        snaps.append(page_snapshot(cfg, hotels, pstate, page, now.date()))
        SNAPS[page["slug"]] = snaps[-1]
        _rows = hotels_for_page(cfg, hotels, pstate, page)
        _dates = [now.date() + timedelta(days=i) for i in range(area_days(cfg, area))]
        hub_items.setdefault(page["feature"], []).append((page, _rows, pstate, _dates, summarize(_rows, _dates, page["feature"], pstate), snaps[-1]))
        shutil.rmtree(out_dir / page["slug"] / "hotel", ignore_errors=True)   # 以前の宿ページ(条件ごと)は /hotel/番号/ にまとめた
        text, n_rows, today_n, digest = render_page(cfg, hotels, pstate, pstats, page, now, args.demo)
        write_page(out_dir, page["slug"], text)
        digests.append((page, n_rows, today_n, digest))
        paths.append(page["slug"] + "/")
        print(f"{page['slug']}: 宿 {n_rows} 軒 / 本日◯ {today_n} 軒 / {len(text) // 1024} KB")

        ctx = page_ctx(cfg, hotels, pstate, page, now)
        ctxs[page["slug"]] = ctx
        if page.get("auto"):
            print("  (宿の条件ページ: 日付別・テーマ別のページは作りません)")
            continue
        scene_list = page_scenes(cfg, now.date())
        for sc in scene_list:
            write_page(out_dir, f"{page['slug']}/{sc['slug']}", render_scene(cfg, page, sc, scene_list, ctx, pstate, pstats, args.demo))
            paths.append(f"{page['slug']}/{sc['slug']}/")
        # テーマ別(高評価・3万円以下など)と、作らなくなった日程のページは消す(古いURLは _redirects で親ページへ)
        for sub in [th["slug"] for th in cfg.get("themes", [])] + list(DROPPED_SCENES):
            shutil.rmtree(out_dir / page["slug"] / sub, ignore_errors=True)
        print(f"  日付別ページ {len(scene_list)} 枚")

    # 宿ページ(1軒1ページ)。古い宿ページはまとめて作り直す
    shutil.rmtree(out_dir / "hotel", ignore_errors=True)
    cfg["features"][ZEN] = {"label": "宿全体の空き", "keywords": [], "hotel": True, "hidden": True, "short": "空きのある宿",
                            "def": "楽天トラベルで、その日に1部屋でも空きがあること(条件は問わない)"}
    for area, vp in vpages.items():
        ctxs.setdefault(vp["slug"] + "#all", page_ctx(cfg, hotels, dict(state, fetched=state.get("fetched_by", {}).get(area, {})), vp, now))
    for no, (page, h) in primary.items():
        area = page["area"]
        pstate = dict(state, fetched=state.get("fetched_by", {}).get(area, {}))
        pre = f"{area}/"
        pstats = dict(stats, trans={f: stats.get("trans", {}).get(pre + f, {}) for f in cfg["features"]})
        hctx = ctxs[page["slug"] + "#all"] if page.get("virtual") else ctxs[page["slug"]]
        write_page(out_dir, f"hotel/{no}", render_hotel(cfg, page, h, hctx, pstate, pstats, args.demo))
        paths.append(f"hotel/{no}/")
    print(f"宿ページ: {len(primary)} 枚(1軒1ページ。うち条件ページに載らない口コミの多い宿 {sum(1 for p_, _ in primary.values() if p_.get('virtual'))} 枚)")
    # 古い宿ページのURL(/エリア-条件/hotel/番号/)から、新しいURLへ自動で案内する(Cloudflare Pages の _redirects)
    # まとめたページ(テーマ別・来週末・クリスマス)の古いURLは、親のページへ自動で案内する
    gone = [th["slug"] for th in cfg.get("themes", [])] + list(DROPPED_SCENES)
    rules = ["/:slug/hotel/:no/ /hotel/:no/ 301", "/:slug/hotel/:no /hotel/:no/ 301"]
    rules += [r for g in gone for r in (f"/:slug/{g}/ /:slug/ 301", f"/:slug/{g} /:slug/ 301")]
    (out_dir / "_redirects").write_text("\n".join(rules) + "\n", encoding="utf-8")

    scene_list = page_scenes(cfg, now.date())
    CUR_AUTO = False
    for fkey, items in hub_items.items():
        if not any(p["slug"] for p, *_ in items):
            continue
        write_page(out_dir, hub_slug(cfg, fkey), render_feature_hub(cfg, fkey, items, scene_list, now, args.demo))
        paths.append(f"{hub_slug(cfg, fkey)}/")
    for fkey in cfg["features"]:
        if is_hotel_feat(cfg, fkey):
            continue
        area_ctxs = [(pg, ctxs[pg["slug"]]) for pg in cfg["pages"] if pg["feature"] == fkey and pg["slug"] in ctxs]
        if not area_ctxs:
            continue
        for g in DROPPED_SCENES:
            shutil.rmtree(out_dir / hub_slug(cfg, fkey) / g, ignore_errors=True)
        for sc in scene_list:
            write_page(out_dir, f"{hub_slug(cfg, fkey)}/{sc['slug']}", render_hub(cfg, fkey, sc, scene_list, area_ctxs, now, args.demo))
            paths.append(f"{hub_slug(cfg, fkey)}/{sc['slug']}/")
    # 全エリア・日程別のページ(今夜・明日・今週末・次の連休・年末年始)と、空きが出た宿
    wl = when_list(cfg, now.date())
    when_sums = {}
    for slug, name, ds, note in wl:
        text, when_sums[slug] = render_when(cfg, hotels, state, slug, name, ds, note, wl, now, args.demo)
        write_page(out_dir, slug, text)
        paths.append(f"{slug}/")
    write_page(out_dir, "ugoki", render_ugoki(cfg, hotels, state, stats, wl, now, args.demo))
    write_page(out_dir, "imakara", render_imakara(cfg, hotels, state, now, args.demo))
    paths.append("imakara/")
    paths.append("ugoki/")
    for area in cfg["areas"]:
        write_page(out_dir, area, render_area(cfg, hotels, state, area, wl, when_sums, now, args.demo))
        paths.append(f"{area}/")
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    (out_dir / DATA_JSON).write_text(json.dumps(data_json(cfg, hotels, state, wl, when_sums, now), ensure_ascii=False, indent=1), encoding="utf-8")
    # どのサイトからでも読めるようにする(Cloudflare Pages の _headers)
    (out_dir / "_headers").write_text("/data/*\n  Access-Control-Allow-Origin: *\n  Cache-Control: public, max-age=600\n", encoding="utf-8")
    write_page(out_dir, "souba", render_souba(cfg, hotels, state, now, args.demo))
    paths.append("souba/")
    months = sorted({d[:7] for d in stats.get("area_daily", {})})
    shutil.rmtree(out_dir / "report", ignore_errors=True)
    for ym in months:
        write_page(out_dir, f"report/{ym}", render_report(cfg, stats, ym, months, now, args.demo))
        if len([d for d in stats["area_daily"] if d.startswith(ym)]) >= REPORT_MIN_DAYS:
            paths.append(f"report/{ym}/")
    write_page(out_dir, "report", render_report_index(cfg, stats, months, now, args.demo))
    paths.append("report/")
    newest_all = max([x for x in (parse_dt(v) for f in state.get("fetched_by", {}).values() for v in f.values()) if x], default=None)
    write_page(out_dir, "stats", render_stats(cfg, snaps, stats, newest_all, now, args.demo))
    ok_snaps = [x for x in snaps if x["hotels"] >= STAT_MIN_HOTELS and x["days"]]
    ok_snaps.sort(key=lambda x: (list(cfg["areas"]).index(x["area"]), list(cfg["features"]).index(x["feat"])))
    (out_dir / "stats" / "latest.csv").write_text(stats_csv(ok_snaps, (newest_all or now).isoformat(timespec="minutes")), encoding="utf-8")
    paths.append("stats/")
    (out_dir / "llms.txt").write_text(llms_txt(cfg, digests, bool(ok_snaps), now, wl), encoding="utf-8")
    write_page(out_dir, "about", render_about(cfg, ctxs, stats, now, args.demo))
    write_page(out_dir, "privacy", render_privacy(cfg, now, args.demo))
    # 存在しないURLに、トップページを200で返さず、正しく404を返すためのページ(Cloudflare Pagesは 404.html があれば使う)
    site0 = cfg["site"]
    inner404 = ('<div class="hero"><h1>ページが見つかりません</h1></div>'
                '<div class="empty"><b>お探しのページは存在しないか、移動した可能性があります。</b><br>'
                '下のリンクから、宿の空室を探してみてください。</div>'
                '<p style="margin:16px 0"><a href="/">' + esc(site0["name"]) + ' トップへ</a></p>')
    # 消えたページ(宿ページ・条件ページ)に来た人は、そのエリアのページへ自動で案内する(日付 ?d= も引き継ぐ)。
    # 検索エンジンには「ページなし(404)」のまま伝わる
    hmap = {}
    for a_, lst in hotels.get("areas", {}).items():
        if a_ in cfg["areas"]:
            for h_ in lst:
                hmap.setdefault(h_["no"], a_)
    alist = list(cfg["areas"])
    goto = {"a": alist, "s": sorted(k for k in ctxs if "#" not in k), "h": {no: alist.index(a_) for no, a_ in hmap.items()}}
    inner404 += ('<p id="mv" hidden>このページは移動しました。エリアのページへご案内します…</p>'
                 "<script>(function(){var G=" + json.dumps(goto, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + ","
                 "p=location.pathname,s=p.split('/').filter(Boolean),t=null;"
                 "if(s[0]==='hotel'&&s[1]){var a=G.h[s[1]];t=(a!==undefined)?'/'+G.a[a]+'/':null;}"
                 "else if(s.length){var k=s[0].split('-')[0];if(G.s.indexOf(s[0])>=0&&s.length>1)t='/'+s[0]+'/';"
                 "else if(G.a.indexOf(k)>=0)t='/'+k+'/';}"
                 "if(t&&t!==p){document.getElementById('mv').hidden=false;location.replace(t+location.search);}})();</script>")
    (out_dir / "404.html").write_text(
        shell(cfg, args.demo, "/", f"ページが見つかりません | {site0['name']}", "お探しのページは見つかりませんでした。",
              site0["url"].rstrip("/") + "/", inner404, None, noindex=True), encoding="utf-8")
    _hol = set(cfg.get("holidays", []))
    _wk = next((ds for slug, _, ds, _ in wl if slug == "shumatsu"), [])
    _wkd = [(d, sum(len(area_vacancy(hotels, state, a, [d])["vacant"]) for a in cfg["areas"])) for d in _wk]
    _pd = next((d for d in _wk if d.weekday() == 5), _wk[-1] if _wk else now.date())
    _pk = home_picks(cfg, hotels, state, _pd)
    home_pick_html = picks_html(_pk, _hol, f"{day_label(_pd, _hol)}に空いている、評価の高い宿")
    (out_dir / "index.html").write_text(render_home(cfg, digests, now, args.demo, page_scenes(cfg, now.date()), home_answer(cfg, when_sums, wl, _hol, _wkd),
                                                           {a: len(area_vacancy(hotels, state, a, [now.date()])["vacant"]) for a in cfg["areas"]},
                                                           round(len({h["no"] for a in cfg["areas"] for h in hotels.get("areas", {}).get(a, [])}), -2),
                                                           home_area_images(cfg, hotels), home_pick_html), encoding="utf-8")

    og = ROOT / "assets" / "ogp.png"
    if og.exists():
        (out_dir / "ogp.png").write_bytes(og.read_bytes())
    key = str(cfg["site"].get("indexnow_key", "")).strip()
    if re.fullmatch(r"[0-9a-f]{8,128}", key):
        (out_dir / f"{key}.txt").write_text(key, encoding="utf-8")   # IndexNow(Bing等への更新通知)の本人確認用
    op = cfg.get("operator", {})
    if not (op.get("name") or op.get("contact")):
        print("注意: config.json の operator(運営者名・連絡先)が空です。信頼性のため、入力することをおすすめします(手順書を参照)")
    if "YOUR-SITE" in base:
        print("注意: config.json の site.url がまだ初期値です。公開URLに書き換えてください")
    if args.demo:
        return
    mod = (parse_dt(state.get("updated", "")) or now).date().isoformat()
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{esc(base + '/' + p)}</loc><lastmod>{mod}</lastmod></url>" for p in paths) + "</urlset>", encoding="utf-8")
    write_robots(out_dir, base)


if __name__ == "__main__":
    main()

