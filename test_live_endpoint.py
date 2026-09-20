#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_live_endpoint.py — 拿真实端点验证插件端到端行为

用法（key 不落仓库，从环境变量读）：
    $env:LIVE_API_KEY = "sk_..."
    $env:LIVE_API_BASE = "https://api.inceptionlabs.ai/v1"     # 可选
    $env:LIVE_API_MODEL = "mercury-2.5"                        # 可选
    python test_live_endpoint.py

它做的是：用 verify_as.py 里逐行复刻出来的 BuildRequest() 生成**与插件完全一致的**
请求 JSON，打到真实端点，再用插件的解析路径取值。所以只要这一步过了，
插件在同样的配置下就应该能跑通。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from verify_as import (Cfg, parse_account_spec_full, resolve_url, resolve_models_url,
                       build_request, start_pair_index)

KEY = os.environ.get("LIVE_API_KEY", "").strip()
BASE = os.environ.get("LIVE_API_BASE", "https://api.inceptionlabs.ai/v1").strip()
MODEL = os.environ.get("LIVE_API_MODEL", "mercury-2.5").strip()

LINES = ["I'll be back.", "Get down!", "Who is that guy?",
         "Run for your life.", "It's not over yet."]


def finalize(raw, dst="zh-CN"):
    """复刻插件 Finalize()"""
    t = raw.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    t = t.strip()
    if len(t) > 200:
        t = t[:200].strip()
    if dst in ("ar", "he", "fa", "ur"):
        t = "\u2067" + t + "\u2069"
    return t


def extract(cfg, root):
    """复刻插件 Translate() 里的取值路径"""
    if cfg.fmt == "anthropic":
        c = root.get("content")
        if isinstance(c, list) and c and isinstance(c[0].get("text"), str):
            return c[0]["text"], True
        return "", False
    ch = root.get("choices")
    if isinstance(ch, list) and ch and isinstance(ch[0].get("message", {}).get("content"), str):
        return ch[0]["message"]["content"], True
    return "", False


def post(url, cfg, body_obj, key):
    hdr = {"Content-Type": "application/json"}
    if cfg.auth == "none":
        pass
    elif cfg.auth == "x-api-key":
        hdr["x-api-key"] = key
    elif cfg.auth == "api-key":
        hdr["api-key"] = key
    elif cfg.auth == "raw":
        hdr["Authorization"] = key
    else:
        hdr["Authorization"] = "Bearer " + key
    if cfg.fmt == "anthropic":
        hdr["anthropic-version"] = "2023-06-01"
    if cfg.extra:
        for e in cfg.extra.split("|"):
            if ":" in e:
                k, v = e.split(":", 1)
                hdr[k.strip()] = v.strip()

    req = urllib.request.Request(url, data=json.dumps(body_obj).encode("utf-8"),
                                 headers=hdr, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8")), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8")), time.time() - t0


def main():
    if not KEY:
        print("缺少 LIVE_API_KEY 环境变量")
        return 2

    print("=" * 72)
    print("0) 端点连通性 + /v1/models")
    cfg0 = Cfg()
    parse_account_spec_full(cfg0, f"url={BASE}; model={MODEL}")
    murl = resolve_models_url(cfg0)
    try:
        req = urllib.request.Request(murl, headers={"Authorization": "Bearer " + KEY})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        ids = [d.get("id") for d in data.get("data", []) if isinstance(d, dict)]
        print(f"   {murl} -> HTTP 200, 模型: {ids}")
    except Exception as exc:  # noqa: BLE001
        print(f"   {murl} -> 失败: {exc}")
        ids = []

    # ---------- 关键：用插件的确切请求形状 ----------
    specs = [
        ("A 默认（不加 body 参数，max_tokens=512）",
         f"url={BASE}; model={MODEL}; maxtok=512"),
        ("B 关闭推理（max_tokens=512）",
         f"url={BASE}; model={MODEL}; maxtok=512; body=\"reasoning_effort\":\"none\""),
        ("C 关闭推理（max_tokens=1024）",
         f"url={BASE}; model={MODEL}; maxtok=1024; body=\"reasoning_effort\":\"none\""),
    ]

    results = {}
    for label, spec in specs:
        print()
        print("=" * 72)
        print(f"{label}")
        print(f"   配置串: {spec}")

        cfg = Cfg()
        parse_account_spec_full(cfg, spec)
        url = resolve_url(cfg)
        print(f"   端点  : {url}   模型: {cfg.model}   max_tokens: {cfg.max_tokens}")

        pairs = []
        ok_count = 0
        total_reason = 0
        for line in LINES:
            body_obj = json.loads(build_request(cfg, line, "", "zh-CN", pairs)[1])
            st, resp, dt = post(url, cfg, body_obj, KEY)

            reason = (resp.get("usage", {})
                          .get("completion_tokens_details", {})
                          .get("reasoning_tokens", 0) or 0)
            total_reason += reason

            raw, got = extract(cfg, resp)
            out = finalize(raw) if got else None
            if out:
                ok_count += 1
                pairs.append((line, out))     # (原文, 译文) —— 与插件的 pairSrc/pairDst 一致

            flag = "OK " if out else "空!"
            print(f"   [{flag}] HTTP {st} {dt:5.2f}s reasoning={reason:<4} "
                  f"{line!r:<22} -> {out!r}")
            if not out and st != 200:
                print(f"          error = {json.dumps(resp.get('error'), ensure_ascii=False)[:200]}")

        results[label] = (ok_count, len(LINES), total_reason)
        print(f"   成功 {ok_count}/{len(LINES)}，累计 reasoning token = {total_reason}")
        print(f"   插件侧上下文对数 = {len(pairs)}（上限 3，实际送给模型 "
              f"{len(pairs) - start_pair_index(list(pairs))} 对）")

    print()
    print("=" * 72)
    print("结论")
    for label, (a, b, r) in results.items():
        verdict = "可用" if a == b else "不可用"
        print(f"   [{verdict}] {label}  {a}/{b}，reasoning {r}")

    best = max(results.items(), key=lambda kv: (kv[1][0], -kv[1][2]))
    print()
    print(f"   推荐配置: {best[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
