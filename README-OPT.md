# v0.4 优化说明 / Optimization Notes

本分支（`opt-full`）在原版 `v0.3` 基础上对 `SubtitleTranslate - DeepSeek.as` 做了一轮完整优化，
并修正了 `installer.py` 的下载源问题。

原版三个 tag（`v0.1` / `v0.2` / `v0.3`）与 `main` 分支保持原样，随时可 `git checkout main` 回滚。

---

## 一、量化效果（实测）

`bench_ctx.py` 复刻了原版的 context 构建循环：

| 场景 | 原版 v0.3 每次请求携带的 context | v0.4 |
|---|---|---|
| 英文字幕 40 字符/条 | 13,552 字节（约 309 条字幕） | ≤ 600 字节 |
| **中文字幕 20 字/条** | **47,380 字节（约 1031 条字幕）** | ≤ 600 字节 |

原版的上限是「填到 3096 token 为止」，中文字幕场景下会把**上千条历史字幕**全部塞进每一次请求。
由于 `HostUrlGetString` 是同步阻塞调用，这直接把单条字幕的往返延迟推到秒级——一旦单条耗时超过
字幕间隔，翻译就会永久落后于播放。

`verify_as.py` 复刻了新的 `BuildRequest()`，实测上行体积：

| 用例 | messages | 上下文对 | 上行字节 |
|---|---|---|---|
| 普通中文（无上下文） | 2 | 0 | 899 |
| 三行上下文 | 6 | 2 | 1,082 |
| 六行历史（应截断到 3） | 8 | 3 | 1,121 |
| 超长单行触发字节上限 | 4 | 1 | 962 |
| 含双引号 / 反斜杠 / 制表换行 | 2 | 0 | ~905 |

即：**每次请求的上行体积从 13–47 KB 降到约 1 KB，缩小约 13–47 倍。**

---

## 二、逐项改动

### P0-1　context 从 ~3000 token 收到 3 条 / 600 字节
- 新增 `BuildContext` 语义的 `StartPairIndex()`，只带最近 3 条历史，且整块不超过 600 字节。
- **预算在"加入前"判定**，因此 600 字节是真正的硬上限；同时永远至少保留最新一条。
  （首版实现是"加入后"判定，会让最后一条顶穿上限——实测从 12 字节直接冲到 2,412 字节，
  `verify_as.py` 的"超长单行"用例专门锁死这个回归。）
- 删除 `GetModelMaxTokens()`（硬编码 4096，已无意义）。

### P0-2　messages 改为 system + user/assistant 交替，修掉"返回很多字幕内容"
原版把 `Context` 和被翻译正文拼在**同一个 user message** 里：

```
Context:
<历史字幕>
Subtitle to translate:
<当前字幕>
```

模型看到两段待译文本，自然会一起翻——这正是 README 里承认的
"有概率返回很多字幕的内容"，代码只能靠"多行只取最后一行"兜底（治标）。

现在改为：

```json
[
  {"role":"system",    "content":"...Translate ONLY the last user message..."},
  {"role":"user",      "content":"<历史原文 1>"},
  {"role":"assistant", "content":"<历史译文 1>"},
  {"role":"user",      "content":"<当前原文>"}
]
```

历史以「原文/译文」成对形式作为 few-shot 上下文，模型天然只对最后一条 user 负责，混译问题从根上消失。

附带收益：`system + 早期历史对` 构成稳定的最长公共前缀，可命中 DeepSeek 的上下文硬盘缓存。

同时 `max_tokens` 从 **1000 降到 256**——字幕译文不可能需要 1000 token，这个值越大越容易诱发复述。

### P0-3　修掉真实 bug：翻译失败的字幕也污染历史
原版 `subtitleHistory.insertLast(Text)` 在**请求发起之前**执行。一旦请求失败，这句没翻出来的
原文就永久留在历史里，之后每一条翻译都带着这段脏 context。

现在改为只在拿到有效译文后调用 `RememberPair()`。

### P1-1　指数退避 + 永久错误短路
- 失败等待从固定 1s 改为 1s → 2s → 4s。
- `IsPermanentError()` 识别 `Authentication` / `Insufficient Balance` / `invalid_api_key` /
  `Model Not Exist` 等，**立即返回不重试**。原版遇到欠费或 key 错误会白重试 3 次、每次白等 1 秒，
  直接把字幕卡死。

### P1-2　启动预热，消除开头几段乱码
README 说"刚开始几段话会显示乱码"。根因是**第一次调用才做 DNS + TCP + TLS 握手**，
这段额外耗时让宿主在拿到结果前先渲染了脏缓冲。

