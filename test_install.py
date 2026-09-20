# -*- coding: utf-8 -*-
"""
test_install.py — 验证安装器真正把插件文件放到位（不碰 PotPlayer、不需要管理员）。

直接调用 installer.py 里的 install_asset()，也就是被冻结进 exe 的同一段逻辑，
在一个临时目录里走完"本地优先 -> 校验 -> 落盘"的全过程，并比对 SHA256。
"""

import hashlib
import os
import shutil
import sys
import tempfile

import installer
from installer import install_asset, validate_as, validate_ico, LANGUAGE_STRINGS, resource_dir


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    strings = LANGUAGE_STRINGS["zh"]
    target = tempfile.mkdtemp(prefix="potplayer-translate-test-")
    print(f"源目录 : {resource_dir()}")
    print(f"目标目录: {target}")
    print()

    rc = 0
    for filename, validator in (
        ("SubtitleTranslate - DeepSeek.as", validate_as),
        ("SubtitleTranslate - DeepSeek.ico", validate_ico),
    ):
        ok = install_asset(filename, validator, strings, target)
        dest = os.path.join(target, filename)
        exists = os.path.exists(dest)

        if not (ok and exists):
            print(f"  [FAIL] {filename} 未安装成功")
            rc = 1
            continue

        src_hash = sha256(os.path.join(resource_dir(), filename))
        dst_hash = sha256(dest)
        same = src_hash == dst_hash
        print(f"  [{'OK' if same else 'FAIL'}] {filename}")
        print(f"         {os.path.getsize(dest)} bytes")
        print(f"         src {src_hash}")
        print(f"         dst {dst_hash}")
        if not same:
            rc = 1

    print()
    print(f"落盘内容:")
    for name in sorted(os.listdir(target)):
        print(f"  {os.path.getsize(os.path.join(target, name)):>8} bytes  {name}")

    shutil.rmtree(target, ignore_errors=True)
    print()
    print("RESULT:", "PASS" if rc == 0 else "FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
