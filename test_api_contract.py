#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_api_contract.py — 用本地 mock 服务验证 HTTP 契约

本机没有 AngelScript 运行时，所以无法直接跑 .as。但真正容易出错的是
「请求体形状 / 认证头 / 响应体解析路径」这三件事，它们完全可以用
一个模拟真实服务商的本地 HTTP 服务来验证：

  * OpenAI 兼容：POST /v1/chat/completions -> choices[0].message.content
  * Anthropic  ：POST /v1/messages         -> content[0].text
  * 错误体     ：{"error":{"message":"..."}} 两种风格都要认

只要 mock 服务按官方文档的真实形状应答，就能证明我们读的 JSON 路径没错。
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from verify_as import (Cfg, parse_account_spec, resolve_url, build_request,
                       resolve_models_url, parse_models_payload, parse_small_int)

CAPTURED = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        """GET /v1/models —— 「获取可用模型」用的就是这条"""
        try:
            CAPTURED.append({
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "x_api_key": self.headers.get("x-api-key"),
            })
            # 注意顺序：/ollama/models 也以 /models 结尾，必须先判它
            if self.path.endswith("/ollama/models"):
                self._json({"models": [{"name": "qwen2.5:7b"}, {"name": "llama3:8b"}]})
                return
            if self.path.endswith("/v1/models") or self.path.endswith("/models"):
                self._json({"object": "list", "data": [
                    {"id": "deepseek-chat", "object": "model"},
                    {"id": "deepseek/deepseek-v4-pro", "object": "model"},
                    {"id": "meituan/LongCat-2.0:free", "object": "model"},
                    {"nope": "missing id"},
                    {"id": "poo1side/laguna-s-2.1-free", "object": "model"},
                ]})
                return
            self._json({"error": {"message": "not found"}}, 404)
        except Exception:
            import traceback
            traceback.print_exc()

    def do_POST(self):
        try:
            self._handle()
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                self._json({"error": {"message": "mock server error"}}, 500)
            except Exception:
                pass

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"_unparseable": raw}

        CAPTURED.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "x_api_key": self.headers.get("x-api-key"),
            "anthropic_version": self.headers.get("anthropic-version"),
            "content_type": self.headers.get("Content-Type"),
            "body": body,
        })

        if self.path.endswith("/v1/chat/completions"):
            if "FAILME" in json.dumps(body, ensure_ascii=False):
                self._json({"error": {"message": "Insufficient Balance",
                                      "type": "insufficient_quota"}}, 402)
                return
            self._json({
                "id": "chatcmpl-1", "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "我们要去哪里"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 120, "completion_tokens": 6},
            })
            return

        if self.path.endswith("/v1/messages"):
            self._json({
                "id": "msg_1", "type": "message", "role": "assistant",
                "model": body.get("model", "?"),
                "content": [{"type": "text", "text": "我们要去哪里"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 120, "output_tokens": 6},
            })
            return

        self._json({"error": {"message": "not found"}}, 404)

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        # 显式关闭连接：HTTP/1.1 的 keep-alive 会让服务端线程继续阻塞在读，
        # 客户端先关就会偶发 RST，测试变成随机的。
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True


class QuietServer(ThreadingHTTPServer):
    """连接收尾时的 RST 不该刷 traceback 干扰测试输出。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


# ---- 复刻 AngelScript 的 BuildHeaders() -----------------------------------
def build_headers(cfg: Cfg, key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if cfg.auth == "none":
        pass
    elif cfg.auth == "x-api-key":
        h["x-api-key"] = key
    elif cfg.auth == "api-key":
        h["api-key"] = key
    elif cfg.auth == "raw":
        h["Authorization"] = key
    else:
        h["Authorization"] = "Bearer " + key
    if cfg.fmt == "anthropic":
        h["anthropic-version"] = "2023-06-01"
    if cfg.extra:
        for e in cfg.extra.split("|"):
            e = e.strip()
            if e and ":" in e:
                k, v = e.split(":", 1)
                h[k.strip()] = v.strip()
    return h


# ---- 复刻 AngelScript 的响应解析 -----------------------------------------
def extract_text(cfg: Cfg, root: dict):
    if cfg.fmt == "anthropic":
        c = root.get("content")
        if isinstance(c, list) and c and isinstance(c[0].get("text"), str):
            return c[0]["text"]
        return None
    choices = root.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message", {})
        if isinstance(msg.get("content"), str):
            return msg["content"]
    return None


def _request(method, port, path, data, headers, retries=3):
    """HTTP 请求：对偶发连接重置做有限重试，避免测试自身 flaky。"""
    import time
    import urllib.error
    import urllib.request

    last = None
    for attempt in range(retries):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.1 * (attempt + 1))
    raise last


def post(port, path, body, headers):
    return _request("POST", port, path,
                    json.dumps(body).encode("utf-8"), headers)


def get(port, path, headers):
    return _request("GET", port, path, None, headers)


def main():
    server = QuietServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"mock 服务: http://127.0.0.1:{port}\n")

    ok = True

    # ---------- 1. OpenAI 兼容（默认 DeepSeek 官方形状） ----------
    print("1) OpenAI 兼容格式")
    cfg = Cfg()
    parse_account_spec(cfg, f"url=http://127.0.0.1:{port}/v1; model=test-model")
    url = resolve_url(cfg)
    expected, raw = build_request(cfg, "Where are we going", "", "zh-CN", [])
    status, resp = post(port, url.replace(f"http://127.0.0.1:{port}", ""),
                        json.loads(raw), build_headers(cfg, "sk-test123"))

    cap = CAPTURED[-1]
    checks = [
        ("路径正确", cap["path"] == "/v1/chat/completions"),
        ("Authorization: Bearer", cap["auth"] == "Bearer sk-test123"),
        ("Content-Type", cap["content_type"] == "application/json"),
        ("model 透传", cap["body"]["model"] == "test-model"),
        ("messages[0] 是 system", cap["body"]["messages"][0]["role"] == "system"),
        ("末条是待译正文", cap["body"]["messages"][-1]["content"] == "Where are we going"),
        ("max_tokens=256", cap["body"]["max_tokens"] == 256),
        ("HTTP 200", status == 200),
        ("解析 choices[0].message.content", extract_text(cfg, resp) == "我们要去哪里"),
    ]
    for name, good in checks:
        print(f"   [{'OK' if good else 'FAIL'}] {name}")
        ok = ok and good

    # ---------- 2. Anthropic 格式 ----------
    print("\n2) Anthropic 格式")
    acfg = Cfg()
    parse_account_spec(acfg, f"url=http://127.0.0.1:{port}; format=anthropic; auth=x-api-key; "
                             f"model=claude-3-5-haiku-latest")
    aurl = resolve_url(acfg)
    _, araw = build_request(acfg, "Where are we going", "", "zh-CN", [])
    astatus, aresp = post(port, aurl.replace(f"http://127.0.0.1:{port}", ""),
                          json.loads(araw), build_headers(acfg, "sk-ant-xyz"))

    cap = CAPTURED[-1]
    checks = [
        ("路径正确 /v1/messages", cap["path"] == "/v1/messages"),
        ("使用 x-api-key 而非 Bearer", cap["x_api_key"] == "sk-ant-xyz" and cap["auth"] is None),
        ("带 anthropic-version", cap["anthropic_version"] == "2023-06-01"),
        ("system 在顶层", isinstance(cap["body"].get("system"), str)),
        ("messages 里无 system 角色",
         all(m["role"] != "system" for m in cap["body"]["messages"])),
        ("HTTP 200", astatus == 200),
        ("解析 content[0].text", extract_text(acfg, aresp) == "我们要去哪里"),
    ]
    for name, good in checks:
        print(f"   [{'OK' if good else 'FAIL'}] {name}")
        ok = ok and good

    # ---------- 3. 错误体 ----------
    print("\n3) 错误响应（欠费）")
    cfg3 = Cfg()
    parse_account_spec(cfg3, f"url=http://127.0.0.1:{port}/v1")
    _, raw3 = build_request(cfg3, "FAILME", "", "zh-CN", [])
    s3, r3 = post(port, "/v1/chat/completions", json.loads(raw3), build_headers(cfg3, "sk-x"))
    msg = r3.get("error", {}).get("message", "")
    for name, good in [
        ("HTTP 402", s3 == 402),
        ("能读到 error.message", msg == "Insufficient Balance"),
        ("命中永久错误判定", any(k in msg for k in
                               ("Insufficient Balance", "insufficient_quota"))),
    ]:
        print(f"   [{'OK' if good else 'FAIL'}] {name}")
        ok = ok and good

    # ---------- 4. 额外请求头 ----------
    print("\n4) extra 自定义头")
    cfg4 = Cfg()
    parse_account_spec(cfg4, f"url=http://127.0.0.1:{port}/v1; extra=X-Tenant:abc|X-Trace:42")
    h4 = build_headers(cfg4, "sk-x")
    for name, good in [
        ("X-Tenant", h4.get("X-Tenant") == "abc"),
        ("X-Trace", h4.get("X-Trace") == "42"),
    ]:
        print(f"   [{'OK' if good else 'FAIL'}] {name}")
        ok = ok and good

    # ---------- 5. 获取可用模型 ----------
    print("\n5) 获取可用模型 (GET /v1/models)")
    cfg5 = Cfg()
    parse_account_spec(cfg5, f"url=http://127.0.0.1:{port}/v1")
    murl = resolve_models_url(cfg5)
    status, resp = get(port, murl.replace(f"http://127.0.0.1:{port}", ""),
                       build_headers(cfg5, "sk-test123"))
    cap = CAPTURED[-1]
    models = parse_models_payload(resp)
    checks = [
        ("路径推导正确", cap["path"] == "/v1/models"),
        ("GET 也带认证头", cap["auth"] == "Bearer sk-test123"),
        ("HTTP 200", status == 200),
        ("解析出模型 id", models == ["deepseek-chat", "deepseek/deepseek-v4-pro",
                                     "meituan/LongCat-2.0:free",
                                     "poo1side/laguna-s-2.1-free"]),
        ("缺 id 的条目被跳过", all("nope" not in m for m in models)),
    ]
    for name, good in checks:
        print(f"   [{'OK' if good else 'FAIL'}] {name}")
        ok = ok and good

    # 用序号选中
    pick = parse_small_int("2")
    chosen = models[pick - 1] if 1 <= pick <= len(models) else ""
    for name, good in [
        ("model=@2 命中第 2 个", chosen == "deepseek/deepseek-v4-pro"),
    ]:
        print(f"   [{'OK' if good else 'FAIL'}] {name}")
        ok = ok and good

    # Ollama 原生形状
    cfg5b = Cfg()
    parse_account_spec(cfg5b, f"url=http://127.0.0.1:{port}/ollama")
    _, resp2 = get(port, "/ollama/models", build_headers(cfg5b, "x"))
    got2 = parse_models_payload(resp2)
    for name, good in [
        ("models[].name 形状兼容", got2 == ["qwen2.5:7b", "llama3:8b"]),
    ]:
        print(f"   [{'OK' if good else 'FAIL'}] {name}")
        ok = ok and good

    server.shutdown()
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
