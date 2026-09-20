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
*/

// ============================================================ Plugin info
string GetTitle() {
    return "{$CP0=DeepSeek Translate$}";
}

string GetVersion() {
    return "0.4";
}

string GetDesc() {
    return "{$CP0=Real-time subtitle translation using DeepSeek$}";
}

string GetLoginTitle() {
    return "{$CP0=API Key Configuration$}";
}

string GetLoginDesc() {
    return "{$CP936=账户名称：自定义接口地址（留空使用 DeepSeek 官方；也可填 https://你的中转/v1）。密码：API Key。$}";
}

string GetPasswordText() {
    return "{$CP0=API Key:$}";
}

// ============================================================== Config
string api_key = "";
string selected_model = "deepseek-chat";
string apiUrl = "https://api.deepseek.com/v1/chat/completions";
string DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions";
string UserAgent = "PotPlayer-DeepSeek-Translate/0.4";

int maxRetries = 3;          // total attempts per subtitle
int baseRetryDelay = 1000;   // ms, doubled on every retry

bool DEBUG_LOG = false;      // set to true to trace requests in the PotPlayer log

// Context / output budgets
int MAX_CTX_SENTENCES = 3;   // how many previous lines to send as context
int MAX_CTX_BYTES     = 600; // hard byte ceiling for those lines (~200 CJK chars)
int MAX_OUTPUT_CHARS  = 200; // clamp on a runaway translation
int CACHE_SIZE        = 32;  // LRU entries

void Dbg(const string &in m) {
    if (DEBUG_LOG) HostPrintUTF8("[DeepSeek] " + m + "\n");
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
    if (Pass.empty()) {
        HostPrintUTF8("{$CP0=API Key not configured. Please enter a valid API Key.$}\n");
        return "fail: API Key is empty";
    }

    api_key = Pass;
    HostSaveString("api_key", api_key);

    // P2-1: the User field is repurposed as an optional custom endpoint.
    User = User.Trim();
    if (!User.empty()) {
        string base = User;
        if (base.find("http") == -1) base = "https://" + base;
        if (base.find("/chat/completions") == -1) {
            if (base.find("/v1") == -1) base += "/v1";
            base += "/chat/completions";
        }
        apiUrl = base;
        HostSaveString("api_base", apiUrl);
    } else {
        apiUrl = DEFAULT_API_URL;
        HostSaveString("api_base", "");
    }

    HostPrintUTF8("{$CP0=API Key successfully configured.$}\n");
    HostPrintUTF8("Endpoint: " + apiUrl + "\n");
    return "200 ok";
}

// ============================================================ String helpers
// P3-2: JSON escaping, control characters included.
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

