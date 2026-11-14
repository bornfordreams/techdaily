"""大事件追踪：把跨天的重大新闻串成"故事线"，每天挂接新进展，直到结局。

规则：
- 只有重大事件才开新故事线（成刊 rank 前三，且 GLM 认定影响行业格局）
- 每天出刊后：今日条目与进行中故事线做一次 GLM 匹配（同一事件=挂接）
- 结局判定：GLM 明确认定完结，或连续 4 天无任何进展（静默即终局）
状态存 data/stories.json，看板与"问AI结合看"都以它为上下文。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date as date_cls

from .config import DATA_DIR, Settings
from .glm import GLMClient

log = logging.getLogger("techdaily.tracker")

STORIES_FILE = DATA_DIR / "stories.json"
SILENCE_CLOSE_DAYS = 4


def load_stories() -> dict:
    if STORIES_FILE.exists():
        try:
            return json.loads(STORIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("stories.json 损坏，重建")
    return {"stories": []}


def _save(stories: dict) -> None:
    STORIES_FILE.write_text(json.dumps(stories, ensure_ascii=False, indent=2), encoding="utf-8")


def update_stories(report: dict, s: Settings, glm: GLMClient) -> dict:
    stories = load_stories()
    items = report.get("items", [])
    if not items:
        return stories
    open_stories = [st for st in stories["stories"] if st["status"] == "open"]

    system = (
        "你是新闻编辑部的追踪主编。判断今日条目与'进行中的大事件故事线'的关系。"
        "只有同一事件的实质性后续进展才能挂接到已有故事线；"
        "今日出现影响行业格局的全新重大事件才开新线（普通资讯不值得追踪）；"
        "某故事线已出现明确结局（宣判/落地/官方确认结束）时标记完结。"
        '只输出JSON：{"match":[{"rank":1,"story_id":"..."}],'
        '"new":[{"rank":2}],'
        '"close":[{"story_id":"...","reason":"一句话结局"}]}，无内容则空数组。'
    )
    payload = {
        "今日条目": [{"rank": it["rank"], "title": it["title"], "one_liner": it.get("one_liner", "")}
                 for it in items],
        "进行中的故事线": [{"story_id": st["id"], "title": st["title"],
                     "最后进展": st["items"][-1]["title"] if st.get("items") else "",
                     "始于": st["created"]} for st in open_stories],
    }
    try:
        verdict = glm.chat_json(system, json.dumps(payload, ensure_ascii=False), max_tokens=1000)
    except Exception as e:
        log.warning("故事线匹配失败（跳过本日追踪）: %s", e)
        _silence_close(stories)
        _save(stories)
        return stories

    by_rank = {it["rank"]: it for it in items}
    closed_ids = set()
    for c in verdict.get("close", []):
        sid = c.get("story_id")
        for st in stories["stories"]:
            if st["id"] == sid and st["status"] == "open":
                st["status"] = "closed"
                st["close_reason"] = c.get("reason", "")
                closed_ids.add(sid)
                log.info("故事线完结：%s（%s）", st["title"], st["close_reason"])
    for m in verdict.get("match", []):
        it = by_rank.get(m.get("rank"))
        sid = m.get("story_id")
        st = next((x for x in stories["stories"] if x["id"] == sid and x["status"] == "open"), None)
        if it and st and report["date"] not in [i["date"] for i in st["items"]]:
            st["items"].append({"date": report["date"], "title": it["title"],
                                "one_liner": it.get("one_liner", ""), "url": it.get("url", "")})
            st["last_date"] = report["date"]
            log.info("故事线进展：%s <- %s", st["title"], it["title"][:30])
    for n in verdict.get("new", []):
        it = by_rank.get(n.get("rank"))
        if not it:
            continue
        stories["stories"].append({
            "id": f"st-{report['date'].replace('-', '')}-{it['rank']}",
            "title": it["title"], "status": "open",
            "created": report["date"], "last_date": report["date"],
            "items": [{"date": report["date"], "title": it["title"],
                       "one_liner": it.get("one_liner", ""), "url": it.get("url", "")}],
        })
        log.info("新故事线：%s", it["title"])

    _silence_close(stories)
    # 只保留最近 60 条故事线
    stories["stories"] = stories["stories"][-60:]
    _save(stories)
    return stories


def _silence_close(stories: dict) -> None:
    today = date_cls.today()
    for st in stories["stories"]:
        if st["status"] != "open":
            continue
        try:
            last = date_cls.fromisoformat(st.get("last_date") or st["created"])
        except ValueError:
            continue
        if (today - last).days >= SILENCE_CLOSE_DAYS:
            st["status"] = "closed"
            st["close_reason"] = f"连续{SILENCE_CLOSE_DAYS}天无新进展，按终局归档"
            log.info("故事线静默归档：%s", st["title"])


def find_story_by_item(item_date: str, url: str) -> dict | None:
    """问AI用：根据条目日期与url找到它所属的故事线（含全部历史进展）。"""
    for st in load_stories()["stories"]:
        for it in st.get("items", []):
            if it.get("date") == item_date and it.get("url") == url:
                return st
    return None
