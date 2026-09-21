/*
    Real-time subtitle translation for PotPlayer
    DeepSeek / 自定义 OpenAI 兼容或 Anthropic 兼容 API
    ------------------------------------------------------------------
    v0.4 优化
      P0-1  context 限制为最近 3 条 / 600 字节（原为 ~3000 token）
      P0-2  system + user/assistant 交替，根除 context 混译；max_tokens 1000→256
      P0-3  只有翻译成功才写入历史（修原版脏 context bug）
      P1-1  指数退避 + 永久错误短路
      P1-2  启动预热，消除开头乱码
      P1-3  LRU 缓存 + 重复行短路
      P2-2  RTL 隔离符成对（U+2067/U+2069）
      P3-2  输出压单行 + 截断，JsonEscape 补齐控制字符

    v0.5  完整自定义 API：账户名支持 preset/url/model/format/auth/extra/ua
          与裸 URL；15 个服务商预设；OpenAI 与 Anthropic 双格式；
          5 种认证方式。

    v0.6  多 API 管理（本版核心）
      * 可保存任意多个自定义 API，随时新增/切换/删除，不再每次改写一个字段
        账户名称:  use=名字 或直接写名字   切换
                   add=名字; preset=...     新增/覆盖
                   del=名字                 删除
                   list                     弹窗列出全部
      * 新增 GetUserText()：给「账户名称」输入框加标签（官方插件都有，原脚本缺）
      * 新增 ServerLogout()
      * 修 v0.4/v0.5 的编译级 bug：int 不能隐式拼进字符串，
        必须用 formatInt()（见 api.txt 自带示例）
      * 候选配置存于 HostSaveString("profiles")，与 api_key 同一机制，已验证可用

    v0.7  获取可用模型
      * models / models=名字 拉取 GET /v1/models，弹窗列出并落成可复制的 txt
      * model=@序号 直接选中清单里的第 N 个

    v0.8  针对真实端点实测暴露的问题（Inception Labs / Mercury）
      * maxtok= 可配（默认 256 → 512）
      * body= 可往请求体注入任意 JSON 片段；推理模型必须
        body="reasoning_effort":"none"，否则 reasoning 会吃光 max_tokens，
        content 返回 null（实测 mercury-2.5 在 256 下 5 条全空）
      * 新增 preset=inception，已带好上述 body 参数
      * 修：error.message 非字符串时被当成"可重试"，现识别为结构化错误直接放弃
      * 修：IsPermanentError 漏掉 "Incorrect API key"（Inception/OpenAI 的措辞）
      * 修：ParseAccountSpec 改为两遍解析，显式值不再被 preset 按书写顺序冲掉
      * 空内容 + usage 里有 reasoning_tokens → 给出可操作的修复提示
*/

// ============================================================ Plugin info
string GetTitle() {
    // 由管理器生成的多条目插件里，用各自的名字显示在 PotPlayer 列表中
    if (!BAKED_NAME.empty()) return BAKED_NAME;
    return "{$CP0=DeepSeek Translate$}";
}

string GetVersion() {
    return "1.1";
}

string GetDesc() {
    return "{$CP0=Real-time subtitle translation. DeepSeek / custom API with profiles$}";
}

string GetLoginTitle() {
    return "{$CP0=API Configuration$}";
}

// 这一行显示在登录框的「账户名称」输入框旁边，告诉用户该填什么
string GetUserText() {
    return "{$CP936=API 配置（名字 / 留空=官方）:$}{$CP0=API profile: $}";
}

string GetLoginDesc() {
    return "{$CP936=切换: use=名字 (或直接写名字) ｜ 新增: add=名字; preset=siliconflow ｜ 删除: del=名字 ｜ 查看: list ｜ 获取可用模型: models ｜ 选模型: model=@序号 ｜ 留空 = DeepSeek 官方。密码栏填 API Key。$}";
}

string GetPasswordText() {
    return "{$CP0=API Key:$}";
}

// ============================================================== Config
string api_key = "";
string USER_AGENT = "PotPlayer-DeepSeek-Translate/0.7";

// ==== BAKED CONFIG BEGIN ====
// 这一段由「API 管理器」生成插件条目时写入，手改会被下次生成覆盖。
// 留空 = 走配置文件 / 界面里的账户设置（默认行为）。
string BAKED_NAME    = "";
string BAKED_ACCOUNT = "";
string BAKED_KEY     = "";
// ==== BAKED CONFIG END ====

// 当前生效的自定义 API 配置
string cfgBase   = "";
string cfgModel  = "deepseek-chat";
string cfgFormat = "openai";
string cfgAuth   = "bearer";
string cfgExtra  = "";
string cfgUA     = "";
string cfgBody   = "";      // 追加进请求体的原始 JSON 片段（如 "reasoning_effort":"none"）
string specKey   = "";      // 配置串里 key= 的值（可选）

int MAX_TOKENS = 512;       // 可用 maxtok= 调整；推理模型需要更大额度

string acctSpec  = "";      // 「账户名称」原始内容
int    activeProfile = -1;  // 当前使用的已保存 API 序号，-1 = 一次性配置

int maxRetries = 3;
int baseRetryDelay = 1000;

string LOG_FILE = "deepseek_translate.log";
bool DEBUG_LOG = true;
bool DEBUG_CONSOLE = true;

int MAX_CTX_SENTENCES = 3;
int MAX_CTX_BYTES     = 600;
int MAX_OUTPUT_CHARS  = 200;
int CACHE_SIZE        = 32;

void LogDebug(const string &in m) {
    if (!DEBUG_LOG) return;
    if (DEBUG_CONSOLE) {
        HostOpenConsole();
        HostPrintUTF8("[DeepSeekLog] " + m + "\n");
    }
    uintptr fp = HostFileCreate(LOG_FILE);
    if (fp != 0) {
        HostFileSeek(fp, 0, 2);
        HostFileWrite(fp, "[" + formatInt(HostGetTickCount()) + "ms] " + m + "\r\n");
        HostFileClose(fp);
    }
}

void Dbg(const string &in m) {
    LogDebug(m);
}

// api.txt 第 33-44 行：长等待会被判脚本超时，
// 要把等待切片并在循环里调 HostIncTimeOut 延长预算。
void BackoffSleep(int ms) {
    int left = ms;
    while (left > 0) {
        int chunk = 200;
        if (left < chunk) chunk = left;
        HostSleep(chunk);
        HostIncTimeOut(chunk);
        left -= chunk;
    }
}

// ====================================================== 已保存 API 的管理
// 分隔符用不可见控制字符：extra 请求头里本身带 ':' 与 '|'，用可见字符会有歧义。
// 全部是单字符，兼容 AngelScript 的 split（它按「字符集合」切分，不是按子串）。
string PROF_SEP  = "\u0001";
string FIELD_SEP = "\u0002";

array<string> profName;   // 名字
array<string> profSpec;   // 配置串（preset=...; model=... 这套）
array<string> profKey;    // 该 API 自己的 Key

