#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_subtitle_tool.py — 智能 AI 字幕翻译与视频字幕合成大师
【已有字幕文件直接翻译】+【无字幕视频提取转写并翻译】+【字幕无损/压制合成到视频】+【自定义 AI API 与模型在线拉取】

功能特性：
1. 【字幕文件直接翻译】：支持 .srt / .vtt / .ass 等外文字幕文件，一键分块批量调用大模型极速翻译为中文或双语字幕。
2. 【无字幕音视频一键生字幕】：利用 ffmpeg 抽取无损音轨 + 本地 CUDA 显卡加速 faster-whisper 极速提取时间轴，大模型批量翻译生成 .srt。
3. 【字幕合成到视频】：
   - 极速内嵌软字幕（Soft Subtitle）：无损流复制封装，1~2秒极速完成，播放器可自由开关切换字幕；
   - 硬字幕压制（Hard Subtitle Burn-in）：支持 NVIDIA NVENC 显卡硬件加速，将字幕直接烧录烙印至视频画面中。
4. 【彻底解决 10054 报错】：分块批量合并翻译（Batch Translation），并发与请求量缩减 95%，配合指数退避重连机制，稳定顺畅不中断。
5. 【内置全功能 AI API 管理】：自由添加修改任意 OpenAI 兼容 API，一键在线探测拉取可用模型列表并自由下拉选择。
"""

import os
import sys
import re
import json
import time
import math
import subprocess
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    from jet_hub_dialog import JetHubDialog
    from jet_hub_service import JetHubAccountManager, PROVIDERS_INFO, BuddyClient
    HAS_JET_HUB = True
except Exception as _je:
    HAS_JET_HUB = False

# 设置 HuggingFace 加速与警告屏蔽
os.environ["HF_ENDPOINT"] = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"

# 全局运行日志文件路径与写入器
LOG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_runtime.log")

def write_runtime_log(msg):
    """同时写入磁盘日志文件，确保异常和调用过程全量留痕"""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 全球主流常用语言选项表 (中文名 + 代码)
# ---------------------------------------------------------------------------
LANGUAGE_OPTIONS = [
    ("自动识别 (Auto Detect)", "auto"),
    ("日语 (ja / 日本語)", "ja"),
    ("英语 (en / English)", "en"),
    ("韩语 (ko / 한국어)", "ko"),
    ("中文/普通话 (zh / 汉语)", "zh"),
    ("粤语 (yue / 廣東話)", "zh"),
    ("法语 (fr / Français)", "fr"),
    ("德语 (de / Deutsch)", "de"),
    ("西班牙语 (es / Español)", "es"),
    ("俄语 (ru / Русский)", "ru"),
    ("意大利语 (it / Italiano)", "it"),
    ("葡萄牙语 (pt / Português)", "pt"),
    ("泰语 (th / ภาษาไทย)", "th"),
    ("越南语 (vi / Tiếng Việt)", "vi"),
    ("印尼语 (id / Bahasa Indonesia)", "id"),
    ("阿拉伯语 (ar / العربية)", "ar"),
    ("土耳其语 (tr / Türkçe)", "tr"),
    ("印地语 (hi / हिन्दी)", "hi"),
    ("乌克兰语 (uk / Українська)", "uk"),
    ("波兰语 (pl / Polski)", "pl"),
    ("荷兰语 (nl / Nederlands)", "nl"),
    ("瑞典语 (sv / Svenska)", "sv"),
    ("希腊语 (el / Ελληνικά)", "el"),
    ("希伯来语 (he / עברית)", "he"),
    ("捷克语 (cs / Čeština)", "cs"),
    ("罗马尼亚语 (ro / Română)", "ro"),
    ("匈牙利语 (hu / Magyar)", "hu"),
    ("丹麦语 (da / Dansk)", "da"),
    ("挪威语 (no / Norsk)", "no"),
    ("芬兰语 (fi / Suomi)", "fi"),
    ("波斯语 (fa / فارسی)", "fa"),
    ("马来语 (ms / Bahasa Melayu)", "ms"),
    ("菲律宾语 (tl / Tagalog)", "tl"),
]

TARGET_LANGUAGE_OPTIONS = [
    ("简体中文 (zh-CN)", "zh-CN"),
    ("繁体中文 (zh-TW)", "zh-TW"),
    ("英语 (en / English)", "en"),
    ("日语 (ja / 日本語)", "ja"),
    ("韩语 (ko / 한국어)", "ko"),
    ("法语 (fr / Français)", "fr"),
    ("德语 (de / Deutsch)", "de"),
    ("西班牙语 (es / Español)", "es"),
    ("俄语 (ru / Русский)", "ru"),
]


def lang_label_to_code(val):
    for label, code in LANGUAGE_OPTIONS:
        if val == label or val == code:
            return code
    return "auto"


def target_lang_label_to_code(val):
    for label, code in TARGET_LANGUAGE_OPTIONS:
        if val == label or val == code:
            return code
    return "zh-CN"


# ---------------------------------------------------------------------------
# 配置文件与外部工具探测
# ---------------------------------------------------------------------------
LOCAL_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apis_config.json")
FALLBACK_CONFIG_PATHS = [
    os.path.join(os.environ.get("APPDATA", ""), "PotPlayerMini64", "deepseek_apis.json"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "PotPlayerMini64", "deepseek_apis.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "deepseek_apis.json"),
]

FFMPEG_CANDIDATES = [
    r"E:\AI\AI qu ren sheng\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\DAUM\PotPlayer\ffmpeg.exe",
    "ffmpeg.exe",
    "ffmpeg",
]


def find_ffmpeg():
    for cand in FFMPEG_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    import shutil
    p = shutil.which("ffmpeg")
    return p or "ffmpeg"


def detect_nvidia_gpus():
    """
    自动探查系统中所有可用的 NVIDIA GPU 型号与显存
    支持多显卡枚举（如 RTX 5090, RTX 5060 Ti 等）
    """
    gpus = []
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="ignore"
        )
        if res.returncode == 0:
            for line in res.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    idx = int(parts[0])
                    name = parts[1]
                    mem = parts[2] if len(parts) > 2 else ""
                    gpus.append({"index": idx, "name": name, "memory": mem})
    except Exception:
        pass
    return gpus


def get_config_file_path():
    """优先使用本地目录的 apis_config.json，自动继承已有旧配置或示例模板"""
    if os.path.isfile(LOCAL_CONFIG_FILE):
        return LOCAL_CONFIG_FILE
    example_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apis_config.example.json")
    if os.path.isfile(example_path):
        try:
            with open(example_path, "r", encoding="utf-8") as fh:
                c = fh.read()
            with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8") as fh:
                fh.write(c)
            return LOCAL_CONFIG_FILE
        except Exception:
            pass
    for p in FALLBACK_CONFIG_PATHS:
        if os.path.isfile(p):
            try:
                # 复制迁移一份到本地专用文件
                with open(p, "r", encoding="utf-8") as fh:
                    c = fh.read()
                with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8") as fh:
                    fh.write(c)
                return LOCAL_CONFIG_FILE
            except Exception:
                return p
    return LOCAL_CONFIG_FILE


def load_all_apis_config():
    """读取所有 API 配置"""
    cfg_path = get_config_file_path()
    default_cfg = {
        "apis": [
            {
                "name": "localcmdcode2api",
                "url": "http://127.0.0.1:11434/v1",
                "model": "deepseek/deepseek-v4.1-flash",
                "format": "openai",
                "auth": "bearer",
                "key": "your_api_key_here",
                "note": "本地网关"
            }
        ],
        "active": "localcmdcode2api"
    }
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "apis" in data:
                return data
        except Exception:
            pass
    return default_cfg


def save_all_apis_config(cfg_dict):
    """保存 API 配置到本地与系统共享目录"""
    cfg_path = get_config_file_path()
    os.makedirs(os.path.dirname(os.path.abspath(cfg_path)), exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg_dict, fh, ensure_ascii=False, indent=2)

    # 同步回写一份到系统中（若存在），保持兼容
    for p in FALLBACK_CONFIG_PATHS:
        if os.path.isfile(p) and os.path.abspath(p) != os.path.abspath(cfg_path):
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    json.dump(cfg_dict, fh, ensure_ascii=False, indent=2)
            except Exception:
                pass


def get_active_api_config(cfg_dict=None):
    if cfg_dict is None:
        cfg_dict = load_all_apis_config()
    apis = cfg_dict.get("apis", [])
    active_name = cfg_dict.get("active", "")
    for a in apis:
        if a.get("name") == active_name:
            return a
    if apis:
        return apis[0]
    return {
        "name": "默认API",
        "url": "http://127.0.0.1:11434/v1",
        "model": "deepseek/deepseek-v4.1-flash",
        "auth": "bearer",
        "key": ""
    }


def fetch_remote_models(base_url, api_key="", auth_type="bearer", timeout=8):
    """
    在线探测拉取可用模型列表
    兼容标准 OpenAI 格式 (data[].id) 及 Ollama 格式 (models[].name)
    """
    u = (base_url or "").strip().rstrip("/")
    if not u:
        u = "http://127.0.0.1:11434/v1"
    if not u.endswith("/models"):
        u += "/models"

    headers = {
        "User-Agent": "SubtitleStudio/2.0",
        "Accept": "application/json",
    }
    if api_key and auth_type != "none":
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = requests.get(u, headers=headers, timeout=timeout)
        data = resp.json()
    except Exception as exc:
        write_runtime_log(f"[拉取模型异常] URL: {u} | 异常: {exc}")
        return []

    models = []
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], list):
            for m in data["data"]:
                mid = m.get("id") if isinstance(m, dict) else str(m)
                if mid:
                    models.append(mid)
        elif "models" in data and isinstance(data["models"], list):
            for m in data["models"]:
                mid = m.get("name") if isinstance(m, dict) else str(m)
                if mid:
                    models.append(mid)
    elif isinstance(data, list):
        for m in data:
            mid = m.get("id") if isinstance(m, dict) else str(m)
            if mid:
                models.append(mid)

    res_models = sorted(list(set(models)))
    write_runtime_log(f"[拉取模型成功] URL: {u} | 获得 {len(res_models)} 款模型")
    return res_models


# ---------------------------------------------------------------------------
# 大模型翻译引擎 (requests 连接池 + 分块批量合并 + 详细运行日志)
# ---------------------------------------------------------------------------
class LlmTranslator:
    def __init__(self, api_cfg, log_callback=None):
        self.log_callback = log_callback or (lambda x: None)
        self.cache = {}
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=5, pool_maxsize=10)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.update_config(api_cfg)

    def update_config(self, api_cfg):
        self.cfg = api_cfg
        self.url = self._resolve_url(api_cfg.get("url", ""))
        self.model = api_cfg.get("model", "deepseek/deepseek-v4.1-flash")
        self.key = api_cfg.get("key", "")
        self.auth = api_cfg.get("auth", "bearer")

    def _resolve_url(self, raw):
        u = (raw or "").strip().rstrip("/")
        if not u:
            u = "http://127.0.0.1:11434/v1"
        if not u.endswith("/chat/completions"):
            u += "/chat/completions"
        return u

    def _send_request(self, payload, timeout=60, max_retries=4):
        # 若是 Jet Hub 方案，直接调用 Jet Hub 账号池进行自动轮换与推理
        if self.cfg.get("format") == "jethub" and HAS_JET_HUB:
            provider = self.cfg.get("provider", "buddy")
            mgr = JetHubAccountManager()
            for attempt in range(1, max_retries + 1):
                t0 = time.time()
                acc = mgr.get_available_account(provider)
                if not acc:
                    err_msg = f"[JetHub提示] 渠道 {provider} 暂无已启用的有效账号，请在 Jet Hub 面板中启用账号"
                    self.log_callback(err_msg)
                    write_runtime_log(err_msg)
                    return None

                cred = acc.get("credential", {})
                headers = BuddyClient.build_headers(cred, provider)
                product = PROVIDERS_INFO.get(provider, PROVIDERS_INFO.get("buddy", {}))
                target_url = f"{product.get('endpoint', 'https://copilot.tencent.com')}/v2/chat/completions"
                try:
                    jh_payload = dict(payload)
                    jh_payload["stream"] = True
                    resp = self.session.post(target_url, json=jh_payload, headers=headers, stream=True, timeout=timeout)
                    dt = time.time() - t0
                    status = resp.status_code
                    if status == 200:
                        content_chunks = []
                        for line in resp.iter_lines():
                            if line:
                                dec = line.decode("utf-8", errors="ignore").strip()
                                if dec.startswith("data:"):
                                    data_body = dec[5:].strip()
                                    if data_body == "[DONE]":
                                        break
                                    try:
                                        chunk_json = json.loads(data_body)
                                        delta = chunk_json.get("choices", [{}])[0].get("delta", {})
                                        c = delta.get("content", "")
                                        if c:
                                            content_chunks.append(c)
                                    except Exception:
                                        pass
                        content = "".join(content_chunks).strip()
                        write_runtime_log(f"[JetHub请求成功] 渠道: {provider} | 账号: {acc.get('nickname')} | 耗时: {dt:.2f}s | 译文长度: {len(content)}")
                        return content
                    elif status == 429:
                        mgr.mark_rate_limited(acc, cooldown_sec=60)
                        self.log_callback(f"[JetHub轮换] 账号 [{acc.get('nickname')}] 触发限流，自动避让并切换下一个账号...")
                        time.sleep(1.0)
                        continue
                    else:
                        err_preview = resp.text[:160].replace("\r", " ").replace("\n", " ")
                        write_runtime_log(f"[JetHub接口报错] 账号: {acc.get('nickname')} | HTTP {status} | 详情: {err_preview}")
                        if attempt < max_retries:
                            time.sleep(attempt * 1.5)
                        else:
                            return None
                except Exception as exc:
                    dt = time.time() - t0
                    err_str = str(exc).replace("\r", " ").replace("\n", " ")
                    write_runtime_log(f"[JetHub连接异常] 耗时: {dt:.2f}s | 详情: {err_str}")
                    if attempt < max_retries:
                        time.sleep(attempt * 2.0)
                    else:
                        return None
            return None

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "SubtitleStudio/2.0",
        }
        if self.key and self.auth != "none":
            headers["Authorization"] = f"Bearer {self.key}"

        model_name = payload.get("model", "")
        msg_count = len(payload.get("messages", []))
        write_runtime_log(f"[LLM发起请求] 目标: {self.url} | 模型: {model_name} | 消息段数: {msg_count}")

        for attempt in range(1, max_retries + 1):
            t0 = time.time()
            try:
                resp = self.session.post(self.url, json=payload, headers=headers, timeout=timeout)
                dt = time.time() - t0
                status = resp.status_code
                if status == 200:
                    res_data = resp.json()
                    choices = res_data.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        content = msg.get("content", "").strip()
                        reasoning = msg.get("reasoning_content", "")
                        finish_reason = choices[0].get("finish_reason", "")
                        write_runtime_log(f"[LLM请求成功] 耗时: {dt:.2f}s | finish_reason: {finish_reason} | 思考字符数: {len(reasoning)} | 译文长度: {len(content)}")
                        if attempt > 1:
                            self.log_callback(f"[续接成功] 接口已恢复响应，继续高速翻译推进！")
                        if not content and "text" in choices[0]:
                            content = choices[0].get("text", "").strip()
                        return content
                    write_runtime_log(f"[LLM警告] 返回 choices 为空: {resp.text[:200]}")
                    return ""
                else:
                    err_preview = resp.text[:200].replace("\r", " ").replace("\n", " ")
                    write_runtime_log(f"[LLM接口报错] 状态码: HTTP {status} | 耗时: {dt:.2f}s | 详情: {err_preview}")
                    is_ratelimit = ("rate limit" in err_preview.lower()) or (status in (429, 502))
                    if attempt < max_retries:
                        wait_sec = max(attempt * 2.5, 3.0) if is_ratelimit else (attempt * 1.5)
                        if is_ratelimit:
                            self.log_callback(f"[LLM频控] 触发网关速率限制，等待 {wait_sec:.1f} 秒恢复 (第 {attempt} 次重试)...")
                        else:
                            self.log_callback(f"[LLM重试] 接口状态码 {status}，等待 {wait_sec:.1f} 秒 (第 {attempt} 次重试)...")
                        time.sleep(wait_sec)
                    else:
                        self.log_callback(f"[LLM错误] 接口状态码 {status}: {err_preview[:60]}")
                        return None
            except Exception as exc:
                dt = time.time() - t0
                err_str = str(exc).replace("\r", " ").replace("\n", " ")
                write_runtime_log(f"[LLM连接异常] 耗时: {dt:.2f}s | 异常: {type(exc).__name__} | 详情: {err_str}")
                if attempt < max_retries:
                    wait_sec = attempt * 2.0
                    is_timeout = isinstance(exc, (requests.exceptions.Timeout, TimeoutError)) or ("timeout" in err_str.lower())
                    if is_timeout:
                        self.log_callback(f"[模型深度思考] 单批耗时较长触发连接窗口，系统正在自动续接第 {attempt} 次 (等待 {wait_sec:.1f} 秒)...")
                    else:
                        self.log_callback(f"[连接重试] 局部网络微小波动，等待 {wait_sec:.1f} 秒 (第 {attempt} 次重试)...")
                    time.sleep(wait_sec)
                else:
                    self.log_callback(f"[LLM错误] 重试 {max_retries} 次后仍失败: {err_str[:60]}")
                    return None
        return None

    def translate(self, text, src_lang="auto", dst_lang="zh-CN", timeout=30):
        text = text.strip()
        if not text:
            return ""
        cache_key = f"{src_lang}:{dst_lang}:{text}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        dst_map = {
            "zh-CN": "简体中文",
            "zh-TW": "繁体中文",
            "en": "英语",
            "ja": "日语",
            "ko": "韩语",
            "fr": "法语",
            "de": "德语",
            "es": "西班牙语",
            "ru": "俄语",
        }
        dst_name = dst_map.get(dst_lang, "简体中文")

        sys_prompt = (
            "你是一名顶级的影视字幕翻译专家。"
            f"任务：将给出的字幕原文字句，翻译成自然、流畅、地道、口语化的{dst_name}字幕。"
            "原则："
            f"1. 直接输出翻译后的一行{dst_name}结果，不要输出任何标点符号（如末尾句号感叹号），绝不输出原文字、说明或解释；"
            "2. 语言风格符合对话语境与角色口吻，简短有力，严禁机翻腔，不要进行长篇分析思考。"
        )
        if src_lang and src_lang != "auto":
            sys_prompt += f" 原文语种：{src_lang}。"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.1,
            "max_tokens": 1024,
        }

        res = self._send_request(payload, timeout=timeout, max_retries=3)
        if res:
            res_clean = res.replace("\r", "").replace("\n", " ").strip()
            self.cache[cache_key] = res_clean
            return res_clean
        return text

    def translate_batch(self, texts, src_lang="auto", dst_lang="zh-CN", batch_size=5, progress_callback=None, cancel_checker=None):
        """分块批量翻译，使用 requests.Session 连接池 + 指数退避，单批 5 句 + 180s 超时防断开"""
        total = len(texts)
        if total == 0:
            return []

        dst_map = {
            "zh-CN": "简体中文",
            "zh-TW": "繁体中文",
            "en": "英语",
            "ja": "日语",
            "ko": "韩语",
            "fr": "法语",
            "de": "德语",
            "es": "西班牙语",
            "ru": "俄语",
        }
        dst_name = dst_map.get(dst_lang, "简体中文")

        results = [None] * total
        num_batches = math.ceil(total / batch_size)
        msg_start = f"[批量翻译启动] 总条数: {total} | 批数: {num_batches} | 每批大小: {batch_size} | 语言: {src_lang} -> {dst_name}"
        write_runtime_log(msg_start)
        self.log_callback(msg_start)

        for b_idx in range(num_batches):
            if cancel_checker and cancel_checker():
                write_runtime_log("[批量翻译] 用户取消任务")
                self.log_callback("[批量翻译] 用户取消任务")
                break

            b_t0 = time.time()
            start_i = b_idx * batch_size
            end_i = min(start_i + batch_size, total)
            batch_slice = texts[start_i:end_i]

            numbered_lines = []
            for sub_i, line_text in enumerate(batch_slice, 1):
                clean_t = line_text.replace("\r", " ").replace("\n", " ").strip()
                numbered_lines.append(f"{sub_i}: {clean_t}")
            user_prompt = "\n".join(numbered_lines)

            sys_prompt = (
                "你是一名顶级的影视字幕翻译专家。\n"
                f"任务：将用户给出的字幕对白按编号逐行翻译为自然、流畅、地道、口语化的{dst_name}。\n"
                "规则：\n"
                "1. 必须严格保留每一行的数字编号，格式如“1: 译文”，每行对应一条；\n"
                "2. 仅输出翻译后的编号和译文，严禁输出任何解释、说明或原文；\n"
                "3. 保证编号数量与原文完全一致，严禁遗漏任何一条；\n"
                "4. 极速直译要求：无需展开长篇推理或剧情分析，以最短思考极速输出标准译文。"
            )
            if src_lang and src_lang != "auto":
                sys_prompt += f"\n原文语种参考：{src_lang}。"

            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.1,
                "max_tokens": max(2048, len(batch_slice) * 128)
            }

            # 超时放宽至 180 秒，极速适应长篇思考模型
            resp_text = self._send_request(payload, timeout=180, max_retries=3)

            res_map = {}
            if resp_text:
                for line in resp_text.splitlines():
                    line_s = line.strip()
                    m = re.match(r"^(?:\[?(\d+)\]?|\(?(\d+)\)?)\s*[:：、.．\- ]\s*(.+)$", line_s)
                    if m:
                        idx_num = int(m.group(1) or m.group(2))
                        res_map[idx_num] = m.group(3).strip()

            missing_count = 0
            for sub_i, line_text in enumerate(batch_slice, 1):
                global_i = start_i + (sub_i - 1)
                if sub_i in res_map and res_map[sub_i]:
                    results[global_i] = res_map[sub_i]
                else:
                    missing_count += 1
                    # 容灾补漏：超时放宽至 60s
                    results[global_i] = self.translate(line_text, src_lang, dst_lang, timeout=60)

            b_dur = time.time() - b_t0
            pct_int = int((end_i / total) * 100)
            log_detail = f"[批次翻译完成] 第 {b_idx+1}/{num_batches} 批 ({start_i+1}..{end_i}) | 匹配成功: {len(res_map)}/{len(batch_slice)} | 补漏单行: {missing_count} | 耗时: {b_dur:.1f}s"
            write_runtime_log(log_detail)
            self.log_callback(f"[翻译进度] 第 {b_idx+1}/{num_batches} 批完成 ({end_i}/{total} 句, {pct_int}%) | 本批耗时 {b_dur:.1f}s")

            if progress_callback:
                progress_callback(end_i, total)

            time.sleep(0.05)

        for i in range(total):
            if results[i] is None:
                results[i] = texts[i]
        write_runtime_log(f"[批量翻译结束] 全部 {total} 句处理完毕")
        return results


# ---------------------------------------------------------------------------
# 字幕解析与保存工具 (.srt / .vtt)
# ---------------------------------------------------------------------------
def parse_subtitle_file(filepath):
    encodings = ["utf-8-sig", "utf-8", "gb18030", "shift_jis", "cp1252"]
    raw_text = None
    for enc in encodings:
        try:
            with open(filepath, "r", encoding=enc) as f:
                raw_text = f.read()
            break
        except (UnicodeDecodeError, OSError):
            continue

    if raw_text is None:
        raise ValueError(f"无法读取字幕文件，请检查编码格式: {filepath}")

    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    items = []
    time_pat = re.compile(r"^(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})")

    current_item = None
    text_buffer = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_item and text_buffer:
                current_item["text"] = "\n".join(text_buffer).strip()
                if current_item["text"]:
                    items.append(current_item)
                current_item = None
                text_buffer = []
            continue

        m = time_pat.search(stripped)
        if m:
            if current_item and text_buffer:
                current_item["text"] = "\n".join(text_buffer).strip()
                if current_item["text"]:
                    items.append(current_item)
                text_buffer = []
            start_ts = m.group(1).replace(".", ",")
            end_ts = m.group(2).replace(".", ",")
            current_item = {
                "index": len(items) + 1,
                "start": start_ts,
                "end": end_ts,
                "text": ""
            }
        elif current_item is not None:
            if stripped.isdigit() and not text_buffer:
                continue
            text_buffer.append(stripped)

    if current_item and text_buffer:
        current_item["text"] = "\n".join(text_buffer).strip()
        if current_item["text"]:
            items.append(current_item)

    return items


def write_srt_file(out_filepath, items, translations, dual_mode=False):
    lines = []
    for idx, (item, trans) in enumerate(zip(items, translations), 1):
        start_str = item["start"]
        end_str = item["end"]
        orig_str = item["text"].strip()
        trans_str = (trans or orig_str).strip()

        if dual_mode:
            content = f"{trans_str}\n{orig_str}"
        else:
            content = trans_str

        lines.append(f"{idx}\n{start_str} --> {end_str}\n{content}\n\n")

    os.makedirs(os.path.dirname(os.path.abspath(out_filepath)), exist_ok=True)
    with open(out_filepath, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))


# ---------------------------------------------------------------------------
# 字幕与视频合成引擎 (VideoSubtitleMuxer)
# ---------------------------------------------------------------------------
class VideoSubtitleMuxer:
    """负责将字幕合成到视频（支持无损封装软字幕 & 硬件加速硬字幕压制），带实时流式百分比与速度进度"""

    @staticmethod
    def _format_seconds(seconds):
        s = max(0, int(seconds))
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        return f"{h:02d}:{m:02d}:{sec:02d}"

    @staticmethod
    def get_video_duration(video_path):
        """快速探测视频文件的总播放时长（秒）"""
        ffmpeg = find_ffmpeg()
        if not ffmpeg or not os.path.isfile(video_path):
            return 0
        try:
            vp = os.path.abspath(video_path)
            cmd = [ffmpeg, "-i", vp]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="ignore")
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
            if m:
                h, m_, s = m.groups()
                return int(h) * 3600 + int(m_) * 60 + float(s)
        except Exception:
            pass
        return 0

    @classmethod
    def _run_ffmpeg_stream_progress(cls, cmd, cwd, total_dur, action_title, log_cb, progress_cb, cancel_checker):
        """通用流式执行 ffmpeg，实时正则提取 time 与 speed 计算百分比，平滑驱动 UI 进度条"""
        log = log_cb or (lambda x: None)
        prog = progress_cb or (lambda p, t: None)
        cancel = cancel_checker or (lambda: False)

        prog(1, f"准备开始{action_title}...")
        log(f"[{action_title}] 启动处理进程...")

        time_pat = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
        speed_pat = re.compile(r"speed=\s*([\d\.]+)x")

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="ignore",
                bufsize=1
            )
        except Exception as exc:
            log(f"[{action_title}启动异常]: {exc}")
            return False, str(exc)

        last_log_pct = -10
        last_log_t = 0
        stderr_buffer = []

        while True:
            if cancel():
                proc.kill()
                log(f"[{action_title}] 用户取消了合成任务。")
                return False, "用户取消任务"

            line = proc.stderr.readline()
            if not line and proc.poll() is not None:
                break

            if line:
                stderr_buffer.append(line)
                if len(stderr_buffer) > 100:
                    stderr_buffer.pop(0)

                m_time = time_pat.search(line)
                if m_time:
                    h, m_, s = m_time.groups()
                    cur_sec = int(h) * 3600 + int(m_) * 60 + float(s)
                    m_spd = speed_pat.search(line)
                    speed_str = f" | 速度: {m_spd.group(1)}x" if m_spd else ""

                    if total_dur > 0:
                        pct = min(99, max(1, int((cur_sec / total_dur) * 100)))
                        cur_str = cls._format_seconds(cur_sec)
                        tot_str = cls._format_seconds(total_dur)
                        status_msg = f"{action_title}: {pct}% ({cur_str}/{tot_str}){speed_str}"
                        prog(pct, status_msg)

                        now_t = time.time()
                        if pct >= last_log_pct + 10 or (now_t - last_log_t >= 10.0 and pct > last_log_pct):
                            last_log_pct = pct
                            last_log_t = now_t
                            log(f"[{action_title}] 进度: {pct}% ({cur_str}/{tot_str}){speed_str}")
                    else:
                        cur_str = cls._format_seconds(cur_sec)
                        prog(50, f"{action_title}: 已处理 {cur_str}{speed_str}")

        proc.wait()
        if proc.returncode == 0:
            prog(100, f"{action_title}完成！")
            return True, ""
        else:
            err_msg = "".join(stderr_buffer[-20:])
            log(f"[{action_title}失败]: {err_msg[:200]}")
            return False, err_msg

    @classmethod
    def mux_soft_subtitle(cls, video_path, srt_path, out_path=None, log_cb=None, progress_cb=None, cancel_checker=None):
        """
        极速封装软字幕轨（Soft Subtitle）：
        无损流拷贝封装（Stream Copy），画质 100% 保持原样，播放器可随意开关/选择字幕。
        带实时处理流进度与速率显示。
        """
        log = log_cb or (lambda x: None)
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            log("[错误] 未找到 ffmpeg 工具，无法合成视频！")
            return False, ""

        if not out_path:
            bname, ext = os.path.splitext(video_path)
            out_ext = ".mkv" if ext.lower() == ".mkv" else ".mp4"
            out_path = f"{bname}_内嵌软字幕{out_ext}"

        sub_codec = "srt" if out_path.lower().endswith(".mkv") else "mov_text"
        cmd = [
            ffmpeg, "-y",
            "-i", os.path.abspath(video_path),
            "-i", os.path.abspath(srt_path),
            "-c", "copy",
            "-c:s", sub_codec,
            "-metadata:s:s:0", "language=chi",
            "-metadata:s:s:0", "title=中文字幕",
            os.path.abspath(out_path)
        ]
        log(f"[软字幕合成] 正在无损封装软字幕轨 -> {os.path.basename(out_path)}...")
        total_dur = cls.get_video_duration(video_path)

        ok, err = cls._run_ffmpeg_stream_progress(
            cmd=cmd,
            cwd=os.path.dirname(os.path.abspath(video_path)),
            total_dur=total_dur,
            action_title="软字幕封装",
            log_cb=log,
            progress_cb=progress_cb,
            cancel_checker=cancel_checker
        )

        if ok and os.path.isfile(out_path):
            log(f"[软字幕成功] 封装完成！输出视频: {out_path}")
            return True, out_path
        return False, ""

    @classmethod
    def burn_hard_subtitle(cls, video_path, srt_path, out_path=None, gpu_idx=0, log_cb=None, progress_cb=None, cancel_checker=None):
        """
        硬字幕画面压制（Hard Subtitle Burn-in）：
        将字幕直接渲染烙印到视频画面中，兼容所有无外挂字幕支持的播放器与移动设备。
        自动检测 NVIDIA NVENC 显卡硬件加速，带实时压制时间与百分比进度。
        """
        log = log_cb or (lambda x: None)
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            log("[错误] 未找到 ffmpeg 工具，无法压制视频！")
            return False, ""

        if not out_path:
            bname, ext = os.path.splitext(video_path)
            out_path = f"{bname}_硬字幕压制.mp4"

        # 检查是否支持 NVENC 硬件加速
        use_nvenc = False
        if gpu_idx is not None and gpu_idx >= 0:
            try:
                chk = subprocess.run([ffmpeg, "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="ignore")
                if "h264_nvenc" in chk.stdout:
                    use_nvenc = True
            except Exception:
                pass

        # Windows 下处理字幕滤镜路径，优先使用同目录相对文件名，避免冒号和转义陷阱
        srt_dir = os.path.dirname(os.path.abspath(srt_path))
        srt_file = os.path.basename(srt_path)

        # 滤镜参数
        vf_param = f"subtitles={srt_file}"

        cmd = [ffmpeg, "-y", "-i", os.path.abspath(video_path), "-vf", vf_param]
        if use_nvenc:
            log(f"[硬字幕压制] 启用 NVIDIA NVENC 显卡硬件加速压制 (GPU {gpu_idx})...")
            cmd.extend(["-c:v", "h264_nvenc", "-gpu", str(gpu_idx), "-preset", "p4", "-cq", "22"])
        else:
            log("[硬字幕压制] 使用 CPU (libx264) 压制字幕画面...")
            cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "21"])

        cmd.extend(["-c:a", "copy", os.path.abspath(out_path)])

        log(f"[硬字幕压制] 开始压制渲染字幕到视频画面: {os.path.basename(out_path)}...")
        total_dur = cls.get_video_duration(video_path)

        ok, err = cls._run_ffmpeg_stream_progress(
            cmd=cmd,
            cwd=srt_dir,
            total_dur=total_dur,
            action_title="硬字幕压制",
            log_cb=log,
            progress_cb=progress_cb,
            cancel_checker=cancel_checker
        )

        if ok and os.path.isfile(out_path):
            log(f"[硬字幕成功] 视频画面硬字幕压制完成！输出视频: {out_path}")
            return True, out_path
        return False, ""


# ---------------------------------------------------------------------------
# 模式一任务：已有字幕文件直接翻译 (+ 可选合成到视频)
# ---------------------------------------------------------------------------
class SubtitleFilePipeline:
    def __init__(self, translator, log_callback, progress_callback):
        self.translator = translator
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def process(self, srt_path, video_path="", src_lang="auto", dst_lang="zh-CN", dual_mode=False, mux_mode="none"):
        self.cancelled = False
        if not os.path.isfile(srt_path):
            self.log_callback(f"[错误] 找不到字幕文件: {srt_path}")
            return False

        self.progress_callback(5, "正在读取并解析字幕文件...")
        self.log_callback(f"[字幕翻译] 解析字幕文件: {os.path.basename(srt_path)}")

        try:
            items = parse_subtitle_file(srt_path)
        except Exception as exc:
            self.log_callback(f"[错误] 解析字幕文件失败: {exc}")
            return False

        total_items = len(items)
        if total_items == 0:
            self.log_callback("[警告] 字幕文件中未提取到有效对白！")
            return False

        self.log_callback(f"[字幕翻译] 成功解析 {total_items} 条字幕，开始大模型批量翻译...")

        def _on_batch_prog(done_count, total_count):
            base_pct = 10 if mux_mode == "none" else 5
            max_pct = 95 if mux_mode == "none" else 75
            pct = base_pct + int((done_count / max(total_count, 1)) * (max_pct - base_pct))
            self.progress_callback(pct, f"翻译进度: {done_count} / {total_count}")

        texts = [it["text"] for it in items]
        translated = self.translator.translate_batch(
            texts,
            src_lang=src_lang,
            dst_lang=dst_lang,
            batch_size=5,
            progress_callback=_on_batch_prog,
            cancel_checker=lambda: self.cancelled
        )

        if self.cancelled:
            return False

        base_name, ext = os.path.splitext(srt_path)
        tag = "zh-CN" if dst_lang == "zh-CN" else dst_lang
        out_srt = f"{base_name}.{tag}.srt"

        self.progress_callback(96 if mux_mode == "none" else 80, "正在保存翻译字幕文件...")
        try:
            write_srt_file(out_srt, items, translated, dual_mode=dual_mode)
            self.log_callback(f"[字幕完成] 已成功生成翻译字幕文件：\n  {out_srt}")
        except Exception as exc:
            self.log_callback(f"[错误] 写入字幕文件失败: {exc}")
            return False

        # 如果需要合成到视频
        if mux_mode != "none" and video_path and os.path.isfile(video_path):
            def _on_mux_prog(pct, text):
                overall_pct = 80 + int(pct * 0.19)
                self.progress_callback(overall_pct, text)

            if mux_mode == "soft":
                ok, v_out = VideoSubtitleMuxer.mux_soft_subtitle(
                    video_path, out_srt, log_cb=self.log_callback,
                    progress_cb=_on_mux_prog, cancel_checker=lambda: self.cancelled
                )
            elif mux_mode == "hard":
                ok, v_out = VideoSubtitleMuxer.burn_hard_subtitle(
                    video_path, out_srt, log_cb=self.log_callback,
                    progress_cb=_on_mux_prog, cancel_checker=lambda: self.cancelled
                )
            else:
                ok, v_out = True, ""

            if ok and v_out:
                self.progress_callback(100, "字幕翻译与视频合成全流程完毕！")
                self.log_callback(f"[全部完成] 带字幕成品视频已生成: {v_out}")
                return True
            elif self.cancelled:
                self.progress_callback(0, "任务已取消")
                return False
            else:
                self.log_callback("[警告] 视频合成未成功，但已成功生成独立 SRT 字幕文件！")
                self.progress_callback(100, "字幕翻译完成（视频合成失败）")
                return True

        self.progress_callback(100, "字幕翻译完毕！")
        return True


# ---------------------------------------------------------------------------
# 模式二任务：无字幕视频先提取转写后翻译 (+ 可选合成到视频)
# ---------------------------------------------------------------------------
class VideoSrtPipeline:
    def __init__(self, translator, log_callback, progress_callback):
        self.translator = translator
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def format_timestamp(self, seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int(round((seconds - int(seconds)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def process(self, video_path, model_name="small", src_lang=None, dst_lang="zh-CN", dual_mode=False, mux_mode="none", gpu_idx=0):
        self.cancelled = False
        if not os.path.isfile(video_path):
            self.log_callback(f"[错误] 找不到视频文件: {video_path}")
            return False

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.log_callback("[错误] 未找到 ffmpeg 工具，请配置环境变量或放置 ffmpeg.exe")
            return False

        base_name, _ = os.path.splitext(video_path)
        out_srt = f"{base_name}.zh-CN.srt" if dst_lang == "zh-CN" else f"{base_name}.{dst_lang}.srt"
        out_fallback_srt = f"{base_name}.srt"
        temp_wav = f"{base_name}.__temp_audio_extract.wav"

        self.progress_callback(5, "正在提取视频高清音轨...")
        self.log_callback(f"[视频生字幕] 提取音轨: {os.path.basename(video_path)}")

        cmd = [
            ffmpeg, "-y", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            temp_wav
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except Exception as exc:
            self.log_callback(f"[错误] ffmpeg 音轨抽取失败: {exc}")
            return False

        try:
            if self.cancelled:
                return False

            self.progress_callback(15, "正在加载 GPU 识别引擎...")
            gpu_tip = f"GPU {gpu_idx}" if (gpu_idx is not None and gpu_idx >= 0) else "CPU"
            self.log_callback(f"[视频生字幕] 加载 faster-whisper ({model_name})，使用加速设备: {gpu_tip}...")
            from faster_whisper import WhisperModel
            import ctranslate2

            use_cuda = (gpu_idx is not None and gpu_idx >= 0 and ctranslate2.get_cuda_device_count() > 0)
            device = "cuda" if use_cuda else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"

            wm_kw = {
                "device": device,
                "compute_type": compute_type,
                "download_root": os.path.expanduser("~/.cache/huggingface/hub")
            }
            if device == "cuda":
                wm_kw["device_index"] = gpu_idx

            model = WhisperModel(model_name, **wm_kw)

            self.progress_callback(25, "正在进行全剧时间轴音频识别...")
            self.log_callback("[视频生字幕] 开始语音识别与说话人时间轴切分...")

            segments_generator, info = model.transcribe(
                temp_wav,
                language=src_lang if src_lang != "auto" else None,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500)
            )

            detected_lang = info.language
            self.log_callback(f"[视频生字幕] 识别完成，语言: {detected_lang}，总时长: {int(info.duration)}秒")

            items = []
            for seg in segments_generator:
                if self.cancelled:
                    return False
                text = seg.text.strip()
                if text:
                    items.append({
                        "index": len(items) + 1,
                        "start": self.format_timestamp(seg.start),
                        "end": self.format_timestamp(seg.end),
                        "text": text,
                    })

            total_items = len(items)
            self.log_callback(f"[视频生字幕] 共提取 {total_items} 句语音分段，开始大模型批量翻译...")

            max_trans_pct = 95 if mux_mode == "none" else 75
            def _on_batch_prog(done_count, total_count):
                pct = 30 + int((done_count / max(total_count, 1)) * (max_trans_pct - 30))
                self.progress_callback(pct, f"翻译进度: {done_count} / {total_count}")

            texts = [it["text"] for it in items]
            translated = self.translator.translate_batch(
                texts,
                src_lang=detected_lang,
                dst_lang=dst_lang,
                batch_size=5,
                progress_callback=_on_batch_prog,
                cancel_checker=lambda: self.cancelled
            )

            if self.cancelled:
                return False

            self.progress_callback(95 if mux_mode == "none" else 80, "正在写入 SRT 字幕文件...")
            write_srt_file(out_srt, items, translated, dual_mode=dual_mode)
            write_srt_file(out_fallback_srt, items, translated, dual_mode=dual_mode)
            self.log_callback(f"[字幕完成] 已成功生成字幕文件：\n  {out_srt}")

            # 合成到视频
            if mux_mode != "none":
                def _on_mux_prog(pct, text):
                    overall_pct = 80 + int(pct * 0.19)
                    self.progress_callback(overall_pct, text)

                if mux_mode == "soft":
                    ok, v_out = VideoSubtitleMuxer.mux_soft_subtitle(
                        video_path, out_srt, log_cb=self.log_callback,
                        progress_cb=_on_mux_prog, cancel_checker=lambda: self.cancelled
                    )
                elif mux_mode == "hard":
                    ok, v_out = VideoSubtitleMuxer.burn_hard_subtitle(
                        video_path, out_srt, gpu_idx=gpu_idx, log_cb=self.log_callback,
                        progress_cb=_on_mux_prog, cancel_checker=lambda: self.cancelled
                    )
                else:
                    ok, v_out = True, ""

                if ok and v_out:
                    self.progress_callback(100, "字幕生成与视频合成全部搞定！")
                    self.log_callback(f"[全部完成] 带字幕成品视频已生成: {v_out}")
                    return True
                elif self.cancelled:
                    self.progress_callback(0, "任务已取消")
                    return False
                else:
                    self.log_callback("[警告] 视频合成未成功，但已成功生成独立 SRT 字幕文件！")
                    self.progress_callback(100, "字幕提取完成（视频合成失败）")
                    return True

            self.progress_callback(100, "字幕提取并翻译完成！")
            return True
        finally:
            if os.path.isfile(temp_wav):
                try:
                    os.remove(temp_wav)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 自定义 AI API 管理与模型获取弹窗 (ApiManagerDialog)
# ---------------------------------------------------------------------------
class ApiManagerDialog:
    def __init__(self, parent, on_save_callback):
        self.parent = parent
        self.on_save_callback = on_save_callback

        self.cfg_data = load_all_apis_config()
        self.apis = self.cfg_data.get("apis", [])

        self.win = tk.Toplevel(parent)
        self.win.title("AI API 接口与可用模型配置管理")
        self.win.geometry("760x530")
        self.win.minsize(700, 490)
        self.win.configure(bg="#252526")
        self.win.transient(parent)
        self.win.grab_set()

        self._build_ui()
        self._load_api_list()

    def _build_ui(self):
        lbl_top = tk.Label(
            self.win,
            text="可自由添加/修改任意兼容 OpenAI 规范的本地或云端 AI 接口，点击「获取可用模型」即可自动探测所有模型供点选：",
            bg="#252526",
            fg="#cccccc",
            font=("Microsoft YaHei UI", 9),
            justify="left"
        )
        lbl_top.pack(anchor="w", padx=16, pady=(12, 6))

        main_box = tk.Frame(self.win, bg="#252526")
        main_box.pack(fill="both", expand=True, padx=16, pady=6)

        left_frame = tk.LabelFrame(main_box, text="已保存的 API 方案", bg="#252526", fg="#aaaaaa", font=("Microsoft YaHei UI", 9))
        left_frame.pack(side="left", fill="y", padx=(0, 10))

        self.listbox = tk.Listbox(left_frame, bg="#1e1e1e", fg="#ffffff", selectbackground="#0e639c", width=22, font=("Microsoft YaHei UI", 9), bd=0)
        self.listbox.pack(fill="both", expand=True, padx=6, pady=6)
        self.listbox.bind("<<ListboxSelect>>", self._on_select_api)

        btn_bar_left = tk.Frame(left_frame, bg="#252526")
        btn_bar_left.pack(fill="x", padx=6, pady=6)

        btn_add = tk.Button(btn_bar_left, text="➕ 新增配置", bg="#3a3d41", fg="#ffffff", bd=0, padx=6, pady=3, command=self._add_new_api)
        btn_add.pack(side="left", fill="x", expand=True, padx=(0, 3))

        btn_del = tk.Button(btn_bar_left, text="🗑️ 删除", bg="#3a3d41", fg="#ff6b6b", bd=0, padx=6, pady=3, command=self._delete_selected_api)
        btn_del.pack(side="right", padx=(3, 0))

        right_frame = tk.LabelFrame(main_box, text="API 参数与模型设置", bg="#252526", fg="#aaaaaa", font=("Microsoft YaHei UI", 9))
        right_frame.pack(side="right", fill="both", expand=True)

        form = tk.Frame(right_frame, bg="#252526", padx=12, pady=10)
        form.pack(fill="both", expand=True)

        tk.Label(form, text="配置别名：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="w", pady=6)
        self.var_name = tk.StringVar()
        ent_name = tk.Entry(form, textvariable=self.var_name, bg="#1e1e1e", fg="#ffffff", insertbackground="#fff", font=("Microsoft YaHei UI", 9))
        ent_name.grid(row=0, column=1, columnspan=2, sticky="ew", pady=6)

        tk.Label(form, text="API 地址 (Base URL)：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, sticky="w", pady=6)
        self.var_url = tk.StringVar(value="http://127.0.0.1:11434/v1")
        ent_url = tk.Entry(form, textvariable=self.var_url, bg="#1e1e1e", fg="#ffffff", insertbackground="#fff", font=("Microsoft YaHei UI", 9))
        ent_url.grid(row=1, column=1, columnspan=2, sticky="ew", pady=6)

        tk.Label(form, text="API Key (密钥)：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=2, column=0, sticky="w", pady=6)
        self.var_key = tk.StringVar()
        ent_key = tk.Entry(form, textvariable=self.var_key, bg="#1e1e1e", fg="#ffffff", insertbackground="#fff", font=("Microsoft YaHei UI", 9))
        ent_key.grid(row=2, column=1, columnspan=2, sticky="ew", pady=6)

        tk.Label(form, text="认证方式：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=3, column=0, sticky="w", pady=6)
        self.var_auth = tk.StringVar(value="bearer")
        combo_auth = ttk.Combobox(form, textvariable=self.var_auth, values=["bearer", "none"], state="readonly", width=12)
        combo_auth.grid(row=3, column=1, sticky="w", pady=6)

        tk.Label(form, text="选择使用模型：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=4, column=0, sticky="w", pady=6)
        self.var_model = tk.StringVar(value="deepseek/deepseek-v4.1-flash")
        self.combo_model = ttk.Combobox(form, textvariable=self.var_model, font=("Microsoft YaHei UI", 9))
        self.combo_model.grid(row=4, column=1, sticky="ew", pady=6)

        self.btn_fetch_models = tk.Button(
            form,
            text="🔍 获取可用模型",
            bg="#0e639c",
            fg="#ffffff",
            font=("Microsoft YaHei UI", 9, "bold"),
            bd=0,
            padx=10,
            pady=3,
            command=self._fetch_models_online
        )
        self.btn_fetch_models.grid(row=4, column=2, sticky="w", padx=(8, 0), pady=6)

        self.lbl_status = tk.Label(form, text="", bg="#252526", fg="#98c379", font=("Microsoft YaHei UI", 9))
        self.lbl_status.grid(row=5, column=0, columnspan=3, sticky="w", pady=6)

        form.columnconfigure(1, weight=1)

        bottom_bar = tk.Frame(self.win, bg="#1e1e1e", pady=10, padx=16)
        bottom_bar.pack(fill="x", side="bottom")

        btn_test = tk.Button(
            bottom_bar,
            text="⚡ 测试当前接口与模型",
            bg="#3a3d41",
            fg="#ffffff",
            bd=0,
            padx=14,
            pady=6,
            command=self._test_current_form
        )
        btn_test.pack(side="left")

        btn_save = tk.Button(
            bottom_bar,
            text="💾 保存并应用此配置",
            bg="#2ecc71",
            fg="#ffffff",
            font=("Microsoft YaHei UI", 10, "bold"),
            bd=0,
            padx=18,
            pady=6,
            command=self._save_and_apply
        )
        btn_save.pack(side="right")

    def _load_api_list(self):
        self.listbox.delete(0, "end")
        for a in self.apis:
            self.listbox.insert("end", a.get("name", "未命名"))
        active = self.cfg_data.get("active", "")
        for idx, a in enumerate(self.apis):
            if a.get("name") == active:
                self.listbox.selection_set(idx)
                self._fill_form(a)
                return
        if self.apis:
            self.listbox.selection_set(0)
            self._fill_form(self.apis[0])

    def _on_select_api(self, event):
        sel = self.listbox.curselection()
        if sel:
            idx = sel[0]
            if 0 <= idx < len(self.apis):
                self._fill_form(self.apis[idx])

    def _fill_form(self, api_item):
        self.var_name.set(api_item.get("name", ""))
        self.var_url.set(api_item.get("url", ""))
        self.var_key.set(api_item.get("key", ""))
        self.var_auth.set(api_item.get("auth", "bearer"))
        self.var_model.set(api_item.get("model", ""))
        self.lbl_status.config(text="")

    def _add_new_api(self):
        new_name = f"自定义API_{len(self.apis) + 1}"
        new_item = {
            "name": new_name,
            "url": "http://127.0.0.1:11434/v1",
            "model": "deepseek/deepseek-v4.1-flash",
            "auth": "bearer",
            "key": ""
        }
        self.apis.append(new_item)
        self.listbox.insert("end", new_name)
        idx = len(self.apis) - 1
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self._fill_form(new_item)

    def _delete_selected_api(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if len(self.apis) <= 1:
            messagebox.showwarning("提示", "至少保留一个 API 配置方案！")
            return
        del self.apis[idx]
        self._load_api_list()

    def _fetch_models_online(self):
        url = self.var_url.get().strip()
        key = self.var_key.get().strip()
        auth = self.var_auth.get().strip()

        self.btn_fetch_models.config(state="disabled", text="正在拉取...")
        self.lbl_status.config(text="正在连接远端 /models 接口探测可用模型...", fg="#61afef")

        def _fetch_worker():
            try:
                models = fetch_remote_models(url, key, auth_type=auth)
                def _done_success():
                    self.btn_fetch_models.config(state="normal", text="🔍 获取可用模型")
                    if models:
                        self.combo_model["values"] = models
                        self.lbl_status.config(text=f"✅ 成功获取 {len(models)} 款可用模型！请在下拉框中点选", fg="#2ecc71")
                        if self.var_model.get() not in models:
                            self.var_model.set(models[0])
                        self.combo_model.event_generate("<Button-1>")
                    else:
                        self.lbl_status.config(text="⚠️ 接口已通但未返回模型列表，可手动输入", fg="#e5c07b")
                self.win.after(0, _done_success)
            except Exception as exc:
                def _done_fail():
                    self.btn_fetch_models.config(state="normal", text="🔍 获取可用模型")
                    self.lbl_status.config(text=f"❌ 获取失败: {exc} (可手动输入模型名称)", fg="#e06c75")
                self.win.after(0, _done_fail)

        threading.Thread(target=_fetch_worker, daemon=True).start()

    def _test_current_form(self):
        url = self.var_url.get().strip()
        key = self.var_key.get().strip()
        auth = self.var_auth.get().strip()
        model = self.var_model.get().strip()

        tmp_cfg = {
            "name": "temp_test",
            "url": url,
            "key": key,
            "auth": auth,
            "model": model
        }
        self.lbl_status.config(text="正在发送测试请求...", fg="#61afef")

        def _test():
            t = LlmTranslator(tmp_cfg)
            res = t.translate("こんにちは、字幕のテストです。", src_lang="ja", dst_lang="zh-CN")
            def _done():
                if res and res != "こんにちは、字幕のテストです。":
                    self.lbl_status.config(text=f"✅ 连接成功！模型返回：{res}", fg="#2ecc71")
                    messagebox.showinfo("测试成功", f"接口与模型连通正常！\n\n测试原文: こんにちは、字幕のテストです。\n翻译结果: {res}")
                else:
                    self.lbl_status.config(text="⚠️ 模型返回异常或超时，请检查配置", fg="#e06c75")
            self.win.after(0, _done)

        threading.Thread(target=_test, daemon=True).start()

    def _save_and_apply(self):
        sel = self.listbox.curselection()
        name = self.var_name.get().strip()
        if not name:
            messagebox.showwarning("提示", "配置别名不能为空！")
            return

        item = {
            "name": name,
            "url": self.var_url.get().strip(),
            "key": self.var_key.get().strip(),
            "auth": self.var_auth.get().strip(),
            "model": self.var_model.get().strip()
        }

        if sel:
            self.apis[sel[0]] = item
        else:
            self.apis.append(item)

        self.cfg_data["apis"] = self.apis
        self.cfg_data["active"] = name
        save_all_apis_config(self.cfg_data)

        self.on_save_callback(item)
        messagebox.showinfo("已保存", f"配置「{name}」已保存并设置为当前激活大模型！")
        self.win.destroy()


# ---------------------------------------------------------------------------
# 主图形界面 (AppWindow)
# ---------------------------------------------------------------------------
class AppWindow:
    def __init__(self, root):
        self.root = root
        self.root.title("智能 AI 字幕翻译与视频字幕合成大师")
        self.root.geometry("900x740")
        self.root.minsize(840, 660)
        self.root.configure(bg="#252526")

        # 加载配置与翻译器
        self.cfg_data = load_all_apis_config()
        self.api_cfg = get_active_api_config(self.cfg_data)
        self.translator = LlmTranslator(self.api_cfg, log_callback=self.log)

        # 任务管道
        self.sub_pipeline = SubtitleFilePipeline(self.translator, self.log, self.update_sub_progress)
        self.video_pipeline = VideoSrtPipeline(self.translator, self.log, self.update_video_progress)

        # 扫描 GPU 设备列表
        self.available_gpus = detect_nvidia_gpus()

        self._build_ui()
        self.log(f"[系统] 当前 AI 方案: {self.api_cfg.get('name')} | 模型: {self.api_cfg.get('model')}")
        self._check_hardware()

    def _check_hardware(self):
        if self.available_gpus:
            self.log(f"[硬件] 自动识别到 {len(self.available_gpus)} 块可用 NVIDIA GPU：")
            for g in self.available_gpus:
                self.log(f"  • [GPU {g['index']}] {g['name']} (显存: {g['memory']}) - 已就绪")
        else:
            self.log("[硬件] 未检测到独立 NVIDIA GPU，将使用 CPU 进行运算。")

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")

        # ---------------- 顶部常驻 API 控制条 ----------------
        top_bar = tk.Frame(self.root, bg="#1e1e1e", pady=8, padx=14)
        top_bar.pack(fill="x", side="top")

        tk.Label(top_bar, text="当前 AI 方案：", bg="#1e1e1e", fg="#aaaaaa", font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.var_active_api = tk.StringVar(value=self.api_cfg.get("name", ""))
        self.combo_active_api = ttk.Combobox(top_bar, textvariable=self.var_active_api, state="readonly", width=18, font=("Microsoft YaHei UI", 9))
        self._refresh_api_dropdown_values()
        self.combo_active_api.pack(side="left", padx=(0, 14))
        self.combo_active_api.bind("<<ComboboxSelected>>", self._on_active_api_changed)

        tk.Label(top_bar, text="使用模型：", bg="#1e1e1e", fg="#aaaaaa", font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.var_active_model = tk.StringVar(value=self.api_cfg.get("model", ""))
        self.combo_active_model = ttk.Combobox(top_bar, textvariable=self.var_active_model, width=26, font=("Microsoft YaHei UI", 9))
        self.combo_active_model.pack(side="left", padx=(0, 6))
        self.combo_active_model.bind("<<ComboboxSelected>>", self._on_active_model_changed)
        self.combo_active_model.bind("<Return>", self._on_active_model_changed)

        btn_fetch_top = tk.Button(
            top_bar,
            text="🔍 获取模型",
            bg="#2d4f7c",
            fg="#ffffff",
            activebackground="#1e3a5f",
            activeforeground="#ffffff",
            bd=0,
            padx=8,
            pady=2,
            font=("Microsoft YaHei UI", 9),
            command=self._fetch_current_models_quick
        )
        btn_fetch_top.pack(side="left", padx=(0, 8))

        btn_test_top = tk.Button(
            top_bar,
            text="⚡ 测试连接",
            bg="#3a3d41",
            fg="#ffffff",
            activebackground="#4a4d51",
            activeforeground="#ffffff",
            bd=0,
            padx=8,
            pady=2,
            font=("Microsoft YaHei UI", 9),
            command=self._test_api_connection
        )
        btn_test_top.pack(side="left", padx=(0, 8))

        btn_jet_hub = tk.Button(
            top_bar,
            text="🚀 Jet Hub 账号管理",
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9, "bold"),
            bd=0,
            padx=12,
            pady=3,
            command=self._open_jet_hub
        )
        btn_jet_hub.pack(side="right", padx=(0, 8))

        btn_manage_api = tk.Button(
            top_bar,
            text="⚙️ 自定义与管理 API",
            bg="#0e639c",
            fg="#ffffff",
            activebackground="#1177bb",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 9, "bold"),
            bd=0,
            padx=12,
            pady=3,
            command=self._open_api_manager
        )
        btn_manage_api.pack(side="right")

        # ---------------- 核心选项卡 Notebook ----------------
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=8)

        # 模式一：翻译已有字幕文件
        tab_sub = tk.Frame(self.nb, bg="#252526", padx=16, pady=16)
        self.nb.add(tab_sub, text=" 📄 模式一：翻译已有字幕文件 (.srt/.vtt) ")
        self._build_sub_tab(tab_sub)

        # 模式二：无字幕视频提取并翻译字幕
        tab_video = tk.Frame(self.nb, bg="#252526", padx=16, pady=16)
        self.nb.add(tab_video, text=" 🎬 模式二：无字幕视频提取转写并生成字幕 ")
        self._build_video_tab(tab_video)

        # 模式三：字幕与视频合成工具箱
        tab_mux = tk.Frame(self.nb, bg="#252526", padx=16, pady=16)
        self.nb.add(tab_mux, text=" 🎞️ 模式三：字幕直接合成到视频工具箱 ")
        self._build_mux_tab(tab_mux)

        # ---------------- 底部运行日志控制台 ----------------
        log_frame = tk.LabelFrame(
            self.root,
            text="运行日志与任务监控",
            bg="#252526",
            fg="#aaaaaa",
            font=("Microsoft YaHei UI", 9)
        )
        log_frame.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        log_top_bar = tk.Frame(log_frame, bg="#252526")
        log_top_bar.pack(fill="x", padx=6, pady=(2, 4))

        tk.Label(
            log_top_bar,
            text=f"日志落盘路径: {os.path.basename(LOG_FILE_PATH)}",
            bg="#252526",
            fg="#888888",
            font=("Microsoft YaHei UI", 8)
        ).pack(side="left")

        btn_open_log = tk.Button(
            log_top_bar,
            text="📄 打开完整运行日志文件 (app_runtime.log)",
            bg="#0e639c",
            fg="#ffffff",
            activebackground="#1177bb",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 8, "bold"),
            bd=0,
            padx=8,
            pady=2,
            command=self._open_log_file
        )
        btn_open_log.pack(side="right", padx=(4, 0))

        btn_clear_log = tk.Button(
            log_top_bar,
            text="清空窗口",
            bg="#3a3d41",
            fg="#cccccc",
            activebackground="#4a4d51",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 8),
            bd=0,
            padx=8,
            pady=2,
            command=self._clear_log_display
        )
        btn_clear_log.pack(side="right", padx=4)

        text_container = tk.Frame(log_frame, bg="#252526")
        text_container.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.log_text = tk.Text(
            text_container,
            bg="#1b1b1c",
            fg="#cccccc",
            insertbackground="#ffffff",
            font=("Consolas", 9),
            wrap="word",
            height=7
        )
        self.log_text.pack(fill="both", expand=True, side="left")

        scrollbar = ttk.Scrollbar(text_container, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=scrollbar.set)

    def _refresh_api_dropdown_values(self):
        names = [a.get("name") for a in self.cfg_data.get("apis", [])]
        self.combo_active_api["values"] = names

    def _on_active_api_changed(self, event):
        target_name = self.var_active_api.get()
        self.cfg_data["active"] = target_name
        save_all_apis_config(self.cfg_data)
        self.api_cfg = get_active_api_config(self.cfg_data)
        self.translator.update_config(self.api_cfg)
        self.var_active_model.set(self.api_cfg.get("model", ""))
        self.log(f"[切换方案] 已切换到 API: {target_name} | 当前模型: {self.api_cfg.get('model')}")

    def _on_active_model_changed(self, event=None):
        new_model = self.var_active_model.get().strip()
        if not new_model:
            return
        self.api_cfg["model"] = new_model
        for a in self.cfg_data.get("apis", []):
            if a.get("name") == self.api_cfg.get("name"):
                a["model"] = new_model
                break
        save_all_apis_config(self.cfg_data)
        self.translator.update_config(self.api_cfg)
        self.log(f"[模型已更新] 当前使用的模型设置为: {new_model}")

    def _fetch_current_models_quick(self):
        if self.api_cfg.get("format") == "jethub" and HAS_JET_HUB:
            provider = self.api_cfg.get("provider", "buddy")
            self.log(f"[JetHub拉取模型] 正在获取渠道 {provider} 的可用模型列表...")
            def _jh_worker():
                models = []
                if provider == "antigravity":
                    mgr = JetHubAccountManager()
                    models = mgr.antigravity_client.get_models()
                else:
                    p_info = PROVIDERS_INFO.get(provider, {})
                    models = p_info.get("defaultModels", [])
                def _jh_done():
                    if models:
                        self.combo_active_model["values"] = models
                        self.log(f"[JetHub模型拉取成功] 获取到 {len(models)} 款模型，请在下拉列表中点选！")
                        self.combo_active_model.event_generate("<Button-1>")
                    else:
                        self.log("[JetHub提示] 暂未获取到模型，请检查账号状态。")
                self.root.after(0, _jh_done)
            threading.Thread(target=_jh_worker, daemon=True).start()
            return

        url = self.api_cfg.get("url", "")
        key = self.api_cfg.get("key", "")
        auth = self.api_cfg.get("auth", "bearer")

        self.log(f"[拉取模型] 正在向 {url}/models 查询可用模型...")

        def _worker():
            try:
                models = fetch_remote_models(url, key, auth_type=auth)
                def _done():
                    if models:
                        self.combo_active_model["values"] = models
                        self.log(f"[拉取成功] 获取到 {len(models)} 款可用模型，请在下拉列表中点选！")
                        self.combo_active_model.event_generate("<Button-1>")
                    else:
                        self.log("[提示] 该 API 响应成功但未提供模型列表，可手动输入模型名称。")
                self.root.after(0, _done)
            except Exception as exc:
                self.log(f"[拉取模型失败]: {exc}")
        threading.Thread(target=_worker, daemon=True).start()

    def _open_jet_hub(self):
        if not HAS_JET_HUB:
            messagebox.showerror("错误", "未能成功加载 Jet Hub 模块，请检查 jet_hub_dialog.py 与 jet_hub_service.py。")
            return
        JetHubDialog(self.root, on_update_callback=self._on_jet_hub_updated)

    def _on_jet_hub_updated(self):
        self.log("[JetHub提示] 账号池配置已更新，正在同步最新凭据与状态...")
        self._refresh_api_dropdown_values()

    def _open_api_manager(self):
        def _on_save(saved_item):
            self.cfg_data = load_all_apis_config()
            self.api_cfg = saved_item
            self.translator.update_config(self.api_cfg)
            self._refresh_api_dropdown_values()
            self.var_active_api.set(saved_item.get("name", ""))
            self.var_active_model.set(saved_item.get("model", ""))
            self.log(f"[配置生效] 激活大模型已更新为: {saved_item.get('name')} [{saved_item.get('model')}]")

        ApiManagerDialog(self.root, _on_save)

    # ---------------- 模式一：翻译已有字幕文件 ----------------
    def _build_sub_tab(self, parent):
        desc = (
            "【模式说明】：针对已有外语字幕文件（如外挂 .srt / .vtt / .ass），直接调用 AI 大模型批量翻译：\n"
            "1. 选中字幕文件，选择原语种与目标语言；\n"
            "2. 可选勾选「将翻译后字幕合成到视频中」（支持无损极速内嵌软字幕 或 硬字幕画面压制）；\n"
            "3. 点击「开始一键翻译字幕」，极速生成高质量中文字幕文件与成品视频！"
        )
        tk.Label(parent, text=desc, justify="left", bg="#252526", fg="#cccccc", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 10))

        # 字幕文件
        file_box = tk.Frame(parent, bg="#252526")
        file_box.pack(fill="x", pady=4)
        tk.Label(file_box, text="目标字幕文件：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 10)).pack(side="left")
        self.sub_file_var = tk.StringVar()
        entry_sub_file = tk.Entry(file_box, textvariable=self.sub_file_var, bg="#1e1e1e", fg="#ffffff", insertbackground="#fff", font=("Microsoft YaHei UI", 9))
        entry_sub_file.pack(side="left", fill="x", expand=True, padx=8)

        btn_browse_sub = tk.Button(file_box, text="浏览字幕...", bg="#3a3d41", fg="#ffffff", bd=0, padx=12, pady=2, command=self._browse_subtitle_file)
        btn_browse_sub.pack(side="right")

        # 关联原视频文件（可选）
        v_box = tk.Frame(parent, bg="#252526")
        v_box.pack(fill="x", pady=4)
        tk.Label(v_box, text="对应原视频 (可选)：", bg="#252526", fg="#aaaaaa", font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.sub_video_var = tk.StringVar()
        entry_sub_v = tk.Entry(v_box, textvariable=self.sub_video_var, bg="#1e1e1e", fg="#ffffff", insertbackground="#fff", font=("Microsoft YaHei UI", 9))
        entry_sub_v.pack(side="left", fill="x", expand=True, padx=8)

        btn_browse_v = tk.Button(v_box, text="浏览视频...", bg="#3a3d41", fg="#ffffff", bd=0, padx=12, pady=2, command=self._browse_sub_assoc_video)
        btn_browse_v.pack(side="right")

        # 语言与双语选项
        opts_box = tk.Frame(parent, bg="#252526")
        opts_box.pack(fill="x", pady=6)

        lang_labels = [opt[0] for opt in LANGUAGE_OPTIONS]
        target_labels = [opt[0] for opt in TARGET_LANGUAGE_OPTIONS]

        tk.Label(opts_box, text="字幕原语种：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="w", pady=4)
        self.sub_lang_var = tk.StringVar(value="自动识别 (Auto Detect)")
        sub_lang_combo = ttk.Combobox(opts_box, textvariable=self.sub_lang_var, state="readonly", width=22)
        sub_lang_combo["values"] = lang_labels
        sub_lang_combo.grid(row=0, column=1, sticky="w", padx=(6, 20), pady=4)

        tk.Label(opts_box, text="目标字幕语言：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=0, column=2, sticky="w", pady=4)
        self.sub_target_lang_var = tk.StringVar(value="简体中文 (zh-CN)")
        sub_t_lang_combo = ttk.Combobox(opts_box, textvariable=self.sub_target_lang_var, state="readonly", width=18)
        sub_t_lang_combo["values"] = target_labels
        sub_t_lang_combo.grid(row=0, column=3, sticky="w", padx=(6, 12), pady=4)

        self.sub_dual_var = tk.BooleanVar(value=False)
        chk_dual = tk.Checkbutton(
            opts_box,
            text="输出双语对照字幕 (中文在上，原文在下)",
            variable=self.sub_dual_var,
            bg="#252526",
            fg="#61afef",
            selectcolor="#1e1e1e",
            activebackground="#252526",
            activeforeground="#61afef",
            font=("Microsoft YaHei UI", 9)
        )
        chk_dual.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)

        # 视频合成模式单选
        mux_box = tk.LabelFrame(parent, text="视频合成选项", bg="#252526", fg="#aaaaaa", font=("Microsoft YaHei UI", 9))
        mux_box.pack(fill="x", pady=6)

        self.sub_mux_mode_var = tk.StringVar(value="none")
        tk.Radiobutton(mux_box, text="仅生成字幕文件 (不合成视频)", variable=self.sub_mux_mode_var, value="none", bg="#252526", fg="#ffffff", selectcolor="#1e1e1e").pack(side="left", padx=10, pady=4)
        tk.Radiobutton(mux_box, text="⚡ 极速内嵌软字幕 (推荐，无损秒级完成)", variable=self.sub_mux_mode_var, value="soft", bg="#252526", fg="#98c379", selectcolor="#1e1e1e").pack(side="left", padx=10, pady=4)
        tk.Radiobutton(mux_box, text="🔥 硬字幕画面压制 (烧录进画面，手机/车载兼容)", variable=self.sub_mux_mode_var, value="hard", bg="#252526", fg="#e5c07b", selectcolor="#1e1e1e").pack(side="left", padx=10, pady=4)

        # 进度条
        self.sub_progress_var = tk.DoubleVar(value=0)
        self.sub_progress_bar = ttk.Progressbar(parent, variable=self.sub_progress_var, maximum=100)
        self.sub_progress_bar.pack(fill="x", pady=10)

        self.sub_status_lbl = tk.Label(parent, text="就绪", bg="#252526", fg="#aaaaaa", font=("Microsoft YaHei UI", 9))
        self.sub_status_lbl.pack(anchor="w")

        action_box = tk.Frame(parent, bg="#252526")
        action_box.pack(fill="x", pady=8)

        self.btn_start_sub = tk.Button(
            action_box,
            text="⚡ 开始一键翻译字幕",
            bg="#2ecc71",
            fg="#ffffff",
            font=("Microsoft YaHei UI", 11, "bold"),
            activebackground="#27ae60",
            activeforeground="#ffffff",
            bd=0,
            padx=20,
            pady=8,
            command=self._start_sub_translation
        )
        self.btn_start_sub.pack(side="left")

        self.btn_cancel_sub = tk.Button(
            action_box,
            text="取消任务",
            bg="#3a3d41",
            fg="#ff6b6b",
            font=("Microsoft YaHei UI", 10),
            bd=0,
            padx=16,
            pady=8,
            state="disabled",
            command=self._cancel_sub_translation
        )
        self.btn_cancel_sub.pack(side="left", padx=12)

    def _browse_subtitle_file(self):
        p = filedialog.askopenfilename(
            title="选择要翻译的字幕文件",
            filetypes=[("字幕文件", "*.srt *.vtt *.ass"), ("所有文件", "*.*")]
        )
        if p:
            self.sub_file_var.set(p)
            # 自动探测同名视频
            bname, _ = os.path.splitext(p)
            for ext in [".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm"]:
                cand = bname + ext
                if os.path.isfile(cand):
                    self.sub_video_var.set(cand)
                    break

    def _browse_sub_assoc_video(self):
        p = filedialog.askopenfilename(
            title="选择对应的视频文件",
            filetypes=[("视频文件", "*.mp4 *.mkv *.avi *.mov *.flv *.webm *.ts"), ("所有文件", "*.*")]
        )
        if p:
            self.sub_video_var.set(p)

    def _start_sub_translation(self):
        srt_path = self.sub_file_var.get().strip()
        if not srt_path or not os.path.isfile(srt_path):
            messagebox.showwarning("提示", "请先选择一个有效的字幕文件！")
            return

        video_path = self.sub_video_var.get().strip()
        mux_mode = self.sub_mux_mode_var.get()
        if mux_mode != "none" and (not video_path or not os.path.isfile(video_path)):
            messagebox.showwarning("提示", "您勾选了合成视频，但尚未选定有效的视频文件！")
            return

        src_code = lang_label_to_code(self.sub_lang_var.get())
        dst_code = target_lang_label_to_code(self.sub_target_lang_var.get())
        dual_mode = self.sub_dual_var.get()

        self.btn_start_sub.config(state="disabled")
        self.btn_cancel_sub.config(state="normal")
        self.sub_progress_var.set(0)

        def _worker():
            ok = self.sub_pipeline.process(
                srt_path,
                video_path=video_path,
                src_lang=src_code,
                dst_lang=dst_code,
                dual_mode=dual_mode,
                mux_mode=mux_mode
            )
            def _done():
                self.btn_start_sub.config(state="normal")
                self.btn_cancel_sub.config(state="disabled")
                if ok:
                    msg = "字幕翻译完毕！"
                    if mux_mode != "none":
                        msg += "\n\n并且已成功合成带字幕的成品视频！"
                    messagebox.showinfo("完成", msg)
            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _cancel_sub_translation(self):
        self.sub_pipeline.cancel()
        self.log("[字幕翻译] 已请求取消任务...")
        self.sub_status_lbl.config(text="任务已取消")
        self.btn_start_sub.config(state="normal")
        self.btn_cancel_sub.config(state="disabled")

    def update_sub_progress(self, percent, text):
        def _do():
            self.sub_progress_var.set(percent)
            self.sub_status_lbl.config(text=f"{text} ({percent}%)")
        self.root.after(0, _do)

    # ---------------- 模式二：无字幕视频提取并翻译字幕 ----------------
    def _build_video_tab(self, parent):
        desc = (
            "【模式说明】：针对无字幕纯视频（生肉影视、动画等），先提取音频再转写翻译：\n"
            "1. 选中生肉视频文件，选择原语种、目标语言及识别精度；\n"
            "2. 可选直接合成带字幕视频（支持无损极速内嵌软字幕 或 硬字幕压制）；\n"
            "3. 点击「开始一键生字幕」，程序自动切分时间轴、分块调用 AI 翻译并输出成品！"
        )
        tk.Label(parent, text=desc, justify="left", bg="#252526", fg="#cccccc", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 10))

        file_box = tk.Frame(parent, bg="#252526")
        file_box.pack(fill="x", pady=6)

        tk.Label(file_box, text="目标视频文件：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 10)).pack(side="left")
        self.video_file_var = tk.StringVar()
        entry_video_file = tk.Entry(file_box, textvariable=self.video_file_var, bg="#1e1e1e", fg="#ffffff", insertbackground="#fff", font=("Microsoft YaHei UI", 9))
        entry_video_file.pack(side="left", fill="x", expand=True, padx=8)

        btn_browse_video = tk.Button(file_box, text="浏览视频...", bg="#3a3d41", fg="#ffffff", bd=0, padx=12, pady=2, command=self._browse_video_file)
        btn_browse_video.pack(side="right")

        opts_box = tk.Frame(parent, bg="#252526")
        opts_box.pack(fill="x", pady=6)

        lang_labels = [opt[0] for opt in LANGUAGE_OPTIONS]
        target_labels = [opt[0] for opt in TARGET_LANGUAGE_OPTIONS]

        tk.Label(opts_box, text="视频原语种：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="w", pady=4)
        self.video_lang_var = tk.StringVar(value="日语 (ja / 日本語)")
        v_lang_combo = ttk.Combobox(opts_box, textvariable=self.video_lang_var, state="readonly", width=22)
        v_lang_combo["values"] = lang_labels
        v_lang_combo.grid(row=0, column=1, sticky="w", padx=(6, 20), pady=4)

        tk.Label(opts_box, text="目标字幕语言：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=0, column=2, sticky="w", pady=4)
        self.video_target_lang_var = tk.StringVar(value="简体中文 (zh-CN)")
        v_t_lang_combo = ttk.Combobox(opts_box, textvariable=self.video_target_lang_var, state="readonly", width=18)
        v_t_lang_combo["values"] = target_labels
        v_t_lang_combo.grid(row=0, column=3, sticky="w", padx=(6, 12), pady=4)

        tk.Label(opts_box, text="转写精度(推荐small/medium)：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, sticky="w", pady=4)
        self.video_model_var = tk.StringVar(value="small")
        v_model_combo = ttk.Combobox(opts_box, textvariable=self.video_model_var, state="readonly", width=22)
        v_model_combo["values"] = ("base", "small", "medium", "large-v3")
        v_model_combo.grid(row=1, column=1, sticky="w", padx=(6, 20), pady=4)

        self.video_dual_var = tk.BooleanVar(value=False)
        chk_v_dual = tk.Checkbutton(
            opts_box,
            text="输出双语对照字幕",
            variable=self.video_dual_var,
            bg="#252526",
            fg="#61afef",
            selectcolor="#1e1e1e",
            activebackground="#252526",
            activeforeground="#61afef",
            font=("Microsoft YaHei UI", 9)
        )
        chk_v_dual.grid(row=1, column=2, columnspan=2, sticky="w", pady=4)

        # Row 2: 计算加速设备 (自动识别 GPU 型号，支持多显卡点选)
        tk.Label(opts_box, text="计算加速设备：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 9)).grid(row=2, column=0, sticky="w", pady=4)
        gpu_options = []
        for g in self.available_gpus:
            gpu_options.append(f"[GPU {g['index']}] {g['name']} ({g['memory']})")
        gpu_options.append("仅使用 CPU (不占用显卡)")

        default_gpu = gpu_options[0] if self.available_gpus else "仅使用 CPU (不占用显卡)"
        self.video_gpu_var = tk.StringVar(value=default_gpu)
        v_gpu_combo = ttk.Combobox(opts_box, textvariable=self.video_gpu_var, state="readonly", width=36)
        v_gpu_combo["values"] = gpu_options
        v_gpu_combo.grid(row=2, column=1, columnspan=3, sticky="w", padx=(6, 20), pady=4)

        # 视频合成模式
        v_mux_box = tk.LabelFrame(parent, text="视频合成选项", bg="#252526", fg="#aaaaaa", font=("Microsoft YaHei UI", 9))
        v_mux_box.pack(fill="x", pady=6)

        self.video_mux_mode_var = tk.StringVar(value="soft")
        tk.Radiobutton(v_mux_box, text="仅输出 .srt 字幕文件", variable=self.video_mux_mode_var, value="none", bg="#252526", fg="#ffffff", selectcolor="#1e1e1e").pack(side="left", padx=10, pady=4)
        tk.Radiobutton(v_mux_box, text="⚡ 自动封装极速软字幕 (推荐，无损秒出成品)", variable=self.video_mux_mode_var, value="soft", bg="#252526", fg="#98c379", selectcolor="#1e1e1e").pack(side="left", padx=10, pady=4)
        tk.Radiobutton(v_mux_box, text="🔥 自动压制硬字幕 (画面烙印，手机微信通用)", variable=self.video_mux_mode_var, value="hard", bg="#252526", fg="#e5c07b", selectcolor="#1e1e1e").pack(side="left", padx=10, pady=4)

        # 进度条
        self.video_progress_var = tk.DoubleVar(value=0)
        self.video_progress_bar = ttk.Progressbar(parent, variable=self.video_progress_var, maximum=100)
        self.video_progress_bar.pack(fill="x", pady=10)

        self.video_status_lbl = tk.Label(parent, text="就绪", bg="#252526", fg="#aaaaaa", font=("Microsoft YaHei UI", 9))
        self.video_status_lbl.pack(anchor="w")

        action_box = tk.Frame(parent, bg="#252526")
        action_box.pack(fill="x", pady=8)

        self.btn_start_video = tk.Button(
            action_box,
            text="⚡ 开始一键提取并生成字幕",
            bg="#007acc",
            fg="#ffffff",
            font=("Microsoft YaHei UI", 11, "bold"),
            activebackground="#005999",
            activeforeground="#ffffff",
            bd=0,
            padx=20,
            pady=8,
            command=self._start_video_processing
        )
        self.btn_start_video.pack(side="left")

        self.btn_cancel_video = tk.Button(
            action_box,
            text="取消任务",
            bg="#3a3d41",
            fg="#ff6b6b",
            font=("Microsoft YaHei UI", 10),
            bd=0,
            padx=16,
            pady=8,
            state="disabled",
            command=self._cancel_video_processing
        )
        self.btn_cancel_video.pack(side="left", padx=12)

    def _browse_video_file(self):
        p = filedialog.askopenfilename(
            title="选择要生成字幕的视频文件",
            filetypes=[("视频文件", "*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.webm *.ts"), ("所有文件", "*.*")]
        )
        if p:
            self.video_file_var.set(p)

    def _start_video_processing(self):
        video_path = self.video_file_var.get().strip()
        if not video_path or not os.path.isfile(video_path):
            messagebox.showwarning("提示", "请先选择一个有效的视频文件！")
            return

        src_code = lang_label_to_code(self.video_lang_var.get())
        dst_code = target_lang_label_to_code(self.video_target_lang_var.get())
        dual_mode = self.video_dual_var.get()
        mux_mode = self.video_mux_mode_var.get()

        selected_gpu_str = self.video_gpu_var.get()
        gpu_idx = 0
        if "仅使用 CPU" in selected_gpu_str or not self.available_gpus:
            gpu_idx = -1
        else:
            m = re.search(r"\[GPU\s*(\d+)\]", selected_gpu_str)
            if m:
                gpu_idx = int(m.group(1))

        self.btn_start_video.config(state="disabled")
        self.btn_cancel_video.config(state="normal")
        self.video_progress_var.set(0)

        def _worker():
            ok = self.video_pipeline.process(
                video_path,
                model_name=self.video_model_var.get(),
                src_lang=src_code,
                dst_lang=dst_code,
                dual_mode=dual_mode,
                mux_mode=mux_mode,
                gpu_idx=gpu_idx
            )
            def _done():
                self.btn_start_video.config(state="normal")
                self.btn_cancel_video.config(state="disabled")
                if ok:
                    msg = "字幕提取并翻译完成！"
                    if mux_mode != "none":
                        msg += "\n\n并且已成功合成带字幕成品视频！"
                    messagebox.showinfo("完成", msg)
            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _cancel_video_processing(self):
        self.video_pipeline.cancel()
        self.log("[视频生字幕] 已请求取消任务...")
        self.video_status_lbl.config(text="任务已取消")
        self.btn_start_video.config(state="normal")
        self.btn_cancel_video.config(state="disabled")

    def update_video_progress(self, percent, text):
        def _do():
            self.video_progress_var.set(percent)
            self.video_status_lbl.config(text=f"{text} ({percent}%)")
        self.root.after(0, _do)

    # ---------------- 模式三：字幕直接合成到视频工具箱 ----------------
    def _build_mux_tab(self, parent):
        desc = (
            "【工具说明】：直接将已有视频与字幕文件合成，无需重新翻译：\n"
            "• 极速内嵌软字幕：1~2秒极速完成，100% 保持原始画质，播放器中可开关与切换字幕轨；\n"
            "• 硬字幕压制：直接将字幕烧录烙印在视频画面上，支持 NVIDIA 显卡高速压制，适合任何手机与播放器。"
        )
        tk.Label(parent, text=desc, justify="left", bg="#252526", fg="#cccccc", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 12))

        # 视频文件
        f1 = tk.Frame(parent, bg="#252526")
        f1.pack(fill="x", pady=6)
        tk.Label(f1, text="选择源视频文件：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 10)).pack(side="left")
        self.tool_video_var = tk.StringVar()
        ent_tv = tk.Entry(f1, textvariable=self.tool_video_var, bg="#1e1e1e", fg="#ffffff", insertbackground="#fff", font=("Microsoft YaHei UI", 9))
        ent_tv.pack(side="left", fill="x", expand=True, padx=8)
        btn_tv = tk.Button(f1, text="浏览视频...", bg="#3a3d41", fg="#ffffff", bd=0, padx=12, pady=2, command=self._browse_tool_video)
        btn_tv.pack(side="right")

        # 字幕文件
        f2 = tk.Frame(parent, bg="#252526")
        f2.pack(fill="x", pady=6)
        tk.Label(f2, text="选择对应字幕文件：", bg="#252526", fg="#ffffff", font=("Microsoft YaHei UI", 10)).pack(side="left")
        self.tool_srt_var = tk.StringVar()
        ent_ts = tk.Entry(f2, textvariable=self.tool_srt_var, bg="#1e1e1e", fg="#ffffff", insertbackground="#fff", font=("Microsoft YaHei UI", 9))
        ent_ts.pack(side="left", fill="x", expand=True, padx=8)
        btn_ts = tk.Button(f2, text="浏览字幕...", bg="#3a3d41", fg="#ffffff", bd=0, padx=12, pady=2, command=self._browse_tool_srt)
        btn_ts.pack(side="right")

        # 合成方式单选
        m_box = tk.LabelFrame(parent, text="选择合成方式", bg="#252526", fg="#aaaaaa", font=("Microsoft YaHei UI", 9))
        m_box.pack(fill="x", pady=12)

        self.tool_mux_type_var = tk.StringVar(value="soft")
        tk.Radiobutton(m_box, text="⚡ 极速内嵌软字幕 (推荐，无损封装，1秒搞定，播放器可开关)", variable=self.tool_mux_type_var, value="soft", bg="#252526", fg="#98c379", selectcolor="#1e1e1e").pack(anchor="w", padx=14, pady=4)
        tk.Radiobutton(m_box, text="🔥 硬字幕画面压制 (画面烧录，支持 NVIDIA GPU 极速压制，任何设备兼容)", variable=self.tool_mux_type_var, value="hard", bg="#252526", fg="#e5c07b", selectcolor="#1e1e1e").pack(anchor="w", padx=14, pady=4)

        # 硬件加速设备选择
        gpu_options = []
        for g in self.available_gpus:
            gpu_options.append(f"[GPU {g['index']}] {g['name']} ({g['memory']})")
        gpu_options.append("仅使用 CPU (不占用显卡)")
        default_gpu = gpu_options[0] if self.available_gpus else "仅使用 CPU (不占用显卡)"

        f3 = tk.Frame(m_box, bg="#252526")
        f3.pack(fill="x", padx=14, pady=(4, 8))
        tk.Label(f3, text="压制加速设备：", bg="#252526", fg="#aaaaaa", font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.tool_gpu_var = tk.StringVar(value=default_gpu)
        t_gpu_combo = ttk.Combobox(f3, textvariable=self.tool_gpu_var, state="readonly", width=34)
        t_gpu_combo["values"] = gpu_options
        t_gpu_combo.pack(side="left", padx=4)

        # 进度与状态显示
        self.tool_mux_prog_var = tk.DoubleVar(value=0)
        self.tool_mux_prog_bar = ttk.Progressbar(parent, variable=self.tool_mux_prog_var, maximum=100)
        self.tool_mux_prog_bar.pack(fill="x", pady=(10, 4))

        self.tool_mux_status_lbl = tk.Label(parent, text="就绪", bg="#252526", fg="#aaaaaa", font=("Microsoft YaHei UI", 9))
        self.tool_mux_status_lbl.pack(anchor="w", pady=(0, 6))

        # 开始合成按钮与取消按钮
        act = tk.Frame(parent, bg="#252526")
        act.pack(fill="x", pady=10)

        self.btn_run_mux = tk.Button(
            act,
            text="🎬 开始一键合成视频",
            bg="#0e639c",
            fg="#ffffff",
            font=("Microsoft YaHei UI", 11, "bold"),
            activebackground="#1177bb",
            activeforeground="#ffffff",
            bd=0,
            padx=24,
            pady=8,
            command=self._start_standalone_mux
        )
        self.btn_run_mux.pack(side="left")

        self.tool_mux_cancelled = False
        self.btn_cancel_mux = tk.Button(
            act,
            text="取消任务",
            bg="#3a3d41",
            fg="#ff6b6b",
            font=("Microsoft YaHei UI", 10),
            bd=0,
            padx=16,
            pady=8,
            state="disabled",
            command=self._cancel_standalone_mux
        )
        self.btn_cancel_mux.pack(side="left", padx=12)

    def _cancel_standalone_mux(self):
        self.tool_mux_cancelled = True
        self.log("[合成工具箱] 已请求取消视频合成任务...")
        self.tool_mux_status_lbl.config(text="正在取消中...")
        self.btn_cancel_mux.config(state="disabled")

    def _browse_tool_video(self):
        p = filedialog.askopenfilename(
            title="选择要合成字幕的视频",
            filetypes=[("视频文件", "*.mp4 *.mkv *.avi *.mov *.flv *.webm *.ts"), ("所有文件", "*.*")]
        )
        if p:
            self.tool_video_var.set(p)
            bname, _ = os.path.splitext(p)
            for ext in [".zh-CN.srt", ".srt", ".vtt"]:
                cand = bname + ext
                if os.path.isfile(cand):
                    self.tool_srt_var.set(cand)
                    break

    def _browse_tool_srt(self):
        p = filedialog.askopenfilename(
            title="选择字幕文件",
            filetypes=[("字幕文件", "*.srt *.vtt *.ass"), ("所有文件", "*.*")]
        )
        if p:
            self.tool_srt_var.set(p)

    def _start_standalone_mux(self):
        v = self.tool_video_var.get().strip()
        s = self.tool_srt_var.get().strip()
        if not v or not os.path.isfile(v):
            messagebox.showwarning("提示", "请选择有效的视频文件！")
            return
        if not s or not os.path.isfile(s):
            messagebox.showwarning("提示", "请选择有效的字幕文件！")
            return

        mtype = self.tool_mux_type_var.get()
        selected_gpu_str = self.tool_gpu_var.get()
        gpu_idx = 0
        if "仅使用 CPU" in selected_gpu_str or not self.available_gpus:
            gpu_idx = -1
        else:
            m = re.search(r"\[GPU\s*(\d+)\]", selected_gpu_str)
            if m:
                gpu_idx = int(m.group(1))

        self.tool_mux_cancelled = False
        self.tool_mux_prog_var.set(0)
        self.btn_run_mux.config(state="disabled")
        self.btn_cancel_mux.config(state="normal")
        self.tool_mux_status_lbl.config(text="准备启动视频合成进程...")

        def _on_prog(pct, text):
            def _update():
                self.tool_mux_prog_var.set(pct)
                self.tool_mux_status_lbl.config(text=text)
            self.root.after(0, _update)

        def _worker():
            if mtype == "soft":
                ok, out_p = VideoSubtitleMuxer.mux_soft_subtitle(
                    v, s, log_cb=self.log,
                    progress_cb=_on_prog,
                    cancel_checker=lambda: self.tool_mux_cancelled
                )
            else:
                ok, out_p = VideoSubtitleMuxer.burn_hard_subtitle(
                    v, s, gpu_idx=gpu_idx, log_cb=self.log,
                    progress_cb=_on_prog,
                    cancel_checker=lambda: self.tool_mux_cancelled
                )

            def _done():
                self.btn_run_mux.config(state="normal")
                self.btn_cancel_mux.config(state="disabled")
                if self.tool_mux_cancelled:
                    self.tool_mux_status_lbl.config(text="任务已取消")
                    self.tool_mux_prog_var.set(0)
                elif ok and out_p:
                    self.tool_mux_prog_var.set(100)
                    self.tool_mux_status_lbl.config(text=f"合成成功！已输出: {os.path.basename(out_p)}")
                    messagebox.showinfo("完成", f"视频合成完毕！\n\n输出视频文件：\n{out_p}")
                else:
                    self.tool_mux_status_lbl.config(text="合成失败，请查看运行日志")
            self.root.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _test_api_connection(self):
        self.log(f"[测试] 正在测试当前大模型 ({self.api_cfg.get('name')} -> {self.api_cfg.get('model')})...")
        def _test():
            self.translator.cache.clear()
            res = self.translator.translate("こんにちは、字幕のテストです。", src_lang="ja", dst_lang="zh-CN")
            def _show():
                if res and res != "こんにちは、字幕のテストです。":
                    self.log(f"[测试成功] 模型返回: {res}")
                    messagebox.showinfo("连接成功", f"大模型接口连通正常！\n\n测试原文: こんにちは、字幕のテストです。\n模型翻译: {res}")
                else:
                    self.log(f"[测试响应异常]: {res}")
                    messagebox.showwarning("连接告警", f"模型已响应但未正常翻译: {res}")
            self.root.after(0, _show)
        threading.Thread(target=_test, daemon=True).start()

    def _open_log_file(self):
        try:
            if not os.path.isfile(LOG_FILE_PATH):
                with open(LOG_FILE_PATH, "w", encoding="utf-8") as fh:
                    fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 日志文件初始化成功。\n")
            os.startfile(LOG_FILE_PATH)
        except Exception as exc:
            messagebox.showerror("打开日志失败", f"无法打开日志文件：{exc}")

    def _clear_log_display(self):
        self.log_text.delete("1.0", "end")

    def log(self, text):
        write_runtime_log(text)
        def _do():
            ts = time.strftime("%H:%M:%S")
            self.log_text.insert("end", f"[{ts}] {text}\n")
            self.log_text.see("end")
        self.root.after(0, _do)


def main():
    root = tk.Tk()
    app = AppWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
