/*
    Real-time subtitle translation for PotPlayer using DeepSeek API
    ------------------------------------------------------------------
    v0.4 optimized build
      P0-1  context limited to last 3 lines / 600 bytes (was ~3000 tokens)
      P0-2  system + alternating user/assistant turns (no context bleed),
            max_tokens 256 (was 1000)
      P0-3  history written only after a successful translation
      P1-1  exponential backoff + permanent errors short-circuit
      P1-2  warm-up request in OnInitialize (kills the garbled first lines)
      P1-3  LRU cache + repeat-line short-circuit
      P1-4  UTF-8 aware token estimate
      P2-1  custom endpoint via the User field; neutral User-Agent
      P2-2  paired RTL isolation marks (U+2067 / U+2069)
      P2-3  punctuation rule clarified
      P3-1  language codes mapped to human readable names
      P3-2  output sanitised/capped, JsonEscape hardened

    v0.4.1 — 依 Extension\api.txt（PotPlayer 官方 AngelScript 接口文档）校正：
      * string 类没有 substr()，只有 Left/Right/Trim*，改用 Left()
      * 长等待切片 + HostIncTimeOut：api.txt 明示长等待会被判超时
      * DEBUG_LOG 打开时调用 HostOpenConsole()，日志可直接在控制台看

    v0.5 — 完整的自定义 API 支持：
      * 「账户名称」字段支持 preset= / url= / model= / format= / auth= / extra= / ua=
        分号分隔的配置串，也兼容直接填一个裸 URL
      * 模型名不再写死，可任意指定（原版固定 deepseek-chat）
      * 支持 OpenAI 兼容格式与 Anthropic 格式两套请求/响应结构
      * 支持 Bearer / x-api-key / api-key / 裸 Authorization / 无认证 五种认证
      * 内置 15 个常用服务商预设，填 preset=xxx 即可
*/

// ============================================================ Plugin info
string GetTitle() {
    return "{$CP0=DeepSeek Translate$}";
}

string GetVersion() {
    return "0.5";
}

string GetDesc() {
    return "{$CP0=Real-time subtitle translation using DeepSeek / custom API$}";
}

string GetLoginTitle() {
    return "{$CP0=API Key Configuration$}";
}

string GetLoginDesc() {
    return "{$CP936=账户名称：留空=DeepSeek官方；也可填 preset=siliconflow;model=Qwen/Qwen2.5-7B-Instruct 这类配置，或直接填 https://你的地址/v1 。密码：API Key。$}";
}

string GetPasswordText() {
    return "{$CP0=API Key:$}";
}

// ============================================================== Config
string api_key = "";
string USER_AGENT = "PotPlayer-DeepSeek-Translate/0.5";

// 当前生效的自定义 API 配置
string cfgBase   = "";                      // 服务地址（可与格式无关地写）
string cfgModel  = "deepseek-chat";         // 模型名
string cfgFormat = "openai";                // openai | anthropic
string cfgAuth   = "bearer";                // bearer | raw | x-api-key | api-key | none
string cfgExtra  = "";                      // 额外请求头，格式 Name:Value|Name:Value
string cfgUA     = "";                      // 自定义 User-Agent（空则用 USER_AGENT）

string acctSpec  = "";                      // 「账户名称」原始内容，持久化用

int maxRetries = 3;          // total attempts per subtitle
int baseRetryDelay = 1000;   // ms, doubled on every retry

bool DEBUG_LOG = false;      // set to true to trace requests

// Context / output budgets
int MAX_CTX_SENTENCES = 3;   // how many previous lines to send as context
int MAX_CTX_BYTES     = 600; // hard byte ceiling for those lines (~200 CJK chars)
int MAX_OUTPUT_CHARS  = 200; // clamp on a runaway translation
int CACHE_SIZE        = 32;  // LRU entries

void Dbg(const string &in m) {
    if (DEBUG_LOG) HostPrintUTF8("[DeepSeek] " + m + "\n");
}

