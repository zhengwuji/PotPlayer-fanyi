# v0.4 优化说明 / Optimization Notes

本分支（`opt-full`）在原版 `v0.3` 基础上对 `SubtitleTranslate - DeepSeek.as` 做了一轮完整优化，
并修正了 `installer.py` 的下载源问题。

原版三个 tag（`v0.1` / `v0.2` / `v0.3`）与 `main` 分支保持原样，随时可 `git checkout main` 回滚。

**当前版本：v0.5** —— v0.4.1 依 PotPlayer 官方 `Extension\api.txt` 校正了接口调用；
v0.5 加入**完整的自定义 API 支持**（换服务商 / 换模型 / 换请求格式），详见第三节。

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

## 三、多 API 管理（v0.6）

### 能做什么

**保存任意多个自定义 API，随时新增、切换、删除**，不必每次重写一长串配置。

「账户名称」输入框（v0.6 起已由新增的 `GetUserText()` 打上标签）支持这些写法：

| 写法 | 作用 |
|---|---|
| *（留空）* | 使用 DeepSeek 官方 |
| `work` 或 `use=work` | **切换到已保存的 API「work」** |
| `add=work; preset=siliconflow; model=xxx` | **新增 / 覆盖保存**一个 API 并立即启用 |
| `del=work` | 删除该 API |
| `list` | **弹窗列出全部**已保存的 API（含地址、模型、格式、认证） |
| `https://xxx/v1` 或 `preset=...; model=...` | 一次性配置，**不保存**（兼容 v0.5 行为） |

保存的 API 各自带自己的 Key，切换时自动一并切换。名字大小写不敏感。

### 现场演示

新增两个：

```
第 1 次  账户名称: add=zhipu; preset=zhipu; model=glm-4-flash
         密码栏  : sk-你的智谱key

第 2 次  账户名称: add=local; preset=ollama; model=qwen2.5:7b
         密码栏  : （留空，本地服务不需要 Key）
```

之后切换只需写名字：

```
账户名称: zhipu         ← 切到智谱
账户名称: local         ← 切到本地 Ollama
账户名称: list          ← 看看都存了些什么
账户名称: del=local     ← 不要了，删掉
```

**Key 的更新**：切到某个 API 时如果在密码栏输了新 Key，会自动写回该 API（方便换 Key）。

### 存储方式

用 `HostSaveString("profiles", ...)` 保存 —— 与 `api_key` 完全相同的机制，
在本项目里已经验证过可以跨会话持久化。

记录之间用不可见控制字符 `\u0001` / `\u0002` 分隔。之所以不用可见字符：
`extra=` 自定义请求头里本身就带 `:` 和 `|`，用它们会切分歧义。
（`AngelScript` 的 `split` 是按「字符集合」切分而非按子串，所以分隔符必须是单字符。）

### 顺带修掉的两个原版/前版缺陷

1. **`GetUserText()` 缺失**。`GetUserText()` 是官方插件（google / DeepL / bing /
   LM Studio / Yandex / papagoNMT / Libre）**全部实现**的入口，用来给「账户名称」
   输入框加标签。原版和 v0.4/v0.5 都没实现，所以那个框没有任何提示 ——
   这正是"不知道该往哪填"的原因之一。现已补上。
2. **`ServerLogout()` 缺失**，多数官方插件都有，一并补上。

### 一个编译级 bug（v0.4/v0.5 存在）

`api.txt` 自带的示例写的是：

```
HostMessageBox("ThreadFunction " + formatInt(num));
```

**不是** `"ThreadFunction " + num` —— 说明 AngelScript 不会把数字隐式转成字符串。
而 v0.4/v0.5 里有一行：

```angelscript
Dbg("attempt " + retryCount + ", model=" + cfgModel + ", body=" + body.length() + " bytes");
```

`retryCount` 是 `int`、`body.length()` 是 `uint`，**这一行会导致脚本编译失败**。
v0.6 已全部改为 `formatInt(...)`。

这个 bug 肉眼极难发现，所以我给验证脚本加了 `check_concat` 规则，
并且做了**双向验证**（`test_concat_checker.py`）：

```
d9c4741 v0.5（已知缺陷）  expect=FAIL  got=FAIL   ← 准确报出 retryCount 与 body.length()
worktree 当前版本         expect=PASS  got=PASS
```

