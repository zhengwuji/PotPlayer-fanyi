#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api_manager.py — PotPlayer DeepSeek Translate 的配套 API 管理器（图形界面）

为什么要有它：PotPlayer 的翻译插件只能拿到「账户名称 + 密码」两个输入框，
没有任何办法自建表单。所以把「新建 API / 获取可用模型 / 切换当前 API」
这套管理界面做成一个独立的小工具，由它来写插件读取的配置文件。

文件分工：
  deepseek_apis.json   本工具自己的 API 列表（可存任意多个）
  deepseek_api.txt     插件读取的生效配置（只写「当前」那一个）

核心逻辑（load_store / write_plugin_config / fetch_models 等）都是纯函数，
可以脱离图形界面单独测试；用 --selftest 跑一轮。
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP_TITLE = "PotPlayer DeepSeek Translate - API 管理器"
CONFIG_DIRS = [
    os.path.join(os.environ.get("APPDATA", ""), "PotPlayerMini64"),
    os.path.join(os.environ.get("APPDATA", ""), "DAUM", "PotPlayerMini64"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "PotPlayerMini64"),
]
STORE_NAME = "deepseek_apis.json"
PLUGIN_CFG_NAME = "deepseek_api.txt"

PRESETS = {
    "（自定义 / 留空）": None,
    "DeepSeek 官方": ("https://api.deepseek.com/v1", "deepseek-chat", "openai", "bearer"),
    "Inception Mercury": ("https://api.inceptionlabs.ai/v1", "mercury-2.5", "openai", "bearer"),
    "硅基流动 SiliconFlow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct", "openai", "bearer"),
    "智谱 GLM": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", "openai", "bearer"),
    "月之暗面 Moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k", "openai", "bearer"),
    "通义千问 DashScope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo", "openai", "bearer"),
    "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini", "openai", "bearer"),
    "OpenRouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "openai", "bearer"),
    "Google Gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "openai", "bearer"),
    "Groq": ("https://api.groq.com/openai/v1", "llama-3.1-8b-instant", "openai", "bearer"),
    "Anthropic Claude": ("https://api.anthropic.com", "claude-3-5-haiku-latest", "anthropic", "x-api-key"),
    "本地 Ollama": ("http://localhost:11434/v1", "qwen2.5:7b", "openai", "none"),
    "本地 LM Studio": ("http://localhost:1234/v1", "local-model", "openai", "none"),
    "本地 one-api / new-api": ("http://localhost:3000/v1", "gpt-4o-mini", "openai", "bearer"),
}

FORMATS = ["openai", "anthropic"]
AUTHS = ["bearer", "raw", "x-api-key", "api-key", "none"]

# 按名字索引的预设，用于解析配置串里的 preset=xxx
PRESET_KEYS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "openai", "bearer"),
    "inception": ("https://api.inceptionlabs.ai/v1", "mercury-2.5", "openai", "bearer"),
    "mercury": ("https://api.inceptionlabs.ai/v1", "mercury-2.5", "openai", "bearer"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "openai", "bearer"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct", "openai", "bearer"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k", "openai", "bearer"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", "openai", "bearer"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", "openai", "bearer"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo", "openai", "bearer"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo", "openai", "bearer"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "openai", "bearer"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.1-8b-instant", "openai", "bearer"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "openai", "bearer"),
    "anthropic": ("https://api.anthropic.com", "claude-3-5-haiku-latest", "anthropic", "x-api-key"),
    "claude": ("https://api.anthropic.com", "claude-3-5-haiku-latest", "anthropic", "x-api-key"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5:7b", "openai", "none"),
    "lmstudio": ("http://localhost:1234/v1", "local-model", "openai", "none"),
    "oneapi": ("http://localhost:3000/v1", "gpt-4o-mini", "openai", "bearer"),
    "newapi": ("http://localhost:3000/v1", "gpt-4o-mini", "openai", "bearer"),
}
# 这些预设需要关掉推理
PRESET_NEEDS_NOREASON = {"inception", "mercury"}


# ============================================================ 路径与存取
def config_dir(create=True):
    for d in CONFIG_DIRS:
        if d and os.path.isdir(d):
            return d
    d = CONFIG_DIRS[0]
    if create and d:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return d


def store_path():
    return os.path.join(config_dir(), STORE_NAME)


def plugin_cfg_path():
    return os.path.join(config_dir(), PLUGIN_CFG_NAME)


def blank_api(name=""):
    return {
        "name": name, "url": "", "model": "", "format": "openai",
        "auth": "bearer", "key": "", "maxtok": 512,
        "no_reasoning": False, "extra": "", "body": "", "note": "",
    }


def default_store():
    return {"apis": [], "active": ""}


def load_store(path=None):
    p = path or store_path()
    if not os.path.isfile(p):
        return default_store()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return default_store()
    if not isinstance(data, dict):
        return default_store()
    apis = data.get("apis")
    if not isinstance(apis, list):
        apis = []
    clean = []
    for a in apis:
        if not isinstance(a, dict):
            continue
        rec = blank_api()
        rec.update({k: v for k, v in a.items() if k in rec})
        clean.append(rec)
    return {"apis": clean, "active": str(data.get("active", ""))}


def save_store(store, path=None):
    p = path or store_path()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=2)
    return p


# ============================================================ 生成配置串
def build_account_spec(api):
    """把一条 API 记录变成插件的 account= 配置串"""
    parts = []
    if api.get("url"):
        parts.append("url=" + api["url"].strip())
    if api.get("model"):
        parts.append("model=" + api["model"].strip())
    if api.get("format") and api["format"] != "openai":
        parts.append("format=" + api["format"])
    if api.get("auth") and api["auth"] != "bearer":
        parts.append("auth=" + api["auth"])
    try:
        mt = int(api.get("maxtok") or 512)
    except (TypeError, ValueError):
        mt = 512
    if mt != 512:
        parts.append("maxtok=" + str(mt))
    if api.get("no_reasoning"):
        parts.append('body="reasoning_effort":"none"')
    if api.get("body"):
        parts.append("body=" + api["body"].strip())
    if api.get("extra"):
        parts.append("extra=" + api["extra"].strip())
    return "; ".join(parts)