// 从 /v1/models 拉回来的可用模型清单（可选中 model=@序号）
array<string> modelList;
string MODEL_FILE = "deepseek_models.txt";   // 写入 PotPlayer 配置目录，便于复制

void LoadModelList() {
    while (int(modelList.length()) > 0) modelList.removeAt(0);
    string blob = HostLoadString("model_list", "");
    if (blob.empty()) return;
    array<string> items = blob.split(PROF_SEP);
    int i = 0;
    int n = int(items.length());
    while (i < n) {
        string it = items[i];
        i++;
        if (!it.empty()) modelList.insertLast(it);
    }
}

void SaveModelList() {
    string blob = "";
    int i = 0;
    int n = int(modelList.length());
    while (i < n) {
        if (i > 0) blob += PROF_SEP;
        blob += modelList[i];
        i++;
    }
    HostSaveString("model_list", blob);
}

// 只认 1..999。AngelScript 没有文档化的 atoi，但 int→string 有 formatInt，
// 用它逐个比对即可，不依赖任何未文档化的东西。
// ============================ 文本配置通道（绕开界面，记事本改一行即可）
// PotPlayer 的账户输入入口很隐蔽（要click列表里「账户」列的那种小格子），
// 所以这里再给一条纯文本通道：配置文件放在配置目录，存在且有效时优先。
//   api.txt 第126行 HostFileOpen「Open local file for read」没有路径限制，
//   而 HostFileCreate 被限死在 config 目录，所以放这里最合适。
string CFG_FILE = "deepseek_api.txt";
string fileAcct = "";
string fileKey  = "";
bool   fileConfigActive = false;
string cfgUsedPath = "";

string WithSlash(string p) {
    if (p.empty()) return p;
    if (p.Right(1) != "\\" && p.Right(1) != "/") p += "\\";
    return p;
}

string ConfigFilePath() {
    return WithSlash(HostGetConfigFolder()) + CFG_FILE;
}

// 依次尝试多个候选位置：配置目录 -> 脚本目录 -> PotPlayer 主程序目录。
// 这样配套管理器把文件写在哪儿都能被读到，不赌某一个目录。
array<string> ConfigCandidates() {
    array<string> v;
    string c = WithSlash(HostGetConfigFolder());
    if (!c.empty()) v.insertLast(c + CFG_FILE);
    string s = WithSlash(HostGetScriptFolder());
    if (!s.empty()) v.insertLast(s + CFG_FILE);
    string e = WithSlash(HostGetExecuteFolder());
    if (!e.empty()) v.insertLast(e + CFG_FILE);
    return v;
}

bool LoadConfigFile() {
    fileAcct = "";
    fileKey = "";
    fileConfigActive = false;
    cfgUsedPath = "";

    string path = "";
    array<string> cands = ConfigCandidates();
    int ci = 0;
    while (ci < int(cands.length())) {
        if (HostFileExist(cands[ci])) {
            path = cands[ci];
            break;
        }
        ci++;
    }

    if (path.empty()) {
        WriteConfigTemplate(ConfigFilePath());
        return false;
    }

    cfgUsedPath = path;
    uintptr fp = HostFileOpen(path);
    if (fp == 0) {
        Dbg("config file exists but could not be opened");
        return false;
    }

    int64 len64 = HostFileLength(fp);
    int len = int(len64);
    if (len <= 0 || len > 65536) {
        HostFileClose(fp);
        return false;
    }

    string txt = HostFileRead(fp, len);
    HostFileClose(fp);
    if (txt.empty()) return false;

    // 记事本可能存成带 BOM 的 UTF-8，BOM 会粘在第一行的键名上；同时剥除 Windows 回车符 \r
    txt.replace("\uFEFF", "");
    txt.replace("\r", "");

    array<string> lines = txt.split("\n");
    int i = 0;
    int n = int(lines.length());
    while (i < n) {
        string line = lines[i].Trim();
        i++;
        if (line.empty()) continue;
        string first = line.Left(1);
        if (first == "#" || first == ";") continue;   // 注释

        int eq = line.find("=");
        if (eq == -1) continue;

        string k = line.Left(eq).Trim().MakeLower();
        string v = line.Right(int(line.length()) - eq - 1).Trim();

        if (k == "account" || k == "user") fileAcct = v;
        else if (k == "key" || k == "apikey" || k == "api_key") fileKey = v;
    }

    fileConfigActive = (!fileAcct.empty() || !fileKey.empty());
    if (fileConfigActive) {
        Dbg("config file: account='" + fileAcct
            + "', keyLen=" + formatInt(int(fileKey.length())));
    }
    return fileConfigActive;
}

// 首次运行放一份带说明的模板，省得用户猜键名
void WriteConfigTemplate(const string &in path) {
    uintptr fp = HostFileCreate(CFG_FILE);
    if (fp == 0) return;
    string t = "# PotPlayer DeepSeek Translate - 文本配置\r\n";
    t += "#\r\n";
    t += "# 这个文件只要存在且填了内容，就【优先于】界面里保存的配置。\r\n";
    t += "# 删掉它即可回到界面配置。\r\n";
    t += "#\r\n";
    t += "# account : 与界面「账户名称」里填的写法完全一致\r\n";
    t += "# key     : API Key\r\n";
    t += "#\r\n";
    t += "# 例子：\r\n";
    t += "#   account=add=inception; preset=inception\r\n";
    t += "#   key=sk-xxxxxxxx\r\n";
    t += "#\r\n";
    t += "# 想看该服务有哪些模型：把 account 改成 models 后重启 PotPlayer\r\n";
    t += "\r\n";
    t += "account=\r\n";
    t += "key=\r\n";
    HostFileWrite(fp, t);
    HostFileClose(fp);
    Dbg("config template created: " + path);
}

int ParseSmallInt(const string &in s) {
    string t = s.Trim();
    if (t.empty()) return -1;
    int i = 1;
    while (i <= 4096) {
        if (formatInt(i) == t) return i;
        i++;
    }
    return -1;
}

void LoadProfiles() {
    while (int(profName.length()) > 0) profName.removeAt(0);
    while (int(profSpec.length()) > 0) profSpec.removeAt(0);
    while (int(profKey.length()) > 0) profKey.removeAt(0);

    string blob = HostLoadString("profiles", "");
    if (blob.empty()) return;

    array<string> recs = blob.split(PROF_SEP);
    int i = 0;
    int n = int(recs.length());
    while (i < n) {
        string rec = recs[i];
        i++;
        if (rec.empty()) continue;
        array<string> flds = rec.split(FIELD_SEP);
        if (int(flds.length()) < 2) continue;
        profName.insertLast(flds[0]);
        profSpec.insertLast(flds[1]);
        if (int(flds.length()) > 2) profKey.insertLast(flds[2]);
        else profKey.insertLast("");
    }
    Dbg("loaded profiles: " + formatInt(int(profName.length())));
}

void SaveProfiles() {
    string blob = "";
    int i = 0;
    int n = int(profName.length());
    while (i < n) {
        if (i > 0) blob += PROF_SEP;
        blob += profName[i] + FIELD_SEP + profSpec[i] + FIELD_SEP + profKey[i];
        i++;
    }
    HostSaveString("profiles", blob);
}