`OnInitialize()` 里现在会打一发 `max_tokens:1` 的探测请求，同时完成三件事：
建立连接、验证 key、把错误原因提前打到日志里。

### P1-3　LRU 缓存 + 重复行短路
PotPlayer 在暂停、拖拽、重播、片头曲等场景会反复用同一句调 `Translate`。
新增 32 条 LRU 缓存（`CacheGet` / `CachePut`），命中即零成本返回。

### P1-4　token 估算改为 UTF-8 感知
原版 `int(float(text.length()) / 4)` 假设 4 字符 = 1 token，那是英文比值。
UTF-8 下 1 个汉字 = 3 字节 ≈ 1 token，对中文低估 12 倍以上——这也是 P0-1 那个循环失控的帮凶。
v0.4 改用固定的 3 条上限，不再依赖 token 估算，从根上绕开这个坑。

### P2-1　端点可配置 + 中性 User-Agent
- **账户名称字段**现在可填自定义接口地址（留空走 DeepSeek 官方）。自动补 `/v1/chat/completions`。
  这样自建中转、国内镜像、SiliconFlow 等 OpenAI 兼容端点都能直接接。
- `UserAgent` 从浏览器 UA 改为 `PotPlayer-DeepSeek-Translate/0.4`。伪装浏览器对 API 调用毫无
  好处，走 Cloudflare 中转时反而更容易被判成机器人。
- 端点持久化到 `api_base`，`OnInitialize` 时读回。

### P2-2　RTL 标记成对，不再泄漏
原版只加了 U+202B (RLE)，没有配对的 U+202C (PDF)。这个标记会**泄漏到后续所有字幕**，
把之后的中文/英文全部反向排列。改用现代隔离符 **U+2067 (RLI) … U+2069 (PDI)**，只作用于当前行。

### P2-3　标点规则自相矛盾已修正
原版 prompt 同时要求 "不含任何标点" 和 "可调整断句以提升可读性"，两者直接冲突——
中文长句去掉逗号顿号后反而难读。现在改为：

> 不加句末标点（`. ! ? 。！？`），但保留必要的句内标点（逗号、顿号、破折号）。

### P3-1　语言代码映射为自然语言名
原版直接把 `zh-CN` / `zh-TW` / `pt` 甩给模型，容易踩坑（繁体出简体、巴西葡语出欧洲葡语）。
新增 `LangName()` 映射表，输出 `Simplified Chinese (简体中文)` 这类明确的语言名。

### P3-2　输出硬化
- `Finalize()` 把多行压成单行（原版是"只取最后一行"——模型正常输出多行时会**静默丢内容**），
  并截断到 200 字符防止糊满屏幕。
- `JsonEscape()` 补齐 `\b` / `\f`。
- 错误提示改为短中文（`[未配置 API Key]` / `[翻译失败]` / `[翻译服务报错]`），
  不再把长英文串直接糊到字幕上。

---

## 三、installer.py 的修正

原版有两个问题：

1. **硬编码单一第三方反代 `github.20246688.xyz`**，非官方域名。
2. **用的是 `/blob/main/` 页面地址**——那返回的是 HTML 页面而不是文件本体。

现在改为：

- `ASSET_BASES` 多源回退：官方 `raw.githubusercontent.com` → jsDelivr CDN →
  `gh-proxy.com` → `ghfast.top`。
- **本地优先**：如果 `.as` / `.ico` 就在安装器同目录，直接复制，可完全离线安装
  （也意味着从本仓库根目录运行时会带上 v0.4 的优化版脚本）。
- **内容校验**：`validate_as()` 拒绝小于 200 字节、以 `<` 开头（反代返回的 HTML 错误页）
  或缺少 `GetTitle`/`Translate` 的载荷；`validate_ico()` 校验 `00 00 01 00` 文件头。
- 全部失败时给出明确的手动放置指引，而不是静默退出。

---

## 四、验证

```bash
python verify_as.py    # 括号配平 + 函数引用 + JSON 结构（复刻 BuildRequest）
python bench_ctx.py    # 原版 context 膨胀量化
python -m py_compile installer.py
```

`verify_as.py` 当前输出 `RESULT: PASS`。

**未验证部分**：本机没有 PotPlayer 的 AngelScript 运行时，因此
**脚本未经真实加载测试**。`Finalize()` 里用到了 `string.substr()`，若你的
PotPlayer 版本不支持该方法，删掉那 3 行截断逻辑即可，其余部分不受影响。

---

