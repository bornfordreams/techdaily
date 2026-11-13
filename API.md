# 科技圈日报 · 外部 API 说明

任何人都可以基于本文档编写自己的客户端（任何语言/平台）。
系统有两种服务形态，客户端按"服务器地址"自动适配（地址含 `gitee.com` 即为形态 B）。

---

## 形态 A：Hub 直连（自建服务器 / 局域网 / VPN 通道）

Base URL：`http://<服务器地址>:<端口>`（默认端口 8300）

| 接口 | 方法 | 鉴权 | 说明 |
|---|---|---|---|
| `/api/ping` | GET | 无 | 探活，返回 `{"ok":true,"service":"techdaily-hub"}` |
| `/api/latest` | GET | 需要 | 最新一期日报（客户端轮询用） |
| `/api/reports` | GET | 需要 | 历史期列表 `[{"date","headline","count"}]` |
| `/api/report/<YYYY-MM-DD>` | GET | 需要 | 指定日期的日报 |

**鉴权方式**：请求头 `X-Team-Code: <团队码>`；错误返回 `403`。

**日报 JSON 结构**（`/api/latest` 与 `/api/report/*` 的响应体）：

```json
{
  "date": "2026-11-13",
  "generated_at": "2026-11-13T07:20:01",
  "headline": "今日科技圈一句话总览",
  "items": [
    {
      "rank": 1,
      "title": "事件标题",
      "one_liner": "一句话摘要",
      "detail": "300-500字长文报道（背景→经过→数据→影响）",
      "evidence": ["🔍 关键数据/型号/日期证据"],
      "topic": "AI/芯片/终端/互联网/航天/新能源/其他",
      "url": "https://www.bilibili.com/video/BV..."
    }
  ]
}
```

---

## 形态 B：Gitee 公网中转（默认推送方式，无需开放任何端口）

客户端只需能访问 gitee.com。服务端每天出刊后把日报**加密**推到仓库，
客户端拉取密文并用团队码解密。

### 端点（均为公开 raw 文件，路人只能看到密文）

| 路径 | 内容 |
|---|---|
| `<仓库raw地址>/latest.json` | 最新一期（对应形态 A 的 `/api/latest`） |
| `<仓库raw地址>/index.json` | 历史期列表（对应 `/api/reports`） |
| `<仓库raw地址>/reports/<YYYY-MM-DD>.json` | 指定日期 |

### 加密信封格式

```json
{"v": 1, "data": "<base64>"}
```

`data` 解 base64 后的字节流 = `nonce(12字节) || 密文 || GCM tag(16字节)`，
算法 **AES-256-GCM**，密钥 = **SHA-256(团队码)**（UTF-8 编码后取哈希，32 字节），
无 AAD。解密后的明文即上面的日报 JSON。

### 各语言解密要点

- **Python**：`cryptography` 库 `AESGCM(hashlib.sha256(team.encode()).digest()).decrypt(blob[:12], blob[12:], None)`
- **Java/Android**：`javax.crypto` `AES/GCM/NoPadding`，`GCMParameterSpec(128, blob, 0, 12)`，`doFinal(blob, 12, len-12)`
- **JavaScript (Node)**：`crypto.createDecipheriv('aes-256-gcm', key, nonce)` + `setAuthTag`
- 团队码错误 → 解密抛出认证失败（InvalidTag），据此提示用户重填

### 最小可用客户端（Python 示例）

```python
import base64, hashlib, json, urllib.request
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SERVER = "https://gitee.com/<用户名>/<仓库>/raw/master"   # 向创建者索取
TEAM_CODE = "<团队码>"                                    # 向创建者索取

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开系统代理
envelope = json.loads(opener.open(SERVER + "/latest.json", timeout=20).read())
blob = base64.b64decode(envelope["data"])
plain = AESGCM(hashlib.sha256(TEAM_CODE.encode()).digest()).decrypt(blob[:12], blob[12:], None)
report = json.loads(plain)
print(report["date"], report["headline"], len(report["items"]), "条")
```

---

## 服务端对接（自建）

服务器安装包内 `run_daily.py` 为出刊引擎、`hub.py` 为形态 A 服务、
`gitee_push.py` 为形态 B 推送器，部署方式见服务器安装包内《部署指南.md》。