int FindProfile(const string &in name) {
    string want = name.Trim().MakeLower();
    if (want.empty()) return -1;
    int i = 0;
    int n = int(profName.length());
    while (i < n) {
        if (profName[i].MakeLower() == want) return i;
        i++;
    }
    return -1;
}

// 列出全部已保存的 API。会临时切换全局配置，所以先快照再还原。
string snapB, snapM, snapF, snapA, snapE, snapU, snapK, snapBo;
int snapT = 512;

void SnapshotCfg() {
    snapB = cfgBase; snapM = cfgModel; snapF = cfgFormat;
    snapA = cfgAuth; snapE = cfgExtra; snapU = cfgUA; snapK = api_key;
    snapBo = cfgBody; snapT = MAX_TOKENS;
}

void RestoreCfg() {
    cfgBase = snapB; cfgModel = snapM; cfgFormat = snapF;
    cfgAuth = snapA; cfgExtra = snapE; cfgUA = snapU; api_key = snapK;
    cfgBody = snapBo; MAX_TOKENS = snapT;
}

void ShowProfiles() {
    SnapshotCfg();

    string msg = "已保存的自定义 API：\n\n";
    int n = int(profName.length());
    if (n == 0) {
        msg += "  （还没有保存任何 API）\n\n";
        msg += "新增方法：把「API 配置」填成\n";
        msg += "  add=名字; preset=siliconflow; model=xxx\n";
    } else {
        int i = 0;
        while (i < n) {
            ParseAccountSpec(profSpec[i]);
            msg += "  " + formatInt(i + 1) + ". " + profName[i];
            if (i == activeProfile) msg += "   <== 当前";
            msg += "\n      模型: " + cfgModel + "\n";
            msg += "      地址: " + ResolveUrl() + "\n";
            msg += "      格式: " + cfgFormat + " / 认证: " + cfgAuth + "\n\n";
            i++;
        }
    }
    msg += "切换: use=名字（或直接写名字）\n删除: del=名字";

    RestoreCfg();
    HostMessageBox(msg, "DeepSeek Translate - API 列表", 2, 0);
}

// ====================================================== 自定义 API 预设
void ApplyPreset(const string &in name) {
    cfgBase   = "https://api.deepseek.com/v1";
    cfgModel  = "deepseek-chat";
    cfgFormat = "openai";
    cfgAuth   = "bearer";
    cfgExtra  = "";
    cfgBody   = "";

    if (name.empty() || name == "deepseek") return;

    if (name == "openai") {
        cfgBase = "https://api.openai.com/v1";
        cfgModel = "gpt-4o-mini";
    } else if (name == "inception" || name == "mercury") {
        // Mercury 是扩散式推理模型：不关推理会把 max_tokens 全用在 reasoning 上，
        // 实测 content 直接返回 null（5 条全空）。必须显式关掉。
        cfgBase = "https://api.inceptionlabs.ai/v1";
        cfgModel = "mercury-2.5";
        cfgBody = "\"reasoning_effort\":\"none\"";
    } else if (name == "siliconflow") {
        cfgBase = "https://api.siliconflow.cn/v1";
        cfgModel = "Qwen/Qwen2.5-7B-Instruct";
    } else if (name == "moonshot") {
        cfgBase = "https://api.moonshot.cn/v1";
        cfgModel = "moonshot-v1-8k";
    } else if (name == "zhipu" || name == "glm") {
        cfgBase = "https://open.bigmodel.cn/api/paas/v4";
        cfgModel = "glm-4-flash";
    } else if (name == "qwen" || name == "dashscope") {
        cfgBase = "https://dashscope.aliyuncs.com/compatible-mode/v1";
        cfgModel = "qwen-turbo";
    } else if (name == "openrouter") {
        cfgBase = "https://openrouter.ai/api/v1";
        cfgModel = "openai/gpt-4o-mini";
    } else if (name == "groq") {
        cfgBase = "https://api.groq.com/openai/v1";
        cfgModel = "llama-3.1-8b-instant";
    } else if (name == "together") {
        cfgBase = "https://api.together.xyz/v1";
        cfgModel = "meta-llama/Llama-3.3-70B-Instruct-Turbo";
    } else if (name == "gemini") {
        cfgBase = "https://generativelanguage.googleapis.com/v1beta/openai";
        cfgModel = "gemini-2.0-flash";
    } else if (name == "anthropic" || name == "claude") {
        cfgBase = "https://api.anthropic.com";
        cfgModel = "claude-3-5-haiku-latest";
        cfgFormat = "anthropic";
        cfgAuth = "x-api-key";
    } else if (name == "ollama") {
        cfgBase = "http://localhost:11434/v1";
        cfgModel = "qwen2.5:7b";
        cfgAuth = "none";
    } else if (name == "lmstudio") {
        cfgBase = "http://localhost:1234/v1";
        cfgModel = "local-model";
        cfgAuth = "none";
    } else if (name == "oneapi" || name == "newapi") {
        cfgBase = "http://localhost:3000/v1";
        cfgModel = "gpt-4o-mini";
    } else if (name == "azure") {
        cfgBase = "https://YOUR-RESOURCE.openai.azure.com/openai/deployments/YOUR-DEPLOYMENT";
        cfgModel = "gpt-4o-mini";
        cfgAuth = "api-key";
    }
}

