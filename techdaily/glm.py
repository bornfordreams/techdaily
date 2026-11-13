"""GLM/多后端客户端：同时支持 OpenAI 兼容与 Anthropic 兼容两种 API 格式。

格式自动识别（api_format=auto）：
  - base_url 含 "anthropic" 或 key 以 "sk-ant-" 开头 -> Anthropic 格式
  - 其余 -> OpenAI 格式（智谱 openapi / DeepSeek / Kimi / OpenRouter / Ollama 等皆通用）
也可在 config.toml [glm].api_format 手动指定 "openai" / "anthropic"。
token 用量落盘便于核对积分。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path

import requests

from .config import USAGE_FILE, Settings

log = logging.getLogger("techdaily.glm")


class GLMError(RuntimeError):
    pass


class GLMClient:
    def __init__(self, s: Settings):
        self.s = s
        self.stats = {"calls": 0, "prompt": 0, "completion": 0}
        self.fmt = self._detect_format(s)
        log.info("GLM 客户端格式：%s（%s）", self.fmt, s.glm_base_url)

    # ---------- 格式识别 ----------
    @staticmethod
    def _detect_format(s: Settings) -> str:
        f = (getattr(s, "glm_api_format", "auto") or "auto").strip().lower()
        if f in ("openai", "anthropic"):
            return f
        if "anthropic" in s.glm_base_url.lower() or s.glm_api_key.startswith("sk-ant-"):
            return "anthropic"
        return "openai"

    # ---------- 对外接口 ----------
    def chat_text(self, system: str, user: str, max_tokens: int = 2000) -> str:
        return self._complete(system, [{"type": "text", "text": user}], max_tokens)

    def chat_json(self, system: str, user: str, max_tokens: int = 2000):
        content = self.chat_text(system, user, max_tokens)
        return self.parse_json(content)

    def vision_json(self, system: str, prompt: str, image_paths: list, max_tokens: int = 3000):
        parts: list = [{"type": "text", "text": prompt}]
        for p in image_paths:
            parts.append({"type": "image", "path": Path(p)})
        content = self._complete(system, parts, max_tokens)
        return self.parse_json(content)

    # ---------- 请求构造与发送 ----------
    def _complete(self, system: str, parts: list, max_tokens: int) -> str:
        if self.fmt == "anthropic":
            url, headers, payload = self._build_anthropic(system, parts, max_tokens)
        else:
            url, headers, payload = self._build_openai(system, parts, max_tokens)
        data = self._post(url, headers, payload)
        return self._extract_text(data)

    def _build_openai(self, system: str, parts: list, max_tokens: int):
        content = []
        for p in parts:
            if p["type"] == "text":
                content.append({"type": "text", "text": p["text"]})
            else:
                b64 = base64.b64encode(Path(p["path"]).read_bytes()).decode()
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        payload = {"model": self.s.glm_model, "messages": messages, "max_tokens": max_tokens,
                   "thinking": {"type": "disabled"}}
        url = f"{self.s.glm_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.s.glm_api_key}",
                   "Content-Type": "application/json"}
        return url, headers, payload

    def _build_anthropic(self, system: str, parts: list, max_tokens: int):
        blocks = []
        for p in parts:
            if p["type"] == "text":
                blocks.append({"type": "text", "text": p["text"]})
            else:
                b64 = base64.b64encode(Path(p["path"]).read_bytes()).decode()
                blocks.append({"type": "image",
                               "source": {"type": "base64", "media_type": "image/jpeg",
                                          "data": b64}})
        payload = {"model": self.s.glm_model, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": blocks}]}
        if system:
            payload["system"] = system
        url = f"{self.s.glm_base_url.rstrip('/')}/v1/messages"
        headers = {"x-api-key": self.s.glm_api_key,
                   "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        return url, headers, payload

    def _post(self, url: str, headers: dict, payload: dict, retries: int = 2,
              timeout: int = 180) -> str:
        last: Exception | None = None
        for i in range(retries + 1):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=timeout)
                if r.status_code == 200:
                    data = r.json()
                    self._log_usage(self._extract_usage(data))
                    return data
                err = GLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise err
                last = err
                log.warning("GLM %s，第 %d 次重试", r.status_code, i + 1)
            except requests.RequestException as e:
                last = GLMError(f"网络错误: {e}")
                log.warning("GLM 网络错误，第 %d 次重试: %s", i + 1, e)
            time.sleep(3 * (i + 1))
        raise last or GLMError("未知错误")

    # ---------- 响应解析 ----------
    def _extract_text(self, data: dict) -> str:
        if self.fmt == "anthropic":
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return text.strip()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        return (msg.get("content") or "").strip()

    def _extract_usage(self, data: dict) -> dict:
        u = data.get("usage") or {}
        if self.fmt == "anthropic":
            return {"prompt_tokens": u.get("input_tokens"),
                    "completion_tokens": u.get("output_tokens"),
                    "total_tokens": (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)}
        return u

    # ---------- 工具 ----------
    @staticmethod
    def parse_json(content: str):
        """从模型回复中稳健提取 JSON（容忍 ```json 围栏与前后废话）。"""
        if not content:
            raise GLMError("模型返回空内容")
        text = re.sub(r"```(?:json)?", "", content).strip("` \n")
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise GLMError(f"回复中无 JSON: {content[:200]}")
        end = max(text.rfind("}"), text.rfind("]"))
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise GLMError(f"JSON 解析失败: {e}; 片段: {text[start:start + 200]}")

    def _log_usage(self, usage: dict) -> None:
        self.stats["calls"] += 1
        self.stats["prompt"] += int(usage.get("prompt_tokens") or 0)
        self.stats["completion"] += int(usage.get("completion_tokens") or 0)
        try:
            USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {"ts": int(time.time()), "fmt": self.fmt, "model": self.s.glm_model,
                 "prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens"),
                 "total": usage.get("total_tokens")},
                ensure_ascii=False,
            )
            with open(USAGE_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            log.debug("usage 记录失败", exc_info=True)
