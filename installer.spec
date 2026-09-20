# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置。

原版仓库把 build/ dist/ *.spec 全部写进了 .gitignore，所以 Releases 里的
installer.exe 无法从源码复现。此文件补上这个缺口。

用法：
    python -m PyInstaller installer.spec --noconfirm

产物：
    dist/PotPlayer-DeepSeek-Translate-Installer.exe   （单文件，约 8-12 MB）

关键点：
  * datas 把插件本体 .as / .ico 打进 exe，解压到 sys._MEIPASS，
    因此 installer.py 的"本地优先"逻辑会在 exe 内部命中，
    安装过程**完全不需要联网**。
  * hiddenimports 显式声明 pywin32 的动态分派模块
    （win32com.client.Dispatch 走的是运行时 ProgID 查找，
     PyInstaller 的静态分析看不到）。
"""

a = Analysis(
    ['installer.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('SubtitleTranslate - DeepSeek.as', '.'),
        ('SubtitleTranslate - DeepSeek.ico', '.'),
    ],
    hiddenimports=[
        'win32com',
        'win32com.client',
        'win32com.shell',
        'win32api',
        'win32con',
        'pythoncom',
        'pywintypes',
        'win32timezone',
        'ctypes.wintypes',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 仅排除确定用不到的重型第三方库。
        # 注意：不要排除 email / html / http.server / sqlite3 / distutils / setuptools
        # —— urllib3(requests) 与 pywin32 会间接依赖它们，排除后 exe 启动即崩。
        'tkinter',
        'PIL',
        'numpy',
        'pandas',
        'matplotlib',
        'scipy',
        'pytest',
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
    name='PotPlayer-DeepSeek-Translate-Installer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # 不用 UPX：避免杀软误报
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,         # 这是命令行安装器，必须保留控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='canglang.ico',
    version='file_version_info.txt',
)