// 解析配置串。返回「规范化」后的串（去掉 key= 与 preset 展开前的原样保留），
// 便于存进 profile。key= 的值放进 specKey。
string ParseAccountSpec(const string &in spec) {
    string s = spec.Trim();
    specKey = "";

    // 第一遍：先定位 preset。
    // 这样「显式写的值一定覆盖预设」与书写顺序无关 ——
    // 例如 model=xxx; preset=zhipu 也能正确得到 xxx，否则会被预设冲掉。
    array<string> parts = s.split(";");
    int i = 0;
    int n = int(parts.length());
    string pname = "";
    while (i < n) {
        string tk = parts[i].Trim();
        i++;
        int e0 = tk.find("=");
        if (e0 == -1) continue;
        if (tk.Left(e0).Trim().MakeLower() == "preset") {
            pname = tk.Right(int(tk.length()) - e0 - 1).Trim().MakeLower();
        }
    }
    ApplyPreset(pname);

    if (s.empty()) return "";

    string norm = "";
    i = 0;
    while (i < n) {
        string tok = parts[i].Trim();
        i++;
        if (tok.empty()) continue;

        int eq = tok.find("=");
        if (eq == -1) {
            cfgBase = tok;
            norm += "url=" + tok + ";";
            continue;
        }

        string k = tok.Left(eq).Trim();
        string kl = k.MakeLower();
        string v = tok.Right(int(tok.length()) - eq - 1).Trim();

        if (kl == "key") {
            specKey = v;
            continue;                      // key 不进 profile 串，单独存
        }
        if (kl == "preset") {
            // 第一遍已经应用过，这里只记进 norm
        } else if (kl == "url" || kl == "base" || kl == "endpoint" || kl == "host") {
            cfgBase = v;
        } else if (kl == "model") {
            // model=@3 → 取「获取可用模型」清单里的第 3 个
            if (v.find("@") == 0) {
                int pick = ParseSmallInt(v.Right(int(v.length()) - 1));
                if (pick >= 1 && pick <= int(modelList.length())) {
                    cfgModel = modelList[pick - 1];
                    Dbg("model @" + formatInt(pick) + " -> " + cfgModel);
                } else {
                    Dbg("model @ 序号超出范围，清单里有 "
                        + formatInt(int(modelList.length())) + " 个。先填 models");
                }
            } else {
                cfgModel = v;
            }
        } else if (kl == "maxtok" || kl == "max_tokens" || kl == "maxtokens") {
            // 推理模型需要更大额度，否则 reasoning 会把 max_tokens 吃光
            int mt = ParseSmallInt(v);
            if (mt > 0) {
                MAX_TOKENS = mt;
                Dbg("max_tokens = " + formatInt(MAX_TOKENS));
            } else {
                Dbg("maxtok 需要 1..4096 的整数，已忽略: " + v);
            }
        } else if (kl == "body" || kl == "params") {
            // 追加进请求体的原始 JSON，例如 body="reasoning_effort":"none"
            cfgBody = v;
        } else if (kl == "format") {
            cfgFormat = v.MakeLower();
        } else if (kl == "auth") {
            cfgAuth = v.MakeLower();
        } else if (kl == "extra" || kl == "header") {
            cfgExtra = v;
        } else if (kl == "ua" || kl == "useragent") {
            cfgUA = v;
        } else {
            Dbg("未知配置项，已忽略: " + k);
        }
        norm += k + "=" + v + ";";
    }
    return norm;
}

string ResolveUrl() {
    string u = cfgBase.Trim();
    if (u.empty()) u = "https://api.deepseek.com/v1";
    if (u.find("http") == -1) u = "https://" + u;

    while (int(u.length()) > 0 && u.Right(1) == "/") {
        u = u.Left(int(u.length()) - 1);
    }

    if (u.find("/chat/completions") != -1) return u;
    if (u.find("/messages") != -1) return u;

    bool hasVersion = false;
    if (u.find("/v1") != -1) hasVersion = true;
    if (u.find("/v2") != -1) hasVersion = true;
    if (u.find("/v3") != -1) hasVersion = true;
    if (u.find("/v4") != -1) hasVersion = true;
    if (!hasVersion) u += "/v1";

    if (cfgFormat == "anthropic") u += "/messages";
    else u += "/chat/completions";
    return u;
}

string CurrentUA() {
    if (cfgUA.empty()) return USER_AGENT;
    return cfgUA;
}

string BuildHeaders() {
    string h = "Content-Type: application/json";

    if (cfgAuth == "none") {
        // 本地 ollama / lmstudio 不需要认证
    } else if (cfgAuth == "x-api-key") {
        h = "x-api-key: " + api_key + "\n" + h;
    } else if (cfgAuth == "api-key") {
        h = "api-key: " + api_key + "\n" + h;
    } else if (cfgAuth == "raw") {
        h = "Authorization: " + api_key + "\n" + h;
    } else {
        h = "Authorization: Bearer " + api_key + "\n" + h;
    }

    if (cfgFormat == "anthropic") h += "\nanthropic-version: 2023-06-01";

    if (!cfgExtra.empty()) {
        array<string> ex = cfgExtra.split("|");
        int i = 0;
        int m = int(ex.length());
        while (i < m) {
            string e = ex[i].Trim();
            i++;
            if (!e.empty()) h += "\n" + e;
        }
    }
    return h;
}

// ================================================ 获取可用模型（/v1/models）
// 从 cfgBase 推出 models 端点：去掉已补全的部分，补齐版本段，再接 /models
string ResolveModelsUrl() {
    string u = cfgBase.Trim();
    if (u.empty()) u = "https://api.deepseek.com/v1";
    if (u.find("http") == -1) u = "https://" + u;

    int cut = u.find("/chat/completions");
    if (cut != -1) u = u.Left(cut);
    cut = u.find("/messages");
    if (cut != -1) u = u.Left(cut);

    while (int(u.length()) > 0 && u.Right(1) == "/") {
        u = u.Left(int(u.length()) - 1);
    }

    bool hasVersion = false;
    if (u.find("/v1") != -1) hasVersion = true;
    if (u.find("/v2") != -1) hasVersion = true;
    if (u.find("/v3") != -1) hasVersion = true;
    if (u.find("/v4") != -1) hasVersion = true;
    if (!hasVersion) u += "/v1";

    return u + "/models";
}

// 兼容两种常见形状：{"data":[{"id":...}]} 与 {"models":[{"name":...}]}
int FetchModels() {
    while (int(modelList.length()) > 0) modelList.removeAt(0);

    string url = ResolveModelsUrl();
    Dbg("fetching models: " + url);

    HostIncTimeOut(20000);
    string resp = HostUrlGetString(url, CurrentUA(), BuildHeaders(), "");
    if (resp.empty()) {
        Dbg("models: empty response");
        return 0;
    }

    JsonReader R;
    JsonValue Root;
    if (!R.parse(resp, Root)) {
        Dbg("models: parse failed, raw=" + resp.Left(200));
        return 0;
    }

    JsonValue arr = Root["data"];
    if (!arr.isArray()) arr = Root["models"];
    if (!arr.isArray()) {
        Dbg("models: no data/models array, raw=" + resp.Left(200));
        return 0;
    }

    int i = 0;
    int n = arr.size();
    while (i < n) {
        JsonValue it = arr[i];
        if (it["id"].isString()) modelList.insertLast(it["id"].asString());
        else if (it["name"].isString()) modelList.insertLast(it["name"].asString());
        i++;
    }

    SaveModelList();
    return int(modelList.length());
}

// 把清单写进 PotPlayer 配置目录的文本文件 —— 消息框里的字是复制不出来的，
// 落成文件才能拿去粘贴。
void SaveModelListFile(const string &in url) {
    uintptr fp = HostFileCreate(MODEL_FILE);
    if (fp == 0) {
        Dbg("models: cannot create file");
        return;
    }
    string txt = "API  : " + url + "\r\n";
    txt += "数量 : " + formatInt(int(modelList.length())) + "\r\n";
    txt += "用法 : 在「API 配置」里填 model=@序号\r\n";
    txt += "----------------------------------------\r\n";
    int i = 0;
    int n = int(modelList.length());
    while (i < n) {
        txt += formatInt(i + 1) + ". " + modelList[i] + "\r\n";
        i++;
    }
    HostFileWrite(fp, txt);
    HostFileClose(fp);
}

