import os
import sys
import ctypes
import shutil
import hashlib
import requests
import locale
import win32com.client
from concurrent.futures import ThreadPoolExecutor
from requests.exceptions import RequestException
import subprocess


# 定义多语言字符串
LANGUAGE_STRINGS = {
    "en": {
        "admin_required": "This script needs to be run with administrator privileges.",
        "select_directory": "Please select the Translate directory for PotPlayer.",
        "invalid_directory": "No valid directory selected. Exiting installation.",
        "creating_directory": "Creating directory: {}",
        "failed_to_create_directory": "Failed to create directory: {}",
        "download_completed": "File downloaded: {} -> {}",
        "download_failed": "Failed to download {}: {}",
        "installation_complete": "Files have been successfully installed to: {}",
        "choose_option": "Directory not found. Please choose an option:\n1. Manually input directory\n2. Automatically scan all drives\nEnter your choice (1/2): ",
        "default_path_not_found": "Default path not found: {}",
        "scanning_drives": "Scanning all drives, please wait...",
        "found_directory": "Directory found: {}",
        "no_directory_found": "No directory found.",
        "installation_done": "Installation completed. Press Enter to exit.",
        "error_occurred": "An error occurred: {}",
        "enter_directory": "Please enter the full path to the PotPlayer Translate directory:",
    },
    "zh": {
        "admin_required": "此脚本需要以管理员权限运行。",
        "select_directory": "请选择PotPlayer的Translate目录。",
        "invalid_directory": "未选择有效目录，退出安装程序。",
        "creating_directory": "创建目录: {}",
        "failed_to_create_directory": "创建目录失败: {}",
        "download_completed": "文件已成功下载: {} -> {}",
        "download_failed": "下载失败 {}: {}",
        "installation_complete": "文件已成功安装到: {}",
        "choose_option": "目录不存在。请选择操作:\n1. 手动输入目录\n2. 自动扫描硬盘(建议)\n输入选项（1/2）：",
        "default_path_not_found": "默认路径未找到: {}",
        "scanning_drives": "正在扫描硬盘，请稍候...",
        "found_directory": "找到目录: {}",
        "no_directory_found": "未找到目录。",
        "installation_done": "安装完成。按回车键退出。",
        "error_occurred": "发生错误: {}",
        "enter_directory": "请输入PotPlayer的Translate目录完整路径：",
    },
}

# 获取系统语言并选择提示语言，默认设置为中文
def get_language():
    try:
        lang_code = locale.getdefaultlocale()[0]
        if lang_code and lang_code.startswith("zh"):
            return "zh"
    except Exception:
        pass
    return "zh"  # 默认设置为中文


# 资源目录：打包成 exe 后 datas 会被解压到 sys._MEIPASS；源码运行时用脚本所在目录
def resource_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


# 检测是否以管理员权限运行
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False  # 非 Windows 系统默认返回 False


# 提升到管理员权限
def restart_as_admin(strings):
    print(strings["admin_required"])
    if sys.platform == "win32":
        # 冻结成 exe 后 sys.executable 就是本 exe，不能再把 sys.argv 原样传回去
        # （否则会把 exe 自身路径当成参数重启）
        params = "" if getattr(sys, "frozen", False) else subprocess.list2cmdline(sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    else:
        print("This script requires administrator privileges, but it is not running on Windows.")
    sys.exit()


# 下载文件函数，带进度显示
def download_file(url, dest_path, strings, max_retries=3):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, stream=True)
            response.raise_for_status()
            total_length = response.headers.get('content-length')

            if total_length is None:
                with open(dest_path, 'wb') as file:
                    file.write(response.content)
                print(strings["download_completed"].format(url, dest_path))
            else:
                dl = 0
                total_length = int(total_length)
                with open(dest_path, 'wb') as file:
                    for chunk in response.iter_content(chunk_size=4096):
                        if chunk:
                            file.write(chunk)
                            dl += len(chunk)
                            done = int(50 * dl / total_length)
                            percent = int(100 * dl / total_length)
                            sys.stdout.write("\r[%s%s] %d%%" % ('=' * done, ' ' * (50 - done), percent))
                            sys.stdout.flush()
                print()  # 换行
                print(strings["download_completed"].format(url, dest_path))
            return  # 下载成功，退出函数
        except RequestException as e:
            print(strings["download_failed"].format(url, e))
            if attempt < max_retries - 1:
                print(f"Retrying... ({attempt + 1}/{max_retries})")
            else:
                print("Max retries reached. Exiting.")
                sys.exit(1)


