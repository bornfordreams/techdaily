"""文本信息源：Hacker News / IT之家 / 36氪 / GitHub Trending。

文本源与 B站视频同池竞争（同一套精筛与成刊去重），但分析阶段不下载不看片——
标题+摘要直接成文（detail 200-350 字，只依据给定文本，不许编造）。
每个源独立容错：单源挂了不影响其他源与主流程。
（X/Twitter 无免费接口且 nitter 公共实例已全部失效，暂不接入；
  X 上的科技圈热点通常会同步出现在 HN/GitHub 中。）
"""
from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from .bilibili import Video

log = logging.getLogger("techdaily.sources")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _mk(bvid: str, title: str, desc: str, url: str, source: str, score: int,
        pubdate: int) -> Video:
    return Video(bvid=f"{source}-{bvid}"[:60], title=re.sub(r"\s+", " ", title)[:80],
                 desc=desc[:400], owner=source, duration_sec=600, pubdate=pubdate,
                 play=score, ext_url=url, source="text")


def fetch_hackernews(quota: int = 12) -> list[Video]:
    """HN 首页高分条目（Firebase 官方 API，免鉴权）。"""
    s = requests.Session(); s.headers.update(UA)
    ids = s.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15).json()[:40]
    out = []
    for i in ids[:25]:
        try:
            it = s.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json", timeout=10).json()
            if not it or it.get("type") != "story" or "title" not in it:
                continue
            score = int(it.get("score") or 0)
            if score < 150:
                continue
            out.append(_mk(str(i), it["title"],
                           (it.get("text") or "")[:300] + f" | HN热度{score} 评论{it.get('descendants', 0)}",
                           it.get("url") or f"https://news.ycombinator.com/item?id={i}",
                           "HackerNews", score, int(it.get("time") or time.time())))
            if len(out) >= quota:
                break
            time.sleep(0.1)
        except Exception:
            continue
    log.info("HackerNews：%d 条", len(out))
    return out


def _rss(url: str, source: str, quota: int, host: str) -> list[Video]:
    s = requests.Session(); s.headers.update(UA)
    r = s.get(url, timeout=15)
    # 36氪等源的 XML 常带非法控制字符，先清洗再解析
    clean = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b" ", r.content)
    root = ET.fromstring(clean)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", "", item.findtext("description") or "")[:300]
        pub = item.findtext("pubDate") or ""
        ts = int(time.time())
        try:
            ts = int(datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z")
                     .astimezone(timezone.utc).timestamp())
        except Exception:
            pass
        if title and link:
            out.append(_mk(link[-30:].replace("/", "-"), title, desc, link, source, 100000, ts))
        if len(out) >= quota:
            break
    log.info("%s：%d 条", source, len(out))
    return out


def fetch_github_trending(quota: int = 8) -> list[Video]:
    """近 48h 高星新仓库（GitHub Search API，免鉴权限流 10次/分，一天一次无压力）。"""
    since = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%d")
    s = requests.Session(); s.headers.update(UA)
    r = s.get("https://api.github.com/search/repositories",
              params={"q": f"created:>{since} stars:>=80", "sort": "stars",
                      "order": "desc", "per_page": quota}, timeout=20)
    out = []
    for it in (r.json().get("items") or []):
        desc = (it.get("description") or "")[:300]
        lang = it.get("language") or "-"
        out.append(_mk(str(it["id"]), f"GitHub 热门：{it['full_name']}（{it['stargazers_count']}★）",
                       f"{desc} | 语言{lang} 星标{it['stargazers_count']}",
                       it["html_url"], "GitHub", min(it["stargazers_count"], 100000),
                       int(time.time())))
    log.info("GitHub Trending：%d 条", len(out))
    return out


def collect_text_sources(s) -> list[Video]:
    """全部文本源聚合，单源失败不影响整体。"""
    out: list[Video] = []
    if s.src_hackernews:
        try:
            out += fetch_hackernews()
        except Exception as e:
            log.warning("HackerNews 拉取失败：%s", e)
    if s.src_ithome:
        try:
            out += _rss("https://www.ithome.com/rss/", "IT之家", 10, "ithome")
        except Exception as e:
            log.warning("IT之家拉取失败：%s", e)
    if s.src_36kr:
        try:
            out += _rss("https://36kr.com/feed", "36氪", 8, "36kr")
        except Exception as e:
            log.warning("36氪拉取失败：%s", e)
    if s.src_github:
        try:
            out += fetch_github_trending()
        except Exception as e:
            log.warning("GitHub拉取失败：%s", e)
    return out
