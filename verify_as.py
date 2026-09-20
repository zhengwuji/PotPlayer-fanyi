#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_as.py — 无 AngelScript 编译器时的静态自检

检查项：
  1. 花括号 / 圆括号 / 方括号 是否配平（忽略字符串字面量与注释）
  2. 所有被调用的自定义函数是否都有定义
  3. 用 Python 复刻 BuildRequest()，验证生成的 JSON 在极端输入下仍然合法
"""

import json
import re
import sys

SRC = "SubtitleTranslate - DeepSeek.as"


# --------------------------------------------------------------------- 1. 配平
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


# --------------------------------------------------------------------- 2. 符号
def check_symbols(src: str) -> bool:
    code = strip_noise(src)
    defined = set(re.findall(r"^\s*(?:void|string|int|bool|array<\w+>)\s+(\w+)\s*\(", code, re.M))
    defined |= set(re.findall(r"^\s*(?:void|string|int|bool|array<\w+>)\s+(\w+)\s*\(", src, re.M))

    code = strip_noise(src)
    called = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", code))
    builtin_prefix = ("op",)
    custom = {c for c in called if c[0].isupper() or c in
              {"Dbg", "Finalize", "CacheGet", "CachePut", "RememberPair",
               "StartPairIndex", "BuildRequest", "IsPermanentError", "LangName",
               "JsonEscape", "ServerLogin", "Translate", "OnInitialize", "OnFinalize",
               "GetTitle", "GetVersion", "GetDesc", "GetLoginTitle", "GetLoginDesc",
               "GetPasswordText", "GetSrcLangs", "GetDstLangs"}}

    host_ok = {c for c in custom if c.startswith("Host") or c in
               {"JsonReader", "JsonValue", "array", "string", "int", "bool", "void"}}
    # AngelScript 内建 / 容器方法
    builtin_methods = {"Trim", "length", "empty", "find", "replace", "split",
                       "insertLast", "removeAt", "isArray", "isString", "asString",
                       "parse", "substr"}
    missing = sorted(c for c in custom - defined - host_ok - builtin_methods
                     if not c.startswith(builtin_prefix))

    if missing:
        print(f"  [WARN] 引用了未在文件内定义的函数: {', '.join(missing)}")
    else:
        print("  [OK]   所有自定义函数均有定义")
    return True


# ---------------------------------------------------------- 3. JSON 复刻验证
def json_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\r", "\\r")
             .replace("\t", "\\t").replace("\b", "\\b").replace("\f", "\\f"))


MAX_CTX_SENTENCES = 3
MAX_CTX_BYTES = 600


def lang_name(code: str) -> str:
    return {
        "zh-CN": "Simplified Chinese (简体中文)",
        "zh-TW": "Traditional Chinese (繁體中文)",
        "ja": "Japanese (日本語)",
    }.get(code, code)


def build_request(text, src, dst, pairs, model="deepseek-chat"):
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

    # StartPairIndex：从最新往回累计；预算在"加入前"判定，因此是硬上限，
    # 但永远至少保留最新一条。AngelScript 的 length() 是 UTF-8 字节数，用字节复刻。
    def blen(s: str) -> int:
        return len(s.encode("utf-8"))

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

    msgs = [{"role": "system", "content": sp}]
    for s, d in pairs[idx:]:
        msgs.append({"role": "user", "content": s})
        msgs.append({"role": "assistant", "content": d})
    msgs.append({"role": "user", "content": text})

    # 用与 AngelScript 相同的字符串拼接方式构造，再交给 json.loads 校验
    raw = '{"model":"' + model + '","messages":['
    raw += '{"role":"system","content":"' + json_escape(sp) + '"}'
    for s, d in pairs[idx:]:
        raw += ',{"role":"user","content":"' + json_escape(s) + '"}'
        raw += ',{"role":"assistant","content":"' + json_escape(d) + '"}'
    raw += ',{"role":"user","content":"' + json_escape(text) + '"}]'
    raw += ',"max_tokens":256,"temperature":0}'

    parsed = json.loads(raw)
    assert parsed["messages"] == msgs, "拼接结果与预期消息结构不一致"
    return parsed, raw


def check_json() -> bool:
    cases = [
        ("普通中文", "我们要去哪里", "zh-CN", []),
        ("含双引号", 'He said "run!" loudly', "zh-CN", []),
        ("含反斜杠", r"C:\Users\test\file.txt", "zh-CN", []),
        ("含制表与换行", "line1\tline2\nline3", "zh-CN", []),
        ("三行上下文", "第三条字幕", "zh-CN",
         [("第一条字幕", "第一行译文"), ("第二条字幕", "第二行译文")]),
        ("六行上下文（应只取后3）", "第七条", "zh-CN",
         [(f"第{i}条", f"译文{i}") for i in range(1, 7)]),
        ("超长单行触发字节上限", "结尾", "zh-CN",
         [("あ" * 400, "イ" * 400), ("短句", "短译")]),
        ("空源语言(自动检测)", "auto detect test", "zh-TW", []),
        ("RTL 目标语言", "hello", "ar", []),
    ]
    ok = True
    for name, text, dst, pairs in cases:
        try:
            parsed, raw = build_request(text, "", dst, list(pairs))
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {name}: {exc}")
            ok = False
            continue
        roles = [m["role"] for m in parsed["messages"]]
        n_pairs = (len(roles) - 2) // 2
        ctx_bytes = sum(
            len(m["content"].encode("utf-8"))
            for m in parsed["messages"][1:-1]
        )
        print(f"  [OK]   {name}: messages={len(roles)} 上下文对={n_pairs} "
              f"上行字节={len(raw.encode('utf-8'))} 上下文字节={ctx_bytes}")
        if name.startswith("六行") and n_pairs != 3:
            print("  [FAIL] 上下文没有按 MAX_CTX_SENTENCES 截断")
            ok = False
        if name.startswith("超长单行") and n_pairs != 1:
            print(f"  [FAIL] 字节上限未生效：期望只保留最新 1 条，实得 {n_pairs} 条")
            ok = False
    return ok


if __name__ == "__main__":
    with open(SRC, "r", encoding="utf-8") as fh:
        source = fh.read()

    print("1) 括号配平")
    a = check_balance(source)
    print("2) 函数引用")
    check_symbols(source)
    print("3) JSON 结构（复刻 BuildRequest）")
    b = check_json()

    print()
    print("RESULT:", "PASS" if (a and b) else "FAIL")
    sys.exit(0 if (a and b) else 1)
