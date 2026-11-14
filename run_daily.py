"""每日主流程：采集 -> 规则粗筛 -> GLM 精筛 -> 视频理解 -> 成刊。

由 Windows 任务计划每天 18:30（非高峰积分时段）以 pythonw 调用；
日志落在 logs/daily-YYYYMMDD.log。
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from techdaily.analyzer import analyze, purge_cache
    from techdaily.bilibili import Bilibili
    from techdaily.config import LOG_DIR, Settings, load_state, save_state
    from techdaily.glm import GLMClient
    from techdaily.prefilter import glm_refine, rule_filter
    from techdaily.report import build_report
except ImportError:
    print("错误：找不到 techdaily 包。请确认已【完整解压】服务器安装包，"
          "且 techdaily 文件夹与 run_daily.py 在同一目录。", file=sys.stderr)
    sys.exit(1)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"daily-{dt.date.today():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"),
                  logging.StreamHandler()],
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="科技圈日报生成")
    ap.add_argument("--date", default=None, help="日报日期标签 YYYY-MM-DD（默认今天）")
    ap.add_argument("--max-videos", type=int, default=None, help="最多分析视频数（覆盖配置）")
    ap.add_argument("--dry", action="store_true", help="只采集+筛选，不下载不分析")
    ap.add_argument("--ignore-seen", action="store_true", help="忽略已看记录（测试用）")
    ap.add_argument("--force", action="store_true", help="今日已出刊也强制重跑")
    args = ap.parse_args()

    setup_logging()
    log = logging.getLogger("daily")
    t0 = time.time()

    # 幂等保护：今日已出刊且非强制重跑 → 直接退出（防止 登录补跑/错过补跑 用空刊覆盖好刊）
    from techdaily.config import REPORT_DIR
    from techdaily.report import load_report
    today = args.date or dt.date.today().isoformat()
    if (REPORT_DIR / f"{today}.json").exists() and not args.ignore_seen and not args.force:
        existing = load_report(today)
        log.info("今日已出刊（%d 条，%s），跳过。重跑请加 --ignore-seen 或 --force",
                 len((existing or {}).get("items", [])),
                 (existing or {}).get("generated_at", "?"))
        return 0

    try:
        s = Settings.load()
    except RuntimeError as e:
        log.error("配置错误：%s", e)
        return 1
    if args.max_videos:
        s.max_analyze = args.max_videos

    glm = GLMClient(s)
    bl = Bilibili(s)
    state = load_state()
    purge_cache(s)  # 出刊前顺手清理过期视频缓存

    log.info("=== 科技圈日报开始 %s ===", dt.datetime.now())
    videos = bl.collect()
    from techdaily.sources import collect_text_sources
    texts = collect_text_sources(s)
    videos += texts
    log.info("外部文本源合计：%d 条（总池 %d）", len(texts), len(videos))
    candidates = rule_filter(videos, s, state)
    refined = glm_refine(candidates, glm)
    todo = refined[: s.max_analyze]
    log.info("进入视频理解：%d 条", len(todo))

    summaries = []
    if not args.dry:
        for i, v in enumerate(todo, 1):
            log.info("[%d/%d] 分析 %s %s", i, len(todo), v.bvid, v.title)
            result = analyze(v, s, glm, bl)
            state["seen"][v.bvid] = int(time.time())  # 看过即记，失败也不重试（防死循环烧积分）
            if result:
                summaries.append(result)
            time.sleep(1)
    # 未被送去分析、但已进入精筛的也标记为已见，避免第二天堆积
    # 注意：--dry 只演练不落痕，--ignore-seen 供测试复跑
    if not args.ignore_seen and not args.dry:
        for v in candidates:
            state["seen"][v.bvid] = int(time.time())

    # 清理 14 天前的已见记录
    cutoff = time.time() - 14 * 86400
    state["seen"] = {k: t for k, t in state["seen"].items() if t >= cutoff}
    state["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
    save_state(state)

    report = build_report(summaries, s, glm, date=args.date)

    # 推送前终审（撤游戏/重复/非科技/编造嫌疑条目）
    from techdaily.report import review_report
    report = review_report(report, s, glm)

    # 大事件追踪（故事线挂接/完结）+ 监控看板生成（失败不阻塞出刊）
    try:
        from techdaily.tracker import update_stories
        update_stories(report, s, glm)
    except Exception as e:
        log.warning("故事线追踪失败：%s", e)
    try:
        from techdaily.board import write_board
        write_board(s, glm)
    except Exception as e:
        log.warning("看板生成失败：%s", e)

    # 公网中转推送（方案B：Gitee 加密推送，队友 APK 直连拉取；失败不阻塞出刊）
    try:
        from techdaily.gitee_push import push_all
        push_all(report, s)
    except Exception as e:
        log.warning("Gitee 公网推送失败（不影响本地出刊）：%s", e)
    st = glm.stats
    log.info("=== 完成：%d 条入刊 | GLM 调用 %d 次（prompt %d / completion %d tokens）| 耗时 %.0f 秒 ===",
             len(report["items"]), st["calls"], st["prompt"], st["completion"], time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