// api.txt 第 33-44 行：等待时间过长会被宿主判定脚本超时，
// 必须把长等待切成小片，并在循环中调用 HostIncTimeOut 延长超时预算。
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

// ====================================================== 自定义 API 预设
// 只列出确定用不到的字段；配置串里的显式赋值永远覆盖预设。
void ApplyPreset(const string &in name) {
    // 默认 = DeepSeek 官方
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
        // 需要自己补 url= 和 api-version 查询串
        cfgBase = "https://YOUR-RESOURCE.openai.azure.com/openai/deployments/YOUR-DEPLOYMENT";
        cfgModel = "gpt-4o-mini";
        cfgAuth = "api-key";
    }
}

// 解析「账户名称」字段：
//   preset=siliconflow; model=Qwen/Qwen2.5-7B-Instruct; auth=bearer; extra=X-Foo:bar|X-Baz:qux
//   也可以直接写一个裸 URL：https://my-gateway.com/v1
void ParseAccountSpec(const string &in spec) {
    ApplyPreset("");                 // 先回到默认
    acctSpec = spec.Trim();
    if (acctSpec.empty()) return;

    array<string> parts = acctSpec.split(";");
    int n = int(parts.length());
    int i = 0;
    while (i < n) {
        string tok = parts[i].Trim();
        i++;
        if (tok.empty()) continue;

        int eq = tok.find("=");
        if (eq == -1) {
            cfgBase = tok;            // 裸 URL 简写
            continue;
        }

        string k = tok.Left(eq).Trim().MakeLower();
        string v = tok.Right(int(tok.length()) - eq - 1).Trim();

        if (k == "preset") {
            ApplyPreset(v.MakeLower());
        } else if (k == "url" || k == "base" || k == "endpoint" || k == "host") {
            cfgBase = v;
        } else if (k == "model") {
            cfgModel = v;
        } else if (k == "format") {
            cfgFormat = v.MakeLower();
        } else if (k == "auth") {
            cfgAuth = v.MakeLower();
        } else if (k == "extra" || k == "header") {
            cfgExtra = v;
        } else if (k == "ua" || k == "useragent") {
            cfgUA = v;
        } else {
            Dbg("未知配置项，已忽略: " + k);
        }
    }
}

// 把 cfgBase 补成最终请求地址。
// 注意 zhipu 是 /api/paas/v4、gemini 是 /v1beta/openai，
// 所以只在完全没有版本段时才补 /v1。
string ResolveUrl() {
    string u = cfgBase.Trim();
    if (u.empty()) u = "https://api.deepseek.com/v1";

    if (u.find("http") == -1) u = "https://" + u;

    // 去掉末尾斜杠
    while (int(u.length()) > 0 && u.Right(1) == "/") {
        u = u.Left(int(u.length()) - 1);
    }

    // 已经是完整路径就直接用
    if (u.find("/chat/completions") != -1) return u;
    if (u.find("/messages") != -1) return u;

    bool hasVersion = false;
    if (u.find("/v1") != -1) hasVersion = true;
    if (u.find("/v2") != -1) hasVersion = true;
    if (u.find("/v3") != -1) hasVersion = true;
    if (u.find("/v4") != -1) hasVersion = true;
    if (!hasVersion) u += "/v1";

    if (cfgFormat == "anthropic") {
        u += "/messages";
    } else {
        u += "/chat/completions";
    }
    return u;
}

string CurrentUA() {
    if (cfgUA.empty()) return USER_AGENT;
    return cfgUA;
}

