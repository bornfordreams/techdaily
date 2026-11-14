r"""把已有日报的条目用长文提示词重新分析并成刊推送（视频有缓存，很快）。
用法：python tools\refresh_detail.py [日期，默认今天]
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from analyzer import analyze
from bilibili import Bilibili, Video
from config import Settings
from gitee_push import push_all
from glm import GLMClient
from report import build_report, load_report


def main() -> int:
    import datetime as dt
    date = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    s = Settings.load()
    glm = GLMClient(s)
    bl = Bilibili(s)
    report = load_report(date)
    if not report or not report.get("items"):
        print(f"{date} 无日报可刷新")
        return 1
    summaries = []
    for it in report["items"]:
        bvid = it["url"].rsplit("/", 1)[1]
        v = Video(bvid=bvid, title=it.get("title", ""))
        print("重新分析", bvid, v.title[:30])
        r = analyze(v, s, glm, bl)
        if r:
            summaries.append(r)
    if not summaries:
        print("全部失败")
        return 1
    new = build_report(summaries, s, glm, date=date)
    push_all(new, s)
    print("完成：", len(new["items"]), "条（已含长文并推送 Gitee）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