# ---------------------------------------------------------------------------
# 下载源策略：官方 raw 直链优先，其次公共 CDN / 反代镜像
# 原版硬编码了单一第三方镜像 github.20246688.xyz，且用的是 /blob/ 页面地址
# （返回 HTML 而非文件本体），此处一并修正为 raw 直链 + 多重回退 + 内容校验。
# ---------------------------------------------------------------------------
ASSET_BASES = [
    "https://raw.githubusercontent.com/Liu8Can/PotPlayer_DeepSeek_Translate/main/",
    "https://cdn.jsdelivr.net/gh/Liu8Can/PotPlayer_DeepSeek_Translate@main/",
    "https://gh-proxy.com/https://raw.githubusercontent.com/Liu8Can/PotPlayer_DeepSeek_Translate/main/",
    "https://ghfast.top/https://raw.githubusercontent.com/Liu8Can/PotPlayer_DeepSeek_Translate/main/",
]


def validate_as(path):
    """确保拿到的是脚本文本，而不是反代返回的 HTML 错误页。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(8192)
    except Exception:
        return False
    if len(head) < 200:
        return False
    if head.lstrip().startswith("<"):
        return False
    return ("GetTitle" in head) and ("Translate" in head)


def validate_ico(path):
    """ICO 文件头 00 00 01 00。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except Exception:
        return False
    return head == b"\x00\x00\x01\x00"


def install_asset(filename, validator, strings, target_dir):
    """先尝试安装器同目录的本地文件（离线安装），再依次尝试各下载源。"""
    dest = os.path.join(target_dir, filename)

    local = os.path.join(resource_dir(), filename)
    if os.path.exists(local) and validator(local):
        shutil.copyfile(local, dest)
        print(f"Local copy used: {local} -> {dest}")
        return True

    for base in ASSET_BASES:
        url = base + filename.replace(" ", "%20")
        print(f"Downloading {filename} from {base} ...")
        try:
            download_file(url, dest, strings, max_retries=1)
        except SystemExit:
            continue
        if validator(dest):
            return True
        print(f"Rejected invalid payload from {base}")

    return False


# 扫描硬盘函数
def scan_drives(strings):
    drives = [f"{chr(x)}:\\" for x in range(65, 91) if os.path.exists(f"{chr(x)}:\\")]
    potential_paths = [os.path.join(drive, "Program Files", "DAUM", "PotPlayer", "Extension", "Subtitle", "Translate") for drive in drives]

    with ThreadPoolExecutor() as executor:
        results = list(executor.map(os.path.exists, potential_paths))

    for path, exists in zip(potential_paths, results):
        if exists:
            return path
    return None


# 从安装目录扫描快捷方式获取路径
def get_path_from_installation_dir(strings):
    # 常见的安装路径
    POTENTIAL_DIRS = [
        r"C:\Program Files\DAUM\PotPlayer",
        r"C:\Program Files (x86)\DAUM\PotPlayer"
    ]

    for drive in [f"{chr(x)}:\\" for x in range(65, 91) if os.path.exists(f"{chr(x)}:\\")]:
        potential_dirs = POTENTIAL_DIRS + [os.path.join(drive, "DAUM", "PotPlayer")]
        for dir_path in potential_dirs:
            if os.path.exists(dir_path):
                for lnk_name in ["PotPlayer 64 bit.lnk", "PotPlayer.lnk", "PotPlayer 32 bit.lnk"]:
                    lnk_path = os.path.join(dir_path, lnk_name)
                    if os.path.exists(lnk_path):
                        potplayer_path = get_path_from_shortcut(lnk_path)
                        if potplayer_path:
                            translate_dir = os.path.join(os.path.dirname(potplayer_path), "Extension", "Subtitle", "Translate")
                            if os.path.exists(translate_dir):
                                print(strings["found_directory"].format(translate_dir))
                                return translate_dir
    return None


# 解析快捷方式文件
def get_path_from_shortcut(shortcut_path):
    if sys.platform != "win32":
        print("Shortcut parsing is only supported on Windows.")
        return None
    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(shortcut_path)
        return shortcut.TargetPath
    except Exception:
        return None


