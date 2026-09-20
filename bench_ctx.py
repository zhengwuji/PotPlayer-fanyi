# -*- coding: utf-8 -*-
"""复刻原版 v0.3 的 context 构建循环，量化改动前的实际上行体积。"""


def original_ctx(history, cur):
    max_tokens = 4096
    token_count = int(float(len(cur)) / 4)
    ctx = ""
    i = len(history) - 2
    while i >= 0 and token_count < (max_tokens - 1000):
        s = history[i]
        token_count += int(float(len(s)) / 4)
        if token_count < (max_tokens - 1000):
            ctx = s + "\n" + ctx
        i -= 1
    return ctx


CASES = [
    ("英文字幕 40 字符/条", "The quick brown fox jumps over the lazy dog"),
    ("中文字幕 20 字/条", "我们要去哪里吃饭今天天气真不错"),
]

for label, sub in CASES:
    hist = [sub] * 2000
    ctx = original_ctx(hist, sub)
    lines = ctx.count("\n") + 1 if ctx else 0
    print(
        "%-18s 原文占用 %5d 字符 / %6d 字节 / 约 %4d 条字幕被塞进 context"
        % (label, len(ctx), len(ctx.encode("utf-8")), lines)
    )

print()
print("v0.4 实测上限: 上下文 <= 600 字节 (MAX_CTX_BYTES), 最多 3 条")
