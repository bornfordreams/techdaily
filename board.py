"""WorldMonitor 式监控看板 v3：横屏指挥中心布局。

PC/横屏：地图占主屏 + 右侧栏（追踪故事线+统计）+ 下方今日卡片墙；
手机竖屏：自动退化为单列（媒体查询自适应）。
坐标来源与缓存逻辑同 v2；纯静态零 JS，加密 board.json 分发。
"""
from __future__ import annotations

import html
import json
import logging
from collections import Counter
from datetime import date as date_cls
from pathlib import Path

from config import DATA_DIR, ROOT, Settings
from report import list_reports, load_report
from tracker import load_stories

log = logging.getLogger("techdaily.board")

TOPIC_COLOR = {"AI": "#e056fd", "芯片": "#f0932b", "终端": "#eb4d4b", "互联网": "#6ab04c",
               "航天": "#22a6b3", "新能源": "#f6b93b", "其他": "#7f8fa6"}
MAP_W, MAP_H = 960, 500
GEOJSON = ROOT / "tools" / "map_data" / "world.geojson"
if not GEOJSON.exists():  # 一键部署 EXE 内置底图（PyInstaller --add-data）
    import sys as _sys
    _bundled = Path(getattr(_sys, "_MEIPASS", "")) / "map_data" / "world.geojson"
    if _bundled.exists():
        GEOJSON = _bundled
MAP_CACHE = DATA_DIR / "world_map.svg"
GEO_CACHE = DATA_DIR / "geo_cache.json"


def _esc(t) -> str:
    return html.escape(str(t or ""))


def _project(lon: float, lat: float):
    return round((lon + 180) / 360 * MAP_W, 1), round((90 - lat) / 180 * MAP_H, 1)


def _world_paths() -> str:
    """GeoJSON -> SVG path（一次性生成缓存）"""
    if MAP_CACHE.exists():
        return MAP_CACHE.read_text(encoding="utf-8")
    if not GEOJSON.exists():
        log.warning("缺少底图 %s，地图区退化为网格", GEOJSON)
        grid = "".join(f'<line x1="0" y1="{y}" x2="{MAP_W}" y2="{y}" stroke="#141d31"/>'
                       f'<line x1="{x}" y1="0" x2="{x}" y2="{MAP_H}" stroke="#141d31"/>'
                       for x in range(0, MAP_W + 1, 80) for y in range(0, MAP_H + 1, 50))
        return grid
    data = json.loads(GEOJSON.read_text(encoding="utf-8"))
    paths = []
    for f in data["features"]:
        geom = f.get("geometry") or {}
        polys = geom.get("coordinates") or []
        if geom.get("type") == "Polygon":
            polys = [polys]
        d = []
        for poly in polys:
            for ring in poly:
                pts = []
                for lon, lat in ring:
                    x, y = _project(lon, lat)
                    pts.append(f"{x} {y}")
                if pts:
                    d.append("M" + "L".join(pts) + "Z")
        if d:
            paths.append(f'<path d="{" ".join(d)}"/>')
    svg = f'<g fill="#161f33" stroke="#243352" stroke-width="0.6">{"".join(paths)}</g>'
    MAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MAP_CACHE.write_text(svg, encoding="utf-8")
    log.info("世界底图 SVG 已生成（%d 国家）", len(paths))
    return svg


