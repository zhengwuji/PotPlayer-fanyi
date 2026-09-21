# -*- coding: utf-8 -*-
"""
Jet Hub 核心服务调度器 (Jet Hub Service Engine)
移植自 F:\\源码\\dsh-codearts-auth

支持：
1. 华为云 CodeArts Agent (IAM/STS)
2. 腾讯 CodeBuddy (国内版 / 国际版)
3. 腾讯 WorkBuddy (国内版 / 国际版)
4. Google Antigravity (本地 Language Server 私有 RPC 隧道直连，零配置免Key)

特性：
- 自动读取无缝复用本机 DSH 已配置的有效凭据 (.credentials.yaml / settings.yaml)
- 多账号池 (Account Pool) 智能轮换、限流检测与自动避让
- 腾讯生态一键每日签到与免费积分领取
- 标准与流式大模型推理翻译封装，直接为字幕工具赋能
"""

import os
import re
import sys
import time
import json
import math
import uuid
import socket
import logging
import subprocess
import threading
from datetime import datetime
import requests

# 尝试导入 yaml，若无则使用内建安全解析
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# ---------------------------------------------------------------------------
# 渠道 Provider 配置元数据
# ---------------------------------------------------------------------------
PROVIDERS_INFO = {
    "codearts": {
        "id": "codearts",
        "displayName": "CodeArts (华为云)",
        "endpoint": "https://snap-access.cn-north-4.myhuaweicloud.com",
        "defaultModels": [
            "deepseek-v4-pro",
            "deepseek-v4-lite",
            "deepseek-v3",
            "deepseek-r1",
            "codearts-snap"
        ]
    },
    "buddy": {
        "id": "buddy",
        "displayName": "CodeBuddy (国内版)",
        "endpoint": "https://copilot.tencent.com",
        "apiDomain": "copilot.tencent.com",
        "productCode": "codebuddy",
        "userAgent": "CodeBuddyIDE/1.106.1",
        "defaultModels": [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "glm-5.3",
            "glm-5.3-flash",
            "kimi-k3-1",
            "kimi-k2.7",
            "minimax-m3",
            "hy4-preview",
            "hy3"
        ]
    },
    "buddy-intl": {
        "id": "buddy-intl",
        "displayName": "CodeBuddy (国际版)",
        "endpoint": "https://www.codebuddy.ai",
        "apiDomain": "www.codebuddy.ai",
        "productCode": "codebuddy",
        "userAgent": "CodeBuddyIDE/1.106.1",
        "defaultModels": [
            "deepseek-v4.1-flash",
            "gpt-6-astra",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gemini-3.5-flash",
            "glm-5.3",
            "kimi-k3"
        ]
    },
    "workbuddy-cn": {
        "id": "workbuddy-cn",
        "displayName": "WorkBuddy (国内版)",
        "endpoint": "https://copilot.tencent.com",
        "apiDomain": "copilot.tencent.com",
        "productCode": "workbuddy",
        "userAgent": "CodeBuddyIDE/1.106.1",
        "defaultModels": [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "glm-5.3",
            "kimi-k3-1",
            "hy4-preview"
        ]
    },
    "workbuddy": {
        "id": "workbuddy",
        "displayName": "WorkBuddy (国际版)",
        "endpoint": "https://www.workbuddy.ai",
        "apiDomain": "www.workbuddy.ai",
        "productCode": "workbuddy",
        "userAgent": "CodeBuddyIDE/1.106.1",
        "defaultModels": [
            "deepseek-v4.1-flash",
            "gpt-6-astra",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.5",
            "gemini-3.5-flash",
            "glm-5.3",
            "kimi-k3"
        ]
    },
    "antigravity": {
        "id": "antigravity",
        "displayName": "Antigravity (Google)",
        "endpoint": "http://127.0.0.1 (本地私有RPC直连)",
        "defaultModels": [
            "Gemini 3.8 Flash (High)",
            "Gemini 3.7 Flash",
            "Gemini 3.1 Pro (High)",
            "Claude 3.7 Sonnet (Thinking)",
            "Claude 3.5 Sonnet",
            "GPT-OSS 120B (Medium)"
        ]
    }
}

