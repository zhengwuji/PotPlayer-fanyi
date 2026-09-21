# -*- mode: python ; coding: utf-8 -*-
"""
API 管理器的 PyInstaller 打包配置。

用法：
    python -m PyInstaller api_manager.spec --noconfirm

产物：
    dist/PotPlayer-DeepSeek-API-Manager.exe    （图形界面，双击即用）

注意：与 installer.spec 不同，这里**不能排除 tkinter** —— 它就是界面本体。
console 设为 False：这是 GUI 程序，不该弹黑框。
"""

import glob
import os
import shutil
import sys
import tempfile
import zipfile


# ---------------------------------------------------------------------------
# Python 3.14 + Tcl/Tk 9 的坑：
#   脚本库以 tcl\libtcl9.0.4.zip / libtk9.0.4.zip 分发，
#   PyInstaller 的 tkinter 钩子不认识这种布局，
#   打包出来的 exe 一启动就报
#     FileNotFoundError: Tcl data directory "...\_tcl_data" not found.
#   这里在构建期把两个 zip 解开，按钩子期望的名字 _tcl_data / _tk_data 塞进去。
# ---------------------------------------------------------------------------
TCL_ROOTNAME = '_tcl_data'
TK_ROOTNAME = '_tk_data'


def stage_tcl_tk():
    tcl_root = os.path.join(sys.prefix, 'tcl')
    out = tempfile.mkdtemp(prefix='pyi-tcltk-')
    datas = []

    for pattern, target in (('libtcl*.zip', TCL_ROOTNAME),
                            ('libtk*.zip', TK_ROOTNAME)):
        dest = os.path.join(out, target)
        os.makedirs(dest, exist_ok=True)
        zips = sorted(glob.glob(os.path.join(tcl_root, pattern)))
        if zips:
            with zipfile.ZipFile(zips[0]) as z:
                for name in z.namelist():
                    if name.endswith('/'):
                        continue
                    parts = name.split('/', 1)
                    if len(parts) < 2:      # 跳过顶层目录本身
                        continue
                    rel = parts[1]
                    path = os.path.join(dest, rel.replace('/', os.sep))
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with z.open(name) as src, open(path, 'wb') as fh:
                        shutil.copyfileobj(src, fh)
            print(f'[tcl/tk] {os.path.basename(zips[0])} -> {target}/ '
                  f'({len(os.listdir(dest))} 项)')
        else:
            # 回退：老式目录布局
            for cand in ('tcl8.6', 'tcl9.0', 'tk8.6', 'tk9.0'):
                d = os.path.join(tcl_root, cand)
                if os.path.isdir(d) and (cand.startswith('tcl')) == (target == TCL_ROOTNAME):
                    for item in os.listdir(d):
                        s = os.path.join(d, item)
                        t = os.path.join(dest, item)
                        shutil.copytree(s, t) if os.path.isdir(s) else shutil.copy2(s, t)
                    print(f'[tcl/tk] {cand} -> {target}/')
                    break
        datas.append((dest, target))
    return datas


a = Analysis(
    ['api_manager.py'],
    pathex=[],
    binaries=[],
    datas=stage_tcl_tk() + [
        ('SubtitleTranslate - DeepSeek.as', '.'),
        ('SubtitleTranslate - DeepSeek.ico', '.'),
    ],
    hiddenimports=[
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 只排除确定用不到的重型库；tkinter 必须保留
        'PIL',
        'numpy',
        'pandas',
        'matplotlib',
        'scipy',
        'pytest',
        'requests',
        'win32com',
        'pythoncom',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PotPlayer-DeepSeek-API-Manager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 程序，不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='canglang.ico',
    version='file_version_info_manager.txt',
)