void ShowModels() {
    string url = ResolveModelsUrl();
    int got = FetchModels();

    if (got == 0) {
        HostMessageBox("没能取到模型列表。\n\n地址: " + url
                       + "\n\n可能原因：\n"
                       + "  · 该服务不提供 GET /v1/models\n"
                       + "  · Key 无效或没权限\n"
                       + "  · 本地服务没启动\n\n"
                       + "也可以直接把模型名手写进配置，例如：\n"
                       + "  add=work; preset=ollama; model=qwen2.5:7b",
                       "DeepSeek Translate - 获取可用模型", 2, 0);
        return;
    }

    SaveModelListFile(url);

    string msg = "共取到 " + formatInt(got) + " 个模型：\n\n";
    int i = 0;
    int shown = 0;
    while (i < got && shown < 25) {
        msg += "  " + formatInt(i + 1) + ". " + modelList[i] + "\n";
        i++;
        shown++;
    }
    if (got > shown) msg += "  ...（其余见下方文件）\n";
    msg += "\n完整列表已写到：\n" + HostGetConfigFolder() + "\\" + MODEL_FILE;
    msg += "\n\n选中某个：在「API 配置」里填\n";
    msg += "  model=@序号\n";
    msg += "例如  model=@" + formatInt(shown > 0 ? 1 : 0);

    HostMessageBox(msg, "DeepSeek Translate - 可用模型", 2, 0);
}

// ============================================ 处理「账户名称」字段的全部写法
string pickKey(const string &in passKey) {
    if (!specKey.empty()) return specKey;
    string pk = passKey.Trim();
    if (!pk.empty()) return pk;
    if (!BAKED_KEY.empty()) return BAKED_KEY;
    if (!fileKey.empty()) return fileKey;
    return "";
}

void ActivateProfile(int idx, const string &in passKey, bool rememberKey) {
    if (idx < 0 || idx >= int(profName.length())) return;
    activeProfile = idx;
    ParseAccountSpec(profSpec[idx]);

    string k = passKey.Trim();
    if (k.empty()) k = profKey[idx];
    else if (rememberKey) {
        profKey[idx] = k;      // 用户在密码栏输了新 Key，顺手更新该 profile
        SaveProfiles();
    }

    api_key = k;
    HostSaveString("api_key", api_key);
    HostSaveString("account_spec", profName[idx]);
}

void HandleAccountSpec(const string &in spec, const string &in passKey, bool showUi) {
    string s = spec.Trim();
    if (s.empty()) {
        if (!BAKED_ACCOUNT.empty()) {
            ParseAccountSpec(BAKED_ACCOUNT);
        } else if (!fileAcct.empty()) {
            ParseAccountSpec(fileAcct);
        } else {
            ParseAccountSpec("");
        }
        activeProfile = -1;
        api_key = pickKey(passKey);
        if (BAKED_ACCOUNT.empty() && BAKED_KEY.empty()) {
            HostSaveString("account_spec", "");
        }
        return;
    }

    string low = s.MakeLower();

    if (low == "list" || low == "?" || low == "help") {
        if (showUi) ShowProfiles();
        return;                     // 保持当前配置不变
    }

    // ---- models 或 models=名字 → 拉取可用模型 ----
    if (low == "models" || low.find("models=") == 0) {
        if (!showUi) return;        // 启动时不联网拉清单

        SnapshotCfg();
        if (int(s.length()) > 7) {
            string target = s.Right(int(s.length()) - 7).Trim();
            int pi = FindProfile(target);
            if (pi >= 0) {
                ParseAccountSpec(profSpec[pi]);
                if (!profKey[pi].empty()) api_key = profKey[pi];
            }
        }
        ShowModels();
        RestoreCfg();
        return;
    }

    // ---- del=名字 ----
    if (low.find("del=") == 0) {
        string nm = s.Right(int(s.length()) - 4).Trim();
        int idx = FindProfile(nm);
        if (idx >= 0) {
            profName.removeAt(idx);
            profSpec.removeAt(idx);
            profKey.removeAt(idx);
            SaveProfiles();
            if (activeProfile == idx) activeProfile = -1;
            else if (activeProfile > idx) activeProfile--;
            HostPrintUTF8("profile deleted: " + nm + "\n");
            if (showUi) HostMessageBox("已删除: " + nm, "DeepSeek Translate", 2, 0);
        } else if (showUi) {
            HostMessageBox("没找到名为 " + nm + " 的 API\n\n填 list 可查看全部",
                           "DeepSeek Translate", 2, 0);
        }
        return;
    }

    // ---- add=名字; 配置 ----
    if (low.find("add=") == 0) {
        array<string> parts = s.split(";");
        string head = parts[0].Trim();
        string name = head.Right(int(head.length()) - 4).Trim();

        string rest = "";
        int i = 1;
        int n = int(parts.length());
        while (i < n) {
            string t = parts[i].Trim();
            i++;
            if (t.empty()) continue;
            if (!rest.empty()) rest += ";";
            rest += t;
        }

        if (name.empty()) {
            if (showUi) HostMessageBox("add= 后面要写名字，例如：\n\nadd=work; preset=siliconflow; model=xxx",
                                       "DeepSeek Translate", 2, 0);
            return;
        }

        string norm = ParseAccountSpec(rest);
        string k = pickKey(passKey);

        int idx = FindProfile(name);
        if (idx >= 0) {
            profSpec[idx] = norm;
            profKey[idx] = k;
        } else {
            profName.insertLast(name);
            profSpec.insertLast(norm);
            profKey.insertLast(k);
            idx = int(profName.length()) - 1;
        }
        SaveProfiles();

        activeProfile = idx;
        api_key = k;
        HostSaveString("api_key", api_key);
        HostSaveString("account_spec", s);

        HostPrintUTF8("profile saved: " + name + " -> " + ResolveUrl() + "\n");
        if (showUi) {
            HostMessageBox("已保存 API：" + name + "\n\n地址: " + ResolveUrl()
                           + "\n模型: " + cfgModel + "\n格式: " + cfgFormat
                           + " / 认证: " + cfgAuth + "\n\n以后在「API 配置」里填 "
                           + name + " 即可切回来。",
                           "DeepSeek Translate", 2, 0);
        }
        return;
    }

    // ---- use=名字 或 直接写名字 ----
    string nm = s;
    if (low.find("use=") == 0) nm = s.Right(int(s.length()) - 4).Trim();

    int hit = FindProfile(nm);
    if (hit >= 0) {
        ActivateProfile(hit, passKey, true);
        HostPrintUTF8("profile active: " + profName[hit] + " -> " + ResolveUrl() + "\n");
        if (showUi) {
            HostMessageBox("已切换到 API：" + profName[hit] + "\n\n地址: " + ResolveUrl()
                           + "\n模型: " + cfgModel,
                           "DeepSeek Translate", 2, 0);
        }
        return;
    }

    // ---- 没匹配到名字：当作一次性配置串（兼容 v0.5） ----
    ParseAccountSpec(s);
    activeProfile = -1;
    api_key = pickKey(passKey);
    if (BAKED_ACCOUNT.empty() && BAKED_KEY.empty()) {
        HostSaveString("account_spec", s);
    }
}