LOCAL_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jet_hub_accounts.json")


# ---------------------------------------------------------------------------
# DSH 凭据提取与持久化
# ---------------------------------------------------------------------------
def get_dsh_paths():
    """获取本机 DSH Desktop 的配置和凭据文件路径"""
    appdata = os.environ.get("APPDATA", "")
    harness_dir = os.path.join(appdata, "dsh-desktop", "harness")
    settings_yaml = os.path.join(harness_dir, "settings.yaml")
    cred_yaml = os.path.join(harness_dir, ".credentials.yaml")
    return settings_yaml, cred_yaml


def load_yaml_safely(filepath):
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        if HAS_YAML:
            return yaml.safe_load(content)
        else:
            return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Antigravity 本地语言服务器自动探活与 RPC 直连
# ---------------------------------------------------------------------------
class AntigravityLocalClient:
    """
    Antigravity 本地私有 RPC 直连客户端
    通过本地 loopback 直连 IDE 内置的 language_server_windows_x64.exe，
    完全免 API Key，直接调用 Gemini 3.8 Flash / Claude 3.7 Sonnet 等模型。
    """
    def __init__(self):
        self.cached_instance = None
        self._lock = threading.Lock()

    def discover(self, force_refresh=False):
        with self._lock:
            if self.cached_instance and not force_refresh:
                if self._check_heartbeat(self.cached_instance):
                    return self.cached_instance

            inst = self._find_instance()
            self.cached_instance = inst
            return inst

    def _check_heartbeat(self, inst):
        try:
            url = f"http://127.0.0.1:{inst['port']}/exa.language_server_pb.LanguageServerService/Heartbeat"
            headers = {
                "Content-Type": "application/json",
                "x-codeium-csrf-token": inst["csrf_token"]
            }
            res = requests.post(url, json={}, headers=headers, timeout=1.5)
            return res.status_code == 200
        except Exception:
            return False

    def _find_instance(self):
        """探测本地 language_server 进程及其 CSRF Token 和监听端口"""
        if sys.platform != "win32":
            return None
        try:
            cmd = 'powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \\"Name like \'language_server%\'\\" | Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"'
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, _ = p.communicate(timeout=4)
            if not out:
                return None
            data = json.loads(out.decode("utf-8", errors="ignore"))
            if isinstance(data, dict):
                procs = [data]
            elif isinstance(data, list):
                procs = data
            else:
                procs = []

            for item in procs:
                pid = item.get("ProcessId")
                cline = item.get("CommandLine", "")
                if not cline or not pid:
                    continue

                m = re.search(r"(?:^|\s)--csrf_token[ =](\S+)", cline)
                if not m:
                    continue
                csrf_token = m.group(1).strip()

                ports = self._get_listening_ports(pid)
                candidate_ports = list(ports) + [17013, 22931, 34771, 34772]
                seen = set()
                for pt in candidate_ports:
                    if pt in seen:
                        continue
                    seen.add(pt)
                    inst = {"port": pt, "csrf_token": csrf_token, "pid": pid}
                    if self._check_heartbeat(inst):
                        return inst
        except Exception:
            pass
        return None

    def _get_listening_ports(self, pid):
        ports = set()
        try:
            cmd = f'netstat -ano | findstr "{pid}"'
            out = subprocess.check_output(cmd, shell=True, timeout=2).decode("utf-8", errors="ignore")
            for line in out.splitlines():
                line = line.strip()
                if "LISTENING" in line:
                    parts = re.split(r"\s+", line)
                    if len(parts) >= 2:
                        local_addr = parts[1]
                        if ":" in local_addr:
                            port_str = local_addr.split(":")[-1]
                            if port_str.isdigit():
                                ports.add(int(port_str))
        except Exception:
            pass
        return ports

    def get_models(self):
        """从本地 language_server 获取真实模型列表"""
        inst = self.discover()
        if not inst:
            return PROVIDERS_INFO["antigravity"]["defaultModels"]
        try:
            url = f"http://127.0.0.1:{inst['port']}/exa.language_server_pb.LanguageServerService/GetCascadeModelConfigData"
            headers = {
                "Content-Type": "application/json",
                "x-codeium-csrf-token": inst["csrf_token"]
            }
            res = requests.post(url, json={}, headers=headers, timeout=3.0)
            if res.status_code == 200:
                body = res.json()
                configs = body.get("clientModelConfigs", [])
                models = []
                for c in configs:
                    lbl = c.get("label")
                    if lbl and lbl not in models:
                        models.append(lbl)
                if models:
                    return models
        except Exception:
            pass
        return PROVIDERS_INFO["antigravity"]["defaultModels"]