def _geo_fallback(items: list, glm) -> dict:
    """旧刊缺坐标时 GLM 补定位（缓存 date+rank -> {place,lat,lon}）"""
    cache = {}
    if GEO_CACHE.exists():
        try:
            cache = json.loads(GEO_CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    missing = [it for it in items
               if not (it.get("location") or {}).get("lat")
               and f"{it.get('_date')}#{it.get('rank')}" not in cache]
    if missing and glm is not None:
        lines = [{"rank": it["rank"], "title": it["title"],
                  "detail": (it.get("detail") or "")[:120]} for it in missing]
        try:
            verdict = glm.chat_json(
                "你是地理编码器。对每条科技新闻给出事件核心发生地（城市级；线上/全球事件取"
                "主要当事方总部所在地）。只输出JSON："
                '{"<rank>":{"place":"地名","lat":数值,"lon":数值}}',
                json.dumps(lines, ensure_ascii=False), max_tokens=800)
            for it in missing:
                g = verdict.get(str(it["rank"])) or {}
                if isinstance(g, dict) and isinstance(g.get("lat"), (int, float)):
                    cache[f"{it.get('_date')}#{it.get('rank')}"] = {
                        "place": str(g.get("place", ""))[:20], "lat": g["lat"], "lon": g["lon"]}
        except Exception as e:
            log.warning("补定位失败：%s", e)
        GEO_CACHE.parent.mkdir(parents=True, exist_ok=True)
        GEO_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return cache


MAP_JS = """
(function(){
var svg=document.getElementById('map'),g=document.getElementById('world');
if(!svg)return;
var vb=svg.viewBox.baseVal,W=vb.width,H=vb.height,s=1,tx=0,ty=0;
var lbls=svg.querySelectorAll('text.lbl'),leads=svg.querySelectorAll('line.lead'),
    dgs=svg.querySelectorAll('g.dg');
function apply(){
  g.setAttribute('transform','translate('+tx+' '+ty+') scale('+s+')');
  fitText();
}
/* 标签/引线/落点全部反向补偿：屏幕上始终保持恒定大小（地图应用行为） */
function fitText(){
  var r=svg.getBoundingClientRect();if(!r.width)return;
  var total=(r.width/W)*s;         /* 一个用户单位在屏幕上的实际像素 */
  var fs=(13/total).toFixed(2);    /* 屏幕上恒定 13px 的字号 */
  var sw=(3/total).toFixed(2);
  var lw=(1.1/total).toFixed(2);
  var ds=(1.45/total).toFixed(3);  /* 落点/脉冲环恒定约6-7px屏幕半径 */
  for(var i=0;i<lbls.length;i++){lbls[i].style.fontSize=fs+'px';lbls[i].style.strokeWidth=sw+'px';}
  for(var j=0;j<leads.length;j++){leads[j].style.strokeWidth=lw+'px';}
  for(var k2=0;k2<dgs.length;k2++){var dg=dgs[k2];
    dg.setAttribute('transform','translate('+dg.getAttribute('data-x')+' '+dg.getAttribute('data-y')+') scale('+ds+')');}
}
window.addEventListener('resize',fitText);
document.addEventListener('fullscreenchange',function(){setTimeout(fitText,80);});
function zoomAt(f,cx,cy){var ns=Math.min(12,Math.max(1,s*f));if(ns===s)return;var k=ns/s;tx=cx-(cx-tx)*k;ty=cy-(cy-ty)*k;s=ns;apply();}
svg.addEventListener('wheel',function(e){e.preventDefault();var r=svg.getBoundingClientRect();
var cx=(e.clientX-r.left)/r.width*W,cy=(e.clientY-r.top)/r.height*H;
zoomAt(e.deltaY<0?1.25:0.8,cx,cy);},{passive:false});
svg.style.touchAction='none';
var pts={},lastD=0,lastMid=null;
svg.addEventListener('pointerdown',function(e){pts[e.pointerId]=[e.clientX,e.clientY];
try{svg.setPointerCapture(e.pointerId);}catch(x){}});
svg.addEventListener('pointermove',function(e){
if(!(e.pointerId in pts))return;
var ids=Object.keys(pts),r=svg.getBoundingClientRect(),k=W/r.width;
if(ids.length===1){var p=pts[e.pointerId];
tx+=(e.clientX-p[0])*k;ty+=(e.clientY-p[1])*k;pts[e.pointerId]=[e.clientX,e.clientY];apply();}
else if(ids.length>=2){pts[e.pointerId]=[e.clientX,e.clientY];
var a=pts[ids[0]],b=pts[ids[1]];
var d=Math.hypot(a[0]-b[0],a[1]-b[1]),mid=[(a[0]+b[0])/2,(a[1]+b[1])/2];
if(lastD){zoomAt(d/lastD,(mid[0]-r.left)/r.width*W,(mid[1]-r.top)/r.height*H);
tx+=(mid[0]-lastMid[0])*k;ty+=(mid[1]-lastMid[1])*k;apply();}
lastD=d;lastMid=mid;}});
function up(e){delete pts[e.pointerId];lastD=0;lastMid=null;}
svg.addEventListener('pointerup',up);svg.addEventListener('pointercancel',up);
document.getElementById('zin').onclick=function(){zoomAt(1.4,W/2,H/2)};
document.getElementById('zout').onclick=function(){zoomAt(1/1.4,W/2,H/2)};
document.getElementById('zreset').onclick=function(){s=1;tx=0;ty=0;apply();};
var box=document.getElementById('mapbox');
document.getElementById('zfull').onclick=function(){
if(document.fullscreenElement){document.exitFullscreen();}
else if(box.requestFullscreen){box.requestFullscreen();}};
apply();
})();
"""


def _map_section(events: list) -> str:
    """events: [{rank,title,topic,place,lat,lon}] -> 可缩放平移的地图 SVG + 引线标签"""
    dots, labels = [], []
    for i, ev in enumerate(events):
        x, y = _project(ev["lon"], ev["lat"])
        x = max(14, min(MAP_W - 14, x))
        y = max(14, min(MAP_H - 14, y))
        color = TOPIC_COLOR.get(ev.get("topic", "其他"), "#7f8fa6")
        lx = max(70, min(MAP_W - 70, x + (36 if i % 3 else -36)))
        ly = max(16, y - 26) if i % 2 == 0 else min(MAP_H - 10, y + 26)
        dots.append(
            f'<g class="dg" data-x="{x}" data-y="{y}">'
            f'<circle r="4.5" fill="{color}" stroke="#fff" stroke-width="1" class="pt"/>'
            f'<circle r="10" fill="{color}" opacity="0.25" class="ring"/></g>')
        labels.append(
            f'<line x1="{x}" y1="{y}" x2="{lx}" y2="{ly - 7}" stroke="{color}" stroke-width="0.8" opacity="0.7" class="lead"/>'
            f'<text x="{lx}" y="{ly}" text-anchor="middle" class="lbl" fill="{color}">#{ev["rank"]} {_esc(ev["title"][:11])}</text>')
    return (f'<div class="mapwrap" id="mapbox">'
            f'<svg id="map" viewBox="0 0 {MAP_W} {MAP_H}" preserveAspectRatio="xMidYMid meet">'
            f'<g id="world">{_world_paths()}<g>{"".join(dots)}{"".join(labels)}</g></g></svg>'
            f'<div class="mapctl"><button id="zin" title="放大">＋</button>'
            f'<button id="zout" title="缩小">－</button>'
            f'<button id="zreset" title="复位">⟲</button>'
            f'<button id="zfull" title="全屏">⛶</button></div></div>'
            f'<script>{MAP_JS}</script>')


def generate_board(s: Settings, glm=None) -> str:
    dates = list_reports()
    today = load_report(dates[0]) if dates else {}
    stories = load_stories()["stories"]
    open_st = [x for x in stories if x["status"] == "open"]
    closed_st = [x for x in stories if x["status"] == "closed"][-8:]
    items = today.get("items", [])
    for it in items:
        it["_date"] = today.get("date", "")

    geo_cache = _geo_fallback(items, glm)
    events = []
    for it in items:
        loc = it.get("location") or {}
        if not loc.get("lat"):
            loc = geo_cache.get(f"{it.get('_date')}#{it.get('rank')}", {})
        if isinstance(loc.get("lat"), (int, float)) and isinstance(loc.get("lon"), (int, float)):
            events.append({"rank": it.get("rank", 0), "title": it.get("title", ""),
                           "topic": it.get("topic", "其他"), "place": loc.get("place", ""),
                           "lat": loc["lat"], "lon": loc["lon"]})
    map_html = _map_section(events) if events else '<div class="sub">今日事件暂无定位</div>'

    cards = []
    for it in items:
        color = TOPIC_COLOR.get(it.get("topic", "其他"), "#7f8fa6")
        place = (it.get("location") or {}).get("place") or geo_cache.get(
            f"{it.get('_date')}#{it.get('rank')}", {}).get("place", "")
        ev = "".join(f'<li>&#128269; {_esc(e)}</li>' for e in (it.get("evidence") or [])[:3])
        cards.append(f"""
        <div class="card" style="border-top:3px solid {color}">
          <div class="meta"><span class="topic" style="background:{color}22;color:{color}">{_esc(it.get('topic'))}</span> #{it.get('rank', 0)}<span class="place">{_esc(('📍' + place) if place else '')}</span></div>
          <div class="title">{_esc(it.get('title'))}</div>
          <div class="one">{_esc(it.get('one_liner'))}</div>
          <details><summary>展开 {len((it.get('detail') or ''))} 字报道与证据</summary>
            <div class="detail">{_esc(it.get('detail'))}</div><ul>{ev}</ul></details>
        </div>""")

    def story_card(st, closed=False):
        badge = ("&#9989; 已完结" if closed else "&#128308; 追踪中")
        style = "opacity:.55" if closed else ""
        days = 0
        try:
            days = (date_cls.today() - date_cls.fromisoformat(st["created"])).days + 1
        except Exception:
            pass
        items_html = "".join(
            f'<div class="ev"><span class="evdate">{_esc(i["date"][5:])}</span> {_esc(i["title"])}</div>'
            for i in st.get("items", []))
        reason = f'<div class="close">&#128216; 结局：{_esc(st.get("close_reason"))}</div>' if closed else ""
        return (f'<div class="story" style="{style}"><div class="sthead">'
                f'<span class="badge {"bclose" if closed else "bopen"}">{badge}</span>'
                f'<span class="sttitle">{_esc(st["title"])}</span>'
                f'<span class="days">D{days} · {len(st.get("items", []))} 篇</span></div>'
                f'{reason}{items_html}</div>')

    stories_html = "".join(story_card(x) for x in open_st) or '<div class="story">暂无追踪中的大事件</div>'
    closed_html = "".join(story_card(x, True) for x in closed_st)

    n_issues = len(dates)
    all_items = []
    for d in dates[:30]:
        r = load_report(d)
        if r:
            all_items += r.get("items", [])
    topics = Counter(it.get("topic", "其他") for it in all_items)
    max_t = max(topics.values()) if topics else 1
    bars = "".join(
        f'<div class="bar-row"><span class="bar-label">{_esc(t)}</span>'
        f'<div class="bar"><div class="bar-fill" style="width:{c * 100 // max_t}%;background:{TOPIC_COLOR.get(t, "#7f8fa6")}"></div></div>'
        f'<span class="bar-num">{c}</span></div>' for t, c in topics.most_common(7))
    stats_html = (f'<div class="stats"><div><span class="bignum">{n_issues}</span>累计期数</div>'
                  f'<div><span class="bignum">{len(all_items)}</span>累计条目</div>'
                  f'<div><span class="bignum">{len(open_st)}</span>追踪中</div></div>'
                  f'<div class="bars">{bars}</div>')

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>科技圈日报 · 世界监控</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0b0f1a;color:#dfe6f3;font-family:'Microsoft YaHei',sans-serif;padding:16px;max-width:1400px;margin:0 auto}}
header{{display:flex;justify-content:space-between;align-items:baseline;padding:4px 4px 8px}}
h1{{font-size:22px;letter-spacing:2px}} .live{{color:#2ed573;font-size:12px}}
.live::before{{content:'';display:inline-block;width:8px;height:8px;border-radius:50%;background:#2ed573;margin-right:6px;animation:p 2s infinite}}
@keyframes p{{50%{{opacity:.3}}}}
.sub{{color:#7f8fa6;font-size:13px;margin:-2px 4px 10px}}
h2{{font-size:13px;color:#8892b0;letter-spacing:3px;margin:14px 4px 8px;border-left:3px solid #1E6FFF;padding-left:8px}}
/* 指挥中心布局：地图主屏 + 右侧栏（PC/横屏） */
.layout{{display:flex;gap:14px;align-items:stretch}}
.main{{flex:2.4 1 640px;min-width:0}}
.side{{flex:1 1 300px;min-width:260px;display:flex;flex-direction:column;gap:10px}}
.sidepanel{{background:#0d1526;border:1px solid #1f2937;border-radius:12px;padding:10px 12px}}
.side h2{{margin:2px 0 6px}}
.mapwrap{{background:#0d1526;border:1px solid #1f2937;border-radius:12px;padding:6px;height:100%;position:relative}}
.mapwrap svg{{width:100%;height:auto;max-height:80vh;display:block;cursor:grab}}
.mapwrap:fullscreen{{display:flex;align-items:center;justify-content:center}}
.mapwrap:fullscreen svg{{max-height:96vh}}
.mapctl{{position:absolute;right:12px;top:10px;display:flex;flex-direction:column;gap:5px}}
.mapctl button{{width:32px;height:32px;border-radius:8px;border:1px solid #2b3a55;background:#111827cc;
color:#dfe6f3;font-size:15px;cursor:pointer}}
.mapctl button:hover{{background:#1E6FFF33}}
.pt{{animation:p 2s infinite}} .ring{{animation:ring 2.4s infinite}}
@keyframes ring{{0%{{r:5;opacity:.5}}100%{{r:16;opacity:0}}}}
.lbl{{font-size:11px;font-weight:700;paint-order:stroke;stroke:#0b0f1a;stroke-width:3px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}}
.card{{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:14px}}
.meta{{font-size:11px;color:#8892b0;display:flex;gap:8px;align-items:center}}
.topic{{padding:1px 8px;border-radius:8px;font-size:11px}} .place{{margin-left:auto;color:#57606f}}
.title{{font-size:15px;font-weight:700;margin:8px 0 4px}}
.one{{font-size:12px;color:#a5b1c7}}
details{{margin-top:8px;font-size:12px}} summary{{cursor:pointer;color:#1E6FFF}}
.detail{{line-height:1.8;color:#cbd5e1;margin-top:8px;white-space:pre-wrap}}
ul{{list-style:none;margin-top:6px;color:#7f8fa6}}
.story{{background:#111827;border:1px solid #1f2937;border-left:3px solid #2ed573;border-radius:10px;padding:10px 12px;margin-bottom:8px}}
.sthead{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.badge{{font-size:11px;padding:2px 8px;border-radius:8px}}
.bopen{{background:#2ed57322;color:#2ed573}} .bclose{{background:#8892b022;color:#8892b0}}
.sttitle{{font-weight:700;font-size:13px}} .days{{color:#57606f;font-size:11px;margin-left:auto}}
.ev{{font-size:12px;color:#a5b1c7;padding:4px 0;border-bottom:1px dashed #1f2937}}
.evdate{{color:#1E6FFF;margin-right:6px;font-family:Consolas}}
.close{{font-size:12px;color:#f6b93b;padding:5px 0}}
.stats{{display:flex;gap:18px;color:#8892b0;font-size:11px;padding:2px 0}}
.bignum{{color:#dfe6f3;font-size:20px;font-weight:700;display:block}}
.bars{{margin-top:6px}}
.bar-row{{display:flex;align-items:center;gap:8px;font-size:11px;color:#8892b0;padding:3px 0}}
.bar-label{{width:52px;text-align:right}} .bar{{flex:1;background:#1f2937;border-radius:4px;height:8px}}
.bar-fill{{height:8px;border-radius:4px}} .bar-num{{width:28px}}
footer{{text-align:center;color:#57606f;font-size:11px;padding:16px 0}}
/* 手机竖屏：退化单列 */
@media (max-width: 760px){{
  .layout{{flex-direction:column}}
  .side{{min-width:0}}
  .mapwrap svg{{max-height:56vh}}
}}
/* 手机横屏：紧凑双栏、控高 */
@media (orientation:landscape) and (max-height: 560px){{
  body{{padding:10px}}
  h1{{font-size:17px}}
  .layout{{gap:10px}}
  .mapwrap svg{{max-height:72vh}}
  .grid{{grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px}}
  .card{{padding:10px}}
}}
</style></head><body>
<header><h1>&#127760; 科技圈日报 · WORLD MONITOR</h1><span class="live">LIVE · {_esc(today.get('date', '--'))}</span></header>
<div class="sub">{_esc(today.get('headline', ''))} · 今日 {len(events)} 个事件落点</div>
<div class="layout">
  <div class="main"><h2>世界地图 · 事件分布</h2>{map_html}</div>
  <div class="side">
    <div class="sidepanel"><h2>追踪中的大事件</h2>{stories_html}</div>
    {f'<div class="sidepanel"><h2>已完结</h2>{closed_html}</div>' if closed_html else ''}
    <div class="sidepanel"><h2>统计（近 {min(n_issues, 30)} 期）</h2>{stats_html}</div>
  </div>
</div>
<h2>今日刊（{_esc(today.get('date', ''))} · {len(items)} 条）</h2>
<div class="grid">{''.join(cards) or '<div class="card">今日暂无</div>'}</div>
<footer>每日 06:00 自动更新 · 密文分发团队码解密 · 科技圈日报引擎</footer>
</body></html>"""


def write_board(s: Settings, glm=None) -> str:
    board = generate_board(s, glm)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "board.html").write_text(board, encoding="utf-8")
    log.info("世界监控看板已生成（%d 字节）", len(board))
    return board


if __name__ == "__main__":
    print(write_board(Settings.load())[:500])
