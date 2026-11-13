"""把成刊推送到 Gitee 公开仓库（git 方式）：内容 AES-256-GCM 加密，密钥 = SHA256(团队码)。

仓库公开但内容是密文；Gitee 私人令牌只存在本机 .env，绝不进 APK。
（曾试 Gitee contents API，在空仓库/无分支场景下 400 抽风，git 直推一劳永逸。）
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path

from .config import DATA_DIR, Settings
from .report import list_reports, load_report

log = logging.getLogger("techdaily.gitee")

_BUNDLED_GIT = Path(r"C:\Program Files\Cherry Studio\resources\app.asar.unpacked"
                    r"\resources\binaries\win32-x64\git\cmd\git.exe")


def _git_exe() -> str:
    g = shutil.which("git")
    if g:
        return g
    if _BUNDLED_GIT.exists():
        return str(_BUNDLED_GIT)
    raise RuntimeError("git 不可用：既不在 PATH 也没有内置副本")


def _run(args: list, token: str = "") -> str:
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[:300]
        if token:
            err = err.replace(token, "***")
        raise RuntimeError(f"git {args[1:3]} 失败: {err}")
    return r.stdout


def _key(team_code: str) -> bytes:
    return hashlib.sha256(team_code.encode("utf-8")).digest()


def _envelope(obj, key: bytes) -> str:
    """对象 -> {"v":1,"data":base64(nonce(12)||密文||tag(16))}"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = __import__("os").urandom(12)
    plain = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, plain, None)
    return json.dumps({"v": 1, "data": base64.b64encode(nonce + ct).decode("utf-8")},
                      ensure_ascii=False)


def push_all(report: dict, s: Settings) -> bool:
    """出刊后调用：git 工作副本里写 latest/index/reports 三件套并推送。"""
    if not s.gitee_push:
        return False
    if not (s.gitee_token and s.gitee_owner and s.gitee_repo):
        log.warning("Gitee 未配置完整（GITEE_TOKEN/owner/repo），跳过公网推送")
        return False

    git = _git_exe()
    work = DATA_DIR / "gitee_repo"
    url_pub = f"https://gitee.com/{s.gitee_owner}/{s.gitee_repo}.git"
    url_tok = f"https://oauth2:{s.gitee_token}@gitee.com/{s.gitee_owner}/{s.gitee_repo}.git"
    # -c http.proxy= 强制无视环境变量里的代理（clash 死代理会拖死推送）
    cfg = ["-c", "user.name=techdaily", "-c", "user.email=techdaily@local",
           "-c", "safe.directory=*", "-c", "http.proxy=", "-c", "https.proxy="]

    if not (work / ".git").exists():
        _run([git] + cfg + ["clone", url_tok, str(work)], s.gitee_token)
        # 工作副本的 remote 不留 token，推送时用一次性 URL
        _run([git, "-C", str(work), "remote", "set-url", "origin", url_pub])
    else:
        try:
            _run([git, "-C", str(work)] + cfg + ["pull", "--ff-only", url_tok, "master"],
                 s.gitee_token)
        except RuntimeError:
            pass  # 远端还没分支时忽略

    key = _key(s.team_code)
    (work / "latest.json").write_text(_envelope(report, key), encoding="utf-8")
    index = []
    for d in list_reports()[:30]:
        r = load_report(d)
        if r:
            index.append({"date": d, "headline": r.get("headline", ""),
                          "count": len(r.get("items", []))})
    (work / "index.json").write_text(_envelope(index, key), encoding="utf-8")
    (work / "reports").mkdir(exist_ok=True)
    (work / "reports" / f"{report['date']}.json").write_text(
        _envelope(report, key), encoding="utf-8")
    if not (work / "README.md").exists():
        (work / "README.md").write_text(
            "# techdaily\n\n科技圈日报公网中转。内容为 AES-256-GCM 密文，团队码即密钥。\n",
            encoding="utf-8")

    _run([git, "-C", str(work)] + cfg + ["add", "-A"])
    r = subprocess.run([git, "-C", str(work)] + cfg + ["commit", "-m", f"daily {report['date']}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 and "nothing to commit" not in out:
        raise RuntimeError(f"git commit 失败: {out[:200]}")
    _run([git, "-C", str(work)] + cfg + ["push", url_tok, "HEAD:master"], s.gitee_token)
    log.info("Gitee 公网推送完成（git 模式）：https://gitee.com/%s/%s", s.gitee_owner, s.gitee_repo)
    return True