# ---------------------------------------------------------------------------
# 腾讯 CodeBuddy / WorkBuddy 鉴权与 API 客户端
# ---------------------------------------------------------------------------
class BuddyClient:
    """
    CodeBuddy / WorkBuddy 客户端
    使用标准 OpenAI Chat Completions 协议 + 腾讯私有鉴权头通信
    """
    @staticmethod
    def build_headers(cred, product_id):
        product = PROVIDERS_INFO.get(product_id, PROVIDERS_INFO["buddy"])
        domain = cred.get("domain") or product.get("apiDomain", "copilot.tencent.com")
        headers = {
            "Authorization": f"Bearer {cred.get('access_token', '')}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Domain": domain,
            "X-Product": "copilot",
            "X-Product-Code": product.get("productCode", "codebuddy"),
            "User-Agent": product.get("userAgent", "CodeBuddyIDE/1.106.1")
        }
        ent_id = cred.get("enterprise_id")
        if ent_id:
            headers["X-Enterprise-Id"] = ent_id
            headers["X-Tenant-Id"] = ent_id
        return headers

    @staticmethod
    def test_connection(account, timeout=8):
        """测试账号连接状态及延迟 (ms)"""
        product_id = account.get("provider", "buddy")
        product = PROVIDERS_INFO.get(product_id, PROVIDERS_INFO["buddy"])
        cred = account.get("credential", {})
        if not cred or not cred.get("access_token"):
            return False, "缺少有效的 Access Token"

        headers = BuddyClient.build_headers(cred, product_id)
        url = f"{product['endpoint']}/v2/chat/completions"
        payload = {
            "model": account.get("preferred_model") or product["defaultModels"][0],
            "messages": [
                {"role": "user", "content": "hi"}
            ],
            "max_tokens": 5,
            "temperature": 0.1,
            "stream": True
        }
        t0 = time.time()
        try:
            resp = requests.post(url, json=payload, headers=headers, stream=True, timeout=timeout)
            latency_ms = int((time.time() - t0) * 1000)
            if resp.status_code == 200:
                return True, f"正常 (延迟 {latency_ms}ms)"
            elif resp.status_code == 429:
                return False, f"已触发频控 (HTTP 429, 延迟 {latency_ms}ms)"
            else:
                err_snippet = resp.text[:60].replace("\n", " ")
                return False, f"异常 (HTTP {resp.status_code}: {err_snippet})"
        except Exception as e:
            return False, f"网络超时或连接失败: {str(e)[:40]}"

    @staticmethod
    def claim_credits(account):
        """腾讯 CodeBuddy 每日签到一键领取积分"""
        product_id = account.get("provider", "buddy")
        product = PROVIDERS_INFO.get(product_id, PROVIDERS_INFO["buddy"])
        cred = account.get("credential", {})
        if not cred or not cred.get("access_token"):
            return False, "缺少 Token"

        headers = BuddyClient.build_headers(cred, product_id)
        url = f"{product['endpoint']}/v2/billing/meter/daily-checkin"
        try:
            resp = requests.post(url, json={}, headers=headers, timeout=10)
            data = resp.json() if resp.text else {}
            code = data.get("code", -1)
            msg = data.get("msg", "")
            if code == 0:
                credit = data.get("data", {}).get("credit", 0)
                return True, f"领取成功！获得 {credit} 积分"
            elif code in (10001, 1001) or "已签到" in msg:
                return True, "今日已完成签到"
            elif code in (1002, 1003):
                return False, f"暂无领取资格或活动已结束 ({msg})"
            else:
                return False, f"领取失败: {msg or resp.text[:40]}"
        except Exception as e:
            return False, f"请求异常: {str(e)[:40]}"


