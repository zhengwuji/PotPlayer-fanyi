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
*/

// ============================================================ Plugin info
string GetTitle() {
    return "{$CP0=DeepSeek Translate$}";
}

string GetVersion() {
    return "0.6";
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
    return "{$CP936=切换: use=名字 (或直接写名字) ｜ 新增: add=名字; preset=siliconflow; model=xxx ｜ 删除: del=名字 ｜ 查看: list ｜ 留空 = DeepSeek 官方。密码栏填 API Key。$}";
}

string GetPasswordText() {
    return "{$CP0=API Key:$}";
}

// ============================================================== Config
string api_key = "";
string USER_AGENT = "PotPlayer-DeepSeek-Translate/0.6";

// 当前生效的自定义 API 配置
string cfgBase   = "";
string cfgModel  = "deepseek-chat";
string cfgFormat = "openai";
string cfgAuth   = "bearer";
string cfgExtra  = "";
string cfgUA     = "";
string specKey   = "";      // 配置串里 key= 的值（可选）

string acctSpec  = "";      // 「账户名称」原始内容
int    activeProfile = -1;  // 当前使用的已保存 API 序号，-1 = 一次性配置

int maxRetries = 3;
int baseRetryDelay = 1000;

bool DEBUG_LOG = false;

int MAX_CTX_SENTENCES = 3;
int MAX_CTX_BYTES     = 600;
int MAX_OUTPUT_CHARS  = 200;
int CACHE_SIZE        = 32;