只对好代码通过的检查器等于没有检查器，所以负向用例是必须的。

### 关于 `substr` 的更正

v0.4.1 时我依据 `api.txt` 判断 "string 类没有 `substr`"，并为此写了硬性禁用规则。
这个判断**不准确**：官方 `google.as` 自己就用了 `substr(start, count)` 和 `findFirst`，
说明 `api.txt` 列出的只是 PotPlayer 的**附加**方法，并非全集
（`find` / `split` / `length` 同样不在列表里，但原版一直在用）。

本脚本仍统一使用文档明确收录的 `Left()`，但禁用规则已从 "FAIL" 降为提示。
那条依据不成立的硬规则已撤销。

### 配置项语法（`add=` 之后、或一次性配置时使用）

分号 `;` 分隔，每项 `键=值`；也可以**直接填一个裸 URL**（等价于 `url=...`）。

| 键 | 别名 | 说明 | 默认 |
|---|---|---|---|
| `preset` | — | 服务商预设，见下表 | `deepseek` |
| `url` | `base` `endpoint` `host` | 服务地址 | 随 preset |
| `model` | — | **模型名**（原版写死，现在可任意指定） | 随 preset |
| `format` | — | `openai` 或 `anthropic` | `openai` |
| `auth` | — | 认证方式，见下表 | `bearer` |
| `extra` | `header` | 额外请求头，`Name:Value\|Name:Value` | 空 |
| `ua` | `useragent` | 自定义 User-Agent | `PotPlayer-DeepSeek-Translate/0.5` |

**地址自动补全**：只填域名或 `.../v1` 都会补成完整路径；已经含 `/chat/completions`
或 `/messages` 的则原样使用。已识别 `/v1` `/v2` `/v3` `/v4` 版本段，
所以智谱的 `/api/paas/v4`、Gemini 的 `/v1beta/openai` 都不会被错误地再插一层 `/v1`。

### 认证方式对照

| `auth=` | 实际发送的请求头 | 用于 |
|---|---|---|
| `bearer`（默认） | `Authorization: Bearer <key>` | 绝大多数服务商 |
| `raw` | `Authorization: <key>` | 少数网关不带 Bearer 前缀 |
| `x-api-key` | `x-api-key: <key>` | Anthropic |
| `api-key` | `api-key: <key>` | Azure OpenAI |
| `none` | 不发认证头 | 本地 Ollama / LM Studio |

选 `anthropic` 格式时会自动附加 `anthropic-version: 2023-06-01`。

### 内置预设

| `preset=` | 地址 | 默认模型 | 格式 |
|---|---|---|---|
| `deepseek`（默认） | api.deepseek.com/v1 | deepseek-chat | openai |
| `openai` | api.openai.com/v1 | gpt-4o-mini | openai |
| `siliconflow` | api.siliconflow.cn/v1 | Qwen/Qwen2.5-7B-Instruct | openai |
| `moonshot` | api.moonshot.cn/v1 | moonshot-v1-8k | openai |
| `zhipu` / `glm` | open.bigmodel.cn/api/paas/v4 | glm-4-flash | openai |
| `qwen` / `dashscope` | dashscope.aliyuncs.com/compatible-mode/v1 | qwen-turbo | openai |
| `openrouter` | openrouter.ai/api/v1 | openai/gpt-4o-mini | openai |
| `groq` | api.groq.com/openai/v1 | llama-3.1-8b-instant | openai |
| `together` | api.together.xyz/v1 | Llama-3.3-70B-Turbo | openai |
| `gemini` | generativelanguage…/v1beta/openai | gemini-2.0-flash | openai |
| `anthropic` / `claude` | api.anthropic.com | claude-3-5-haiku-latest | **anthropic** |
| `ollama` | localhost:11434/v1 | qwen2.5:7b | openai（auth=none） |
| `lmstudio` | localhost:1234/v1 | local-model | openai（auth=none） |
| `oneapi` / `newapi` | localhost:3000/v1 | gpt-4o-mini | openai |
| `azure` | 需自己补 url= | gpt-4o-mini | openai（auth=api-key） |

### 直接可粘的示例