def write_plugin_config(api, path=None):
    """写出插件读取的 deepseek_api.txt"""
    p = path or plugin_cfg_path()
    spec = build_account_spec(api)
    lines = [
        "# 由 API 管理器生成 —— 不要手改，改完会被下次保存覆盖",
        "# 插件优先读这个文件；删掉它即可回到 PotPlayer 界面里的账户配置",
        "#",
        "# 当前 API: " + (api.get("name") or "(未命名)"),
    ]
    if api.get("note"):
        lines.append("# 备注: " + api["note"])
    lines += ["", "account=" + spec, "key=" + (api.get("key") or "")]
    with open(p, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return p


# ==================================================== 端点推导与模型拉取
def resolve_urls(base, fmt="openai"):
    """从用户填的地址推出 chat 与 models 两个端点（与插件同规则）"""
    u = (base or "").strip()
    if not u:
        return "", ""
    if "http" not in u:
        u = "https://" + u
    while u.endswith("/"):
        u = u[:-1]

    chat = u
    if "/chat/completions" not in chat and "/messages" not in chat:
        if not any(v in chat for v in ("/v1", "/v2", "/v3", "/v4")):
            chat += "/v1"
        chat += "/messages" if fmt == "anthropic" else "/chat/completions"

    m = u
    for suf in ("/chat/completions", "/messages"):
        i = m.find(suf)
        if i != -1:
            m = m[:i]
    m = m.rstrip("/")
    if not any(v in m for v in ("/v1", "/v2", "/v3", "/v4")):
        m += "/v1"
    return chat, m + "/models"


def build_headers(api):
    key = (api.get("key") or "").strip()
    auth = api.get("auth") or "bearer"
    h = {"Content-Type": "application/json"}
    if auth == "none" or not key:
        pass
    elif auth == "x-api-key":
        h["x-api-key"] = key
    elif auth == "api-key":
        h["api-key"] = key
    elif auth == "raw":
        h["Authorization"] = key
    else:
        h["Authorization"] = "Bearer " + key
    if api.get("format") == "anthropic":
        h["anthropic-version"] = "2023-06-01"
    if api.get("extra"):
        for e in str(api["extra"]).split("|"):
            if ":" in e:
                k, v = e.split(":", 1)
                h[k.strip()] = v.strip()
    return h


def fetch_models(api, timeout=25):
    """GET /v1/models，返回 (模型id列表, 错误信息)"""
    _, murl = resolve_urls(api.get("url", ""), api.get("format", "openai"))
    if not murl:
        return [], "请先填写 API 地址"
    st, txt = _http("GET", murl, build_headers(api), timeout=timeout)
    if st == 0:
        return [], txt
    if st != 200:
        return [], f"HTTP {st}: {txt[:200]}"
    try:
        data = json.loads(txt)
    except ValueError:
        return [], "响应不是合法 JSON"

    arr = data.get("data")
    if not isinstance(arr, list):
        arr = data.get("models")
    if not isinstance(arr, list):
        return [], "响应里没有 data[] 或 models[]"
    out = []
    for it in arr:
        if isinstance(it, dict):
            if isinstance(it.get("id"), str):
                out.append(it["id"])
            elif isinstance(it.get("name"), str):
                out.append(it["name"])
    return out, "" if out else "列表为空"


def explain_error(msg):
    """把常见报错翻译成「下一步该干什么」。

    这些提示全部来自实际踩到的坑，不是泛泛而谈。
    """
    m = msg or ""
    hints = []

    if "ConnectionResetError" in m or "10054" in m:
        hints.append("端口上确实有服务，但它中途把连接断掉了。常见原因：")
        hints.append("  · 本地网关/代理过载或限流 —— 稍等几秒重试即可")
        hints.append("  · 协议选错了：网关是 OpenAI 兼容时，「协议」要选 openai 而不是 anthropic")
    elif "ConnectionRefused" in m or "10061" in m:
        hints.append("这个端口上没有服务在监听 —— 本地服务没启动，或地址/端口写错。")

    if "invalid API key" in m or "Incorrect API key" in m:
        hints.append("Key 不被接受。如果连的是本地网关，注意这里有两套 Key：")
        hints.append("  · 客户端 Key —— 给插件/客户端用的（如 ccgw-...），应该填这个")
        hints.append("  · 上游账号 Key —— 网关拿去访问上游的（如 user_...），填它会 401")
    elif "missing Authorization header" in m:
        hints.append("完全没发认证头 —— 检查「认证」是否被设成了 none，或 Key 是空的。")

    if "MODEL_NOT_IN_PLAN" in m:
        hints.append("该模型不在你的套餐内 —— 用「获取可用模型」换一个能用的。")

    if "model_not_found" in m or "No such model" in m or "Model Not Exist" in m:
        hints.append("模型名不对 —— 用「获取可用模型」从列表里挑，别手打。")

    if "reasoning" in m and "token" in m:
        hints.append("这是推理模型把输出额度吃光了 —— 勾选「关闭模型推理」。")

    return "\n".join(hints)


def dialog_text(msg, api=None):
    """拼成最终展示给用户的文本"""
    extra = explain_error(msg)
    return msg + ("\n\n———————— 诊断建议 ————————\n" + extra if extra else "")


def murl_of(api):
    """该 API 实际会去请求的 models 端点，报错时一并显示便于核对"""
    return resolve_urls(api.get("url", ""), api.get("format", "openai"))[1] or "(地址为空)"


# ============================================ 自动适配（协议 / Key / 模型）
def split_host_port(url):
    """从 http(s)://host:port/... 里取出 host 与 port"""
    u = (url or "").strip()
    if "//" in u:
        u = u.split("//", 1)[1]
    u = u.split("/", 1)[0]
    if u.startswith("["):                      # IPv6
        host = u[:u.find("]") + 1]
        rest = u[u.find("]") + 1:]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
    elif ":" in u:
        host, p = u.rsplit(":", 1)
        port = int(p) if p.isdigit() else None
    else:
        host, port = u, None
    if port is None:
        port = 443 if (url or "").strip().lower().startswith("https") else 80
    return host, port


def is_local_host(host):
    h = (host or "").lower().strip("[]")
    if h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    return h.startswith(("127.", "10.", "192.168.", "172.16.", "172.17.",
                         "172.18.", "172.19.", "172.2", "172.30.", "172.31."))


def pid_on_port(port):
    """谁在监听这个端口（Windows: netstat -ano）"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:  # noqa: BLE001
        return 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[1].endswith(":" + str(port)):
            continue
        if "LISTEN" in parts[3].upper() and parts[4].isdigit():
            return int(parts[4])
    return 0


def process_path(pid):
    """取进程可执行文件路径（纯 ctypes，不依赖 psutil）"""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)      # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass
    return ""


KEY_RE = re.compile(
    r"""(?ix) ["']?(api[_-]?key|key|token)["']?\s*[=:]\s*["']?([A-Za-z0-9_\-\.]{8,})["']?""")

CONFIG_NAMES = ("config.yaml", "config.yml", "config.json", "settings.json",
                "config.toml", "keys.json", "api_keys.json")


def keys_in_file(path):
    """从配置文件里抠出所有像 Key 的字符串，保持出现顺序"""
    try:
        raw = open(path, "r", encoding="utf-8", errors="ignore").read()
    except OSError:
        return []
    out = []
    for m in KEY_RE.finditer(raw):
        v = m.group(2)
        if v and v not in out:
            out.append(v)
    return out


def discover_local_keys(url, log):
    """本地地址且 Key 不认时，去监听该端口那个程序的目录里找可用 Key。

    这正是「把上游账号 Key 自动换成客户端 Key」要用的机制：
    网关的 config.yaml 里同时有 api_keys[].key（客户端）和
    accounts[].api_key（上游），把候选全捞出来逐个实测，能通的自然是对的。
    """
    host, port = split_host_port(url)
    if not is_local_host(host):
        return []
    pid = pid_on_port(port)
    log.append(f"  监听 {port} 端口的进程 PID: {pid or '未找到'}")
    if not pid:
        return []
    exe = process_path(pid)
    log.append(f"  进程路径: {exe or '未知'}")
    if not exe:
        return []
    d = os.path.dirname(exe)

    files = [os.path.join(d, n) for n in CONFIG_NAMES
             if os.path.isfile(os.path.join(d, n))]
    if not files:
        try:
            files = [os.path.join(d, f) for f in sorted(os.listdir(d))
                     if f.lower().endswith((".yaml", ".yml", ".json", ".toml"))]
        except OSError:
            files = []
    log.append(f"  候选配置文件: {[os.path.basename(f) for f in files] or '无'}")

    cands = []
    for f in files:
        for k in keys_in_file(f):
            if k not in cands:
                cands.append(k)
    return cands


def _http(method, url, headers, data=None, timeout=15, tries=3):
    """发一次 HTTP，只对「连接级异常」重试。

    本地中转网关偶发 RST（WinError 10054），不重试就会把
    一次偶发网络抖动误判成「这个端点/协议不可用」。
    返回 (status, 文本)；status=0 表示始终没连上。
    """
    last = ""
    for i in range(max(1, tries)):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            if i < tries - 1:
                time.sleep(0.7 * (i + 1))
    return 0, last


def probe_models(api, key=None, timeout=10):
    a = dict(api)
    if key is not None:
        a["key"] = key
    _, murl = resolve_urls(a.get("url", ""), a.get("format", "openai"))
    if not murl:
        return False, "地址为空"
    st, txt = _http("GET", murl, build_headers(a), timeout=timeout)
    if st == 0:
        return False, txt[:60]
    return st == 200, f"HTTP {st}"


def _format_verdict(api, fmt, timeout):
    """判断某协议在这个地址上是否可用。

    返回 (verdict, 说明)：True=确认支持，False=确认不支持，None=无法判断。

    关键：只有 404/405 才说明「这个端点不存在」。其它任何 HTTP 状态
    （401/403/400/422…）都证明端点存在、只是这次请求没被接受，
    因而算「支持」。网络级错误一律算「无法判断」并重试，
    否则一次偶发 RST 就会把正确协议误判掉。
    """
    a = dict(api)
    a["format"] = fmt
    chat, _ = resolve_urls(a.get("url", ""), fmt)
    body = {"model": a.get("model") or "x",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16, "temperature": 0}
    if fmt == "anthropic":
        body["system"] = "test"
    st, txt = _http("POST", chat, build_headers(a),
                     json.dumps(body).encode("utf-8"), timeout=timeout)
    if st == 0:
        return None, txt[:48]
    if st in (404, 405):
        return False, f"HTTP {st}（端点不存在）"
    return True, f"HTTP {st}"


def detect_format(api, timeout=25):
    """探测该地址支持哪种协议，返回 (fmt, 说明)；判断不出返回 ('', 说明)"""
    cur = api.get("format", "openai")
    verdicts, notes = {}, []
    for fmt in ("openai", "anthropic"):
        v, note = _format_verdict(api, fmt, timeout)
        verdicts[fmt] = v
        notes.append(f"{fmt}:{note}")

    o, an = verdicts.get("openai"), verdicts.get("anthropic")
    if o is True and an is not True:
        return "openai", "openai 可用，anthropic " + notes[1].split(":", 1)[1]
    if an is True and o is not True:
        return "anthropic", "anthropic 可用，openai " + notes[0].split(":", 1)[1]
    if o is True and an is True:
        keep = cur if cur in ("openai", "anthropic") else "openai"
        return keep, f"两种都可用，保留 {keep}"
    return "", "无法判断（" + ", ".join(notes) + "）"


def pick_model(models, prefer=("flash", "mini", "haiku", "turbo", "free", "fast")):
    """从模型列表里挑一个适合字幕翻译的（优先快/便宜的小模型）"""
    low = [(m, m.lower()) for m in models]
    for kw in prefer:
        for m, lm in low:
            if kw in lm:
                return m
    return models[0] if models else ""


def auto_adapt(api, log=None):
    """一键自动适配：把「不能用的配置」自动修成「能用的配置」

    步骤：① 用当前 Key 试 ② 不行就找本地网关的 Key 逐个实测
         ③ 探测协议 ④ 模型不在列表里就自动挑一个
    """
    log = log if log is not None else []
    a = dict(api)
    url = a.get("url", "")

    log.append("① 用当前 Key 探测可用性")
    ok, note = probe_models(a, timeout=8)
    log.append(f"   结果: {note}")

    if not ok:
        log.append("② Key 不可用，尝试从本地服务自动取用")
        cands = discover_local_keys(url, log)
        if cands:
            log.append(f"   找到 {len(cands)} 个候选，逐个实测：")
            for k in cands:
                good, n2 = probe_models(a, key=k, timeout=8)
                log.append(f"     {k[:14]}… -> {n2}")
                if good:
                    a["key"] = k
                    log.append("     ✓ 采用这个")
                    ok = True
                    break
            if not ok:
                log.append("     全部不可用")
        else:
            log.append("   没找到可自动取用的本地 Key（非本地地址，或没有配置文件）")
    if not ok:
        log.append("！Key 仍未解决，请手工确认")
        return a, log

    log.append("③ 探测协议")
    fmt, note3 = detect_format(a)
    log.append(f"   {note3}")
    if fmt and fmt != a.get("format"):
        log.append(f"   协议由 {a.get('format')} 自动改为 {fmt}")
        a["format"] = fmt

    log.append("④ 核对模型")
    models, err = fetch_models(a)
    if err:
        log.append(f"   取模型列表失败（{err}），保留原模型名")
    elif not models:
        log.append("   列表为空，保留原模型名")
    elif a.get("model") in models:
        log.append(f"   原模型 {a['model']} 在列表里，保持不变")
    else:
        picked = pick_model(models)
        log.append(f"   原模型 {a.get('model') or '(空)'} 不在列表（共 {len(models)} 个）")
        log.append(f"   自动挑用: {picked}")
        a["model"] = picked

    log.append("完成：配置已可用")
    return a, log


def ping(api, timeout=30):
    """发一次最小翻译请求，返回 (是否成功, 说明)"""
    chat, _ = resolve_urls(api.get("url", ""), api.get("format", "openai"))
    if not chat:
        return False, "请先填写 API 地址"
    body = {"model": api.get("model") or "x",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64, "temperature": 0}
    if api.get("no_reasoning"):
        body["reasoning_effort"] = "none"
    if api.get("body"):
        try:
            body.update(json.loads("{" + api["body"] + "}"))
        except ValueError:
            pass

    t0 = time.time()
    st, txt = _http("POST", chat, build_headers(api),
                    json.dumps(body).encode("utf-8"), timeout=timeout)
    dt = time.time() - t0
    if st == 0:
        return False, txt
    if st != 200:
        return False, f"HTTP {st}: {txt[:200]}"
    try:
        data = json.loads(txt)
    except ValueError:
        return False, "响应不是合法 JSON"

    if api.get("format") == "anthropic":
        c = data.get("content")
        out = c[0].get("text") if isinstance(c, list) and c else None
    else:
        ch = data.get("choices")
        out = ch[0]["message"].get("content") if isinstance(ch, list) and ch else None
    if isinstance(out, str) and out.strip():
        return True, f"成功 {dt:.1f}s，返回: {out.strip()[:40]}"
    reason = (data.get("usage", {}).get("completion_tokens_details", {})
                  .get("reasoning_tokens"))
    if reason:
        return False, (f"返回空内容，且消耗了 {reason} 个推理 token —— "
                       f"勾选「关闭模型推理」或加大输出上限")
    return False, f"返回里没有译文: {json.dumps(data, ensure_ascii=False)[:150]}"


def import_plugin_config(path=None):
    """反向导入：如果 deepseek_api.txt 是手写的，把它读成一条 API 记录"""
    p = path or plugin_cfg_path()
    if not os.path.isfile(p):
        return None
    acct = key = ""
    try:
        with open(p, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line[0] in "#;":
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip().lower(), v.strip()
                if k in ("account", "user"):
                    acct = v
                elif k in ("key", "apikey", "api_key"):
                    key = v
    except OSError:
        return None
    if not acct and not key:
        return None

    rec = blank_api("导入自 deepseek_api.txt")

    # 支持界面里的 add=名字; 其余配置 这种写法
    toks = [t.strip() for t in acct.split(";") if t.strip()]
    if toks and toks[0].lower().startswith("add="):
        nm = toks[0].split("=", 1)[1].strip()
        if nm:
            rec["name"] = nm
        toks = toks[1:]
    elif toks and toks[0].lower().startswith("use="):
        rec["name"] = toks[0].split("=", 1)[1].strip() or rec["name"]
        toks = toks[1:]

    for tok in toks:
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        k, v = k.strip().lower(), v.strip()
        if k in ("url", "base", "endpoint", "host"):
            rec["url"] = v
        elif k == "model":
            rec["model"] = v
        elif k == "format":
            rec["format"] = v
        elif k == "auth":
            rec["auth"] = v
        elif k in ("maxtok", "max_tokens", "maxtokens"):
            try:
                rec["maxtok"] = int(v)
            except ValueError:
                pass
        elif k in ("extra", "header"):
            rec["extra"] = v
        elif k in ("body", "params"):
            if "reasoning_effort" in v and "none" in v:
                rec["no_reasoning"] = True
            else:
                rec["body"] = v
        elif k == "preset":
            pv = PRESET_KEYS.get(v.lower())
            if pv:
                rec["url"], rec["model"], rec["format"], rec["auth"] = pv
                if v.lower() in PRESET_NEEDS_NOREASON:
                    rec["no_reasoning"] = True
            else:
                rec["note"] = (rec.get("note", "") + f" 未知预设 {v}").strip()

    rec["key"] = key
    return rec


# ================================================================ 图形界面
def run_gui(auto_close_ms=None):
    import tkinter as tk
    from tkinter import messagebox, ttk

    store = load_store()
    state = {"index": -1}

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("980x620")
    root.minsize(900, 560)

    pad = {"padx": 6, "pady": 4}
    left = ttk.Frame(root)
    left.pack(side="left", fill="both", expand=False, padx=8, pady=8)
    right = ttk.Frame(root)
    right.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=8)
    status = tk.StringVar(value="就绪")

    # ---------------- 左：列表
    ttk.Label(left, text="已保存的 API", font=("", 10, "bold")).pack(anchor="w")
    cols = ("name", "model", "cur")
    # 注意：ttk.Treeview 不接受 -width（那是 Listbox 的选项）。
    # Tk 8.6 会静默忽略，Tcl/Tk 9 直接抛 TclError: unknown option "-width"。
    tv = ttk.Treeview(left, columns=cols, show="headings", height=20)
    for c, t, w in (("name", "名称", 110), ("model", "模型", 110), ("cur", "当前", 40)):
        tv.heading(c, text=t)
        tv.column(c, width=w, anchor="w")
    tv.pack(fill="both", expand=True)

    btns = ttk.Frame(left)
    btns.pack(fill="x", pady=(6, 0))
    for txt, cb in (("新建", lambda: on_new()), ("复制", lambda: on_dup()),
                    ("删除", lambda: on_del())):
        b = ttk.Button(btns, text=txt, width=7)
        b.pack(side="left", padx=2)
        b.configure(command=cb)

    # ---------------- 右：表单
    form = ttk.LabelFrame(right, text="API 详情")
    form.pack(fill="x")
    form.columnconfigure(1, weight=1)

    v_name = tk.StringVar()
    v_preset = tk.StringVar(value="（自定义 / 留空）")
    v_url = tk.StringVar()
    v_model = tk.StringVar()
    v_fmt = tk.StringVar(value="openai")
    v_auth = tk.StringVar(value="bearer")
    v_key = tk.StringVar()
    v_maxtok = tk.StringVar(value="512")
    v_noreason = tk.BooleanVar(value=False)
    v_extra = tk.StringVar()
    v_body = tk.StringVar()
    v_note = tk.StringVar()

    def row(r, text, widget):
        ttk.Label(form, text=text).grid(row=r, column=0, sticky="w", **pad)
        widget.grid(row=r, column=1, sticky="ew", **pad)
        return widget

    row(0, "名称", ttk.Entry(form, textvariable=v_name))

    pf = ttk.Frame(form)
    cb = ttk.Combobox(pf, textvariable=v_preset, values=list(PRESETS.keys()),
                      state="readonly", width=26)
    cb.pack(side="left")
    ttk.Button(pf, text="套用预设", command=lambda: on_preset()).pack(side="left", padx=4)
    row(1, "服务商预设", pf)

    row(2, "API 地址", ttk.Entry(form, textvariable=v_url))
    row(3, "模型名", ttk.Entry(form, textvariable=v_model))

    mf = ttk.Frame(form)
    ttk.Button(mf, text="获取可用模型", command=lambda: on_models()).pack(side="left")
    ttk.Button(mf, text="测试连通性", command=lambda: on_ping()).pack(side="left", padx=4)
    row(4, "", mf)

    ff = ttk.Frame(form)
    ttk.Label(ff, text="协议").pack(side="left")
    ttk.Combobox(ff, textvariable=v_fmt, values=FORMATS, width=12,
                 state="readonly").pack(side="left", padx=(2, 10))
    ttk.Label(ff, text="认证").pack(side="left")
    ttk.Combobox(ff, textvariable=v_auth, values=AUTHS, width=12,
                 state="readonly").pack(side="left", padx=2)
    row(5, "格式 / 认证", ff)

    kf = ttk.Frame(form)
    e_key = ttk.Entry(kf, textvariable=v_key, show="*")
    e_key.pack(side="left", fill="x", expand=True)

    def toggle_key():
        e_key.configure(show="" if e_key.cget("show") else "*")

    ttk.Button(kf, text="显示", width=6, command=toggle_key).pack(side="left", padx=4)
    row(6, "API Key", kf)

    of = ttk.Frame(form)
    ttk.Label(of, text="输出上限").pack(side="left")
    ttk.Entry(of, textvariable=v_maxtok, width=7).pack(side="left", padx=(2, 12))
    ttk.Checkbutton(of, text="关闭模型推理（推理模型必勾）",
                    variable=v_noreason).pack(side="left")
    row(7, "生成参数", of)

    row(8, "额外请求头", ttk.Entry(form, textvariable=v_extra))
    row(9, "额外参数(JSON)", ttk.Entry(form, textvariable=v_body))
    row(10, "备注", ttk.Entry(form, textvariable=v_note))

    hint = ttk.Label(right, foreground="#555", justify="left", wraplength=620,
                     text=("「关闭模型推理」会写入 body=\"reasoning_effort\":\"none\"。\n"
                           "推理模型（如 Inception Mercury）不关掉的话，"
                           "推理会吃光输出上限、译文返回空。"))
    hint.pack(anchor="w", pady=(8, 0))

    act = ttk.Frame(right)
    act.pack(fill="x", pady=10)
    ttk.Button(act, text="① 自动适配（推荐）",
               command=lambda: on_autoadapt()).pack(side="left")
    ttk.Button(act, text="② 设为当前并写入插件",
               command=lambda: on_activate()).pack(side="left", padx=6)
    ttk.Button(act, text="保存", command=lambda: on_save()).pack(side="left")
    ttk.Button(act, text="打开配置文件位置",
               command=lambda: on_open()).pack(side="left", padx=6)

    tk.Label(root, textvariable=status, anchor="w",
             relief="sunken").pack(side="bottom", fill="x")

    # ---------------- 逻辑
    def current():
        i = state["index"]
        if 0 <= i < len(store["apis"]):
            return store["apis"][i]
        return None

    def form_to_api():
        a = current()
        if a is None:
            return None
        a["name"] = v_name.get().strip()
        a["url"] = v_url.get().strip()
        a["model"] = v_model.get().strip()
        a["format"] = v_fmt.get()
        a["auth"] = v_auth.get()
        a["key"] = v_key.get().strip()
        a["no_reasoning"] = bool(v_noreason.get())
        a["extra"] = v_extra.get().strip()
        a["body"] = v_body.get().strip()
        a["note"] = v_note.get().strip()
        try:
            a["maxtok"] = int(v_maxtok.get() or 512)
        except ValueError:
            a["maxtok"] = 512
            v_maxtok.set("512")
        return a

    def api_to_form(a):
        if a is None:
            for v in (v_name, v_url, v_model, v_key, v_extra, v_body, v_note):
                v.set("")
            v_fmt.set("openai")
            v_auth.set("bearer")
            v_maxtok.set("512")
            v_noreason.set(False)
            v_preset.set("（自定义 / 留空）")
            return
        v_name.set(a.get("name", ""))
        v_url.set(a.get("url", ""))
        v_model.set(a.get("model", ""))
        v_fmt.set(a.get("format", "openai"))
        v_auth.set(a.get("auth", "bearer"))
        v_key.set(a.get("key", ""))
        v_maxtok.set(str(a.get("maxtok", 512)))
        v_noreason.set(bool(a.get("no_reasoning")))
        v_extra.set(a.get("extra", ""))
        v_body.set(a.get("body", ""))
        v_note.set(a.get("note", ""))
        v_preset.set("（自定义 / 留空）")

    def refresh(select=None):
        tv.delete(*tv.get_children())
        for i, a in enumerate(store["apis"]):
            mark = "*" if a.get("name") == store.get("active") else ""
            tv.insert("", "end", iid=str(i),
                      values=(a.get("name") or "(未命名)", a.get("model", ""), mark))
        if select is not None and 0 <= select < len(store["apis"]):
            state["index"] = select
            tv.selection_set(str(select))
            tv.see(str(select))
            api_to_form(store["apis"][select])
        elif store["apis"]:
            state["index"] = 0
            tv.selection_set("0")
            api_to_form(store["apis"][0])
        else:
            state["index"] = -1
            api_to_form(None)

    def on_select(_evt=None):
        sel = tv.selection()
        if sel:
            state["index"] = int(sel[0])
            api_to_form(store["apis"][state["index"]])

    tv.bind("<<TreeviewSelect>>", on_select)

    def on_new():
        name = f"api{len(store['apis']) + 1}"
        store["apis"].append(blank_api(name))
        save_store(store)
        refresh(len(store["apis"]) - 1)
        status.set(f"已新建 {name} —— 填好后点「保存」")

    def on_dup():
        a = current()
        if a is None:
            return
        b = dict(a)
        b["name"] = a.get("name", "") + "-copy"
        store["apis"].append(b)
        save_store(store)
        refresh(len(store["apis"]) - 1)

    def on_del():
        a = current()
        if a is None:
            return
        if not messagebox.askyesno("删除", f"确定删除「{a.get('name')}」？"):
            return
        name = a.get("name")
        del store["apis"][state["index"]]
        if store.get("active") == name:
            store["active"] = ""
        save_store(store)
        refresh(0)

    def on_preset():
        val = PRESETS.get(v_preset.get())
        if not val:
            return
        v_url.set(val[0])
        v_model.set(val[1])
        v_fmt.set(val[2])
        v_auth.set(val[3])
        if val[3] == "none":
            v_key.set("")
        if "inceptionlabs" in val[0]:
            v_noreason.set(True)
        status.set("已套用预设（记得点「保存」）")

    def on_save():
        a = form_to_api()
        if a is None:
            return
        if not a.get("url"):
            messagebox.showwarning("缺少地址", "请填写 API 地址")
            return
        save_store(store)
        refresh(state["index"])
        status.set("已保存")

    def on_activate():
        a = form_to_api()
        if a is None:
            return
        if not a.get("url"):
            messagebox.showwarning("缺少地址", "请填写 API 地址")
            return
        if a.get("auth") != "none" and not a.get("key"):
            if not messagebox.askyesno("没有 Key",
                                       "这条 API 没有填 Key。\n本地服务可以继续；"
                                       "云端服务会失败。\n\n仍然继续？"):
                return
        store["active"] = a.get("name", "")
        save_store(store)
        try:
            p = write_plugin_config(a)
        except OSError as exc:
            messagebox.showerror("写入失败", f"{p}\n\n{exc}")
            return
        refresh(state["index"])
        status.set(f"已写入插件配置：{p}  —— 重启 PotPlayer 生效")
        messagebox.showinfo("完成",
                            f"已把「{a.get('name')}」写入：\n{p}\n\n"
                            "重启 PotPlayer 即可生效。")

    def on_open():
        d = config_dir()
        try:
            os.startfile(d)  # noqa: S606
        except OSError as exc:
            messagebox.showerror("打不开", str(exc))

    def on_autoadapt():
        """一键自动适配：Key / 协议 / 模型 全自动，不需要手工转换"""
        a = form_to_api()
        if a is None:
            a = blank_api(f"api{len(store['apis']) + 1}")
            store["apis"].append(a)
            save_store(store)
            state["index"] = len(store["apis"]) - 1
        if not a.get("url"):
            messagebox.showwarning("缺少地址", "至少要填 API 地址，其余交给自动适配")
            return

        status.set("正在自动适配（探测 Key / 协议 / 模型）…")
        root.update_idletasks()
        try:
            fixed, log = auto_adapt(a)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("自动适配出错", f"{type(exc).__name__}: {exc}")
            status.set("自动适配出错")
            return

        for k in ("format", "key", "model"):
            if fixed.get(k):
                a[k] = fixed[k]
        api_to_form(a)
        save_store(store)
        refresh(state["index"])
        status.set("自动适配完成" + ("" if a.get("key") else "（Key 未解决）"))
        messagebox.showinfo(
            "自动适配结果",
            "\n".join(log)
            + "\n\n最终配置：\n"
            + f"  协议: {a['format']}\n  模型: {a['model']}\n"
            + f"  Key : {(a['key'][:16] + '…') if a.get('key') else '(空)'}")

    def on_models():
        a = form_to_api() or blank_api()
        if not a.get("url"):
            messagebox.showwarning("缺少地址", "先填 API 地址，再获取模型")
            return
        status.set("正在获取模型列表…")
        root.update_idletasks()
        models, err = fetch_models(a)

        # 取不到就先自动适配一次再重试 —— 「不用我手动转换」的兜底
        if err:
            status.set("获取失败，先尝试自动适配…")
            root.update_idletasks()
            try:
                fixed, log = auto_adapt(a)
                for k in ("format", "key", "model"):
                    if fixed.get(k):
                        a[k] = fixed[k]
                api_to_form(a)
                save_store(store)
                models, err = fetch_models(a)
                if not err:
                    status.set(f"自动适配后取到 {len(models)} 个模型")
                    messagebox.showinfo("已自动适配后取到模型",
                                        "\n".join(log)
                                        + f"\n\n共 {len(models)} 个模型。")
            except Exception:  # noqa: BLE001
                pass

        if err:
            status.set("获取失败")
            messagebox.showerror("获取可用模型失败",
                                 dialog_text(err) + f"\n\n地址：{murl_of(a)}")
            return
        status.set(f"取到 {len(models)} 个模型")

        w = tk.Toplevel(root)
        w.title("可用模型 —— 双击选用")
        w.geometry("520x420")
        ttk.Label(w, text=f"共 {len(models)} 个，双击选中：").pack(anchor="w", padx=8, pady=6)
        lb = tk.Listbox(w, font=("Consolas", 10))
        lb.pack(fill="both", expand=True, padx=8)
        for m in models:
            lb.insert("end", m)
        if a.get("model") in models:
            lb.selection_set(models.index(a["model"]))
            lb.see(models.index(a["model"]))

        def choose(_evt=None):
            sel = lb.curselection()
            if sel:
                v_model.set(models[sel[0]])
                w.destroy()
                status.set("已选用 " + models[sel[0]] + "（记得点「保存」）")

        lb.bind("<Double-Button-1>", choose)
        bar = ttk.Frame(w)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="选用", command=choose).pack(side="left")
        ttk.Button(bar, text="关闭", command=w.destroy).pack(side="left", padx=6)

    def on_ping():
        a = form_to_api() or blank_api()
        if not a.get("url") or not a.get("model"):
            messagebox.showwarning("信息不全", "先填 API 地址与模型名")
            return
        status.set("正在测试…")
        root.update_idletasks()
        ok, msg = ping(a)
        status.set(msg)
        (messagebox.showinfo if ok else messagebox.showerror)(
            "测试连通性",
            msg + ("\n\n注意：PotPlayer 里的「测试」按钮会返回缓存结果，"
                   "改配置后要改一下测试文本。" if ok else "\n\n———————— 诊断建议 ————————\n"
                   + explain_error(msg)))

    # 首次运行：没有记录时，试着从插件配置导入一条
    if not store["apis"]:
        imported = import_plugin_config()
        if imported:
            store["apis"].append(imported)
            store["active"] = imported["name"]
            save_store(store)
            status.set("已从 deepseek_api.txt 导入 1 条配置")

    refresh(0)

    if auto_close_ms:
        # 打包后的 GUI 构造自检：真建出界面，短暂显示后自动关闭
        def _probe():
            try:
                n_api = len(store["apis"])
                print(f"GUI OK: 已构建界面，列表 {n_api} 条，"
                      f"Tk {tk.TkVersion}")
                print(f"配置目录: {config_dir()}")
                print("RESULT: PASS")
            finally:
                root.destroy()
        root.after(auto_close_ms, _probe)
    root.mainloop()
    return 0