string BuildHeaders() {
    string h = "Content-Type: application/json";

    if (cfgAuth == "none") {
        // 本地 ollama / lmstudio 之类不需要认证
    } else if (cfgAuth == "x-api-key") {
        h = "x-api-key: " + api_key + "\n" + h;
    } else if (cfgAuth == "api-key") {
        h = "api-key: " + api_key + "\n" + h;
    } else if (cfgAuth == "raw") {
        h = "Authorization: " + api_key + "\n" + h;
    } else {
        h = "Authorization: Bearer " + api_key + "\n" + h;
    }

    if (cfgFormat == "anthropic") {
        h += "\nanthropic-version: 2023-06-01";
    }

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

// P3-1: give the model a real language name instead of a bare locale code.
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
    return code; // unmapped codes pass through unchanged
}

// ============================================================ Login / config
string ServerLogin(string User, string Pass) {
    Pass = Pass.Trim();

    // 「账户名称」现在是完整的自定义 API 配置入口
    ParseAccountSpec(User);

    bool needKey = (cfgAuth != "none");
    if (needKey && Pass.empty()) {
        HostPrintUTF8("{$CP0=API Key not configured. Please enter a valid API Key.$}\n");
        return "fail: API Key is empty";
    }

    api_key = Pass;
    HostSaveString("api_key", api_key);
    HostSaveString("account_spec", acctSpec);

    HostPrintUTF8("{$CP0=Configured.$}\n");
    HostPrintUTF8("  endpoint: " + ResolveUrl() + "\n");
    HostPrintUTF8("  model   : " + cfgModel + "\n");
    HostPrintUTF8("  format  : " + cfgFormat + " / auth: " + cfgAuth + "\n");
    return "200 ok";
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

// P3-2 / api.txt: string 类只有 Left/Right，没有 substr。
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
        t = "\u2067" + t + "\u2069"; // RLI ... PDI, never leaks past the line
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

// 从最新一条往回累计；预算在「加入前」判定，因此是硬上限，
// 但永远至少保留最新一条。
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

// v0.5：按 cfgFormat 生成 OpenAI 兼容或 Anthropic 格式的请求体。
string BuildRequest(const string &in text, const string &in src, const string &in dst) {
    string sp = BuildSystemPrompt(src, dst);

    int start = StartPairIndex();
    int n = int(pairSrc.length());
    string req;
    int i;

    if (cfgFormat == "anthropic") {
        // Anthropic: system 是顶层字段，messages 只能是 user/assistant 交替
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

    // OpenAI 兼容：system + 交替 user/assistant
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

// 同时兼容 OpenAI 的 choices[0].message.content 与 Anthropic 的 content[0].text。
// 刻意内联而不是抽成函数：api.txt 只保证 JsonValue &out 形式的引用传参，
// 这里直接读局部变量最稳妥。

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

    if (SrcLang.empty() || SrcLang == "{$CP0=Auto Detect$}") {
        SrcLang = "";
    }

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
        Dbg("attempt " + retryCount + ", model=" + cfgModel + ", body=" + body.length() + " bytes");

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
    // 调试模式下打开宿主调试控制台（api.txt: Open debug console），
    // 这样 HostPrintUTF8 的输出能实时看到，不用去翻日志文件。
    if (DEBUG_LOG) HostOpenConsole();

    HostPrintUTF8("{$CP0=DeepSeek translation plugin loaded.$} (v0.5)\n");

    api_key  = HostLoadString("api_key", "");
    acctSpec = HostLoadString("account_spec", "");
    ParseAccountSpec(acctSpec);

    Dbg("endpoint = " + ResolveUrl());
    Dbg("model = " + cfgModel + " / format = " + cfgFormat + " / auth = " + cfgAuth);

    if (api_key.empty() && cfgAuth != "none") {
        HostPrintUTF8("{$CP0=No saved API Key found. Please configure it in the settings menu.$}\n");
        return;
    }

    // P1-2: 预热请求，提前完成 DNS/TCP/TLS 握手并验证 Key
    string url = ResolveUrl();
    string warmBody = BuildRequest("ping", "", "zh-CN");
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

// ======================================================= Plugin finalization
void OnFinalize() {
    HostPrintUTF8("{$CP0=DeepSeek translation plugin unloaded.$}\n");
}
