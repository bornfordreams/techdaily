"""配置加载：.env（密钥）+ config.toml（行为参数）。密钥只存在于 .env，绝不写进代码。"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

# 全局去代理（血泪教训）：Clash 等系统代理一挂，继承的 *_proxy 环境变量会拖死
# 本进程所有 requests/git 网络请求（曾致成刊崩毁与整天空刊）。B站/GLM/Gitee 均可直连。
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

log = logging.getLogger("techdaily.config")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORT_DIR = DATA_DIR / "reports"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = ROOT / "logs"
STATE_FILE = DATA_DIR / "state.json"
USAGE_FILE = DATA_DIR / "usage.log"


def _load_dotenv(path: Path) -> dict:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


@dataclass
class Settings:
    # 密钥（来自 .env）
    glm_api_key: str = ""
    bilibili_cookie: str = ""
    # glm
    glm_base_url: str = "https://open.bigmodel.cn/api/coding/paas/v4"
    glm_model: str = "glm-5.3-flash"
    glm_api_format: str = "auto"  # auto/openai/anthropic
    # bilibili
    popular_pages: int = 2
    search_keywords: list = field(default_factory=lambda: ["AI", "大模型", "芯片"])
    days_window: int = 2
    search_pages: int = 1
    # filter
    min_play: int = 20000
    min_duration: int = 60
    max_duration: int = 1800
    block_words: list = field(default_factory=list)
    require_words_any: list = field(default_factory=list)
    max_candidates: int = 25
    max_analyze: int = 12
    # analyze
    frames: int = 8
    frame_width: int = 640
    subtitle_max_chars: int = 3000
    max_download_mb: int = 150
    # hub
    hub_bind: str = "127.0.0.1"
    hub_port: int = 8300
    team_code: str = ""
    # gitee 公网中转（方案B：公开仓库+密文）
    gitee_push: bool = False
    gitee_owner: str = ""
    gitee_repo: str = "techdaily"
    gitee_token: str = ""
    # 外部文本源（与B站视频同池竞争）
    src_hackernews: bool = True
    src_ithome: bool = True
    src_36kr: bool = True
    src_github: bool = True
    # report
    max_items: int = 12
    min_items: int = 8

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = root or ROOT
        env = _load_dotenv(root / ".env")
        raw: dict = {}
        cfg_path = root / "config.toml"
        if cfg_path.exists():
            raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        s = cls()
        s.glm_api_key = env.get("GLM_API_KEY", "") or os.environ.get("GLM_API_KEY", "")
        s.bilibili_cookie = env.get("BILIBILI_COOKIE", "") or os.environ.get("BILIBILI_COOKIE", "")
        s.gitee_token = env.get("GITEE_TOKEN", "") or os.environ.get("GITEE_TOKEN", "")
        _get = lambda section, key, default: raw.get(section, {}).get(key, default)
        s.glm_base_url = _get("glm", "base_url", s.glm_base_url)
        s.glm_model = _get("glm", "model", s.glm_model)
        s.glm_api_format = _get("glm", "api_format", s.glm_api_format)
        s.popular_pages = int(_get("bilibili", "popular_pages", s.popular_pages))
        s.search_keywords = _get("bilibili", "search_keywords", s.search_keywords)
        s.days_window = int(_get("bilibili", "days_window", s.days_window))
        s.search_pages = int(_get("bilibili", "search_pages", s.search_pages))
        s.min_play = int(_get("filter", "min_play", s.min_play))
        s.min_duration = int(_get("filter", "min_duration", s.min_duration))
        s.max_duration = int(_get("filter", "max_duration", s.max_duration))
        s.block_words = [str(w).lower() for w in _get("filter", "block_words", [])]
        s.require_words_any = _get("filter", "require_words_any", [])
        s.max_candidates = int(_get("filter", "max_candidates", s.max_candidates))
        s.max_analyze = int(_get("filter", "max_analyze", s.max_analyze))
        s.frames = int(_get("analyze", "frames", s.frames))
        s.frame_width = int(_get("analyze", "frame_width", s.frame_width))
        s.subtitle_max_chars = int(_get("analyze", "subtitle_max_chars", s.subtitle_max_chars))
        s.max_download_mb = int(_get("analyze", "max_download_mb", s.max_download_mb))
        s.hub_bind = _get("hub", "bind", s.hub_bind)
        s.hub_port = int(_get("hub", "port", s.hub_port))
        s.team_code = _get("hub", "team_code", s.team_code)
        s.gitee_push = bool(_get("gitee", "push", False))
        s.gitee_owner = _get("gitee", "owner", "")
        s.gitee_repo = _get("gitee", "repo", "techdaily")
        s.src_hackernews = str(_get("sources", "hackernews", "true")).lower() != "false"
        s.src_ithome = str(_get("sources", "ithome", "true")).lower() != "false"
        s.src_36kr = str(_get("sources", "kr36", "true")).lower() != "false"
        s.src_github = str(_get("sources", "github_trending", "true")).lower() != "false"
        s.max_items = int(_get("report", "max_items", s.max_items))
        s.min_items = int(_get("report", "min_items", s.min_items))
        if not s.glm_api_key:
            raise RuntimeError("缺少 GLM_API_KEY：请在 .env 中填写（参考 .env.example）")
        if not s.team_code:
            raise RuntimeError("缺少团队码：请在 config.toml [hub].team_code 设置")
        for d in (DATA_DIR, REPORT_DIR, CACHE_DIR, LOG_DIR):
            d.mkdir(parents=True, exist_ok=True)
        return s


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:  # 损坏则重建
            log.warning("state.json 损坏，重建: %s", e)
    return {"seen": {}, "last_run": None}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
