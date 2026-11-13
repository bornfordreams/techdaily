"""B站采集：分区热门 + 关键词搜索（wbi 签名），字幕尽力而为（无 Cookie 时大概率拿不到，自动跳过）。"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests

from .config import Settings

log = logging.getLogger("techdaily.bilibili")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# B站 wbi 混淆密钥置换表（公开算法）
MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
             27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
             37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
             22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]


@dataclass
class Video:
    bvid: str
    title: str
    desc: str = ""
    owner: str = ""
    duration_sec: int = 0
    pubdate: int = 0
    play: int = 0
    tags: list = field(default_factory=list)
    source: str = "search"  # popular=全站热门 / search=关键词搜索
    mid: str = ""           # UP主 uid（用于查粉丝数）
    fans: int = 0           # UP主粉丝数（信源质量权重）

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"

    @property
    def text(self) -> str:
        """供文本筛选用的一切文字。"""
        return " ".join([self.title, self.desc, self.owner, " ".join(self.tags)])

    @staticmethod
    def _clean(raw: str) -> str:
        return re.sub(r"<[^>]+>", "", raw or "").strip()

    @staticmethod
    def _dur_to_sec(d) -> int:
        if isinstance(d, int):
            return d
        if not d:
            return 0
        parts = str(d).split(":")
        try:
            sec = 0
            for p in parts:
                sec = sec * 60 + int(p)
            return sec
        except ValueError:
            return 0

    @classmethod
    def from_search(cls, item: dict) -> "Video":
        return cls(
            bvid=item.get("bvid", ""),
            title=cls._clean(item.get("title", "")),
            desc=cls._clean(item.get("description", ""))[:300],
            owner=item.get("author", ""),
            duration_sec=cls._dur_to_sec(item.get("duration")),
            pubdate=int(item.get("pubdate") or 0),
            play=int(item.get("play") or 0),
            tags=[t for t in (item.get("tag") or "").split(",") if t],
            mid=str(item.get("mid") or ""),
        )

    @classmethod
    def from_rank(cls, item: dict) -> "Video":
        stat = item.get("stat") or {}
        return cls(
            bvid=item.get("bvid", ""),
            title=cls._clean(item.get("title", "")),
            desc=cls._clean(item.get("desc") or item.get("description") or "")[:300],
            owner=(item.get("owner") or {}).get("name", ""),
            duration_sec=cls._dur_to_sec(item.get("duration")),
            pubdate=int(item.get("pubdate") or 0),
            play=int(stat.get("view") or 0),
            source="popular",
            mid=str((item.get("owner") or {}).get("mid") or ""),
        )


class Bilibili:
    def __init__(self, s: Settings):
        self.s = s
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        if s.bilibili_cookie:
            self.sess.headers["Cookie"] = s.bilibili_cookie
        self._img_key = ""
        self._sub_key = ""
        self._bootstrapped = False
        self._fans_cache: dict[str, int] = {}

    # ---------- 基础 ----------
    def _bootstrap(self) -> None:
        """匿名访问也要 buvid3，否则搜索接口风控概率高。"""
        if self._bootstrapped or self.s.bilibili_cookie:
            self._bootstrapped = True
            return
        try:
            self.sess.get("https://www.bilibili.com/", timeout=10)
        except requests.RequestException as e:
            log.debug("buvid3 bootstrap 失败（继续尝试）: %s", e)
        self._bootstrapped = True

    def _get(self, url: str, params: dict | None = None) -> dict | None:
        try:
            r = self.sess.get(url, params=params, timeout=15)
            j = r.json()
        except Exception as e:
            log.warning("请求失败 %s: %s", url, e)
            return None
        if j.get("code") != 0:
            log.warning("接口错误 %s: code=%s msg=%s", url, j.get("code"), j.get("message"))
            return None
        return j.get("data")

    def _wbi_keys(self) -> bool:
        """nav 即使未登录（code=-101）也会返回 wbi_img，不能按 code 判断成败。"""
        if self._img_key and self._sub_key:
            return True
        try:
            r = self.sess.get("https://api.bilibili.com/x/web-interface/nav", timeout=15)
            wbi = ((r.json().get("data") or {}).get("wbi_img")) or {}
            self._img_key = wbi.get("img_url", "").rsplit("/", 1)[1].split(".")[0]
            self._sub_key = wbi.get("sub_url", "").rsplit("/", 1)[1].split(".")[0]
        except Exception as e:
            log.warning("wbi key 获取失败：%s", e)
        return bool(self._img_key and self._sub_key)

    def _wbi_sign(self, params: dict) -> dict:
        if not self._wbi_keys():
            raise RuntimeError("wbi 密钥不可用，跳过搜索")
        mixin = "".join((self._img_key + self._sub_key)[i] for i in MIXIN_TAB)[:32]
        p = {k: str(v).replace("!", "").replace("'", "").replace("(", "")
                .replace(")", "").replace("*", "") for k, v in params.items()}
        p["wts"] = int(time.time())
        qs = urlencode(sorted(p.items()))
        p["w_rid"] = hashlib.md5((qs + mixin).encode()).hexdigest()
        return p

    # ---------- 采集 ----------
    def _fresh(self, v: Video) -> bool:
        cutoff = datetime.now() - timedelta(days=self.s.days_window)
        return v.pubdate >= int(cutoff.timestamp())

    def collect(self) -> list[Video]:
        self._bootstrap()
        out: dict[str, Video] = {}
        # 全站综合热门（匿名可用）：捕捉跨圈大爆点，非科技内容由后续筛选兜住
        for pn in range(1, self.s.popular_pages + 1):
            data = self._get("https://api.bilibili.com/x/web-interface/popular",
                             {"ps": 20, "pn": pn})
            for item in (data or {}).get("list") or []:
                v = Video.from_rank(item)
                if v.bvid and v.bvid not in out:
                    out[v.bvid] = v
        log.info("全站热门采集：%d 条", len(out))
        # 关键词搜索（wbi 签名，匿名可用）
        found = 0
        try:
            for kw in self.s.search_keywords:
                for page in range(1, self.s.search_pages + 1):
                    data = self._get("https://api.bilibili.com/x/web-interface/wbi/search/type",
                                     self._wbi_sign({"search_type": "video", "keyword": kw,
                                                     "page": page, "platform": "pc"}))
                    results = (data or {}).get("result") or []
                    found += len(results)
                    for item in results:
                        v = Video.from_search(item)
                        if v.bvid and v.bvid not in out:
                            out[v.bvid] = v
                time.sleep(1.2)  # 防风控，礼貌间隔
        except RuntimeError as e:
            log.error("搜索采集不可用（仅靠全站热门）：%s", e)
        log.info("关键词搜索采集：%d 条，合计去重 %d 条", found, len(out))
        fresh = [v for v in out.values() if self._fresh(v)]
        log.info("最近 %d 天发布：%d 条", self.s.days_window, len(fresh))
        self._enrich_fans(fresh)
        return fresh

    def _enrich_fans(self, fresh: list[Video]) -> None:
        """给播放量前 60 的候选补齐 UP主粉丝数（信源质量优先的排序依据）。"""
        need: dict[str, bool] = {}
        for v in sorted(fresh, key=lambda x: x.play, reverse=True)[:60]:
            if v.mid and v.mid not in self._fans_cache:
                need[v.mid] = True
        for mid in need:
            data = self._get("https://api.bilibili.com/x/relation/stat", {"vmid": mid})
            if data:
                self._fans_cache[mid] = int(data.get("follower") or 0)
            time.sleep(0.15)  # 礼貌限速
        for v in fresh:
            v.fans = self._fans_cache.get(v.mid, 0)
        big = sum(1 for v in fresh if v.fans >= 300_000)
        log.info("粉丝数已补齐（涉及 %d 位UP主，其中 30万粉以上 %d 条）", len(need), big)

    # ---------- 字幕（尽力而为） ----------
    def fetch_subtitle(self, bvid: str) -> str | None:
        try:
            data = self._get("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
            if not data:
                return None
            aid, cid = data.get("aid"), data.get("cid")
            data = self._get("https://api.bilibili.com/x/player/wbi/v2",
                             self._wbi_sign({"aid": aid, "cid": cid}))
            subs = ((data or {}).get("subtitle") or {}).get("subtitles") or []
            if not subs:
                return None
            url = subs[0].get("subtitle_url", "")
            if url.startswith("//"):
                url = "https:" + url
            r = self.sess.get(url, timeout=15)
            body = r.json().get("body") or []
            text = " ".join(x.get("content", "") for x in body)
            return text or None
        except Exception as e:
            log.debug("字幕获取失败 %s: %s", bvid, e)
            return None
