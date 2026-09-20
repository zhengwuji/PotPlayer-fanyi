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

from verify_as import Cfg, parse_account_spec, resolve_url, build_request

CAPTURED = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

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
        self.end_headers()
        self.wfile.write(data)


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


def post(port, path, body, headers):
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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

    server.shutdown()
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