# ---------------------------------------------------------------------------
# 多账号池管理引擎 (AccountPool Manager)
# ---------------------------------------------------------------------------
class JetHubAccountManager:
    """
    Jet Hub 全局账号池管理器
    自动同步 DSH 已有凭据，并支持本地管理、自动故障切换与负载均衡
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(JetHubAccountManager, cls).__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self.accounts = []
        self.antigravity_client = AntigravityLocalClient()
        self.reload_all_accounts()

    def reload_all_accounts(self):
        """从 DSH 与本地存储双向融合载入账号"""
        loaded_accounts = []
        seen_refs = set()

        # 1. 尝试从本地独立存储载入
        if os.path.exists(LOCAL_STORE_PATH):
            try:
                with open(LOCAL_STORE_PATH, "r", encoding="utf-8") as f:
                    local_data = json.load(f)
                    if isinstance(local_data, list):
                        for acc in local_data:
                            loaded_accounts.append(acc)
                            if acc.get("credentialRef"):
                                seen_refs.add(acc["credentialRef"])
            except Exception:
                pass

        # 2. 尝试从本机 DSH 读取已配置账号与凭据
        settings_path, cred_path = get_dsh_paths()
        if os.path.exists(settings_path) and os.path.exists(cred_path):
            try:
                credentials_map = {}
                with open(cred_path, "r", encoding="utf-8") as cf:
                    cred_content = cf.read()
                if HAS_YAML:
                    parsed_cred = yaml.safe_load(cred_content)
                    if isinstance(parsed_cred, dict):
                        refs = parsed_cred.get("refs", {})
                        for k, v in refs.items():
                            if isinstance(v, str):
                                try:
                                    credentials_map[k] = json.loads(v)
                                except Exception:
                                    credentials_map[k] = {"raw": v}
                            elif isinstance(v, dict):
                                credentials_map[k] = v

                with open(settings_path, "r", encoding="utf-8") as sf:
                    sett_content = sf.read()
                if HAS_YAML:
                    parsed_sett = yaml.safe_load(sett_content)
                    if isinstance(parsed_sett, dict):
                        jet_hub_section = parsed_sett.get("jet-hub", {})
                        dsh_accs = jet_hub_section.get("accounts", [])
                        for acc in dsh_accs:
                            ref = acc.get("credentialRef")
                            if ref and ref in credentials_map:
                                acc_id = acc.get("id", f"dsh-{ref}")
                                if ref not in seen_refs:
                                    full_acc = {
                                        "id": acc_id,
                                        "provider": acc.get("provider", "buddy"),
                                        "nickname": acc.get("nickname", "DSH账号"),
                                        "enabled": acc.get("enabled", True),
                                        "credentialRef": ref,
                                        "refreshable": acc.get("refreshable", True),
                                        "createdAt": acc.get("createdAt", int(time.time()*1000)),
                                        "expiresAt": acc.get("expiresAt", int(time.time()*1000) + 86400*30*1000),
                                        "credential": credentials_map[ref],
                                        "source": "dsh"
                                    }
                                    loaded_accounts.append(full_acc)
                                    seen_refs.add(ref)
            except Exception:
                pass

        # 3. 自动挂载 Antigravity (本地直连单例)
        antigravity_acc = {
            "id": "antigravity-local",
            "provider": "antigravity",
            "nickname": "Google Antigravity 本地语言服务器",
            "enabled": True,
            "credentialRef": "LOCAL_ANTIGRAVITY_RPC",
            "refreshable": False,
            "createdAt": int(time.time()*1000),
            "expiresAt": 0,
            "credential": {"type": "local_rpc"},
            "source": "builtin"
        }
        loaded_accounts.append(antigravity_acc)

        self.accounts = loaded_accounts

    def save_local_accounts(self):
        """将非 DSH 来源或用户自定义修改的账号写回本地存储"""
        try:
            with open(LOCAL_STORE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.accounts, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def list_accounts(self, provider=None):
        if provider:
            return [a for a in self.accounts if a.get("provider") == provider]
        return self.accounts

    def get_account_by_id(self, acc_id):
        for a in self.accounts:
            if a.get("id") == acc_id:
                return a
        return None

    def add_custom_account(self, provider, nickname, access_token, domain=None):
        acc_id = f"{provider}-{uuid.uuid4().hex[:8]}"
        ref = f"{provider.upper()}_ACCOUNT_{uuid.uuid4().hex[:8].upper()}"
        new_acc = {
            "id": acc_id,
            "provider": provider,
            "nickname": nickname,
            "enabled": True,
            "credentialRef": ref,
            "refreshable": False,
            "createdAt": int(time.time()*1000),
            "expiresAt": int(time.time()*1000) + 86400*30*1000,
            "credential": {
                "access_token": access_token.strip(),
                "domain": domain
            },
            "source": "custom"
        }
        self.accounts.append(new_acc)
        self.save_local_accounts()
        return new_acc

    def toggle_account(self, acc_id, enabled=None):
        acc = self.get_account_by_id(acc_id)
        if acc:
            if enabled is None:
                acc["enabled"] = not acc.get("enabled", True)
            else:
                acc["enabled"] = enabled
            self.save_local_accounts()
            return acc["enabled"]
        return False

    def delete_account(self, acc_id):
        self.accounts = [a for a in self.accounts if a.get("id") != acc_id]
        self.save_local_accounts()

    def get_available_account(self, provider):
        """
        获取当前 Provider 下下一个可用的健康账号（轮询调度算法）
        """
        candidates = [
            a for a in self.accounts
            if a.get("provider") == provider and a.get("enabled", True)
        ]
        if not candidates:
            return None
        now = time.time()
        for acc in candidates:
            cooldown_until = acc.get("_cooldown_until", 0)
            if now >= cooldown_until:
                return acc
        candidates.sort(key=lambda x: x.get("_cooldown_until", 0))
        return candidates[0]

    def mark_rate_limited(self, account, cooldown_sec=60):
        """标记某个账号遭遇频控，设定短期冷却期并自动避让"""
        account["_cooldown_until"] = time.time() + cooldown_sec


# ---------------------------------------------------------------------------
# Jet Hub 统一翻译对接器 (JetHubTranslator)
# ---------------------------------------------------------------------------
class JetHubTranslator:
    """
    Jet Hub 字幕翻译执行器
    无缝契合 audio_subtitle_tool 的翻译协议，支持自动轮询与模型推理
    """
    def __init__(self, provider="buddy", model="deepseek-v4-flash", log_callback=None):
        self.provider = provider
        self.model = model
        self.log_callback = log_callback or (lambda msg: None)
        self.mgr = JetHubAccountManager()
        self.session = requests.Session()

    def set_provider_and_model(self, provider, model):
        self.provider = provider
        self.model = model

    def translate(self, text, src_lang="auto", dst_lang="zh-CN", timeout=60):
        res = self.translate_batch([text], src_lang=src_lang, dst_lang=dst_lang, batch_size=1)
        return res[0] if res else text

    def translate_batch(self, texts, src_lang="auto", dst_lang="zh-CN", batch_size=8, progress_callback=None, cancel_checker=None):
        total = len(texts)
        if total == 0:
            return []

        dst_map = {
            "zh-CN": "简体中文", "zh-TW": "繁体中文", "en": "英语",
            "ja": "日语", "ko": "韩语", "fr": "法语", "de": "德语", "es": "西班牙语", "ru": "俄语"
        }
        dst_name = dst_map.get(dst_lang, "简体中文")
        results = [None] * total
        num_batches = math.ceil(total / batch_size)

        for b_idx in range(num_batches):
            if cancel_checker and cancel_checker():
                break

            start_i = b_idx * batch_size
            end_i = min(start_i + batch_size, total)
            batch_slice = texts[start_i:end_i]

            numbered_lines = [f"{i}: {t.strip()}" for i, t in enumerate(batch_slice, 1)]
            user_prompt = "\n".join(numbered_lines)

            sys_prompt = (
                f"你是一名顶级的影视字幕翻译专家。\n"
                f"任务：将用户给出的字幕对白按编号逐行翻译为自然流畅地道的{dst_name}。\n"
                f"规则：严格保留编号“1: 译文”，仅输出编号和译文，严禁输出任何解释说明。"
            )
            if src_lang and src_lang != "auto":
                sys_prompt += f"\n原文语种：{src_lang}。"

            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.1,
                "max_tokens": max(2048, len(batch_slice) * 128)
            }

            resp_text = self._send_request_with_failover(payload, timeout=120)
            res_map = {}
            if resp_text:
                for line in resp_text.splitlines():
                    line_s = line.strip()
                    m = re.match(r"^(?:\[?(\d+)\]?|\(?(\d+)\)?)\s*[:：、.．\- ]\s*(.+)$", line_s)
                    if m:
                        idx_num = int(m.group(1) or m.group(2))
                        res_map[idx_num] = m.group(3).strip()

            for sub_i, line_text in enumerate(batch_slice, 1):
                global_i = start_i + (sub_i - 1)
                if sub_i in res_map and res_map[sub_i]:
                    results[global_i] = res_map[sub_i]
                else:
                    results[global_i] = line_text

            if progress_callback:
                progress_callback(end_i, total)

        for i in range(total):
            if results[i] is None:
                results[i] = texts[i]
        return results

    def _send_request_with_failover(self, payload, timeout=120, max_retries=3):
        """具备多账号自动轮换与故障转移的请求分发器"""
        for attempt in range(1, max_retries + 1):
            acc = self.mgr.get_available_account(self.provider)
            if not acc:
                self.log_callback(f"[JetHub错误] 渠道 {self.provider} 无任何已启用的有效账号！")
                return None

            cred = acc.get("credential", {})
            headers = BuddyClient.build_headers(cred, self.provider)
            product = PROVIDERS_INFO.get(self.provider, PROVIDERS_INFO["buddy"])
            url = f"{product['endpoint']}/v2/chat/completions"

            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "").strip()
                elif resp.status_code == 429:
                    self.log_callback(f"[JetHub轮换] 账号 [{acc.get('nickname')}] 触发频控，自动切换下一个账号...")
                    self.mgr.mark_rate_limited(acc, cooldown_sec=60)
                    time.sleep(1.0)
                    continue
                else:
                    self.log_callback(f"[JetHub重试] 状态码 {resp.status_code}，正在重试...")
                    time.sleep(2.0)
            except Exception as e:
                self.log_callback(f"[JetHub网络重试] 连接异常 ({str(e)[:40]})，第 {attempt} 次重试...")
                time.sleep(2.0)
        return None