## 五、部署

### 方式 A：一键安装器 exe（推荐）

直接运行仓库根目录的：

```
dist\PotPlayer-DeepSeek-Translate-Installer.exe
```

会自动请求管理员权限（UAC），然后扫描硬盘找 PotPlayer 的 `Translate` 目录并安装。
**整个过程不需要联网**——插件本体已内嵌在 exe 里。

先自检（不写盘、不触发 UAC），确认 exe 完好：

```powershell
.\dist\PotPlayer-DeepSeek-Translate-Installer.exe --check
```

### 方式 B：手工复制

1. 把 `SubtitleTranslate - DeepSeek.as` 和 `SubtitleTranslate - DeepSeek.ico` 复制到
   PotPlayer 的翻译插件目录，例如
   `D:\Program Files\DAUM\PotPlayer\Extension\Subtitle\Translate`
2. 重启 PotPlayer。
3. 设置里：**账户名称**留空（或用自定义端点），**密码**填 DeepSeek API Key。
4. 想排查问题就把脚本里的 `DEBUG_LOG` 改成 `true`，然后看 PotPlayer 的日志。

---

## 六、从源码构建 exe

原版仓库把 `build/` `dist/` `*.spec` 全部写进了 `.gitignore`，所以 Releases 里的
`installer.exe` **无法从源码复现**——这是"仓库不是完整源码"的真正缺口。本分支补上了：

```
installer.spec               PyInstaller 配置（含图标、版本资源、hiddenimports）
file_version_info.txt        exe 的版本资源（属性页里能看到 0.4.0.0）
requirements-build.txt       构建期依赖
```

构建：

```powershell
python -m pip install -r requirements.txt -r requirements-build.txt
python -m PyInstaller installer.spec --noconfirm --clean
# 产物: dist\PotPlayer-DeepSeek-Translate-Installer.exe  (约 15 MB)
```

### 冻结模式下的三个必要修正

把 `installer.py` 打包成 exe 后，有三处会出真实故障，已一并修好：

| 问题 | 原因 | 修正 |
|---|---|---|
| 找不到内嵌资源 | `__file__` 在 onefile 下指向 `sys._MEIPASS` 临时目录 | 新增 `resource_dir()`，冻结时返回 `sys._MEIPASS` |
| 提权重启会自我递归 | `shell32.ShellExecuteW(..., sys.executable, list2cmdline(sys.argv))` 在冻结后会把 exe 自身路径当参数回传 | 冻结时 `params=""` |
| `--check` 无法自检 | 原本没有任何非交互入口，一跑就弹 UAC 或卡在菜单 | 新增 `--check` / `--version` |

### 打包踩坑记录（供后续维护）

**不要**在 `installer.spec` 的 `excludes` 里排除 `email` / `html` / `http.server` /
`sqlite3` / `distutils` / `setuptools`。首版这样排之后，exe 一启动就崩：

```
File "urllib3\exceptions.py", line 6, in <module>
ModuleNotFoundError: No module named 'email'
```

`urllib3`（`requests` 的依赖）间接依赖 `email`。目前只排除确定用不到的
`tkinter` / `PIL` / `numpy` / `pandas` / `matplotlib` / `scipy` / `pytest`。

---

## 七、验证命令一览

```powershell
python verify_as.py            # .as 静态自检：括号配平 + 函数引用 + JSON 结构
python bench_ctx.py            # 原版 context 膨胀量化
python test_install.py         # 安装落盘端到端测试（临时目录，不碰 PotPlayer）
python -m py_compile installer.py

# exe 冒烟自检（打印内嵌资源的 SHA256，可与源文件比对）
.\dist\PotPlayer-DeepSeek-Translate-Installer.exe --check
```

全部输出 `PASS`。

### 内嵌资源哈希（v0.4 当前值）

```
b5dad273c3ee355dfa1898b859274d86ed71725c37cf8152756a3959db863fb9  SubtitleTranslate - DeepSeek.as
2cef8f8a8b0d8fc9beaf954ec49a9c19952dff9eb5f0f753f8f54ec02d41ed89  SubtitleTranslate - DeepSeek.ico
```

`--check` 打印的哈希与上表一致，即证明 exe 内嵌的是本分支的 v0.4 脚本。

**未验证部分**：本机没有 PotPlayer 的 AngelScript 运行时，因此
**`.as` 脚本未经真实加载测试**。`Finalize()` 里用到了 `string.substr()`，若你的
PotPlayer 版本不支持该方法，删掉那 3 行截断逻辑即可，其余部分不受影响。