// P3-2: flatten to one line, clamp the length, apply RTL isolation (P2-2).
string Finalize(const string &in raw, const string &in dst) {
    string t = raw;
    t.replace("\r\n", " ");
    t.replace("\n", " ");
    t.replace("\r", " ");
    t.replace("\t", " ");
    t = t.Trim();

    if (int(t.length()) > MAX_OUTPUT_CHARS) {
        t = t.substr(0, uint(MAX_OUTPUT_CHARS));
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
// P0-3: only successful translations ever enter the history.
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

// P0-1: walk backwards from the newest line and stop at the budget,
// then return the index the forward pass should start from.
// The budget is tested BEFORE a line is admitted, so the ceiling is a real
// ceiling; the newest line is always kept even if it alone exceeds it.
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

// P0-2 / P2-3: system prompt plus alternating user/assistant turns.
string BuildRequest(const string &in text, const string &in src, const string &in dst) {
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

    string req = "{\"model\":\"" + selected_model + "\",\"messages\":[";
    req += "{\"role\":\"system\",\"content\":\"" + JsonEscape(sp) + "\"}";

    int start = StartPairIndex();
    int n = int(pairSrc.length());
    int i = start;
    while (i < n) {
        req += ",{\"role\":\"user\",\"content\":\"" + JsonEscape(pairSrc[i]) + "\"}";
        req += ",{\"role\":\"assistant\",\"content\":\"" + JsonEscape(pairDst[i]) + "\"}";
        i++;
    }

    req += ",{\"role\":\"user\",\"content\":\"" + JsonEscape(text) + "\"}]";
    req += ",\"max_tokens\":256,\"temperature\":0}";
    return req;
}

// P1-1: errors that will never succeed on a retry.
bool IsPermanentError(const string &in msg) {
    if (msg.find("Authentication") != -1) return true;
    if (msg.find("authentication") != -1) return true;
    if (msg.find("Insufficient Balance") != -1) return true;
    if (msg.find("Invalid API key") != -1) return true;
    if (msg.find("invalid_api_key") != -1) return true;
    if (msg.find("insufficient_quota") != -1) return true;
    if (msg.find("Model Not Exist") != -1) return true;
    if (msg.find("model_not_found") != -1) return true;
    return false;
}

// ============================================================== Translate
string Translate(string Text, string &in SrcLang, string &in DstLang) {
    if (api_key.empty()) {
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

    // P1-3: repeat line (pause / seek / replay) costs nothing.
    string ck = DstLang + "||" + SrcLang + "||" + Text;
    string cached = CacheGet(ck);
    if (!cached.empty()) {
        Dbg("cache hit");
        SrcLang = "UTF8";
        DstLang = "UTF8";
        return cached;
    }

    string body = BuildRequest(Text, SrcLang, DstLang);
    string headers = "Authorization: Bearer " + api_key + "\nContent-Type: application/json";

    int retryCount = 0;
    int delay = baseRetryDelay;

    while (retryCount < maxRetries) {
        Dbg("attempt " + retryCount + ", body bytes = " + body.length());

        string response = HostUrlGetString(apiUrl, UserAgent, headers, body);

        if (response.empty()) {
            HostPrintUTF8("{$CP0=Translation request failed. Retrying...$}\n");
            retryCount++;
            if (retryCount < maxRetries) {
                HostSleep(delay);
                delay = delay * 2;
            }
            continue;
        }

        JsonReader Reader;
        JsonValue Root;
        if (!Reader.parse(response, Root)) {
            HostPrintUTF8("{$CP0=Failed to parse API response. Retrying...$}\n");
            retryCount++;
            if (retryCount < maxRetries) {
                HostSleep(delay);
                delay = delay * 2;
            }
            continue;
        }

        JsonValue choices = Root["choices"];
        if (choices.isArray() && choices[0]["message"]["content"].isString()) {
            string translated = Finalize(choices[0]["message"]["content"].asString(), DstLang);
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
        }

        retryCount++;
        if (retryCount < maxRetries) {
            HostSleep(delay);
            delay = delay * 2;
        }
    }

    HostPrintUTF8("{$CP0=Translation failed after maximum retries.$}\n");
    return "[翻译失败]";
}

// ====================================================== Plugin initialization
void OnInitialize() {
    HostPrintUTF8("{$CP0=DeepSeek translation plugin loaded.$} (v0.4)\n");

    api_key = HostLoadString("api_key", "");
    apiUrl = HostLoadString("api_base", "");
    if (apiUrl.empty()) apiUrl = DEFAULT_API_URL;

    if (api_key.empty()) {
        HostPrintUTF8("{$CP0=No saved API Key found. Please configure it in the settings menu.$}\n");
        return;
    }

    Dbg("endpoint = " + apiUrl);

    // P1-2: validate the key and warm up DNS/TCP/TLS before the first subtitle
    // line arrives, which removes the garbled opening lines.
    string warm = "{\"model\":\"" + selected_model + "\","
                + "\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],"
                + "\"max_tokens\":1,\"temperature\":0}";
    string warmHeaders = "Authorization: Bearer " + api_key + "\nContent-Type: application/json";

    string warmResponse = HostUrlGetString(apiUrl, UserAgent, warmHeaders, warm);

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

    if (WarmRoot["choices"].isArray()) {
        HostPrintUTF8("{$CP0=Saved API Key loaded.$}\n");
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
