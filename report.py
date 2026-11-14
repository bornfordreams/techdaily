"""成刊：合并去重 -> GLM 排序定稿 -> 日报 JSON + Markdown 落盘。"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from config import REPORT_DIR, Settings
from glm import GLMClient

log = logging.getLogger("techdaily.report")


def _recent_events(current_date: str, days: int = 7) -> list[str]:
    """最近 N 期已报道事件（标题+导语），供成刊时做事件级去重。"""
    events: list[str] = []
    dates = [d for d in list_reports() if d != current_date][:days]
    for d in dates:
        r = load_report(d)
        if not r:
            continue
        for it in r.get("items", []):
            t = (it.get("title") or "").strip()
            if t:
                events.append(f"{d}: {t}｜{(it.get('one_liner') or '')[:40]}")
    return events


def build_report(summaries: list[dict], s: Settings, glm: GLMClient,
                 date: str | None = None) -> dict:
    date = date or dt.date.today().isoformat()
    if not summaries:
        report = {"date": date, "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                  "headline": "今日无符合条件的科技爆点", "items": []}
        _save(report)
        return report

    system = (
        "你是《科技圈日报》主编。把候选条目合并成当日日报："
        "同一事件多个来源只保留信息最全的一条；按对科技行业的影响力排序；"
        f"输出最多 {s.max_items} 条。"
        "游戏内容（我的世界/MC/红石/模组/实况/攻略/游戏产业新闻/游戏公司动态）"
        "无论多火一律剔除，无任何例外。"
    )
    lines = [{"url": x["url"], "title": x.get("title", ""), "one_liner": x.get("one_liner", ""),
              "detail": x.get("detail", ""), "location": x.get("location"),
              "key_points": x.get("key_points", []), "key_data": x.get("key_data", []),
              "importance": x.get("importance", 5), "why": x.get("why", ""),
              "play": x.get("play", 0)} for x in summaries]
    past = _recent_events(date or dt.date.today().isoformat())
    past_block = ""
    if past:
        past_block = ("\n\n已报道过的事件（**禁止重复报道**，同一事件即使换了视频/来源也不行；"
                      "仅当出现实质性重大新进展才允许保留，且标题必须写明新进展本身）：\n"
                      + "\n".join(f"- {e}" for e in past))
    user = (
        "候选（JSON）：\n" + json.dumps(lines, ensure_ascii=False) + past_block +
        "\n\n输出JSON：{\"headline\":\"今日科技圈一句话总览(≤30字)\","
        "\"items\":[{\"rank\":1,\"title\":\"事件标题\",\"one_liner\":\"一句话摘要\","
        "\"detail\":\"详细报道：完整保留该事件候选中的 detail（同一事件多来源则合并细节润色成一篇），"
        "不得缩写不得编造\","
        "\"url\":\"必须原样使用候选中的url\",\"evidence\":[\"关键数据/型号/日期证据\"],"
        "\"topic\":\"AI/芯片/终端/互联网/航天/新能源/其他\"}]}\n"
        "evidence 只用候选中真实出现的信息，不得编造。"
    )
    data = {}
    try:
        # 12条长文透传的输出体量约 6000-9000 tokens，2500 会截断 JSON 导致降级
        data = glm.chat_json(system, user, max_tokens=8000)
        items = data.get("items", [])
    except Exception as e:
        log.warning("成刊 GLM 调用失败，按 importance 降级排序: %s", e)
        items = [{"rank": i + 1, "title": x.get("title", ""), "one_liner": x.get("one_liner", ""),
                  "detail": x.get("detail", ""),
                  "url": x["url"], "evidence": x.get("key_data", []), "topic": "其他"}
                 for i, x in enumerate(sorted(summaries, key=lambda y: y.get("importance", 0),
                                               reverse=True))][: s.max_items]

    # 安全校验：url 必须来自候选，防模型幻觉链接
    valid_urls = {x["url"] for x in summaries}
    by_url = {x["url"]: x for x in summaries}
    cleaned = []
    for i, it in enumerate(items):
        url = it.get("url")
        if url not in valid_urls:
            continue
        merged = dict(it)
        merged["rank"] = len(cleaned) + 1
        merged.setdefault("evidence", by_url[url].get("key_data", []))
        if not merged.get("detail"):
            merged["detail"] = by_url[url].get("detail", "")
        if not merged.get("location"):
            merged["location"] = by_url[url].get("location") or {}
        cleaned.append(merged)
    report = {"date": date, "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
              "headline": data.get("headline", "科技圈日报") if isinstance(data, dict) else "科技圈日报",
              "items": cleaned[: s.max_items]}
    _save(report)
    log.info("成刊完成：%d 条", len(report["items"]))
    return report


def render_md(report: dict) -> str:
    lines = [f"# 科技圈日报 {report['date']}", "",
             f"> {report.get('headline', '')}", ""]
    if not report.get("items"):
        lines.append("今日无符合条件的科技爆点。")
    for it in report["items"]:
        lines.append(f"## {it['rank']}. {it['title']}  `{it.get('topic', '')}`")
        lines.append(f"**{it.get('one_liner', '')}**")
        lines.append("")
        detail = (it.get("detail") or "").strip()
        if detail:
            lines.append(detail)
            lines.append("")
        lines.append(f"来源：{it.get('url', '')}")
        for ev in it.get("evidence", []) or ["（无具体数据）"]:
            lines.append(f"- 🔍 {ev}")
        lines.append("")
    return "\n".join(lines)


def review_report(report: dict, s: Settings, glm: GLMClient) -> dict:
    """推送前终审：撤掉游戏/重复/非科技/编造嫌疑条目，重排序后重存。
    终审失败时放行原刊（宁推送不卡死），但记录告警。"""
    if not report.get("items"):
        return report
    past = _recent_events(report["date"])
    user = (
        "本期日报（JSON）：\n" + json.dumps(report["items"], ensure_ascii=False) +
        ("\n\n近7期已报道事件：\n" + "\n".join(past) if past else "") +
        "\n\n逐条审查，输出JSON：{\"pass\":true/false,\"remove\":[要撤掉的rank序号],\"notes\":[\"每条一句话说明\"]}\n"
        "撤稿标准（任一命中即撤）：\n"
        "1. 任何游戏内容：我的世界MC/红石/模组/实况/攻略/游戏资讯/游戏产业新闻/游戏公司动态，无例外\n"
        "2. 与已报道事件重复且无实质性新进展\n"
        "3. 非科技：娱乐/情感/生活/纯时政，与科技行业无关联\n"
        "4. 编造嫌疑：detail/evidence 中的具体数字、机构、日期明显不像视频里能有的\n"
        "拿不准的保留（pass 指整体合格；remove 留空数组表示全过）。"
    )
    try:
        verdict = glm.chat_json("你是《科技圈日报》推送前的终审主编，标准从严但不高估自己的确定性。", user, max_tokens=1500)
    except Exception as e:
        log.warning("终审失败，放行原刊：%s", e)
        return report
    remove = {int(r) for r in (verdict.get("remove") or []) if str(r).isdigit()}
    notes = verdict.get("notes") or []
    for n in notes:
        log.info("终审意见：%s", n)
    if not remove:
        log.info("终审通过（%d 条）", len(report["items"]))
        return report
    kept = [it for it in report["items"] if it.get("rank") not in remove]
    for i, it in enumerate(kept, 1):
        it["rank"] = i
    report["items"] = kept
    log.info("终审撤稿 %d 条，余 %d 条", len(remove), len(kept))
    _save(report)
    return report


def _save(report: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{report['date']}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORT_DIR / f"{report['date']}.md").write_text(render_md(report), encoding="utf-8")


def load_report(date: str) -> dict | None:
    p: Path = REPORT_DIR / f"{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_reports() -> list[str]:
    if not REPORT_DIR.exists():
        return []
    return sorted((p.stem for p in REPORT_DIR.glob("*.json")), reverse=True)
