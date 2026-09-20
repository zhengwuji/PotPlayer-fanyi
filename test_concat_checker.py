# -*- coding: utf-8 -*-
"""反向验证 check_concat：对历史版本（含已知缺陷）必须报 FAIL。"""
import subprocess
import sys

import verify_as as v

REVS = [
    ("d9c4741", "v0.5（已知把 int 直接拼进字符串）", "FAIL"),
    (None, "工作区当前版本", "PASS"),
]

for rev, label, expect in REVS:
    if rev is None:
        src = open("SubtitleTranslate - DeepSeek.as", encoding="utf-8").read()
    else:
        try:
            src = subprocess.run(
                ["git", "show", f"{rev}:SubtitleTranslate - DeepSeek.as"],
                capture_output=True, text=True, encoding="utf-8", check=True).stdout
        except Exception as exc:
            print(f"无法读取 {rev}: {exc}")
            continue
    got = "PASS" if v.check_concat(src) else "FAIL"
    mark = "OK  " if got == expect else "BAD "
    print(f"  [{mark}] {rev or 'worktree'} {label}  expect={expect} got={got}\n")