// ============================================================ Login / logout
string ServerLogin(string User, string Pass) {
    LogDebug("ServerLogin() called: User='" + User + "', Pass length=" + formatInt(int(Pass.length())));
    EnsureInitialized();
    Pass = Pass.Trim();
    LoadProfiles();
    HandleAccountSpec(User, Pass, true);

    if (api_key.empty() && cfgAuth != "none") {
        LogDebug("ServerLogin: failed, API Key is empty");
        HostPrintUTF8("{$CP0=API Key not configured.$}\n");
        return "fail: API Key is empty";
    }

    HostSaveString("api_key", api_key);
    LogDebug("ServerLogin: 200 ok, endpoint=" + ResolveUrl() + ", model=" + cfgModel);
    HostPrintUTF8("{$CP0=Configured.$}\n");
    HostPrintUTF8("  endpoint: " + ResolveUrl() + "\n");
    HostPrintUTF8("  model   : " + cfgModel + "\n");
    HostPrintUTF8("  format  : " + cfgFormat + " / auth: " + cfgAuth + "\n");
    return "200 ok";
}

void ServerLogout() {
    LogDebug("ServerLogout() called");
    api_key = "";
    HostPrintUTF8("{$CP0=Logged out.$}\n");
}

// ============================================================ Language table
array<string> LangTable =
{
    "{$CP0=Auto Detect$}", "af", "sq", "am", "ar", "hy", "az", "eu", "be", "bn", "bs", "bg", "ca",
    "ceb", "ny", "zh-CN",
    "zh-TW", "co", "hr", "cs", "da", "nl", "en", "eo", "et", "tl", "fi", "fr",
    "fy", "gl", "ka", "de", "el", "gu", "ht", "ha", "haw", "he", "hi", "hmn", "hu", "is", "ig", "id", "ga", "it", "ja", "jw", "kn", "kk", "km",
    "ko", "ku", "ky", "lo", "la", "lv", "lt", "lb", "mk", "ms", "mg", "ml", "mt", "mi", "mr", "mn", "my", "ne", "no", "ps", "fa", "pl", "pt",
    "pa", "ro", "ru", "sm", "gd", "sr", "st", "sn", "sd", "si", "sk", "sl", "so", "es", "su", "sw", "sv", "tg", "ta", "te", "th", "tr", "uk",
    "ur", "uz", "vi", "cy", "xh", "yi", "yo", "zu"
};

array<string> GetSrcLangs() {
    array<string> ret = LangTable;
    return ret;
}

array<string> GetDstLangs() {
    array<string> ret = LangTable;
    return ret;
}

string LangName(const string &in code) {
    if (code == "zh-CN") return "Simplified Chinese (简体中文)";
    if (code == "zh-TW") return "Traditional Chinese (繁體中文)";
    if (code == "en") return "English";
    if (code == "ja") return "Japanese (日本語)";
    if (code == "ko") return "Korean (한국어)";
    if (code == "fr") return "French (Français)";
    if (code == "de") return "German (Deutsch)";
    if (code == "es") return "Spanish (Español)";
    if (code == "ru") return "Russian (Русский)";
    if (code == "pt") return "Brazilian Portuguese (Português do Brasil)";
    if (code == "it") return "Italian (Italiano)";
    if (code == "ar") return "Arabic (العربية)";
    if (code == "he") return "Hebrew (עברית)";
    if (code == "fa") return "Persian (فارسی)";
    if (code == "th") return "Thai (ไทย)";
    if (code == "vi") return "Vietnamese (Tiếng Việt)";
    if (code == "id") return "Indonesian (Bahasa Indonesia)";
    if (code == "tr") return "Turkish (Türkçe)";
    if (code == "pl") return "Polish (Polski)";
    if (code == "nl") return "Dutch (Nederlands)";
    if (code == "uk") return "Ukrainian (Українська)";
    if (code == "hi") return "Hindi (हिन्दी)";
    return code;
}

// ============================================================ String helpers
string JsonEscape(const string &in input) {
    string res = "";
    int i = 0;
    int n = int(input.length());
    while (i < n) {
        string c = input.substr(i, 1);
        if (c == "\\") res += "\\\\";
        else if (c == "\"") res += "\\\"";
        else if (c == "\n") res += "\\n";
        else if (c == "\r") res += "\\r";
        else if (c == "\t") res += "\\t";
        else if (c == "\b") res += "\\b";
        else if (c == "\f") res += "\\f";
        else res += c;
        i++;
    }
    return res;
}

string Finalize(const string &in raw, const string &in dst) {
    string t = raw;
    t.replace("\r\n", " ");
    t.replace("\n", " ");
    t.replace("\r", " ");
    t.replace("\t", " ");
    t = t.Trim();

    if (int(t.length()) > MAX_OUTPUT_CHARS) {
        t = t.Left(MAX_OUTPUT_CHARS);
        t = t.Trim();
    }

    if (dst == "ar" || dst == "he" || dst == "fa" || dst == "ur") {
        t = "\u2067" + t + "\u2069";
    }
    return t;
}

// ================================================================ LRU cache
array<string> gCacheKey;
array<string> gCacheVal;

string CacheGet(const string &in k) {
    int i = int(gCacheKey.length()) - 1;
    while (i >= 0) {
        if (gCacheKey[i] == k) return gCacheVal[i];
        i--;
    }
    return "";
}

void CachePut(const string &in k, const string &in v) {
    if (k.empty() || v.empty()) return;
    gCacheKey.insertLast(k);
    gCacheVal.insertLast(v);
    while (int(gCacheKey.length()) > CACHE_SIZE) {
        gCacheKey.removeAt(0);
        gCacheVal.removeAt(0);
    }
}

// ========================================================== Context history
array<string> pairSrc;
array<string> pairDst;

void RememberPair(const string &in src, const string &in dst) {
    pairSrc.insertLast(src);
    pairDst.insertLast(dst);
    while (int(pairSrc.length()) > MAX_CTX_SENTENCES) {
        pairSrc.removeAt(0);
        pairDst.removeAt(0);
    }
}

// 预算在「加入前」判定，因此是硬上限；但永远至少保留最新一条。
int StartPairIndex() {
    int n = int(pairSrc.length());
    if (n == 0) return 0;

    int used = 0;
    int taken = 0;
    int idx = n;
    while (idx > 0 && taken < MAX_CTX_SENTENCES) {
        int cand = idx - 1;
        int cost = int(pairSrc[cand].length()) + int(pairDst[cand].length());
        if (taken > 0 && used + cost > MAX_CTX_BYTES) break;
        idx = cand;
        used += cost;
        taken++;
    }
    return idx;
}