# 主安装逻辑
def install(strings):
    print("Starting installation...")
    print("---------------------------------------------------------")
    print("本安装程序由GitHub Felix3322实现，原项目地址：https://github.com/Felix3322/PotPlayer_Chatgpt_Translate")
    print("本安装程序由哔哩哔哩沧浪同学修改，原项目地址：https://github.com/Liu8Can/PotPlayer_DeepSeek_Translate")
    print("本安装程序适用于PotPlayer的DeepSeek字幕翻译插件，一键安装。")
    print("---------------------------------------------------------")

    # 优先检测安装目录中的快捷方式
    target_path = get_path_from_installation_dir(strings)

    if not target_path:
        default_path = r"C:\Program Files\DAUM\PotPlayer\Extension\Subtitle\Translate"
        if not os.path.exists(default_path):
            print(strings["default_path_not_found"].format(default_path))
            choice = input(strings["choose_option"]).strip()

            if choice == '2':
                # 自动扫描
                print(strings["scanning_drives"])
                target_path = scan_drives(strings)
                if not target_path:
                    print(strings["no_directory_found"])
                    target_path = input(strings["enter_directory"]).strip()
            else:
                target_path = input(strings["enter_directory"]).strip()

            if not target_path or not os.path.exists(target_path):
                print(strings["invalid_directory"])
                sys.exit(1)
        else:
            target_path = default_path

    print(f"Target Directory: {target_path}")

    # 确保目标目录存在
    if not os.path.exists(target_path):
        try:
            os.makedirs(target_path)
            print(strings["creating_directory"].format(target_path))
        except Exception as e:
            print(strings["failed_to_create_directory"].format(target_path))
            sys.exit(1)

    # 安装插件文件（本地优先，其次官方源与镜像，逐级回退并校验内容）
    ok_as = install_asset("SubtitleTranslate - DeepSeek.as", validate_as, strings, target_path)
    ok_ico = install_asset("SubtitleTranslate - DeepSeek.ico", validate_ico, strings, target_path)

    if not (ok_as and ok_ico):
        print("ERROR: could not obtain a valid copy of the plugin files.")
        print("Put 'SubtitleTranslate - DeepSeek.as' and 'SubtitleTranslate - DeepSeek.ico'")
        print("next to this installer and run it again for a fully offline install.")
        sys.exit(1)

    print(strings["installation_complete"].format(target_path))


APP_VERSION = "0.8"


def self_check():
    """打包后的冒烟自检：确认内嵌资源有效、关键依赖可加载。不写任何文件。"""
    print(f"PotPlayer DeepSeek Translate installer v{APP_VERSION}")
    print(f"frozen        : {getattr(sys, 'frozen', False)}")
    print(f"resource_dir  : {resource_dir()}")
    print(f"target arch   : {sys.maxsize > 2**32 and '64-bit' or '32-bit'}")

    rc = 0
    for filename, validator in (
        ("SubtitleTranslate - DeepSeek.as", validate_as),
        ("SubtitleTranslate - DeepSeek.ico", validate_ico),
    ):
        path = os.path.join(resource_dir(), filename)
        exists = os.path.exists(path)
        valid = bool(exists and validator(path))
        size = os.path.getsize(path) if exists else 0
        print(f"  {'OK ' if valid else 'BAD'} {filename}  {size} bytes")
        if exists:
            with open(path, "rb") as fh:
                print(f"      sha256 {hashlib.sha256(fh.read()).hexdigest()}")
        if not valid:
            rc = 1

    for mod in ("requests", "win32com.client"):
        try:
            __import__(mod)
            print(f"  OK  {mod}")
        except Exception as exc:  # noqa: BLE001
            print(f"  BAD {mod}: {exc}")
            rc = 1

    print("RESULT:", "PASS" if rc == 0 else "FAIL")
    return rc


def main():
    # 打包后的冒烟自检入口：不触发提权、不进入交互菜单
    if "--check" in sys.argv or "--version" in sys.argv:
        sys.exit(self_check())

    lang = get_language()
    strings = LANGUAGE_STRINGS[lang]

    if not is_admin():
        restart_as_admin(strings)

    try:
        install(strings)
    except KeyboardInterrupt:
        print("\nInstallation interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(strings["error_occurred"].format(e))
        sys.exit(1)
    input(strings["installation_done"])


if __name__ == "__main__":
    main()