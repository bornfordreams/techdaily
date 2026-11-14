"""两段筛选：规则粗筛（零成本）+ GLM 批量精筛（小积分）。"""
from __future__ import annotations

import json
import logging

from .bilibili import Video
from .config import Settings
from .glm import GLMClient

log = logging.getLogger("techdaily.prefilter")

# 全站热门虽能捕捉大爆点，但娱乐内容播放量碾压科技区，
# 不限流会把搜索源的科技视频全部挤出候选，故给热门源设席位上限
POPULAR_QUOTA = 8


def _fans_tier(fans: int) -> int:
    """UP主粉丝档位：信源质量优先（100万粉=3档 > 30万=2 > 5万=1 > 0）"""
    if fans >= 1_000_000:
        return 3
    if fans >= 300_000:
        return 2
    if fans >= 50_000:
        return 1
    return 0


def rule_filter(videos: list[Video], s: Settings, state: dict) -> list[Video]:
    seen: dict = state.get("seen", {})
    passed = []
    for v in videos:
        if v.bvid in seen:
            continue
        text = v.text.lower()
        if any(w in text for w in s.block_words):
            continue
        if s.require_words_any and not any(w.lower() in text for w in s.require_words_any):
            continue
        if v.source == "text":
            passed.append(v)  # 文本源无时长/播放概念，只走屏蔽词
            continue
        if not (s.min_duration <= v.duration_sec <= s.max_duration):
            continue
        if v.play < s.min_play:
            continue
        passed.append(v)
    # 排序：UP主粉丝档位优先，其次播放量（大V的新视频早期播放低但价值高）
    order = lambda x: (-_fans_tier(x.fans), -x.play)
    popular = sorted((v for v in passed if v.source == "popular"), key=order)[:POPULAR_QUOTA]
    search = sorted((v for v in passed if v.source != "popular"), key=order)
    kept = sorted(popular + search, key=order)[: s.max_candidates]
    n_pop = sum(1 for v in kept if v.source == "popular")
    n_big = sum(1 for v in kept if v.fans >= 300_000)
    log.info("规则粗筛：%d -> %d 条（热门源 %d，30万粉以上大V %d 条）",
             len(videos), len(kept), n_pop, n_big)
    return kept


def glm_refine(videos: list[Video], glm: GLMClient) -> list[Video]:
    """GLM 按列表批量判定是否科技相关，返回被保留的视频（保持原顺序）。"""
    if not videos:
        return []
    system = (
        "你是科技媒体的内容编辑，为《科技圈日报》挑选素材。"
        "判断标准：AI/大模型、芯片半导体、消费电子、互联网行业动态、航天、新能源、"
        "机器人、重大发布会、开源动态、科技政策、科技人物/公司动态等属于科技内容；"
        "以下一律不属于，无论播放多高UP主多大：一切游戏内容（我的世界MC、红石机械、"
        "游戏模组、游戏实况、速通、通关、游戏攻略/资讯，**包括游戏产业新闻、游戏公司动态、"
        "新游发布、游戏财报**，哪怕看起来很'工程'很'科技'）、纯娱乐、生活vlog、情感、带货广告。"
        "原则：宁多勿漏——只要与科技沾边就保留，边缘内容也留下"
        "（后续视频理解层还有 is_tech 二次把关，漏了就永远丢了）；目标保留 10 条以上。"
        '只输出JSON：{"keep":[{"id":"BV号","reason":"一句话"}]}，不要输出被拒项。'
    )
    keep_ids: set[str] = set()
    batch = 10
    for i in range(0, len(videos), batch):
        chunk = videos[i:i + batch]
        lines = [{"id": v.bvid, "标题": v.title, "简介": v.desc[:120],
                  "UP主": v.owner, "播放": v.play, "标签": v.tags[:6]} for v in chunk]
        try:
            data = glm.chat_json(system, json.dumps(lines, ensure_ascii=False), max_tokens=1200)
            for item in data.get("keep", []):
                if isinstance(item, dict) and item.get("id"):
                    keep_ids.add(str(item["id"]))
        except Exception as e:
            log.warning("GLM 精筛失败（本批按保留处理以防漏报）: %s", e)
            keep_ids.update(v.bvid for v in chunk)  # 失败降级为全保留，宁可多看不可漏
    kept = [v for v in videos if v.bvid in keep_ids]
    if not kept and videos:
        # 防御精筛失灵（响应异常/过严）：降级放行搜索源前3条（更可能是真科技），
        # 视频理解层还有 is_tech 二次判定兜底，不会让非科技内容混入成刊
        log.warning("GLM 精筛全灭（%d 条候选），降级放行搜索源前 3 条交视频理解把关", len(videos))
        kept = [v for v in videos if v.source != "popular"][:3] or videos[:3]
    log.info("GLM 精筛：%d -> %d 条", len(videos), len(kept))
    return kept
