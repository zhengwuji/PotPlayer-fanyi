# -*- coding: utf-8 -*-
"""
verify_exe_strings.py — 真正地检查打包后的 exe 里还含不含某些字符串。

为什么不能直接扫二进制：PyInstaller 把字节码压缩存在归档里，
原文根本不会以明文出现（一开始我扫二进制得到「通过」是假通过，
因为连本该存在的中文串都搜不到 —— 加了对照检查才发现）。

正确做法：解出 PYZ → 反序列化 installer 模块的代码对象 →
递归遍历 co_consts，查里面的字符串常量。
"""
import marshal
import os
import sys
import tempfile
import types

from PyInstaller.archive.readers import CArchiveReader

EXE = sys.argv[1] if len(sys.argv) > 1 else \
    r"dist\PotPlayer-DeepSeek-Translate-Installer.exe"
MODULE = sys.argv[2] if len(sys.argv) > 2 else "installer"

MUST_NOT = ["Felix3322", "沧浪", "哔哩哔哩", "PotPlayer_Chatgpt_Translate"]
MUST_HAVE = ["一键安装", "本安装程序适用于"]


def walk_consts(code, out):
    if isinstance(code, types.CodeType):
        for c in code.co_consts:
            if isinstance(c, str):
                out.append(c)
            elif isinstance(c, types.CodeType):
                walk_consts(c, out)
    return out


def main():
    exe = os.path.abspath(EXE)
    if not os.path.isfile(exe):
        print(f"找不到 {exe}")
        return 2

    reader = CArchiveReader(exe)
    # 入口脚本是以裸名字（无扩展名）直接放在 CArchive 里的，不在 PYZ 里 ——
    # 这一点是列出条目才看出来的。
    entry = None
    for key in (MODULE, "installer"):
        try:
            entry = reader.extract(key)
            break
        except Exception:  # noqa: BLE001
            continue
    if entry is None:
        print(f"归档里找不到入口模块 {MODULE}")
        print("现有条目:", [k for k in reader.toc][:40])
        return 2

    if isinstance(entry, tuple):
        entry = entry[-1]
    code = marshal.loads(entry) if isinstance(entry, (bytes, bytearray)) else entry
    strings = walk_consts(code, [])
    blob = "\n".join(strings)

    print(f"入口模块 {MODULE}：提取到 {len(strings)} 个字符串常量")
    ok = True

    for s in MUST_NOT:
        if s in blob:
            print(f"  [FAIL] 仍存在: {s}")
            ok = False
        else:
            print(f"  [OK]   已移除: {s}")

    # 对照检查：证明本次提取与检索确实有效
    for s in MUST_HAVE:
        if s in blob:
            print(f"  [OK]   对照命中: {s}")
        else:
            print(f"  [FAIL] 对照未命中: {s}（提取或检索方式有问题）")
            ok = False

    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
