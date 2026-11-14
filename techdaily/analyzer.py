"""视频理解：yt-dlp 下载低清视频流 -> ffmpeg 抽帧（+B站字幕）-> GLM 视觉摘要。

GLM-5.3-Flash 走 Coding Plan 端点的 image_url 输入（已实测可用），
帧按时间顺序排列，配合字幕近似还原"画面+语音"的完整理解。
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from .bilibili import Bilibili, Video
from .config import CACHE_DIR, Settings
from .glm import GLMClient

log = logging.getLogger("techdaily.analyzer")


def purge_cache(s: Settings) -> tuple:
    """清理过期视频缓存（默认留3天供重刷），返回 (删除目录数, 释放MB)。"""
    import shutil
    import time as _time
    if not CACHE_DIR.exists():
        return 0, 0
    cutoff = _time.time() - s.cache_days * 86400
    n = mb = 0
    for d in CACHE_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            if d.stat().st_mtime < cutoff:
                sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d, ignore_errors=True)
                n += 1
                mb += sz
        except Exception:
            continue
    if n:
        log.info("视频缓存清理：%d 个目录 / %.0fMB（超过 %d 天）", n, mb / 1048576, s.cache_days)
    return n, int(mb / 1048576)


def _analyze_text(v: Video, s: Settings, glm: GLMClient) -> dict | None:
    """文本源成文：标题+摘要+来源热度直接写报道（200-350字，只依据给定文本）。"""
    system = ("你是资深科技媒体编辑。基于给定的新闻标题与摘要写结构化报道。"
              "一切游戏内容一律 is_tech=false。资料之外的信息不得编造，"
              "信息不足时如实写'报道未提及'。")
    prompt = (
        f"来源：{v.owner}\n标题：{v.title}\n摘要：{v.desc}\n热度/星标：{v.play}\n\n"
        "输出JSON：\n"
        '{"is_tech": true/false, "title": "事件标题(≤20字)", '
        '"one_liner": "一句话摘要(≤40字)", '
        '"detail": "详细报道(200-350字，只依据上述资料)", '
        '"key_points": ["要点"], "key_data": ["资料中的具体数据/型号/日期"], '
        '"importance": 1-10, '
        '"location": {"place": "发生地城市", "lat": 数值, "lon": 数值}, '
        '"why": "重要性理由"}\n'
        "importance 标准：影响行业格局=8-10，重要产品/公司动态=6-7，一般资讯=4-5。"
        "线上/全球事件取主要当事方总部所在地。is_tech=false 会被丢弃。"
    )
    try:
        result = glm.chat_json(system, prompt, max_tokens=1500)
    except Exception as e:
        log.warning("文本源分析失败 %s: %s", v.bvid, e)
        return None
    if not result.get("is_tech", False):
        log.info("文本源判定非科技，丢弃：%s", v.title[:30])
        return None
    result["url"] = v.url
    result["bvid"] = v.bvid
    result["owner"] = v.owner
    result["play"] = v.play
    return result


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _probe_duration(ffmpeg: str, path: Path) -> float:
    try:
        r = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)", r.stderr)
        if m:
            h, mi, sec = map(int, m.groups())
            return h * 3600 + mi * 60 + sec
    except Exception as e:
        log.debug("时长探测失败 %s: %s", path, e)
    return 0.0


def download_video(v: Video, s: Settings, ffmpeg: str) -> Path | None:
    """只下视频流（480p 纯视频 mp4，不需要音频与合并），控制流量与时间。"""
    outdir = CACHE_DIR / v.bvid
    outdir.mkdir(parents=True, exist_ok=True)
    marker = outdir / "src.mp4"
    if marker.exists() and marker.stat().st_size > 100_000:
        return marker
    opts = {
        # 480p 纯视频优先；部分视频最低 640p 起步（且无合并流），必须留任意档兜底
        "format": "bv*[ext=mp4][height<=480]/bv*[ext=mp4]/bv*",
        "outtmpl": str(outdir / "src.%(ext)s"),
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "max_filesize": s.max_download_mb * 1024 * 1024,
        "socket_timeout": 20, "retries": 2,
        "ffmpeg_location": ffmpeg,
    }
    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL(opts) as ydl:
            ydl.download([v.url])
    except Exception as e:
        log.warning("下载失败 %s: %s", v.bvid, e)
        return None
    for f in sorted(outdir.glob("src.*")):
        if f.stat().st_size > 100_000:
            if f.suffix != ".mp4":
                dst = f.with_suffix(".mp4")
                f.rename(dst)
                return dst
            return f
    return None


def extract_frames(video: Path, s: Settings, ffmpeg: str, bvid: str) -> list[Path]:
    outdir = CACHE_DIR / bvid
    dur = _probe_duration(ffmpeg, video)
    n = max(s.frames, 1)
    interval = dur / n if dur > 0 else 30.0
    for old in outdir.glob("f*.jpg"):
        old.unlink(missing_ok=True)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
           "-ss", "3", "-i", str(video),
           "-vf", f"fps=1/{interval:.2f},scale={s.frame_width}:-2",
           "-frames:v", str(n), "-q:v", "5", str(outdir / "f%02d.jpg")]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    except subprocess.SubprocessError as e:
        log.warning("抽帧失败 %s: %s", bvid, e)
        return []
    frames = sorted(outdir.glob("f*.jpg"))
    log.info("抽帧 %s：%d 帧（视频 %.0f 秒）", bvid, len(frames), dur)
    return frames


def analyze(v: Video, s: Settings, glm: GLMClient, bl: Bilibili) -> dict | None:
    """单条目理解，返回摘要 dict；任何一步失败返回 None（不阻塞整体流程）。

    source == "text"（HackerNews/IT之家/36氪/GitHub）走纯文本成文，不下载不看片。
    """
    if v.source == "text":
        return _analyze_text(v, s, glm)
    try:
        ffmpeg = _ffmpeg_exe()
        video = download_video(v, s, ffmpeg)
        if not video:
            log.warning("跳过 %s（无视频文件）", v.bvid)
            return None
        frames = extract_frames(video, s, ffmpeg, v.bvid)
        if not frames:
            log.warning("跳过 %s（抽帧为空）", v.bvid)
            return None
        subtitle = bl.fetch_subtitle(v.bvid)
        sub_text = (subtitle or "")[: s.subtitle_max_chars]
        system = ("你是资深科技媒体编辑，基于视频抽帧画面与字幕理解内容，输出结构化摘要。"
                  "注意：一切游戏内容（我的世界/MC/红石/模组/游戏实况等）一律 is_tech=false，"
                  "即使画面看起来像工程/科技创作。")
        prompt = (
            f"视频标题：{v.title}\nUP主：{v.owner}\n简介：{v.desc}\n"
            f"字幕（可能缺失或截断）：{sub_text or '（无字幕，仅凭画面判断）'}\n\n"
            f"下面是按时间顺序的 {len(frames)} 帧画面。请判断这是否值得进《科技圈日报》，并输出JSON：\n"
            '{"is_tech": true/false, "title": "事件标题(≤20字)", '
            '"one_liner": "一句话摘要(≤40字，讲清楚发生了什么)", '
            '"detail": "详细报道(300-500字)：按 背景→事件经过→关键数据/细节→影响与意义→后续看点 的结构写，'
            '基于画面与字幕中真实出现的信息，不得编造，写成像科技媒体正文的连贯长文，不要分点", '
            '"key_points": ["要点1", "要点2"], '
            '"key_data": ["画面/字幕中的具体数据、型号、日期等证据"], '
            '"location": {"place": "事件核心发生地(城市级)", "lat": 数值, "lon": 数值}, '
            '"importance": 1-10, "why": "重要性理由(一句话)"}\n'
            "importance 标准：影响行业格局或大量人群=8-10，重要产品/公司动态=6-7，"
            "一般资讯=4-5，趣味性内容=1-3。is_tech=false 会被丢弃。"
        )
        result = glm.vision_json(system, prompt, frames, max_tokens=2500)
        if not isinstance(result, dict) or not result.get("is_tech", False):
            log.info("GLM 判定非科技内容，丢弃 %s", v.bvid)
            return None
        result["url"] = v.url
        result["bvid"] = v.bvid
        result["owner"] = v.owner
        result["play"] = v.play
        return result
    except Exception as e:
        log.warning("分析失败 %s: %s", v.bvid, e)
        return None