string BuildSystemPrompt(const string &in src, const string &in dst) {
    string sp = "You are a professional subtitle translator. "
              + "Translate ONLY the last user message into natural, fluent, colloquial subtitles. "
              + "Use the earlier turns as context to keep terminology, character names and tone consistent, "
              + "but never translate or repeat them. "
              + "Rules: output exactly one line with no line breaks; "
              + "do not add sentence-final punctuation such as . ! ? 。 ！ ？; "
              + "keep necessary internal punctuation (commas, enumeration marks, dashes) so the line stays readable; "
              + "do not merge or split sentences; do not add explanations, notes, quotes or the original text; "
              + "for ambiguous terms pick the reading that best fits the context; "
              + "keep the translation about as long as the source so it fits on screen. "
              + "Target language: " + LangName(dst) + ".";
    if (!src.empty()) sp += " Source language: " + LangName(src) + ".";
    return sp;
}

string BuildRequest(const string &in text, const string &in src, const string &in dst) {
    string sp = BuildSystemPrompt(src, dst);

    int start = StartPairIndex();
    int n = int(pairSrc.length());
    string req;
    int i;

    if (cfgFormat == "anthropic") {
        req = "{\"model\":\"" + JsonEscape(cfgModel) + "\",";
        req += "\"system\":\"" + JsonEscape(sp) + "\",";
        req += "\"messages\":[";
        i = start;
        bool first = true;
        while (i < n) {
            if (!first) req += ",";
            first = false;
            req += "{\"role\":\"user\",\"content\":\"" + JsonEscape(pairSrc[i]) + "\"}";
            req += ",{\"role\":\"assistant\",\"content\":\"" + JsonEscape(pairDst[i]) + "\"}";
            i++;
        }
        if (!first) req += ",";
        req += "{\"role\":\"user\",\"content\":\"" + JsonEscape(text) + "\"}]";
        req += ",\"max_tokens\":" + formatInt(MAX_TOKENS) + ",\"temperature\":0";
        if (!cfgBody.empty()) req += "," + cfgBody;
        req += "}";
        return req;
    }

    req = "{\"model\":\"" + JsonEscape(cfgModel) + "\",\"messages\":[";
    req += "{\"role\":\"system\",\"content\":\"" + JsonEscape(sp) + "\"}";
    i = start;
    while (i < n) {
        req += ",{\"role\":\"user\",\"content\":\"" + JsonEscape(pairSrc[i]) + "\"}";
        req += ",{\"role\":\"assistant\",\"content\":\"" + JsonEscape(pairDst[i]) + "\"}";
        i++;
    }
    req += ",{\"role\":\"user\",\"content\":\"" + JsonEscape(text) + "\"}]";
    req += ",\"max_tokens\":" + formatInt(MAX_TOKENS) + ",\"temperature\":0";
    if (!cfgBody.empty()) req += "," + cfgBody;
    req += "}";
    return req;
}

bool IsPermanentError(const string &in msg) {
    if (msg.find("Authentication") != -1) return true;
    if (msg.find("authentication") != -1) return true;
    if (msg.find("Unauthorized") != -1) return true;
    if (msg.find("Insufficient Balance") != -1) return true;
    if (msg.find("Invalid API key") != -1) return true;
    if (msg.find("Incorrect API key") != -1) return true;   // Inception/OpenAI 的措辞
    if (msg.find("invalid_api_key") != -1) return true;
    if (msg.find("insufficient_quota") != -1) return true;
    if (msg.find("Model Not Exist") != -1) return true;
    if (msg.find("model_not_found") != -1) return true;
    if (msg.find("Model must be one of") != -1) return true; // 结构化校验错误
    if (msg.find("Value error") != -1) return true;
    if (msg.find("No such model") != -1) return true;
    if (msg.find("invalid_request_error") != -1) return true;
    return false;
}

// ============================================================== Lazy Initialization
bool g_initialized = false;

void EnsureInitialized() {
    if (g_initialized) return;
    g_initialized = true;

    LogDebug("=== EnsureInitialized() START ===");
    LogDebug("Plugin: " + GetTitle());
    LogDebug("BAKED_NAME: '" + BAKED_NAME + "'");
    LogDebug("BAKED_ACCOUNT: '" + BAKED_ACCOUNT + "'");
    LogDebug("BAKED_KEY length: " + formatInt(int(BAKED_KEY.length())));

    LoadProfiles();
    LoadModelList();

    // 1. 优先级最高：API 管理器烘焙进本文件的独立条目配置
    if (!BAKED_ACCOUNT.empty() || !BAKED_KEY.empty()) {
        acctSpec = BAKED_ACCOUNT;
        if (!BAKED_KEY.empty()) api_key = BAKED_KEY;
        LogDebug("Config source: BAKED (generated entry)");
        HostPrintUTF8("{$CP0=config source: baked-in (generated entry)$}\n");
    } else if (LoadConfigFile()) {
        // 2. 其次：文本配置通道 deepseek_api.txt
        string fa = fileAcct.Trim();
        string fl = fa.MakeLower();
        if (fl == "models" || fl == "list" || fl == "help" || fa.empty()) {
            LogDebug("Config file: '" + fa + "' is a UI command, fallback to saved settings");
            HostPrintUTF8("config file: '" + fa + "' is a UI command, using saved settings\n");
            api_key  = HostLoadString("api_key", "");
            acctSpec = HostLoadString("account_spec", "");
        } else {
            acctSpec = fa;
            if (!fileKey.empty()) api_key = fileKey;
            LogDebug("Config source: file " + cfgUsedPath);
            HostPrintUTF8("{$CP0=config source: $}" + cfgUsedPath + "\n");
        }
    } else {
        // 3. 最后：界面上保存的账户设置
        api_key  = HostLoadString("api_key", "");
        acctSpec = HostLoadString("account_spec", "");
        LogDebug("Config source: saved UI settings");
    }

    LogDebug("Before HandleAccountSpec: acctSpec='" + acctSpec + "', api_key length=" + formatInt(int(api_key.length())));
    // 初始化阶段不弹窗，避免每次启动都跳消息框
    HandleAccountSpec(acctSpec, api_key, false);

    // 终极保底：如果 BAKED_KEY / fileKey 存在但 api_key 依然为空，恢复它
    if (api_key.empty()) {
        if (!BAKED_KEY.empty()) {
            api_key = BAKED_KEY;
            LogDebug("Fallback: restored api_key from BAKED_KEY");
        } else if (!fileKey.empty()) {
            api_key = fileKey;
            LogDebug("Fallback: restored api_key from fileKey");
        }
    }

    LogDebug("After HandleAccountSpec: endpoint=" + ResolveUrl());
    LogDebug("model=" + cfgModel + ", format=" + cfgFormat + ", auth=" + cfgAuth);
    LogDebug("Final api_key length=" + formatInt(int(api_key.length())));
    LogDebug("profiles=" + formatInt(int(profName.length())) + ", active=" + formatInt(activeProfile));
    LogDebug("=== EnsureInitialized() END ===");
}