void Dbg(const string &in m) {
    if (DEBUG_LOG) HostPrintUTF8("[DeepSeek] " + m + "\n");
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
string snapB, snapM, snapF, snapA, snapE, snapU;

void SnapshotCfg() {
    snapB = cfgBase; snapM = cfgModel; snapF = cfgFormat;
    snapA = cfgAuth; snapE = cfgExtra; snapU = cfgUA;
}

void RestoreCfg() {
    cfgBase = snapB; cfgModel = snapM; cfgFormat = snapF;
    cfgAuth = snapA; cfgExtra = snapE; cfgUA = snapU;
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

    if (name.empty() || name == "deepseek") return;

    if (name == "openai") {
        cfgBase = "https://api.openai.com/v1";
        cfgModel = "gpt-4o-mini";
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
    ApplyPreset("");
    specKey = "";

    string s = spec.Trim();
    if (s.empty()) return "";

    string norm = "";
    array<string> parts = s.split(";");
    int i = 0;
    int n = int(parts.length());
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
            ApplyPreset(v.MakeLower());
        } else if (kl == "url" || kl == "base" || kl == "endpoint" || kl == "host") {
            cfgBase = v;
        } else if (kl == "model") {
            cfgModel = v;
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

// ============================================ 处理「账户名称」字段的全部写法
string pickKey(const string &in passKey) {
    if (!specKey.empty()) return specKey;
    return passKey.Trim();
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
        ParseAccountSpec("");
        activeProfile = -1;
        api_key = pickKey(passKey);
        HostSaveString("account_spec", "");
        return;
    }

    string low = s.MakeLower();

    if (low == "list" || low == "?" || low == "help") {
        if (showUi) ShowProfiles();
        return;                     // 保持当前配置不变
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
    HostSaveString("account_spec", s);
}

// ============================================================ Login / logout
string ServerLogin(string User, string Pass) {
    Pass = Pass.Trim();
    LoadProfiles();
    HandleAccountSpec(User, Pass, true);

    if (api_key.empty() && cfgAuth != "none") {
        HostPrintUTF8("{$CP0=API Key not configured.$}\n");
        return "fail: API Key is empty";
    }

    HostSaveString("api_key", api_key);
    HostPrintUTF8("{$CP0=Configured.$}\n");
    HostPrintUTF8("  endpoint: " + ResolveUrl() + "\n");
    HostPrintUTF8("  model   : " + cfgModel + "\n");
    HostPrintUTF8("  format  : " + cfgFormat + " / auth: " + cfgAuth + "\n");
    return "200 ok";
}

void ServerLogout() {
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
    string output = input;
    output.replace("\\", "\\\\");
    output.replace("\"", "\\\"");
    output.replace("\n", "\\n");
    output.replace("\r", "\\r");
    output.replace("\t", "\\t");
    output.replace("\b", "\\b");
    output.replace("\f", "\\f");
    return output;
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
              + "do not add sentence-final punctuation such as . ! ? \u3002 \uFF01 \uFF1F; "
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
        req += "\"max_tokens\":256,\"temperature\":0,\"messages\":[";
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
        req += "{\"role\":\"user\",\"content\":\"" + JsonEscape(text) + "\"}]}";
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
    req += ",\"max_tokens\":256,\"temperature\":0}";
    return req;
}

bool IsPermanentError(const string &in msg) {
    if (msg.find("Authentication") != -1) return true;
    if (msg.find("authentication") != -1) return true;
    if (msg.find("Insufficient Balance") != -1) return true;
    if (msg.find("Invalid API key") != -1) return true;
    if (msg.find("invalid_api_key") != -1) return true;
    if (msg.find("insufficient_quota") != -1) return true;
    if (msg.find("Model Not Exist") != -1) return true;
    if (msg.find("model_not_found") != -1) return true;
    if (msg.find("invalid_request_error") != -1) return true;
    return false;
}

// ============================================================== Translate
string Translate(string Text, string &in SrcLang, string &in DstLang) {
    if (api_key.empty() && cfgAuth != "none") {
        HostPrintUTF8("{$CP0=API Key not configured. Please enter it in the settings menu.$}\n");
        return "[未配置 API Key]";
    }

    if (DstLang.empty() || DstLang == "{$CP0=Auto Detect$}") {
        HostPrintUTF8("{$CP0=Target language not specified. Please select a target language.$}\n");
        return "[未选择目标语言]";
    }

    if (SrcLang.empty() || SrcLang == "{$CP0=Auto Detect$}") SrcLang = "";

    string ck = DstLang + "||" + SrcLang + "||" + Text;
    string cached = CacheGet(ck);
    if (!cached.empty()) {
        Dbg("cache hit");
        SrcLang = "UTF8";
        DstLang = "UTF8";
        return cached;
    }

    string url = ResolveUrl();
    string body = BuildRequest(Text, SrcLang, DstLang);
    string headers = BuildHeaders();

    int retryCount = 0;
    int delay = baseRetryDelay;

    while (retryCount < maxRetries) {
        // 注意：int 必须走 formatInt，AngelScript 不会把数字隐式拼进字符串
        Dbg("attempt " + formatInt(retryCount) + ", model=" + cfgModel
            + ", body=" + formatInt(int(body.length())) + " bytes");

        HostIncTimeOut(20000);
        string response = HostUrlGetString(url, CurrentUA(), headers, body);

        if (response.empty()) {
            HostPrintUTF8("{$CP0=Translation request failed. Retrying...$}\n");
            retryCount++;
            if (retryCount < maxRetries) {
                BackoffSleep(delay);
                delay = delay * 2;
            }
            continue;
        }

        JsonReader Reader;
        JsonValue Root;
        if (!Reader.parse(response, Root)) {
            HostPrintUTF8("{$CP0=Failed to parse API response. Retrying...$}\n");
            Dbg("raw: " + response.Left(300));
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
            if (c.isArray() && c[0]["text"].isString()) {
                extracted = c[0]["text"].asString();
                gotIt = true;
            }
        } else {
            JsonValue choices = Root["choices"];
            if (choices.isArray() && choices[0]["message"]["content"].isString()) {
                extracted = choices[0]["message"]["content"].asString();
                gotIt = true;
            }
        }

        if (gotIt) {
            string translated = Finalize(extracted, DstLang);
            if (!translated.empty()) {
                RememberPair(Text, translated);
                CachePut(ck, translated);
                Dbg("ok: " + translated);
                SrcLang = "UTF8";
                DstLang = "UTF8";
                return translated;
            }
        }

        if (Root["error"]["message"].isString()) {
            string errorMessage = Root["error"]["message"].asString();
            HostPrintUTF8("{$CP0=API Error: $}" + errorMessage + "\n");
            if (IsPermanentError(errorMessage)) {
                HostPrintUTF8("{$CP0=Permanent error, giving up on this line.$}\n");
                return "[翻译服务报错]";
            }
        } else {
            HostPrintUTF8("{$CP0=Translation failed. Retrying...$}\n");
            Dbg("raw: " + response.Left(300));
        }

        retryCount++;
        if (retryCount < maxRetries) {
            BackoffSleep(delay);
            delay = delay * 2;
        }
    }

    HostPrintUTF8("{$CP0=Translation failed after maximum retries.$}\n");
    return "[翻译失败]";
}

// ====================================================== Plugin initialization
void OnInitialize() {
    if (DEBUG_LOG) HostOpenConsole();

    HostPrintUTF8("{$CP0=DeepSeek translation plugin loaded.$} (v0.6)\n");

    LoadProfiles();

    api_key  = HostLoadString("api_key", "");
    acctSpec = HostLoadString("account_spec", "");

    // 初始化阶段不弹窗，避免每次启动都跳消息框
    HandleAccountSpec(acctSpec, api_key, false);

    Dbg("endpoint = " + ResolveUrl());
    Dbg("model = " + cfgModel + " / format = " + cfgFormat + " / auth = " + cfgAuth);
    Dbg("profiles = " + formatInt(int(profName.length()))
        + ", active = " + formatInt(activeProfile));

    if (api_key.empty() && cfgAuth != "none") {
        HostPrintUTF8("{$CP0=No saved API Key found. Please configure it in the settings menu.$}\n");
        return;
    }

    string url = ResolveUrl();
    string warmBody = BuildRequest("ping", "", "zh-CN");
    HostIncTimeOut(20000);
    string warmResponse = HostUrlGetString(url, CurrentUA(), BuildHeaders(), warmBody);

    if (warmResponse.empty()) {
        HostPrintUTF8("{$CP0=Warm-up failed: no response. Check network or endpoint.$}\n");
        return;
    }

    JsonReader WarmReader;
    JsonValue WarmRoot;
    if (!WarmReader.parse(warmResponse, WarmRoot)) {
        HostPrintUTF8("{$CP0=Warm-up response could not be parsed.$}\n");
        return;
    }

    bool warmOk = false;
    string warmText = "";
    if (cfgFormat == "anthropic") {
        JsonValue wc = WarmRoot["content"];
        if (wc.isArray() && wc[0]["text"].isString()) {
            warmText = wc[0]["text"].asString();
            warmOk = true;
        }
    } else {
        JsonValue wch = WarmRoot["choices"];
        if (wch.isArray() && wch[0]["message"]["content"].isString()) {
            warmText = wch[0]["message"]["content"].asString();
            warmOk = true;
        }
    }

    if (warmOk) {
        HostPrintUTF8("{$CP0=Saved API Key loaded.$} [" + cfgModel + "]\n");
        Dbg("warm-up reply: " + warmText);
    } else if (WarmRoot["error"]["message"].isString()) {
        HostPrintUTF8("{$CP0=Warm-up API error: $}" + WarmRoot["error"]["message"].asString() + "\n");
    } else {
        HostPrintUTF8("{$CP0=Warm-up returned an unexpected payload.$}\n");
    }
}

void OnFinalize() {
    HostPrintUTF8("{$CP0=DeepSeek translation plugin unloaded.$}\n");
}