```
（留空）                                              → DeepSeek 官方
preset=siliconflow; model=deepseek-ai/DeepSeek-V3     → 硅基流动换模型
preset=zhipu                                          → 智谱 glm-4-flash
preset=ollama; model=qwen2.5:7b                       → 本地 Ollama，免 Key
preset=anthropic; model=claude-3-5-sonnet-latest      → Claude（自动切 anthropic 格式）
https://my-gateway.com/v1                             → 裸 URL 简写
url=my-proxy.local/openai; model=gpt-4o; auth=raw; extra=X-Tenant:abc
```

设置好后插件启动会打印生效的端点、模型、格式与认证方式，便于核对。

### 双格式说明

| | OpenAI 兼容 | Anthropic |
|---|---|---|
| 路径 | `/v1/chat/completions` | `/v1/messages` |
| system 提示词 | `messages[0].role = "system"` | 顶层 `system` 字段 |
| 取译文 | `choices[0].message.content` | `content[0].text` |

两种格式的**响应解析路径不同**，插件会自动按 `format=` 选择。
本机没有 AngelScript 运行时，但请求体形状、认证头、响应解析路径都由
`test_api_contract.py`（本地 mock 服务）逐项验证过，见第四节。

---

## 四、installer.py 的修正

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

## 五、验证

```bash
python verify_as.py            # 括号配平 + 函数引用 + 复刻逻辑 + api.txt 交叉校验
python test_api_contract.py    # 本地 mock 服务验证 HTTP 契约（22 项）
python test_install.py         # 安装落盘端到端测试
python bench_ctx.py            # 原版 context 膨胀量化
python -m py_compile installer.py
```

全部输出 `RESULT: PASS`。

`verify_as.py` 的第三项会把 AngelScript 逻辑**逐行复刻成 Python** 再跑断言 —— 因为本机
无法执行 `.as`，这是唯一能锁死行为的手段。它覆盖：10 组配置串解析 / URL 归一化、
9 组 OpenAI 格式请求体、3 组 Anthropic 格式请求体。

`test_api_contract.py` 起一个模拟真实服务商响应体的本地 HTTP 服务，验证路径补全、
认证头、`extra` 自定义头、两种响应解析路径、以及欠费错误体的识别。

**未验证部分**：本机没有 PotPlayer 的 AngelScript 运行时，因此
**`.as` 脚本仍未经真实加载测试**。所有 `Host*` 调用已逐一比对官方 `api.txt`
（7/7 命中），`substr` 这类未文档化方法已清除，但语法层面的问题只能在实机暴露。

---

## 六、部署

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
3. **账户名称**：留空用 DeepSeek 官方；或按第三节填 `preset=...;model=...` 自定义 API。
   **密码**填 API Key（`auth=none` 的本地服务可留空）。
4. 想排查问题就把脚本里的 `DEBUG_LOG` 改成 `true` —— 插件会调用
   `HostOpenConsole()` 打开宿主的调试控制台，日志实时可见，不用去翻文件。

---

## 七、从源码构建 exe

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
# 产物: dist\PotPlayer-DeepSeek-Translate-Installer.exe  (约 18 MB)
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

## 八、验证命令一览

```powershell
python verify_as.py            # .as 静态自检 + 复刻逻辑断言 + api.txt 交叉校验
python test_concat_checker.py  # 反向验证：对已知缺陷版本必须报 FAIL
python test_api_contract.py    # HTTP 契约测试（本地 mock 服务，22 项）
python test_install.py         # 安装落盘端到端测试（临时目录，不碰 PotPlayer）
python bench_ctx.py            # 原版 context 膨胀量化
python -m py_compile installer.py

# exe 冒烟自检（不写盘、不触发 UAC，打印内嵌资源的 SHA256）
.\dist\PotPlayer-DeepSeek-Translate-Installer.exe --check
```

全部输出 `PASS`。

### 内嵌资源哈希

用 `--check` 打印的 SHA256 与下面比对，即可确认 exe 内嵌的正是本分支的 v0.5 脚本：

```
1b175900bc5bbf1c4e2f7f6968a7828f902f03910d12b83f04d35c7f21751da6  SubtitleTranslate - DeepSeek.as
2cef8f8a8b0d8fc9beaf954ec49a9c19952dff9eb5f0f753f8f54ec02d41ed89  SubtitleTranslate - DeepSeek.ico
```

（每次改脚本后哈希会变，以 `--check` 实际输出为准；`verify_as.py` 的第四项会
断言脚本里没有 `substr` 等 `api.txt` 未收录的调用，防止回退。）