// ============================================================== Translate
string Translate(string Text, string &in SrcLang, string &in DstLang) {
    LogDebug("=== Translate() START ===");
    LogDebug("Text: '" + Text.Left(60) + "'");
    LogDebug("SrcLang: '" + SrcLang + "', DstLang: '" + DstLang + "'");

    EnsureInitialized();

    if (api_key.empty() && cfgAuth != "none") {
        LogDebug("[ERROR] api_key is EMPTY (cfgAuth=" + cfgAuth + ")!");
        HostPrintUTF8("{$CP0=API Key not configured. Please enter it in the settings menu.$}\n");
        SrcLang = "UTF8";
        DstLang = "UTF8";
        return "[未配置 API Key: 请在API管理器中输入Key并重新生成]";
    }

    if (DstLang.empty() || DstLang == "{$CP0=Auto Detect$}") {
        LogDebug("[ERROR] Target language not specified: '" + DstLang + "'");
        HostPrintUTF8("{$CP0=Target language not specified. Please select a target language.$}\n");
        SrcLang = "UTF8";
        DstLang = "UTF8";
        return "[未选择目标语言: 请在PotPlayer右下角选择目标语言]";
    }

    if (SrcLang.empty() || SrcLang == "{$CP0=Auto Detect$}") SrcLang = "";

    string ck = DstLang + "||" + SrcLang + "||" + Text;
    string cached = CacheGet(ck);
    if (!cached.empty()) {
        LogDebug("Cache hit, returning cached translation");
        SrcLang = "UTF8";
        DstLang = "UTF8";
        return cached;
    }

    LogDebug("Preparing request...");
    string url = ResolveUrl();
    LogDebug("Target URL: " + url);
    string body = BuildRequest(Text, SrcLang, DstLang);
    LogDebug("Request body generated (" + formatInt(int(body.length())) + " bytes)");
    string headers = BuildHeaders();
    LogDebug("Headers generated");

    int retryCount = 0;
    int delay = baseRetryDelay;

    while (retryCount < maxRetries) {
        LogDebug("HTTP request attempt #" + formatInt(retryCount + 1) + " / " + formatInt(maxRetries) + "...");
        HostIncTimeOut(30000);
        string response = HostUrlGetString(url, CurrentUA(), headers, body);

        if (response.empty()) {
            LogDebug("[WARN] HTTP request returned empty (network timeout or connection failed)");
            HostPrintUTF8("{$CP0=Translation request failed. Retrying...$}\n");
            retryCount++;
            if (retryCount < maxRetries) {
                BackoffSleep(delay);
                delay = delay * 2;
            }
            continue;
        }

        LogDebug("HTTP response length: " + formatInt(int(response.length())) + " bytes");
        LogDebug("HTTP response preview: " + response.Left(300));

        JsonReader Reader;
        JsonValue Root;
        if (!Reader.parse(response, Root)) {
            LogDebug("[WARN] JSON parse failed, response: " + response.Left(200));
            HostPrintUTF8("{$CP0=Failed to parse API response. Retrying...$}\n");
            retryCount++;
            if (retryCount < maxRetries) {
                BackoffSleep(delay);
                delay = delay * 2;
            }
            continue;
        }

        bool gotIt = false;
        string extracted = "";
        if (cfgFormat == "anthropic") {
            JsonValue c = Root["content"];
            if (c.isArray() && c.size() > 0 && c[0]["text"].isString()) {
                extracted = c[0]["text"].asString();
                if (!extracted.empty()) gotIt = true;
            }
        } else {
            JsonValue choices = Root["choices"];
            if (choices.isArray() && choices.size() > 0) {
                JsonValue msg = choices[0]["message"];
                if (msg["content"].isString()) {
                    extracted = msg["content"].asString();
                    if (!extracted.empty()) gotIt = true;
                }
                if (!gotIt && msg["reasoning_content"].isString()) {
                    string rc = msg["reasoning_content"].asString();
                    if (!rc.empty()) {
                        LogDebug("Fallback: using reasoning_content as response");
                        extracted = rc;
                        gotIt = true;
                    }
                }
                if (!gotIt && choices[0]["text"].isString()) {
                    extracted = choices[0]["text"].asString();
                    if (!extracted.empty()) gotIt = true;
                }
            }
        }

        if (gotIt) {
            string translated = Finalize(extracted, DstLang);
            if (!translated.empty()) {
                RememberPair(Text, translated);
                CachePut(ck, translated);
                LogDebug("[SUCCESS] Translated: " + translated);
                SrcLang = "UTF8";
                DstLang = "UTF8";
                return translated;
            }
        }

        if (Root["error"]["message"].isString()) {
            string errorMessage = Root["error"]["message"].asString();
            LogDebug("[ERROR] API returned error message: " + errorMessage);
            HostPrintUTF8("{$CP0=API Error: $}" + errorMessage + "\n");
            if (IsPermanentError(errorMessage)) {
                LogDebug("[ERROR] Permanent error, aborting this line.");
                HostPrintUTF8("{$CP0=Permanent error, giving up on this line.$}\n");
                SrcLang = "UTF8";
                DstLang = "UTF8";
                return "[翻译服务报错: " + errorMessage + "]";
            }
        } else if (Root["error"].isObject()) {
            LogDebug("[ERROR] API returned structured error: " + response.Left(400));
            HostPrintUTF8("{$CP0=API returned a structured error (not a plain message).$}\n");
            HostPrintUTF8("  " + response.Left(400) + "\n");
            SrcLang = "UTF8";
            DstLang = "UTF8";
            return "[翻译服务报错: " + response.Left(100) + "]";
        } else if (!gotIt) {
            JsonValue rt = Root["usage"]["completion_tokens_details"]["reasoning_tokens"];
            if (rt.isInt() || rt.isUInt()) {
                LogDebug("[ERROR] Empty content: reasoning tokens exhausted max_tokens");
                HostPrintUTF8("{$CP0=Empty content: the model spent the whole token budget on reasoning.$}\n");
                HostPrintUTF8("  hint: add  body=\"reasoning_effort\":\"none\"  or raise  maxtok=1024\n");
                SrcLang = "UTF8";
                DstLang = "UTF8";
                return "[推理占满额度: 请在配置中添加 body=\"reasoning_effort\":\"none\"]";
            }
            LogDebug("[WARN] Empty translation content, retrying...");
            HostPrintUTF8("{$CP0=Translation failed. Retrying...$}\n");
        } else {
            LogDebug("[WARN] Translation finalized to empty, retrying...");
            HostPrintUTF8("{$CP0=Translation failed. Retrying...$}\n");
        }

        retryCount++;
        if (retryCount < maxRetries) {
            BackoffSleep(delay);
            delay = delay * 2;
        }
    }

    LogDebug("[ERROR] Translation failed after maximum retries.");
    HostPrintUTF8("{$CP0=Translation failed after maximum retries.$}\n");
    SrcLang = "UTF8";
    DstLang = "UTF8";
    return "[翻译失败: 超过最大重试次数]";
}

// ====================================================== Plugin initialization
void OnInitialize() {
    LogDebug("OnInitialize() called");
    HostPrintUTF8("{$CP0=DeepSeek translation plugin loaded.$} (v1.1)\n");
    EnsureInitialized();
}

void OnFinalize() {
    LogDebug("OnFinalize() called");
    HostPrintUTF8("{$CP0=DeepSeek translation plugin unloaded.$}\n");
}