# ==================================================================== 自检
def selftest():
    import tempfile
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {label}{('  ' + extra) if extra else ''}")
        if not cond:
            ok = False

    print("1) 配置串生成")
    a = blank_api("inception")
    a.update({"url": "https://api.inceptionlabs.ai/v1", "model": "mercury-2.5",
              "no_reasoning": True, "key": "sk-1"})
    spec = build_account_spec(a)
    check("含 url/model", "url=https://api.inceptionlabs.ai/v1" in spec
          and "model=mercury-2.5" in spec, spec)
    check("关闭推理写入 body", 'body="reasoning_effort":"none"' in spec)
    check("默认 format/auth 不写", "format=" not in spec and "auth=" not in spec)
    check("默认 maxtok 不写", "maxtok=" not in spec)

    b = blank_api("local")
    b.update({"url": "http://localhost:11434/v1", "model": "qwen2.5:7b",
              "auth": "none", "maxtok": 1024})
    sb = build_account_spec(b)
    check("auth=none 写出", "auth=none" in sb, sb)
    check("maxtok 非默认写出", "maxtok=1024" in sb)

    print("2) 端点推导")
    for base, fmt, want_chat, want_models in [
        ("https://api.deepseek.com/v1", "openai",
         "https://api.deepseek.com/v1/chat/completions", "https://api.deepseek.com/v1/models"),
        ("https://open.bigmodel.cn/api/paas/v4", "openai",
         "https://open.bigmodel.cn/api/paas/v4/chat/completions",
         "https://open.bigmodel.cn/api/paas/v4/models"),
        ("https://api.anthropic.com", "anthropic",
         "https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/models"),
        ("https://x.com/v1/chat/completions", "openai",
         "https://x.com/v1/chat/completions", "https://x.com/v1/models"),
        ("my-gw.com", "openai", "https://my-gw.com/v1/chat/completions",
         "https://my-gw.com/v1/models"),
    ]:
        c, m = resolve_urls(base, fmt)
        check(f"{base} ({fmt})", c == want_chat and m == want_models, f"{c} | {m}")

    print("3) 写文件 / 读回 / 导入")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "deepseek_api.txt")
        write_plugin_config(a, p)
        raw = open(p, encoding="utf-8").read()
        check("写出的文件含 account=", "account=" in raw)
        check("写出的文件含 key=", "key=sk-1" in raw)
        rec = import_plugin_config(p)
        check("能反向导入", rec is not None and rec["url"] == a["url"]
              and rec["model"] == a["model"] and rec["key"] == "sk-1"
              and rec["no_reasoning"] is True,
              str(rec))

        # 用户当前实际使用的那种写法
        p2 = os.path.join(td, "real.txt")
        with open(p2, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write("# 手写的\naccount=add=inception; preset=inception\nkey=sk_real\n")
        r2 = import_plugin_config(p2)
        check("导入 add=名字; preset=inception 写法",
              r2 is not None and r2["name"] == "inception"
              and r2["url"] == "https://api.inceptionlabs.ai/v1"
              and r2["model"] == "mercury-2.5"
              and r2["no_reasoning"] is True and r2["key"] == "sk_real",
              str(r2))

        sp = os.path.join(td, "store.json")
        st = default_store()
        st["apis"] = [a, b]
        st["active"] = "local"
        save_store(st, sp)
        back = load_store(sp)
        check("store 往返一致", len(back["apis"]) == 2 and back["active"] == "local")
        check("坏 JSON 不炸", load_store(os.path.join(td, "missing.json"))["apis"] == [])

    print("4) 请求头")
    h = build_headers(a)
    check("bearer", h.get("Authorization") == "Bearer sk-1")
    h2 = build_headers(b)
    check("auth=none 不发认证", "Authorization" not in h2 and "x-api-key" not in h2)
    c2 = blank_api()
    c2.update({"key": "sk-a", "format": "anthropic", "auth": "x-api-key"})
    h3 = build_headers(c2)
    check("anthropic 用 x-api-key + version",
          h3.get("x-api-key") == "sk-a" and h3.get("anthropic-version") == "2023-06-01")

    print("5) 报错诊断建议")
    cases = [
        ("ConnectionResetError: [WinError 10054]", "协议选错"),
        ("HTTP 401: invalid API key", "客户端 Key"),
        ("HTTP 403: MODEL_NOT_IN_PLAN", "套餐"),
        ("HTTP 401: missing Authorization header", "认证头"),
        ("reasoning_tokens", "推理"),
    ]
    for msg, want in cases:
        got = explain_error(msg)
        check(f"{msg[:34]!r} 有建议且提到「{want}」", want in got)
    check("无法识别的错误不硬编建议", explain_error("something weird") == "")

    print("6) models 端点展示")
    check("murl_of 正确", murl_of(a) == "https://api.inceptionlabs.ai/v1/models",
          murl_of(a))
    check("地址为空时不炸", murl_of(blank_api()) == "(地址为空)")

    print("7) 自动适配的判定逻辑")
    for url, host, port in [
        ("http://127.0.0.1:11434/v1", "127.0.0.1", 11434),
        ("https://api.deepseek.com/v1", "api.deepseek.com", 443),
        ("http://localhost:3000", "localhost", 3000),
        ("http://192.168.1.9:8080/v1", "192.168.1.9", 8080),
    ]:
        h, p = split_host_port(url)
        check(f"{url} -> {h}:{p}", h == host and p == port)
    for h, want in [("127.0.0.1", True), ("localhost", True), ("::1", True),
                    ("192.168.1.5", True), ("10.0.0.7", True),
                    ("api.deepseek.com", False), ("8.8.8.8", False)]:
        check(f"is_local_host({h}) == {want}", is_local_host(h) is want)

    ml = ["gpt-5.5", "deepseek/deepseek-v4-flash", "claude-opus-5"]
    check("pick_model 优先挑 flash", pick_model(ml) == "deepseek/deepseek-v4-flash",
          pick_model(ml))
    check("没有关键词时退回第一个", pick_model(["aaa", "bbb"]) == "aaa")
    check("空列表返回空串", pick_model([]) == "")

    print("8) 从网关配置里抠 Key")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "config.yaml")
        # 注意：这里只能用同形态的假值。夹具要贴近真实配置结构，
        # 但绝不能把真凭据写进仓库。
        demo_client = "ccgw-" + "0" * 48
        demo_upstream = "user_" + "A" * 90
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("api_keys:\n"
                     "    - name: dsh\n"
                     "      key: ccgw-demo-client-key\n"
                     "    - name: local\n"
                     f"      key: {demo_client}\n"
                     "commandcode:\n"
                     "    accounts:\n"
                     "        - name: main\n"
                     f"          api_key: {demo_upstream}\n")
        ks = keys_in_file(p)
        check("客户端 Key 与上游 Key 都被捞出来", len(ks) == 3, str(ks))
        check("客户端 Key 在前（config 里的书写顺序）", ks[0] == "ccgw-demo-client-key")
        check("能识别十六进制形态的客户端 Key", demo_client in ks)
        check("能识别 user_ 形态的上游 Key",
              any(k.startswith("user_") for k in ks))
        check("不存在的文件返回空", keys_in_file(os.path.join(td, "nope.yaml")) == [])

    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--print" in sys.argv:
        print("store      :", store_path())
        print("plugin cfg :", plugin_cfg_path())
        st = load_store()
        print(f"APIs       : {len(st['apis'])}  当前: {st.get('active') or '(无)'}")
        for x in st["apis"]:
            print(f"  - {x['name']}: {x['model']} @ {x['url']}")
        return 0
    try:
        return run_gui(auto_close_ms=1200 if "--guitest" in sys.argv else None)
    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        traceback.print_exc()
        print("RESULT: FAIL")

        # 把完整堆栈落盘，便于排查（GUI 程序没有控制台，光看弹窗没用）
        logp = ""
        try:
            logp = os.path.join(config_dir(), "api_manager_error.log")
            with open(logp, "w", encoding="utf-8") as fh:
                fh.write(APP_TITLE + "\n\n")
                fh.write(tb)
        except OSError:
            logp = ""

        try:
            import tkinter.messagebox as mb
            mb.showerror(
                APP_TITLE,
                "启动失败：\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                + (f"完整错误已写入：\n{logp}\n\n" if logp else "")
                + "把这段内容发给开发者即可定位。")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
