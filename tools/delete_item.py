r"""手动撤稿：说删哪条就删哪条。用法：
python tools\delete_item.py 3          # 删今天第3条
python tools\delete_item.py 3,5 2026-11-10   # 删指定日期第3、5条
撤稿后自动重排序、重存 JSON/MD、重推 Gitee。
想把某关键词永久拉黑：改 config.toml 的 block_words。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings
from gitee_push import push_all
from report import load_report, render_md


def main() -> int:
    import datetime as dt
    if len(sys.argv) < 2 or not sys.argv[1].replace(",", "").isdigit():
        print(__doc__)
        return 1
    ranks = {int(x) for x in sys.argv[1].split(",") if x.strip()}
    date = sys.argv[2] if len(sys.argv) > 2 else dt.date.today().isoformat()
    report = load_report(date)
    if not report or not report.get("items"):
        print(f"{date} 无日报")
        return 1
    print("撤稿前：")
    for it in report["items"]:
        mark = " [撤]" if it["rank"] in ranks else ""
        print(f"  {it['rank']}. {it['title']}{mark}")
    kept = [it for it in report["items"] if it["rank"] not in ranks]
    if not kept:
        print("不能把整期删空")
        return 1
    for i, it in enumerate(kept, 1):
        it["rank"] = i
    report["items"] = kept
    from report import REPORT_DIR
    (REPORT_DIR / f"{date}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORT_DIR / f"{date}.md").write_text(render_md(report), encoding="utf-8")
    s = Settings.load()
    pushed = push_all(report, s)
    print(f"已撤 {len(ranks)} 条，余 {len(kept)} 条；Gitee 重推：{'OK' if pushed else '跳过'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
