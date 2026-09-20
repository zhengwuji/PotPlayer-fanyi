#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_as.py — 无 AngelScript 编译器时的静态自检

检查项：
  1. 花括号 / 圆括号 / 方括号 是否配平
  2. 所有被调用的自定义函数是否都有定义
  3. 复刻 BuildRequest() / ParseAccountSpec() / ResolveUrl()，验证
     - 生成的 JSON 在极端输入下仍然合法
     - 预设解析、URL 归一化、双格式（openai / anthropic）行为正确
  4. 对照 PotPlayer 官方 Extension\\api.txt 校验 API 调用真实存在

第 3 项是关键：AngelScript 逻辑无法在本机运行，
只能靠 Python 逐行复刻来锁死行为，避免"看起来对"。
"""

import json
import os
import re
import sys

SRC = "SubtitleTranslate - DeepSeek.as"


# ========================================================== 1. 括号配平
def strip_noise(src: str) -> str:
    """去掉块注释、行注释和字符串字面量，避免其中的括号干扰计数。"""
    out = []
    i, n = 0, len(src)
    while i < n:
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j == -1 else j
        elif src[i] == '"':
            i += 1
            while i < n and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
            i += 1
            out.append('""')
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def check_balance(src: str) -> bool:
    code = strip_noise(src)
    stack, pairs = [], {")": "(", "]": "[", "}": "{"}
    ok = True
    for idx, ch in enumerate(code):
        if ch in "([{":
            stack.append((ch, idx))
        elif ch in ")]}":
            if not stack or stack[-1][0] != pairs[ch]:
                line = code[:idx].count("\n") + 1
                print(f"  [FAIL] 第 {line} 行出现不匹配的 '{ch}'")
                ok = False
            else:
                stack.pop()
    if stack:
        line = code[: stack[-1][1]].count("\n") + 1
        print(f"  [FAIL] 未闭合的 '{stack[-1][0]}'（第 {line} 行）")
        ok = False
    if ok:
        print("  [OK]   括号全部配平")
    return ok


# ============================================================== 2. 符号
def check_symbols(src: str) -> bool:
    code = strip_noise(src)
    defined = set(re.findall(
        r"^\s*(?:void|string|int|bool|array<\w+>)\s+(\w+)\s*\(", code, re.M))
    called = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", code))

    custom = {c for c in called if c[0].isupper() or c in
              {"Dbg", "Finalize", "CacheGet", "CachePut", "RememberPair",
               "StartPairIndex", "BuildRequest", "IsPermanentError", "LangName",
               "JsonEscape", "ServerLogin", "Translate", "OnInitialize", "OnFinalize",
               "GetTitle", "GetVersion", "GetDesc", "GetLoginTitle", "GetLoginDesc",
               "GetPasswordText", "GetSrcLangs", "GetDstLangs",
               "ApplyPreset", "ParseAccountSpec", "ResolveUrl", "CurrentUA",
               "BuildHeaders", "BuildSystemPrompt", "BackoffSleep"}}

    host_ok = {c for c in custom if c.startswith("Host") or c in
               {"JsonReader", "JsonValue", "array", "string", "int", "bool", "void"}}
    builtin_methods = {"Trim", "length", "empty", "find", "replace", "split",
                       "insertLast", "removeAt", "isArray", "isString", "asString",
                       "parse", "substr", "Left", "Right", "MakeLower", "MakeUpper",
                       "TrimLeft", "TrimRight", "findFirst", "findLast", "erase",
                       "insertAt", "size", "getKeys",
                       # PotPlayer 内建全局函数（api.txt 自带示例里用了 formatInt）
                       "formatInt"}
    missing = sorted(c for c in custom - defined - host_ok - builtin_methods
                     if not c.startswith("op"))

    if missing:
        print(f"  [WARN] 引用了未在文件内定义的函数: {', '.join(missing)}")
    else:
        print("  [OK]   所有自定义函数均有定义")
    return True


# ---------------------------------------------- 2b. int 隐式拼串检测
# api.txt 自己的示例是 HostMessageBox("ThreadFunction " + formatInt(num))，
# 说明 AngelScript 不会把 int 隐式转成 string。漏用 formatInt 会直接编译失败，
# 而肉眼看代码几乎发现不了。
NUMERIC_NAMES = {
    "retryCount", "delay", "maxRetries", "baseRetryDelay", "i", "j", "n", "m",
    "idx", "taken", "used", "cost", "cand", "left", "chunk", "ms", "start",
    "count", "rec", "len", "hit", "semi", "eq", "activeProfile", "DEBUG_LOG",
    "CACHE_SIZE", "MAX_CTX_SENTENCES", "MAX_CTX_BYTES", "MAX_OUTPUT_CHARS",
}


def check_concat(src: str) -> bool:
    code = strip_noise(src)
    # 先把 formatInt(...) 整体抹掉（含一层嵌套），剩下的裸数字才算错
    masked = re.sub(r"formatInt\s*\(([^()]|\([^()]*\))*\)", "FMT", code)

    # 类型消歧：形参/局部变量可能同名而类型不同，
    # 例如 Dbg(const string &in m) 的 m 是字符串，不是计数器。
    declared_string = set(re.findall(
        r"\bstring\s+(?:&\w+\s+)?(\w+)", code))

    def numeric(name: str) -> bool:
        return name in NUMERIC_NAMES and name not in declared_string

    bad = []
    for m in re.finditer(r"\+\s*([A-Za-z_]\w*)\s*(?=[+\),;]|$)", masked, re.M):
        if numeric(m.group(1)):
            bad.append(m.group(1))
    for m in re.finditer(r"([A-Za-z_]\w*)\s*\+\s*\"", masked):
        if numeric(m.group(1)):
            bad.append(m.group(1))
    for m in re.finditer(r"\+\s*[\w\[\]]+\.(?:length|size)\(\)", masked):
        bad.append(m.group(0).strip())

    bad = sorted(set(bad))
    if bad:
        print(f"  [FAIL] 数字被直接拼进字符串（必须包 formatInt）: {', '.join(bad)}")
        return False
    print("  [OK]   所有数字拼串都走了 formatInt")
    return True


# ================================ 3. 复刻 AngelScript 逻辑并验证行为
def json_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\r", "\\r")
             .replace("\t", "\\t").replace("\b", "\\b").replace("\f", "\\f"))


MAX_CTX_SENTENCES = 3
MAX_CTX_BYTES = 600

PRESETS = {
    "deepseek":    ("https://api.deepseek.com/v1", "deepseek-chat", "openai", "bearer"),
    "openai":      ("https://api.openai.com/v1", "gpt-4o-mini", "openai", "bearer"),
    "siliconflow": ("https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct", "openai", "bearer"),
    "moonshot":    ("https://api.moonshot.cn/v1", "moonshot-v1-8k", "openai", "bearer"),
    "zhipu":       ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", "openai", "bearer"),
    "qwen":        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo", "openai", "bearer"),
    "openrouter":  ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "openai", "bearer"),
    "groq":        ("https://api.groq.com/openai/v1", "llama-3.1-8b-instant", "openai", "bearer"),
    "gemini":      ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash", "openai", "bearer"),
    "anthropic":   ("https://api.anthropic.com", "claude-3-5-haiku-latest", "anthropic", "x-api-key"),
    "ollama":      ("http://localhost:11434/v1", "qwen2.5:7b", "openai", "none"),
    "lmstudio":    ("http://localhost:1234/v1", "local-model", "openai", "none"),
}


class Cfg:
    def __init__(self):
        self.base = ""
        self.model = ""
        self.fmt = ""
        self.auth = ""
        self.extra = ""
        self.ua = ""


def apply_preset(cfg: Cfg, name: str) -> None:
    """复刻 ApplyPreset()"""
    cfg.base = "https://api.deepseek.com/v1"
    cfg.model = "deepseek-chat"
    cfg.fmt = "openai"
    cfg.auth = "bearer"
    cfg.extra = ""
    if not name or name == "deepseek":
        return
    if name in PRESETS:
        b, m, f, a = PRESETS[name]
        cfg.base, cfg.model, cfg.fmt, cfg.auth = b, m, f, a


def parse_account_spec(cfg: Cfg, spec: str) -> str:
    """复刻 ParseAccountSpec()，返回 acctSpec"""
    apply_preset(cfg, "")
    spec = spec.strip()
    if not spec:
        return ""
    for tok in spec.split(";"):
        tok = tok.strip()
        if not tok:
            continue
        if "=" not in tok:
            cfg.base = tok
            continue
        k, v = tok.split("=", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "preset":
            apply_preset(cfg, v.lower())
        elif k in ("url", "base", "endpoint", "host"):
            cfg.base = v
        elif k == "model":
            cfg.model = v
        elif k == "format":
            cfg.fmt = v.lower()
        elif k == "auth":
            cfg.auth = v.lower()
        elif k in ("extra", "header"):
            cfg.extra = v
        elif k in ("ua", "useragent"):
            cfg.ua = v
    return spec


def resolve_url(cfg: Cfg) -> str:
    """复刻 ResolveUrl()"""
    u = cfg.base.strip() or "https://api.deepseek.com/v1"
    if "http" not in u:
        u = "https://" + u
    while u and u.endswith("/"):
        u = u[:-1]
    if "/chat/completions" in u:
        return u
    if "/messages" in u:
        return u
    has_version = any(v in u for v in ("/v1", "/v2", "/v3", "/v4"))
    if not has_version:
        u += "/v1"
    u += "/messages" if cfg.fmt == "anthropic" else "/chat/completions"
    return u


def lang_name(code: str) -> str:
    return {"zh-CN": "Simplified Chinese (简体中文)",
            "zh-TW": "Traditional Chinese (繁體中文)",
            "ja": "Japanese (日本語)"}.get(code, code)


def build_system_prompt(src: str, dst: str) -> str:
    sp = ("You are a professional subtitle translator. "
          "Translate ONLY the last user message into natural, fluent, colloquial subtitles. "
          "Use the earlier turns as context to keep terminology, character names and tone consistent, "
          "but never translate or repeat them. "
          "Rules: output exactly one line with no line breaks; "
          "do not add sentence-final punctuation such as . ! ? 。 ！ ？; "
          "keep necessary internal punctuation (commas, enumeration marks, dashes) so the line stays readable; "
          "do not merge or split sentences; do not add explanations, notes, quotes or the original text; "
          "for ambiguous terms pick the reading that best fits the context; "
          "keep the translation about as long as the source so it fits on screen. "
          f"Target language: {lang_name(dst)}.")
    if src:
        sp += f" Source language: {lang_name(src)}."
    return sp


def start_pair_index(pairs) -> int:
    """复刻 StartPairIndex()；长度按 UTF-8 字节算，与 AngelScript 一致"""
    def blen(s):
        return len(s.encode("utf-8"))

    if not pairs:
        return 0
    used = taken = 0
    idx = len(pairs)
    while idx > 0 and taken < MAX_CTX_SENTENCES:
        cand = idx - 1
        cost = blen(pairs[cand][0]) + blen(pairs[cand][1])
        if taken > 0 and used + cost > MAX_CTX_BYTES:
            break
        idx = cand
        used += cost
        taken += 1
    return idx


def build_request(cfg: Cfg, text, src, dst, pairs):
    """复刻 BuildRequest()，返回 (期望结构, 拼接原文)"""
    sp = build_system_prompt(src, dst)
    start = start_pair_index(list(pairs))
    sel = list(pairs)[start:]

    if cfg.fmt == "anthropic":
        msgs = []
        for s, d in sel:
            msgs.append({"role": "user", "content": s})
            msgs.append({"role": "assistant", "content": d})
        msgs.append({"role": "user", "content": text})
        expected = {"model": cfg.model, "system": sp,
                    "max_tokens": 256, "temperature": 0, "messages": msgs}

        raw = '{"model":"' + json_escape(cfg.model) + '",'
        raw += '"system":"' + json_escape(sp) + '",'
        raw += '"max_tokens":256,"temperature":0,"messages":['
        parts = []
        for s, d in sel:
            parts.append('{"role":"user","content":"' + json_escape(s) + '"}')
            parts.append('{"role":"assistant","content":"' + json_escape(d) + '"}')
        parts.append('{"role":"user","content":"' + json_escape(text) + '"}')
        raw += ",".join(parts) + "]}"
        return expected, raw

    msgs = [{"role": "system", "content": sp}]
    for s, d in sel:
        msgs.append({"role": "user", "content": s})
        msgs.append({"role": "assistant", "content": d})
    msgs.append({"role": "user", "content": text})
    expected = {"model": cfg.model, "messages": msgs,
                "max_tokens": 256, "temperature": 0}

    raw = '{"model":"' + json_escape(cfg.model) + '","messages":['
    raw += '{"role":"system","content":"' + json_escape(sp) + '"}'
    for s, d in sel:
        raw += ',{"role":"user","content":"' + json_escape(s) + '"}'
        raw += ',{"role":"assistant","content":"' + json_escape(d) + '"}'
    raw += ',{"role":"user","content":"' + json_escape(text) + '"}'
    raw += '],"max_tokens":256,"temperature":0}'
    return expected, raw


def check_config() -> bool:
    """预设解析 + URL 归一化"""
    print("  [配置解析]")
    ok = True
    cases = [
        ("（留空）", "", "https://api.deepseek.com/v1/chat/completions", "deepseek-chat", "openai"),
        ("裸 URL", "https://my-gw.com/v1", "https://my-gw.com/v1/chat/completions", "deepseek-chat", "openai"),
        ("裸域名", "my-gw.com", "https://my-gw.com/v1/chat/completions", "deepseek-chat", "openai"),
        ("preset=ollama", "preset=ollama", "http://localhost:11434/v1/chat/completions", "qwen2.5:7b", "openai"),
        ("preset=zhipu（/v4 不该补 /v1）", "preset=zhipu",
         "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4-flash", "openai"),
        ("preset=gemini（/v1beta/openai）", "preset=gemini",
         "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-2.0-flash", "openai"),
        ("preset=anthropic（改格式与认证）", "preset=anthropic",
         "https://api.anthropic.com/v1/messages", "claude-3-5-haiku-latest", "anthropic"),
        ("preset + 覆盖 model", "preset=siliconflow; model=deepseek-ai/DeepSeek-V3",
         "https://api.siliconflow.cn/v1/chat/completions", "deepseek-ai/DeepSeek-V3", "openai"),
        ("自定义 url + 额外头", "url=my-proxy.local/openai; model=gpt-4o; auth=raw; extra=X-Tenant:abc|X-Trace:1",
         "https://my-proxy.local/openai/v1/chat/completions", "gpt-4o", "openai"),
        ("已含完整路径", "https://x.com/v1/chat/completions", "https://x.com/v1/chat/completions",
         "deepseek-chat", "openai"),
    ]
    for label, spec, want_url, want_model, want_fmt in cases:
        cfg = Cfg()
        parse_account_spec(cfg, spec)
        got_url, got_model, got_fmt = resolve_url(cfg), cfg.model, cfg.fmt
        good = (got_url == want_url and got_model == want_model and got_fmt == want_fmt)
        if not good:
            print(f"    [FAIL] {label}\n           期望 {want_url} / {want_model} / {want_fmt}"
                  f"\n           实得 {got_url} / {got_model} / {got_fmt}")
            ok = False
        else:
            print(f"    [OK]   {label} -> {got_url}")
    return ok


PROF_SEP = "\u0001"
FIELD_SEP = "\u0002"


def parse_account_spec_full(cfg: Cfg, spec: str):
    """复刻 v0.6 的 ParseAccountSpec()，返回 (norm, spec_key)"""
    apply_preset(cfg, "")
    spec_key = ""
    s = spec.strip()
    if not s:
        return "", ""
    norm = ""
    for tok in s.split(";"):
        tok = tok.strip()
        if not tok:
            continue
        if "=" not in tok:
            cfg.base = tok
            norm += "url=" + tok + ";"
            continue
        k, v = tok.split("=", 1)
        k, kl, v = k.strip(), k.strip().lower(), v.strip()
        if kl == "key":
            spec_key = v
            continue
        if kl == "preset":
            apply_preset(cfg, v.lower())
        elif kl in ("url", "base", "endpoint", "host"):
            cfg.base = v
        elif kl == "model":
            cfg.model = v
        elif kl == "format":
            cfg.fmt = v.lower()
        elif kl == "auth":
            cfg.auth = v.lower()
        elif kl in ("extra", "header"):
            cfg.extra = v
        elif kl in ("ua", "useragent"):
            cfg.ua = v
        norm += k + "=" + v + ";"
    return norm, spec_key


class Store:
    """复刻 .as 里的 profile 存取与 HandleAccountSpec() 分支"""

    def __init__(self):
        self.names, self.specs, self.keys = [], [], []
        self.cfg = Cfg()
        self.key = ""
        self.active = -1
        self.box = None

    def blob(self):
        return PROF_SEP.join(
            f"{n}{FIELD_SEP}{s}{FIELD_SEP}{k}"
            for n, s, k in zip(self.names, self.specs, self.keys))

    def load(self, blob):
        self.names, self.specs, self.keys = [], [], []
        if not blob:
            return
        for rec in blob.split(PROF_SEP):
            if not rec:
                continue
            f = rec.split(FIELD_SEP)
            if len(f) < 2:
                continue
            self.names.append(f[0])
            self.specs.append(f[1])
            self.keys.append(f[2] if len(f) > 2 else "")

    def find(self, name):
        w = name.strip().lower()
        if not w:
            return -1
        for i, n in enumerate(self.names):
            if n.lower() == w:
                return i
        return -1

    def handle(self, spec, pass_key, show_ui):
        s = spec.strip()
        if not s:
            parse_account_spec_full(self.cfg, "")
            self.active = -1
            _, k = parse_account_spec_full(self.cfg, "")
            self.key = k or pass_key.strip()
            return
        low = s.lower()

        if low in ("list", "?", "help"):
            if show_ui:
                self.box = "list"
            return

        if low.startswith("del="):
            nm = s[4:].strip()
            idx = self.find(nm)
            if idx >= 0:
                for arr in (self.names, self.specs, self.keys):
                    arr.pop(idx)
                if self.active == idx:
                    self.active = -1
                elif self.active > idx:
                    self.active -= 1
                self.box = "deleted"
            else:
                self.box = "notfound"
            return

        if low.startswith("add="):
            parts = s.split(";")
            head = parts[0].strip()
            name = head[4:].strip()
            rest = ";".join(p.strip() for p in parts[1:] if p.strip())
            if not name:
                self.box = "needname"
                return
            norm, sk = parse_account_spec_full(self.cfg, rest)
            k = sk or pass_key.strip()
            idx = self.find(name)
            if idx >= 0:
                self.specs[idx] = norm
                self.keys[idx] = k
            else:
                self.names.append(name)
                self.specs.append(norm)
                self.keys.append(k)
                idx = len(self.names) - 1
            self.active = idx
            self.key = k
            self.box = "saved"
            return

        nm = s
        if low.startswith("use="):
            nm = s[4:].strip()
        hit = self.find(nm)
        if hit >= 0:
            self.active = hit
            parse_account_spec_full(self.cfg, self.specs[hit])
            k = pass_key.strip()
            if k:
                self.keys[hit] = k
            else:
                k = self.keys[hit]
            self.key = k
            self.box = "active"
            return

        _, sk = parse_account_spec_full(self.cfg, s)
        self.active = -1
        self.key = sk or pass_key.strip()


def check_profiles() -> bool:
    print("  [多 API 管理]")
    ok = True
    st = Store()

    # 1. 新增两个 API
    st.handle("add=zhipu; preset=zhipu; model=glm-4-flash", "sk-zhipu-1", True)
    if st.box != "saved" or st.names != ["zhipu"] or st.key != "sk-zhipu-1":
        print(f"    [FAIL] 第一个 API 保存失败: names={st.names} key={st.key}")
        ok = False
    else:
        print(f"    [OK]   add=zhipu -> 已保存，模型 {st.cfg.model}")

    st.handle("add=local; preset=ollama", "", True)
    if st.names != ["zhipu", "local"] or st.key != "":
        print(f"    [FAIL] 第二个 API 保存失败: names={st.names} key={st.key}")
        ok = False
    else:
        print(f"    [OK]   add=local -> 已保存，auth={st.cfg.auth}（本地服务免 Key）")

    # 2. 落盘/读回（往返一致性，这是 HostSaveString 的实际行为）
    blob = st.blob()
    st2 = Store()
    st2.load(blob)
    if st2.names != st.names or st2.specs != st.specs or st2.keys != st.keys:
        print("    [FAIL] 序列化往返不一致")
        ok = False
    else:
        print(f"    [OK]   序列化往返一致（{len(blob)} 字符，含不可见分隔符）")

    # 3. 切换：只写名字
    st.handle("local", "", True)
    if st.active != 1 or st.cfg.auth != "none":
        print(f"    [FAIL] 直接写名字切换失败: active={st.active} auth={st.cfg.auth}")
        ok = False
    else:
        print(f"    [OK]   写名字 'local' 切换成功（免 Key 生效）")

    # 4. 切换：use= 形式 + 带新 Key 覆盖
    st.handle("use=zhipu", "", True)
    if st.active != 0 or st.key != "sk-zhipu-1":
        print(f"    [FAIL] use= 切换失败: active={st.active} key={st.key}")
        ok = False
    else:
        print(f"    [OK]   use=zhipu 切换成功，取回已存 Key {st.key}")

    st.handle("use=zhipu", "sk-rotated", True)
    if st.keys[0] != "sk-rotated" or st.key != "sk-rotated":
        print(f"    [FAIL] 密码栏更新 Key 未写回 profile: {st.keys[0]}")
        ok = False
    else:
        print("    [OK]   密码栏输入新 Key 会写回该 API")

    # 5. key= 直接写进配置串
    st.handle("add=inline; preset=groq; key=sk-inline", "", True)
    if st.keys[-1] != "sk-inline":
        print(f"    [FAIL] key= 未生效: {st.keys[-1]}")
        ok = False
    else:
        print("    [OK]   key= 写在配置串里同样生效")

    # 6. 删除
    st.handle("del=inline", "", True)
    if "inline" in st.names or st.box != "deleted":
        print(f"    [FAIL] 删除失败: {st.names}")
        ok = False
    else:
        print(f"    [OK]   del=inline 删除成功，剩余 {st.names}")

    # 7. 一次性配置（v0.5 兼容）不落库
    before = list(st.names)
    st.handle("https://my-gw.com/v1", "sk-gw", True)
    if st.names != before or st.active != -1 or st.cfg.base != "https://my-gw.com/v1":
        print(f"    [FAIL] 一次性配置行为不对: names={st.names} active={st.active}")
        ok = False
    else:
        print("    [OK]   裸 URL 仍是一次性配置，不污染已存列表")

    # 8. 名字大小写不敏感
    st.handle("USE=ZHIPU", "", True)
    if st.active != 0:
        print(f"    [FAIL] 大小写不敏感匹配失败: active={st.active}")
        ok = False
    else:
        print("    [OK]   use=ZHIPU 大小写不敏感")

    return ok


def resolve_models_url(cfg: Cfg) -> str:
    """复刻 ResolveModelsUrl()"""
    u = (cfg.base or "https://api.deepseek.com/v1").strip()
    if "http" not in u:
        u = "https://" + u
    for suffix in ("/chat/completions", "/messages"):
        cut = u.find(suffix)
        if cut != -1:
            u = u[:cut]
    while u and u.endswith("/"):
        u = u[:-1]
    if not any(v in u for v in ("/v1", "/v2", "/v3", "/v4")):
        u += "/v1"
    return u + "/models"


def parse_small_int(s: str):
    """复刻 ParseSmallInt()：只认 1..999"""
    t = s.strip()
    if not t:
        return -1
    for i in range(1, 1000):
        if str(i) == t:
            return i
    return -1


def parse_models_payload(root):
    """复刻 FetchModels() 的两形状兼容：data[].id 与 models[].id/name"""
    arr = root.get("data")
    if not isinstance(arr, list):
        arr = root.get("models")
    if not isinstance(arr, list):
        return []
    out = []
    for it in arr:
        if isinstance(it, dict):
            if isinstance(it.get("id"), str):
                out.append(it["id"])
            elif isinstance(it.get("name"), str):
                out.append(it["name"])
    return out


def check_models() -> bool:
    print("  [可用模型获取]")
    ok = True

    # 1. models 端点推导
    cases = [
        ("deepseek 官方", "", "https://api.deepseek.com/v1/models"),
        ("preset=ollama", "preset=ollama", "http://localhost:11434/v1/models"),
        ("preset=zhipu（/v4 不加 /v1）", "preset=zhipu",
         "https://open.bigmodel.cn/api/paas/v4/models"),
        ("preset=gemini", "preset=gemini",
         "https://generativelanguage.googleapis.com/v1beta/openai/models"),
        ("preset=anthropic（去掉 /messages）", "preset=anthropic",
         "https://api.anthropic.com/v1/models"),
        ("已写完整对话路径要能剥掉", "https://x.com/v1/chat/completions",
         "https://x.com/v1/models"),
        ("裸域名", "my-gw.com", "https://my-gw.com/v1/models"),
        ("末尾带斜杠", "https://y.com/v1/", "https://y.com/v1/models"),
    ]
    for label, spec, want in cases:
        cfg = Cfg()
        parse_account_spec_full(cfg, spec)
        got = resolve_models_url(cfg)
        if got != want:
            print(f"    [FAIL] {label}: 期望 {want} 实得 {got}")
            ok = False
        else:
            print(f"    [OK]   {label} -> {got}")

    # 2. 响应体两形状兼容
    shapes = [
        ("OpenAI 形状", {"object": "list", "data": [
            {"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]},
         ["deepseek-chat", "deepseek-reasoner"]),
        ("Ollama 形状", {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3:8b"}]},
         ["qwen2.5:7b", "llama3:8b"]),
        ("混合/缺字段", {"data": [{"id": "a"}, {"nope": 1}, {"id": "b"}]}, ["a", "b"]),
        ("没有数组", {"error": {"message": "nope"}}, []),
    ]
    for label, payload, want in shapes:
        got = parse_models_payload(payload)
        if got != want:
            print(f"    [FAIL] {label}: 期望 {want} 实得 {got}")
            ok = False
        else:
            print(f"    [OK]   {label} -> {got}")

    # 3. 序号解析
    for text, want in [("1", 1), ("12", 12), ("999", 999), ("0", -1), ("1000", -1), ("", -1)]:
        got = parse_small_int(text)
        if got != want:
            print(f"    [FAIL] ParseSmallInt('{text}') 期望 {want} 实得 {got}")
            ok = False
    print("    [OK]   序号解析 1..999 行为正确")

    # 4. model=@N 取值
    cfg = Cfg()
    apply_preset(cfg, "siliconflow")
    model_list = ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-7B-Instruct", "glm-4-flash"]
    for spec, want in [("model=@1", "deepseek-ai/DeepSeek-V3"),
                       ("model=@3", "glm-4-flash"),
                       ("model=@9", "Qwen/Qwen2.5-7B-Instruct"),   # 越界 → 保持预设模型
                       ("model=Qwen/Qwen2.5-32B", "Qwen/Qwen2.5-32B")]:
        cfg2 = Cfg()
        apply_preset(cfg2, "siliconflow")
        for tok in spec.split(";"):
            k, _, v = tok.partition("=")
            if k.strip() == "model":
                if v.startswith("@"):
                    pick = parse_small_int(v[1:])
                    if 1 <= pick <= len(model_list):
                        cfg2.model = model_list[pick - 1]
                else:
                    cfg2.model = v
        if cfg2.model != want:
            print(f"    [FAIL] {spec}: 期望 {want} 实得 {cfg2.model}")
            ok = False
        else:
            print(f"    [OK]   {spec} -> {cfg2.model}")

    return ok


def check_json() -> bool:
    ok = True
    print("  [OpenAI 兼容格式]")
    cfg = Cfg()
    parse_account_spec(cfg, "")
    cases = [
        ("普通中文", "我们要去哪里", "zh-CN", []),
        ("含双引号", 'He said "run!" loudly', "zh-CN", []),
        ("含反斜杠", r"C:\Users\test\file.txt", "zh-CN", []),
        ("含制表与换行", "line1\tline2\nline3", "zh-CN", []),
        ("两行上下文", "第三条字幕", "zh-CN",
         [("第一条字幕", "第一行译文"), ("第二条字幕", "第二行译文")]),
        ("六行历史（应只取后3）", "第七条", "zh-CN",
         [(f"第{i}条", f"译文{i}") for i in range(1, 7)]),
        ("超长单行触发字节上限", "结尾", "zh-CN",
         [("あ" * 400, "イ" * 400), ("短句", "短译")]),
        ("RTL 目标语言", "hello", "ar", []),
        ("模型名含斜杠", "test", "zh-CN", []),
    ]
    for item in cases:
        name, text, dst, pairs = item
        if name == "模型名含斜杠":
            parse_account_spec(cfg, "preset=siliconflow")   # 模型名 Qwen/Qwen2.5-7B-Instruct
        try:
            expected, raw = build_request(cfg, text, "", dst, pairs)
            parsed = json.loads(raw)
            assert parsed == expected, "拼接结果与预期结构不一致"
        except Exception as exc:  # noqa: BLE001
            print(f"    [FAIL] {name}: {exc}")
            ok = False
            continue
        roles = [m["role"] for m in parsed["messages"]]
        n_pairs = (len(roles) - 2) // 2
        print(f"    [OK]   {name}: messages={len(roles)} 上下文对={n_pairs} "
              f"上行字节={len(raw.encode('utf-8'))}")
        if name.startswith("六行") and n_pairs != 3:
            print("    [FAIL] 上下文没有按 MAX_CTX_SENTENCES 截断")
            ok = False
        if name.startswith("超长单行") and n_pairs != 1:
            print(f"    [FAIL] 字节上限未生效：期望 1 条，实得 {n_pairs} 条")
            ok = False

    print("  [Anthropic 格式]")
    acfg = Cfg()
    parse_account_spec(acfg, "preset=anthropic")
    for name, text, pairs in [
        ("无上下文", "hello world", []),
        ("两行上下文", "第三条", [("一", "one"), ("二", "two")]),
        ("含引号与换行", 'say "hi"\nnext', [("之前", "before")]),
    ]:
        try:
            expected, raw = build_request(acfg, text, "", "zh-CN", pairs)
            parsed = json.loads(raw)
            assert parsed == expected, "拼接结果与预期结构不一致"
        except Exception as exc:  # noqa: BLE001
            print(f"    [FAIL] {name}: {exc}")
            ok = False
            continue
        assert "system" in parsed and isinstance(parsed["system"], str)
        assert all(m["role"] != "system" for m in parsed["messages"]), \
            "Anthropic 的 messages 里不能出现 system 角色"
        print(f"    [OK]   {name}: messages={len(parsed['messages'])} "
              f"system 顶层={len(parsed['system'])} 字符")
    return ok


# ==================================== 4. 对照 PotPlayer 官方 API 文档
API_DOC = r"C:\Program Files\DAUM\PotPlayer\Extension\api.txt"


def check_api() -> bool:
    if not os.path.exists(API_DOC):
        print(f"  [SKIP] 未找到 {API_DOC}")
        return True

    with open(API_DOC, "r", encoding="utf-8", errors="ignore") as fh:
        doc = fh.read()
    documented_host = set(re.findall(r"\b(Host\w+)\s*\(", doc))

    with open(SRC, "r", encoding="utf-8") as fh:
        code = strip_noise(fh.read())

    used_host = set(re.findall(r"\b(Host\w+)\s*\(", code))
    unknown_host = sorted(used_host - documented_host)

    print(f"  Host* 调用 {len(used_host)} 个，api.txt 收录 {len(documented_host)} 个")
    if unknown_host:
        print(f"  [FAIL] 调用了 api.txt 里不存在的方法: {', '.join(unknown_host)}")
    else:
        print("  [OK]   所有 Host* 调用都在 api.txt 中")

    # substr 曾被误判为不存在 —— 官方 google.as 自己就用了 substr(start, count)，
    # 所以它确实可用。这里只做提示，不再当作错误。
    if ".substr(" in code:
        print("  [NOTE] 使用了 substr()（google.as 证实可用；本脚本已统一改用 Left）")

    return not unknown_host


if __name__ == "__main__":
    with open(SRC, "r", encoding="utf-8") as fh:
        source = fh.read()

    print("1) 括号配平")
    a = check_balance(source)
    print("2) 函数引用")
    check_symbols(source)
    print("2b) 数字拼串（必须走 formatInt）")
    d = check_concat(source)
    print("3) 复刻逻辑验证")
    b = check_config()
    b = check_profiles() and b
    b = check_models() and b
    b = check_json() and b
    print("4) 对照 PotPlayer 官方 API 文档 (api.txt)")
    c = check_api()

    print()
    ok = a and b and c and d
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
