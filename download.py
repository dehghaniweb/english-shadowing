#!/usr/bin/env python3
"""
Worker: download a YouTube video + subtitles, burn / embed them, upload the result to Telegram.

Runs inside GitHub Actions (see .github/workflows/download.yml); the Telegram bot "brain"
(Google Apps Script) starts it with: ACTION, VIDEO_ID, LANGS, MODE, HEIGHT, CHAT_ID, MSG_ID, STORAGE_CHAT.

ACTION=inspect  -> list the video's subtitles (own + automatic) as buttons in Telegram
ACTION=download -> download, add the chosen subtitle, upload to Telegram
"""
import asyncio
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import subpage                      # the bilingual HTML subtitle page (stdlib only; it never imports this file)

CLOUD_API = "https://api.telegram.org"
CLOUD_LIMIT = 48_000_000            # official Bot API upload limit is 50 MB
LOCAL_LIMIT = 1_950_000_000         # local Bot API server: 2000 MB
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
RETRY_WAITS = (15, 45, 90)          # seconds between subtitle retries (YouTube answers 429 often)
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,16})*$")
SPEC_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,16})*(?::(?:auto|manual))?$")   # "en", "en:auto", "en:manual"


# =====================================================================  config
@dataclass
class Cfg:
    bot_token: str = ""
    storage_chat: str = ""
    cookies_file: str = ""
    local_api: str = ""
    fallback_used: bool = False      # True when the 2 GB server could not be used
    fallback_reason: str = ""        # why (one short line, no secrets)
    ai_provider: str = ""            # anthropic / openai / gemini (set by ai_setup)
    ai_key: str = ""
    ai_model: str = ""
    ai_models: list = field(default_factory=list)   # models to try, in order
    work_dir: Path = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "ytjob"

    @classmethod
    def from_env(cls) -> "Cfg":
        e = lambda k: (os.environ.get(k) or "").strip()      # noqa: E731
        cookies = e("YOUTUBE_COOKIES_FILE")
        return cls(bot_token=e("BOT_TOKEN"), storage_chat=e("STORAGE_CHAT") or e("CHAT_ID"),
                   cookies_file=cookies if cookies and Path(cookies).is_file() else "",
                   local_api=e("TG_LOCAL_API"))


# =====================================================================  telegram
def clean_reason(text: str, limit: int = 160) -> str:
    """One short line that is safe to show in the chat: no bot token, no 32-hex hash, no line breaks."""
    t = re.sub(r"\d{6,}:[A-Za-z0-9_-]{30,}", "<token>", str(text))
    t = re.sub(r"\b[0-9a-fA-F]{32}\b", "<hash>", t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


class TelegramError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Telegram:
    def __init__(self, token: str, base: str = CLOUD_API):
        self.token, self.base = token, base.rstrip("/")
        self.last_error = ""

    @property
    def url(self) -> str:
        return f"{self.base}/bot{self.token}"

    def call(self, method: str, retries: int = 4, **params):
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(f"{self.url}/{method}", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read())
                except Exception:                                   # noqa: BLE001
                    data = {"ok": False, "description": str(e), "error_code": e.code}
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = TelegramError(f"network: {e}")
                time.sleep(2 * (attempt + 1))
                continue
            if data.get("ok"):
                return data["result"]
            retry_after = (data.get("parameters") or {}).get("retry_after")
            last = TelegramError(data.get("description", "error"), data.get("error_code"))
            if retry_after and attempt < retries - 1:
                time.sleep(min(int(retry_after) + 1, 60))
                continue
            raise last
        raise last or TelegramError("unknown error")

    def send_message(self, chat_id, text):
        return self.call("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML",
                         link_preview_options={"is_disabled": True})

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        try:
            return self.call("editMessageText", retries=4, chat_id=chat_id, message_id=message_id,
                             text=text[:4000], parse_mode="HTML", reply_markup=reply_markup,
                             link_preview_options={"is_disabled": True})
        except TelegramError as e:
            if "not modified" in str(e).lower():
                return None
            raise

    def upload(self, method: str, field: str, path: str, fields: dict, thumb: str = None,
               timeout: int = 7200):
        """Multipart upload through curl, so a 2 GB file is streamed instead of loaded into RAM."""
        cmd = ["curl", "-sS", "--max-time", str(timeout), "-X", "POST", f"{self.url}/{method}",
               "-F", f"{field}=@{path}"]
        if thumb:
            cmd += ["-F", f"thumbnail=@{thumb}"]
        for k, v in fields.items():
            if v is not None:
                cmd += ["--form-string", f"{k}={v}"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise TelegramError(f"curl failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise TelegramError(f"bad response: {proc.stdout[:300]}")
        if not data.get("ok"):
            raise TelegramError(data.get("description", "upload failed"), data.get("error_code"))
        return data["result"]

    def reachable(self, tries: int = 1, pause: float = 4.0) -> bool:
        """getMe, patiently: a freshly started local server needs a few seconds to log in. self.last_error = why not."""
        for n in range(tries):
            try:
                self.call("getMe", retries=1)
                return True
            except Exception as ex:                                 # noqa: BLE001
                self.last_error = clean_reason(str(ex))
                if n < tries - 1:
                    time.sleep(pause)
        return False


# =====================================================================  subtitles
LANG_NAMES = {
    "fa": "فارسی", "en": "English", "ar": "العربية", "tr": "Türkçe", "de": "Deutsch", "nl": "Nederlands",
    "fr": "Français", "es": "Español", "ru": "Русский", "it": "Italiano", "pt": "Português",
    "ja": "日本語", "ko": "한국어", "zh": "中文", "hi": "हिन्दी", "ur": "اردو", "uk": "Українська", "pl": "Polski",
    "he": "עברית", "id": "Indonesia", "sv": "Svenska", "vi": "Tiếng Việt", "th": "ไทย",
}
ISO3 = {
    "fa": "fas", "en": "eng", "ar": "ara", "tr": "tur", "de": "deu", "nl": "nld", "fr": "fra",
    "es": "spa", "ru": "rus", "it": "ita", "pt": "por", "ja": "jpn", "ko": "kor", "zh": "zho",
    "hi": "hin", "ur": "urd", "uk": "ukr", "pl": "pol", "he": "heb", "id": "ind", "sv": "swe",
}
RTL = {"fa", "ar", "ur", "he", "ps", "ku", "sd", "ug"}
SKIP_KEYS = {"live_chat"}
SOURCE_LABELS = {"manual": "زیرنویس اصلی", "auto-orig": "خودکار", "auto": "خودکار",
                 "auto-translated": "ترجمهٔ خودکار"}


def base_lang(code: str) -> str:
    return code.split("-")[0].lower()


def pick_track(info: dict, lang: str, only: Optional[str] = None) -> Optional[tuple]:
    """Best subtitle track for `lang` -> (key, source) or None.

    Order: uploader's own subtitles > auto-generated in the video's own language ('xx-orig')
    > YouTube's machine translation into `lang`.   only="auto" / "manual" restricts the search.
    """
    manual = set() if only == "auto" else {k for k, v in (info.get("subtitles") or {}).items() if v and k not in SKIP_KEYS}
    auto = set() if only == "manual" else {k for k, v in (info.get("automatic_captions") or {}).items() if v}
    if lang in manual:
        return lang, "manual"
    cands = sorted(k for k in manual if base_lang(k) == base_lang(lang) and "-" in k)
    if "-" not in lang and cands:
        return cands[0], "manual"
    if f"{lang}-orig" in auto:
        return f"{lang}-orig", "auto-orig"
    if lang in auto:
        has_orig = any(k.endswith("-orig") for k in auto)
        return lang, ("auto-translated" if has_orig or manual else "auto")
    cands = sorted(k for k in auto if base_lang(k) == base_lang(lang) and not k.endswith("-orig"))
    if "-" not in lang and cands:
        return cands[0], "auto-translated"
    return None


def pick_for(info: dict, spec: str) -> Optional[tuple]:
    """spec is 'en' (best available), 'en:auto' or 'en:manual'."""
    lang, _, pref = spec.partition(":")
    return pick_track(info, lang, only=pref if pref in ("auto", "manual") else None)


_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})"
_TIMING = re.compile(rf"^{_TS}\s*-->\s*{_TS}")
_TAG = re.compile(r"<[^>]*>")


def _secs(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(text: str) -> list:
    """-> [(start, end, [lines])] with inline tags stripped."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip("\n").split("\n")
        idx = next((i for i, l in enumerate(lines) if _TIMING.match(l.strip())), None)
        if idx is None:
            continue
        m = _TIMING.match(lines[idx].strip())
        start, end = _secs(*m.groups()[0:4]), _secs(*m.groups()[4:8])
        body = []
        for l in lines[idx + 1:]:
            l = html.unescape(_TAG.sub("", l)).replace("\u00a0", " ").strip()
            if l:
                body.append(l)
        cues.append((start, end, body))
    return cues


def looks_rolling(cues: list) -> bool:
    """YouTube auto-captions ('karaoke' style) repeat the previous line inside every cue."""
    if len(cues) < 4:
        return False
    tiny = sum(1 for s, e, _ in cues if e - s < 0.05)
    repeats = sum(1 for a, b in zip(cues, cues[1:]) if len(b[2]) >= 2 and a[2] and b[2][0] == a[2][-1])
    return (tiny + repeats) / len(cues) >= 0.25


def clean_rolling(cues: list) -> list:
    out, prev = [], []
    for s, e, lines in cues:
        if e - s < 0.05:                       # 10 ms transition cues
            prev = lines or prev
            continue
        new = [l for l in lines if l not in prev]
        prev = lines
        if new:
            out.append((s, e, new))
    return out


def tidy_timings(cues: list, min_dur: float = 1.0) -> list:
    out = []
    for i, (s, e, lines) in enumerate(cues):
        end = max(e, s + min_dur)
        if i + 1 < len(cues):
            nxt = cues[i + 1][0]
            if end > nxt:
                end = nxt if nxt - s >= 0.2 else max(e, s + 0.2)
        out.append((s, end, lines))
    return out


def _fmt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


LRE, RLE, PDF = "\u202a", "\u202b", "\u202c"


def line_is_rtl(line: str, lang: Optional[str] = None) -> bool:
    """Reading direction of ONE line, decided by its own letters. A line that is still English inside a Persian subtitle
    (its translation failed) must stay left-to-right, otherwise its '.' and ',' jump to the wrong end of the line.
    `lang` only decides lines without any letter (numbers, '...')."""
    rtl = ltr = 0
    for ch in line:
        k = unicodedata.bidirectional(ch)
        if k in ("R", "AL"):
            rtl += 1
        elif k == "L":
            ltr += 1
    if rtl or ltr:
        return rtl > ltr
    return bool(lang) and base_lang(lang) in RTL


def wrap_dir(line: str, lang: Optional[str]) -> str:
    """Forces the reading direction of one line (LTR for English, RTL for Persian ...), so that '?' '.' '!'
    always end up on the correct side, in burned-in subtitles AND in phone players with a Persian UI."""
    if not lang:
        return line
    return (RLE if line_is_rtl(line, lang) else LRE) + line + PDF


def to_srt(cues: list, lang: Optional[str] = None, marks: bool = False) -> str:
    parts = []
    for i, (s, e, lines) in enumerate(cues, 1):
        body = "\n".join(wrap_dir(l, lang) if marks else l for l in lines)
        parts.append(f"{i}\n{_fmt(s)} --> {_fmt(e)}\n{body}\n")
    return "\n".join(parts)


def cues_from_vtt(vtt_text: str) -> list:
    cues = [c for c in parse_vtt(vtt_text) if c[2] or c[1] - c[0] < 0.05]
    cues = clean_rolling(cues) if looks_rolling(cues) else [c for c in cues if c[2]]
    return tidy_timings(cues)


def vtt_to_srt(vtt_text: str, rtl: bool = False) -> tuple:
    cues = cues_from_vtt(vtt_text)
    return to_srt(cues, "fa" if rtl else None, marks=rtl), len(cues)


# ---- Persian clean-up for machine translated text
_FA_DIGITS = str.maketrans("0123456789٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹۰۱۲۳۴۵۶۷۸۹")


def fix_fa(t: str) -> str:
    """Persian typography (digits, ؟ ، ؛). Text that is really English (a line whose translation failed, or speech the
    model wrongly called Persian) is left alone: it must keep its 14 and its , ? ;"""
    if len(re.findall(r"[A-Za-z]", t)) > len(re.findall(r"[\u0600-\u06FF]", t)):
        return re.sub(r"[ \t]{2,}", " ", t).strip()
    t = t.replace("ي", "ی").replace("ك", "ک")
    t = re.sub(r"(?<![A-Za-z])\d+(?![A-Za-z])", lambda m: m.group().translate(_FA_DIGITS), t)
    t = re.sub(r"(?<=[\u0600-\u06FF])\s*\?", "؟", t)
    t = re.sub(r"(?<=[\u0600-\u06FF]),", "،", t)
    t = re.sub(r"(?<=[\u0600-\u06FF]);", "؛", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


# =====================================================================  subtitle look (size / colour / background / alignment)
SIZES = {"s": 0.046, "m": 0.0625, "l": 0.078, "x": 0.094}     # share of the video height ("m" = the old look)
COLORS = {"w": "FFFFFF", "y": "FFE600", "g": "66FF66", "c": "4DD9FF"}
ALIGN = {"c": 2, "l": 1, "r": 3}


@dataclass
class Opts:
    sz: str = "x"
    co: str = "y"
    bg: str = "t"          # o = outline, b = black box, t = translucent box (default)
    al: str = "c"
    du: str = ""           # dubbing language ("" = no dubbing)
    vc: str = "f"          # f / m voice

    @classmethod
    def parse(cls, s: str) -> "Opts":
        o = cls()
        for part in re.split(r"[,;\s]+", s or ""):
            k, _, v = part.partition("=")
            v = v.strip()
            if k == "sz" and v in SIZES: o.sz = v
            elif k == "co" and v in COLORS: o.co = v
            elif k == "bg" and v in ("o", "b", "t"): o.bg = v
            elif k == "al" and v in ALIGN: o.al = v
            elif k == "du" and LANG_RE.match(v): o.du = v
            elif k == "vc" and v in ("f", "m"): o.vc = v
        return o


RTL_FONTS = ["Vazirmatn", "Noto Sans Arabic", "Noto Naskh Arabic", "DejaVu Sans"]
LTR_FONTS = ["Inter", "Noto Sans", "DejaVu Sans"]
_FONTS: list = []


def pick_font(lang: str) -> str:
    if not _FONTS:
        try:
            out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
            _FONTS.append({f.strip().lower() for line in out.splitlines() for f in line.split(",")})
        except Exception:                                           # noqa: BLE001
            _FONTS.append(set())
    prefs = RTL_FONTS if base_lang(lang) in RTL else LTR_FONTS
    return next((f for f in prefs if f.lower() in _FONTS[0]), prefs[-1])


def _ass_t(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _bgr(rgb: str, alpha: int = 0) -> str:
    return f"&H{alpha:02X}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"


def build_ass(cues: list, lang: str, opts: Opts, w: int, h: int) -> str:
    w, h = max(int(w or 1280), 64), max(int(h or 720), 64)
    # the size follows the height of a 16:9 picture. A portrait / square / 4:3 picture would get tiny letters from that
    # (720x1280 -> 25 px), so it never goes below the shorter side. 16:9 and wider pictures are unchanged.
    ref = max(min(h, w * 9 / 16), min(w, h))
    fs = max(12, round(ref * SIZES[opts.sz]))
    box = opts.bg in ("b", "t")
    outline = round(fs * 0.14, 1) if box else max(2.0, round(fs * 0.075, 1))     # box: the padding; two wrapped lines overlap only a little
    shadow = 0 if box else round(fs * 0.03, 1)
    back = _bgr("000000", 0 if opts.bg == "b" else (0x80 if opts.bg == "t" else 0))
    style = (f"Style: Default,{pick_font(lang)},{fs},{_bgr(COLORS[opts.co])},{_bgr(COLORS[opts.co])},{back},&H80000000,"
             f"-1,0,0,0,100,100,0,0,{3 if box else 1},{outline},{shadow},{ALIGN[opts.al]},"
             f"{int(w * 0.05)},{int(w * 0.05)},{int(h * 0.06)},1")
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
            "YCbCr Matrix: None\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n%s\n\n[Events]\nFormat: Layer, Start, End, "
            "Style, Name, MarginL, MarginR, MarginV, Effect, Text\n") % (w, h, style)
    rows = []
    for s, e, lines in cues:
        txt = "\\N".join(wrap_dir(l.replace("{", "(").replace("}", ")").replace("\\", "/"), lang) for l in lines if l)
        if txt:
            rows.append(f"Dialogue: 0,{_ass_t(s)},{_ass_t(e)},Default,,0,0,0,,{txt}")
    return head + "\n".join(rows) + "\n"


# =====================================================================  AI translation (Claude / ChatGPT / Gemini)
class AIError(Exception):
    pass


AI_DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-6.1-sol", "gemini": "gemini-flash-latest"}
AI_BACKUP_MODELS = {"gemini": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
                    "anthropic": ["claude-haiku-5-5"], "openai": ["gpt-6-luna"]}
AI_NAMES = {"anthropic": "Claude", "openai": "ChatGPT", "gemini": "Gemini"}
LANG_EN = {"fa": "Persian (Farsi)", "en": "English", "ar": "Arabic", "tr": "Turkish", "de": "German", "fr": "French",
           "es": "Spanish", "ru": "Russian", "it": "Italian", "pt": "Portuguese", "ja": "Japanese", "ko": "Korean",
           "zh": "Chinese", "hi": "Hindi", "ur": "Urdu", "nl": "Dutch", "pl": "Polish", "uk": "Ukrainian",
           "he": "Hebrew", "id": "Indonesian", "sv": "Swedish"}


def ai_setup(cfg: "Cfg") -> None:
    e = lambda k: (os.environ.get(k) or "").strip()                 # noqa: E731
    keys = {"anthropic": e("ANTHROPIC_API_KEY"), "openai": e("OPENAI_API_KEY"), "gemini": e("GEMINI_API_KEY")}
    want = e("AI_PROVIDER").lower()
    prov = want if keys.get(want) else next((p for p in ("anthropic", "openai", "gemini") if keys[p]), "")
    if prov:
        cfg.ai_provider, cfg.ai_key = prov, keys[prov]
        cfg.ai_model = e("AI_MODEL") or AI_DEFAULT_MODELS[prov]
        cfg.ai_models = [cfg.ai_model] if e("AI_MODEL") else [cfg.ai_model] + AI_BACKUP_MODELS.get(prov, [])


def _api_msg(txt: str) -> str:
    """The readable message of an API error (JSON {"error": {"message": ...}}) in one line."""
    try:
        d = json.loads(txt)
        m = (d.get("error") or {}).get("message") if isinstance(d, dict) else None
        if isinstance(m, str) and m:
            return re.sub(r"\s+", " ", m)
    except ValueError:
        pass
    return re.sub(r"\s+", " ", txt)


def _get_json(url: str, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


_GEMINI_NOT_TEXT = re.compile(r"image|tts|live|audio|embed|robot|omni|transcribe|translate|computer|aqa|imagen|veo|lyria|thinking|exp|vision|cyber|nano|gemma", re.I)
_DISCOVERED: set = set()


def discover_gemini_models(cfg: "Cfg") -> list:
    """Asks Google which models THIS key may use and returns the best text models first (newest Flash, then Flash-Lite, then Pro).
    Model names get retired every few months; this way the bot keeps working without a code change."""
    try:
        d = _get_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", {"x-goog-api-key": cfg.ai_key})
    except Exception as ex:                                         # noqa: BLE001
        print(f"could not list the Gemini models: {ex}")
        return []
    ranked = []
    for m in d.get("models") or []:
        name = (m.get("name") or "")
        name = name[len("models/"):] if name.startswith("models/") else name
        mm = re.match(r"gemini-(\d+(?:\.\d+)?)-(flash-lite|flash|pro)", name)
        if not mm or "generateContent" not in (m.get("supportedGenerationMethods") or []) or _GEMINI_NOT_TEXT.search(name):
            continue
        ranked.append(({"flash": 2, "flash-lite": 1, "pro": 0}[mm.group(2)], "preview" not in name, float(mm.group(1)), name))
    ranked.sort(reverse=True)                                       # Flash > Flash-Lite > Pro; stable > preview; newest first
    return [r[3] for r in ranked[:6]]


def _post_json(url: str, headers: dict, body: dict, tries: int = 3) -> dict:
    data = json.dumps(body).encode()
    waits = [5, 15, 30]
    last = "unknown"
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=100) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as ex:
            txt = ex.read().decode("utf-8", "replace")[:600]
            last = f"HTTP {ex.code}: {_api_msg(txt)[:260]}"
            print(f"AI request failed ({attempt + 1}/{tries}): {last[:200]}")
            if ex.code == 429 and re.search(r"limit:\s*0|billing|quota exceeded for", txt, re.I):
                raise AIError("سهمیهٔ رایگان این کلید/منطقه صفر است (HTTP 429). " + txt[:120])
            if ex.code in (429, 500, 502, 503, 504, 529) and attempt < tries - 1:
                try:
                    wait = min(int(ex.headers.get("retry-after") or 0), 60) or waits[attempt]
                except ValueError:
                    wait = waits[attempt]
                time.sleep(wait)
                continue
            raise AIError(last)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as ex:
            last = f"network: {ex}"
            print(f"AI request failed ({attempt + 1}/{tries}): {last[:200]}")
            if attempt < tries - 1:
                time.sleep(waits[attempt])
                continue
    raise AIError(last)


def llm(cfg: "Cfg", system: str, user: str, max_tokens: int = 6000) -> str:
    """Tries the models in order; a busy/missing model (503, 429, 404 ...) moves on to the next one.
    When every Gemini model is gone (HTTP 404, "no longer available") it asks Google which models this key can use."""
    last = None
    tried: set = set()
    while True:
        for model in list(cfg.ai_models or [cfg.ai_model]):
            if model in tried:
                continue
            tried.add(model)
            try:
                out = _llm_one(cfg, model, system, user, max_tokens)
                if model != cfg.ai_models[0]:
                    cfg.ai_models.remove(model)
                    cfg.ai_models.insert(0, model)
                    cfg.ai_model = model
                return out
            except AIError as ex:
                last = ex
                print(f"model {model} failed: {str(ex)[:200]}")
                if not re.search(r"HTTP (429|404|500|502|503|504|529)|network", str(ex)):
                    raise
        if cfg.ai_provider == "gemini" and last is not None and re.search(r"HTTP 404", str(last)) and "gemini" not in _DISCOVERED:
            _DISCOVERED.add("gemini")
            found = [m for m in discover_gemini_models(cfg) if m not in tried]
            print(f"Gemini models this key can use: {found}")
            if found:
                cfg.ai_models = found + [m for m in cfg.ai_models if m not in found]
                continue
        raise last or AIError("no model")


def _llm_one(cfg: "Cfg", model: str, system: str, user: str, max_tokens: int = 6000) -> str:
    p, key = cfg.ai_provider, cfg.ai_key
    if p == "anthropic":
        d = _post_json("https://api.anthropic.com/v1/messages",
                       {"x-api-key": key, "anthropic-version": "2023-06-01"},
                       {"model": model, "max_tokens": max_tokens, "system": system,
                        "messages": [{"role": "user", "content": user}]})
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    if p == "openai":
        d = _post_json("https://api.openai.com/v1/chat/completions", {"Authorization": f"Bearer {key}"},
                       {"model": model, "max_completion_tokens": max(max_tokens, 16000),
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        return ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if p == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        base = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}]}
        d, err = None, None
        # short "thinking" keeps it fast; older/newer models name it differently, so fall back step by step
        for think in ({"thinkingLevel": "low"}, {"thinkingBudget": 0}, None):
            gc = {"maxOutputTokens": 16000}
            if think:
                gc["thinkingConfig"] = think
            try:
                d = _post_json(url, {"x-goog-api-key": key}, {**base, "generationConfig": gc})
                break
            except AIError as ex:
                err = ex
                if "HTTP 400" not in str(ex):
                    raise
        if d is None:
            raise err
        parts = (((d.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
        return "".join(x.get("text", "") for x in parts if not x.get("thought"))
    raise AIError("no AI key")


_NUM_LINE = re.compile(r"^\s*(\d+)\s*(?:[\t:|.)\]-]\s*|\s+)(.*)$")


def _parse_numbered(text: str) -> dict:
    out, last = {}, None
    for line in text.replace("\r", "").split("\n"):
        line = line.strip().strip("`")
        if not line:
            continue
        m = _NUM_LINE.match(line)
        if m:
            last = int(m.group(1))
            out[last] = m.group(2).strip()
        elif last is not None:
            out[last] = (out[last] + " " + line).strip()
    return out


def _translate_batch(cfg, idx, texts, prev, src, dst, ctx) -> dict:
    s_name, d_name = LANG_EN.get(base_lang(src), src), LANG_EN.get(base_lang(dst), dst)
    system = (f"You are a professional subtitle translator. Translate numbered subtitle lines from {s_name} to {d_name}.\n"
              "Rules:\n- Output exactly one line per input line, formatted as: NUMBER<TAB>translation. Same numbers. "
              "Never merge, split, skip or add lines. No comments, no markdown.\n"
              "- The lines are fragments of speech: a sentence may continue in the next line. Translate so the text reads "
              "naturally across consecutive lines, but keep each translation tied to its own line.\n"
              "- Natural, fluent, spoken-style language. Short enough to read as a subtitle. Keep names and numbers.\n"
              "- Sound tags like [Music] stay as tags, translated, in brackets.\n")
    if base_lang(dst) == "fa":
        system += ("- Persian: standard modern Persian (فارسی معیار گفتاری), correct half-spaces (نیم‌فاصله) such as «می‌شود», "
                   "Persian letters ی and ک, Persian punctuation (، ؛ ؟) and Persian digits.\n")
    user = f"Video title: {ctx.get('title', '')}\nChannel: {ctx.get('channel', '')}\n"
    if ctx.get("desc"):
        user += f"Description (excerpt): {ctx['desc']}\n"
    if prev:
        user += "\nPrevious lines, for context only (do not output them):\n" + "\n".join(f"{i + 1}\t{a}\t=> {b}" for i, a, b in prev) + "\n"
    user += "\nTranslate these lines:\n" + "\n".join(f"{i + 1}\t{texts[i]}" for i in idx)
    got = _parse_numbered(llm(cfg, system, user))
    return {i: got[i + 1] for i in idx if got.get(i + 1)}


def translate_cues(cfg: "Cfg", cues: list, src: str, dst: str, ctx: dict, progress=None) -> tuple:
    """-> (translated cues, number of lines that stayed untranslated)"""
    texts = [" ".join(l) for _, _, l in cues]
    out: list = [None] * len(cues)
    bad, step = 0, 25
    for b0 in range(0, len(cues), step):
        idx = list(range(b0, min(len(cues), b0 + step)))
        if progress:
            progress(f"🌐 ترجمه با هوش مصنوعی ({AI_NAMES.get(cfg.ai_provider, '')})\n{bar(b0 / max(1, len(cues)))}")
        prev = [(i, texts[i], out[i]) for i in range(max(0, b0 - 4), b0) if out[i]]
        print(f"translating lines {b0 + 1}-{idx[-1] + 1} of {len(cues)}")
        got = _translate_batch(cfg, idx, texts, prev, src, dst, ctx)
        for _ in range(2):
            miss = [i for i in idx if i not in got]
            if not miss:
                break
            got.update(_translate_batch(cfg, miss, texts, prev, src, dst, ctx))
        for i in idx:
            if i in got:
                out[i] = fix_fa(got[i]) if base_lang(dst) == "fa" else got[i]
            else:
                out[i], bad = texts[i], bad + 1
    return [(s, e, [out[i]]) for i, (s, e, _l) in enumerate(cues)], bad


# =====================================================================  dubbing (text-to-speech from the subtitle)
DUB_SR = 24000
DUB_MAX_SPEED = 1.45
VOICES = {   # (female, male) Microsoft Edge neural voices (free, no key)
    "fa": ("fa-IR-DilaraNeural", "fa-IR-FaridNeural"), "en": ("en-US-AriaNeural", "en-US-GuyNeural"),
    "ar": ("ar-SA-ZariyahNeural", "ar-SA-HamedNeural"), "tr": ("tr-TR-EmelNeural", "tr-TR-AhmetNeural"),
    "de": ("de-DE-KatjaNeural", "de-DE-ConradNeural"), "fr": ("fr-FR-DeniseNeural", "fr-FR-HenriNeural"),
    "es": ("es-ES-ElviraNeural", "es-ES-AlvaroNeural"), "ru": ("ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"),
    "it": ("it-IT-ElsaNeural", "it-IT-DiegoNeural"), "pt": ("pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"),
    "ja": ("ja-JP-NanamiNeural", "ja-JP-KeitaNeural"), "ko": ("ko-KR-SunHiNeural", "ko-KR-InJoonNeural"),
    "zh": ("zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"), "hi": ("hi-IN-SwaraNeural", "hi-IN-MadhurNeural"),
    "ur": ("ur-PK-UzmaNeural", "ur-PK-AsadNeural"), "nl": ("nl-NL-ColetteNeural", "nl-NL-MaartenNeural"),
    "pl": ("pl-PL-ZofiaNeural", "pl-PL-MarekNeural"), "uk": ("uk-UA-PolinaNeural", "uk-UA-OstapNeural"),
    "he": ("he-IL-HilaNeural", "he-IL-AvriNeural"), "id": ("id-ID-GadisNeural", "id-ID-ArdiNeural"),
    "sv": ("sv-SE-SofieNeural", "sv-SE-MattiasNeural"),
}


def group_for_dub(cues: list) -> list:
    """Joins neighbouring cues into speakable segments -> [(start, hard_end, text)]. Sound tags are not spoken."""
    segs, cur = [], None
    for s, e, lines in cues:
        t = " ".join(lines).strip()
        t = re.sub(r"\[[^\]]*\]|\([^)]*\)|[♪♫]", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if not re.search(r"\w", t):
            if cur:
                segs.append(cur)
                cur = None
            continue
        if cur and s - cur[1] <= 0.6 and e - cur[0] <= 9 and len(cur[2]) + len(t) <= 220 \
                and not (re.search(r"[.!?؟…]$", cur[2]) and cur[1] - cur[0] > 2.5):
            cur = [cur[0], e, cur[2] + " " + t]
        else:
            if cur:
                segs.append(cur)
            cur = [s, e, t]
    if cur:
        segs.append(cur)
    return [tuple(x) for x in segs]


def _decode_pcm(path, speed: float = 1.0):
    import numpy as np
    af, s = [], speed
    while s > 2.0:
        af.append("atempo=2.0")
        s /= 2.0
    if abs(s - 1.0) > 0.01:
        af.append(f"atempo={s:.4f}")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path)] + (["-af", ",".join(af)] if af else []) \
        + ["-f", "f32le", "-ac", "1", "-ar", str(DUB_SR), "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise FFmpegError(p.stderr.decode("utf-8", "replace")[-300:])
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def _trim(x, thr=0.01, head=0.03, tail=0.08):
    import numpy as np
    idx = np.flatnonzero(np.abs(x) > thr)
    if idx.size == 0:
        return x
    return x[max(0, int(idx[0] - head * DUB_SR)): min(len(x), int(idx[-1] + tail * DUB_SR))]


def dub_layout(segs: list, lens: list) -> list:
    """-> [(start_at, speed)]. Speech never overlaps; it speeds up (max x1.45) when the slot is short."""
    out, cursor = [], 0.0
    for (st, he, _t), L in zip(segs, lens):
        avail = max(0.3, he - st)
        speed = min(DUB_MAX_SPEED, L / avail) if L > avail * 1.02 else 1.0
        at = max(st, cursor)
        lag = at - st
        if lag > 1.5 and speed < DUB_MAX_SPEED:                     # behind the picture: catch up
            speed = min(DUB_MAX_SPEED, max(speed, L / max(0.5, avail - lag * 0.5)))
        out.append((at, speed))
        cursor = at + L / speed + 0.08
    return out


def _pick_voice(lang: str, gender: str, notes: list) -> str:
    pair = VOICES.get(base_lang(lang))
    if not pair:
        raise RuntimeError(f"no voice for {lang}")
    want = pair[0] if gender == "f" else pair[1]
    try:
        import edge_tts
        voices = asyncio.run(edge_tts.list_voices())
        names = {v["ShortName"] for v in voices}
        if want not in names:
            alt = [v["ShortName"] for v in voices if v["Locale"].lower().startswith(base_lang(lang))
                   and v.get("Gender", "").lower() == ("female" if gender == "f" else "male")]
            if alt:
                notes.append(f"voice {want} replaced by {alt[0]}")
                return alt[0]
    except Exception as ex:                                         # noqa: BLE001
        print(f"voice list failed: {ex}")
    return want


def synth_clips(segs: list, voice: str, outdir: Path, progress=None) -> list:
    import edge_tts
    done = [0]

    async def one(sem, i, text):
        path = outdir / f"c{i:04d}.mp3"
        async with sem:
            for attempt in range(5):
                try:
                    await edge_tts.Communicate(text, voice).save(str(path))
                    if path.stat().st_size > 500:
                        break
                except Exception as ex:                             # noqa: BLE001
                    print(f"tts {i} attempt {attempt + 1}: {ex}")
                await asyncio.sleep(2 * (attempt + 1))
            else:
                path = None
        done[0] += 1
        if progress and done[0] % 5 == 0:
            progress(f"🎙 ساخت صدای دوبله\n{bar(done[0] / len(segs))}")
        return path

    async def run():
        sem = asyncio.Semaphore(3)
        return await asyncio.gather(*(one(sem, i, s[2]) for i, s in enumerate(segs)))

    return asyncio.run(run())


def build_dub_audio(cues: list, lang: str, opts: Opts, video: Path, duration: float, work: Path, progress=None):
    """-> (path of the mixed AAC track, notes). Raises on total failure."""
    import numpy as np
    notes: list = []
    segs = group_for_dub(cues)
    if not segs:
        raise RuntimeError("no speakable text")
    voice = _pick_voice(lang, opts.vc, notes)
    outdir = work / "tts"
    outdir.mkdir(exist_ok=True)
    clips = synth_clips(segs, voice, outdir, progress)
    ok = [(s, c) for s, c in zip(segs, clips) if c]
    if len(ok) < max(1, len(segs) * 0.7):
        raise RuntimeError(f"text-to-speech failed for {len(segs) - len(ok)} of {len(segs)} segments")
    if len(ok) < len(segs):
        notes.append(f"⚠️ صدای {len(segs) - len(ok)} جمله از {len(segs)} ساخته نشد.")
    segs2 = [s for s, _ in ok]
    raw = [_trim(_decode_pcm(c)) for _, c in ok]
    pos = dub_layout(segs2, [len(r) / DUB_SR for r in raw])
    total = max(duration, segs2[-1][1]) + 5
    track = np.zeros(int(total * DUB_SR) + DUB_SR * 20, dtype=np.float32)
    for (_s, c), (at, sp) in zip(ok, pos):
        x = _trim(_decode_pcm(c, sp)) if sp > 1.01 else _trim(_decode_pcm(c))
        i = int(at * DUB_SR)
        if i + len(x) > len(track):
            track = np.concatenate([track, np.zeros(i + len(x) - len(track) + DUB_SR, dtype=np.float32)])
        track[i:i + len(x)] += x
    track = track[: int(total * DUB_SR)]
    import wave
    wav = work / "dub.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(DUB_SR)
        w.writeframes((np.clip(track, -1, 1) * 32767).astype("<i2").tobytes())
    out = work / "dub_mix.m4a"
    has_audio = bool(probe(video)["acodec"])
    if has_audio:
        fc = ("[0:a:0]aresample=48000,aformat=channel_layouts=stereo[o];"
              "[1:a]aresample=48000,pan=stereo|c0=c0|c1=c0,volume=1.6[d];[d]asplit=2[d1][d2];"
              "[o][d1]sidechaincompress=threshold=0.01:ratio=20:attack=15:release=400:makeup=1[oc];"
              "[oc][d2]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[m]")
    else:
        fc = "[1:a]aresample=48000,pan=stereo|c0=c0|c1=c0,volume=1.6,alimiter=limit=0.95[m]"
    p = subprocess.run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(wav),
                        "-filter_complex", fc, "-map", "[m]", "-t", f"{duration:.2f}", "-c:a", "aac", "-b:a", "160k", str(out)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise FFmpegError(f"dub mix failed: {p.stderr[-400:]}")
    return out, notes


# =====================================================================  ffmpeg
class FFmpegError(RuntimeError):
    pass


def probe(path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height,duration:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    if p.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {p.stderr[-300:]}")
    data = json.loads(p.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    num = lambda x: float(x) if x not in (None, "N/A", "") else 0.0   # noqa: E731
    try:
        dur = num((data.get("format") or {}).get("duration"))
    except ValueError:
        dur = 0.0
    return {"duration": dur, "width": v.get("width") or 0, "height": v.get("height") or 0,
            "vcodec": v.get("codec_name", ""), "acodec": a.get("codec_name", ""),
            "vdur": num(v.get("duration")), "adur": num(a.get("duration"))}


def decoded_seconds(path, kind: str, limit: int = 7200) -> tuple:
    """Really decodes the video ('v') or audio ('a') stream -> (seconds that could be decoded, error lines)."""
    sel = ["-map", "0:v:0", "-an"] if kind == "v" else ["-map", "0:a:0", "-vn"]
    p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-progress", "pipe:1", "-i", str(path)] + sel + ["-f", "null", "-"],
                       capture_output=True, text=True, timeout=limit)
    last = 0.0
    for line in p.stdout.splitlines():
        m = re.match(r"out_time_us=(\d+)", line)
        if m:
            last = int(m.group(1)) / 1e6
    errs = [l for l in p.stderr.splitlines() if l.strip()]
    return last, errs


def check_media(path, expected: float) -> dict:
    """Is the file complete? Compares the real (decoded) length of picture and sound with the length YouTube announced."""
    m = probe(path)
    rep = {"ok": True, "problems": [], "container": m["duration"], "video": 0.0, "audio": 0.0, "expected": expected}
    try:
        rep["video"], verr = decoded_seconds(path, "v")
        if m["acodec"]:
            rep["audio"], aerr = decoded_seconds(path, "a")
        else:
            aerr = []
    except subprocess.TimeoutExpired:
        rep["video"], verr, aerr = m["duration"], [], []
    ref = expected or m["duration"]
    tol = max(3.0, ref * 0.03)
    if ref and rep["video"] < ref - tol:
        rep["problems"].append(f"تصویر فقط {rep['video']:.0f} ثانیه از {ref:.0f} است")
    if ref and m["acodec"] and rep["audio"] < ref - tol:
        rep["problems"].append(f"صدا فقط {rep['audio']:.0f} ثانیه از {ref:.0f} است")
    if len(verr) + len(aerr) > 20:
        rep["problems"].append(f"{len(verr) + len(aerr)} خطای رمزگشایی")
    rep["ok"] = not rep["problems"]
    return rep


def render(video: Path, out: Path, hard: Optional[Path] = None, soft: Optional[list] = None,
           duration: float = 0.0, on_progress: Optional[Callable[[float], None]] = None,
           dub_audio: Optional[Path] = None, force_encode: bool = False, dub_lang: str = "") -> None:
    """hard = ASS file burned into the picture; soft = [(srt, lang)] tracks; dub_audio = dubbed track (original kept as 2nd track).
    Always writes a clean MP4 (faststart, fresh timestamps) that phones and Telegram can play."""
    soft = soft or []
    video, out = Path(video), Path(out)
    work = video.parent
    info = probe(video)
    reencode = bool(hard) or force_encode or info["vcodec"] != "h264"

    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
    if not reencode:
        cmd += ["-fflags", "+genpts"]
    cmd += ["-i", video.name]
    for srt, _ in soft:
        cmd += ["-i", Path(srt).name]
    dub_idx = 1 + len(soft)
    if dub_audio:
        cmd += ["-i", Path(dub_audio).name]
    cmd += ["-map", "0:v:0"]
    if dub_audio:
        cmd += ["-map", f"{dub_idx}:a:0"]
        if info["acodec"]:
            cmd += ["-map", "0:a:0"]
    else:
        cmd += ["-map", "0:a?"]
    for i in range(len(soft)):
        cmd += ["-map", f"{i + 1}:0"]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-g", "60", "-sc_threshold", "0"]
        if hard:
            cmd += ["-vf", f"subtitles=filename={Path(hard).name}"]
    else:
        cmd += ["-c:v", "copy", "-avoid_negative_ts", "make_zero"]
    if dub_audio:
        cmd += ["-c:a:0", "copy"]
        if info["acodec"]:
            cmd += ["-c:a:1", "copy" if info["acodec"] == "aac" else "aac",
                    "-metadata:s:a:0", f"title=Dub {dub_lang}".strip(), "-metadata:s:a:1", "title=Original",
                    "-disposition:a:0", "default", "-disposition:a:1", "0"]
    else:
        cmd += ["-c:a", "copy"] if info["acodec"] == "aac" else ["-c:a", "aac", "-b:a", "128k"]
    if soft:
        cmd += ["-c:s", "mov_text"]
        for i, (_, lang) in enumerate(soft):
            cmd += [f"-metadata:s:s:{i}", f"language={ISO3.get(base_lang(lang), 'und')}",
                    f"-metadata:s:s:{i}", f"title={LANG_NAMES.get(base_lang(lang), lang)}",
                    f"-disposition:s:{i}", "default" if (i == 0 and not hard) else "0"]
    cmd += ["-movflags", "+faststart", out.name if out.parent == work else str(out)]

    with subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        for line in proc.stdout:
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if m and on_progress and duration > 0:
                on_progress(min(1.0, int(m.group(1)) / 1e6 / duration))
        err = proc.stderr.read()
        code = proc.wait()
    if code != 0:
        raise FFmpegError(f"ffmpeg failed: {err[-600:]}")


def thumbnail(video: Path, out: Path, duration: float = 0) -> Optional[Path]:
    t = min(max(duration * 0.1, 1), 15) if duration else 1
    p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(out)], capture_output=True)
    return out if p.returncode == 0 and Path(out).exists() and Path(out).stat().st_size > 0 else None


def split_by_size(video: Path, max_bytes: int, duration: float) -> list:
    """Cut into parts below max_bytes (stream copy, no quality loss)."""
    video = Path(video)
    size = video.stat().st_size
    if size <= max_bytes:
        return [video]
    duration = duration or probe(video)["duration"]
    factor = 0.85
    for _ in range(4):
        seg = max(5, math.floor(duration * (max_bytes * factor) / size))
        for old in video.parent.glob(f"{video.stem}.part*.mp4"):
            old.unlink()
        pattern = str(video.parent / f"{video.stem}.part%02d.mp4")
        p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-map", "0", "-c", "copy",
                            "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1",
                            "-segment_format_options", "movflags=+faststart", pattern],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise FFmpegError(f"split failed: {p.stderr[-400:]}")
        parts = sorted(video.parent.glob(f"{video.stem}.part*.mp4"))
        if parts and all(x.stat().st_size <= max_bytes for x in parts):
            return parts
        factor *= 0.7
    raise FFmpegError("could not split below the upload limit")


# =====================================================================  yt-dlp
class CaptureLogger:
    """Keeps yt-dlp's last warnings/errors so they can be shown to the user (they never contain cookie values)."""

    def __init__(self):
        self.lines = []
        self.total = 0                 # how many lines were ever added (the list itself is trimmed)

    def _add(self, msg):
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(msg)).strip()
        if msg:
            self.lines.append(msg[:220])
            self.total += 1
            del self.lines[:-30]
            print(msg)

    def since(self, mark: int) -> list:
        """Lines added after `mark` (a value of .total taken earlier)."""
        n = self.total - mark
        return self.lines[-n:] if n > 0 else []

    def debug(self, msg):      # progress chatter, not interesting
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self._add(msg)

    def error(self, msg):
        self._add(msg)

    def tail(self, n: int = 4) -> list:
        return self.lines[-n:]


LOG = CaptureLogger()
CLIENT_OVERRIDE: list = []          # clients that turned out to work (see extract_info)
COOKIES_OFF = [False]               # True when a no-cookies attempt was the one that worked
# attempts, in order: (player clients, use cookies). YouTube answers 403 / "bot" differently per client and per cookie.
PLANS = [(None, True), (["mweb"], True), (["tv"], True), (None, False), (["tv_downgraded", "visionos", "web_embedded"], False)]
RATE_WAITS = (30, 75)               # seconds to wait before re-asking YouTube after an HTTP 429
CLIENT_PAUSE = 3                    # short pause between two attempts so we never hammer YouTube
SUB_PAUSE = 4                       # pause between two subtitle requests (YouTube rate-limits caption downloads)


def is_rate_limit(x) -> bool:
    low = str(x).lower()
    return "http error 429" in low or "too many requests" in low


def is_bot_check(ex) -> bool:
    low = str(ex).lower()
    return "sign in to confirm" in low or "not a bot" in low


def is_blocked(ex) -> bool:
    """403 / no player response: another client or no cookies may still work."""
    low = str(ex).lower()
    return is_bot_check(ex) or "http error 403" in low or "failed to extract any player response" in low \
        or "unable to download api page" in low or "requested format is not available" in low


def ydl_opts(cfg: Cfg, **extra) -> dict:
    o = {"quiet": True, "no_warnings": False, "noprogress": True, "logger": LOG,
         "remote_components": ["ejs:github"], "sleep_interval_requests": 1,
         "retries": 10, "fragment_retries": 10, "extractor_retries": 3, "socket_timeout": 30,
         "source_address": "0.0.0.0",              # IPv4 only: GitHub runners often have no IPv6 route (-> "Network is unreachable")
         "skip_unavailable_fragments": False}      # a missing piece must fail loudly, never give a silently cut video
    if cfg.cookies_file and not COOKIES_OFF[0]:
        o["cookiefile"] = cfg.cookies_file
    # Do NOT force player_client by default: some clients (android_vr, web_embedded) return no caption tracks.
    clients = [c.strip() for c in os.environ.get("YT_PLAYER_CLIENTS", "").split(",") if c.strip()] or list(CLIENT_OVERRIDE)
    if clients:
        o["extractor_args"] = {"youtube": {"player_client": clients}}
    o.update(extra)
    return o


def extract_info(cfg: Cfg, url: str) -> dict:
    """Reads the video's metadata. Blocked (bot check / 403 / no player response) -> the next attempt in PLANS
    (other clients, then without cookies). HTTP 429 -> wait and ask again. A working attempt is kept for the whole job."""
    import yt_dlp
    first = None
    for n, (clients, cookies) in enumerate(PLANS):
        if n:
            time.sleep(CLIENT_PAUSE)
        CLIENT_OVERRIDE[:] = clients or []
        COOKIES_OFF[0] = not cookies
        waits = list(RATE_WAITS)
        while True:
            mark = LOG.total
            try:
                with yt_dlp.YoutubeDL(ydl_opts(cfg, skip_download=True, ignore_no_formats_error=True)) as ydl:
                    info = ydl.extract_info(url, download=False)
                # ignore_no_formats_error makes yt-dlp only LOG a block and hand back an empty shell
                blocked = [l for l in LOG.since(mark) if is_blocked(Exception(l)) or is_rate_limit(l)]
                if blocked and not info.get("formats"):
                    raise RuntimeError(blocked[-1])
                if clients or not cookies:
                    print(f"using clients={clients} cookies={cookies}")
                return info
            except Exception as ex:                                 # noqa: BLE001
                first = first or ex
                if is_rate_limit(ex) and waits:
                    wait = waits.pop(0)
                    print(f"YouTube answered 429; waiting {wait} s before asking again")
                    time.sleep(wait)
                    continue
                if not is_blocked(ex):
                    CLIENT_OVERRIDE[:] = []
                    COOKIES_OFF[0] = False
                    raise
                break                                               # blocked: try the next plan
    CLIENT_OVERRIDE[:] = []
    COOKIES_OFF[0] = False
    raise first


def _download_once(cfg: Cfg, url: str, work: Path, height: int, hook=None) -> None:
    import yt_dlp
    h = height
    fmt = (f"bv*[height<={h}][vcodec^=avc1][protocol=https]+ba[acodec^=mp4a][protocol=https]/"
           f"bv*[height<={h}][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b")
    opts = ydl_opts(cfg, format=fmt, merge_output_format="mp4", outtmpl=str(work / "%(id)s.%(ext)s"),
                    progress_hooks=[hook] if hook else [])
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def download_video(cfg: Cfg, url: str, work: Path, vid: str, height: int, expected: float, hook=None, progress=None) -> tuple:
    """Downloads the video and PROVES it is complete (real decoding, length compared with YouTube's). A cut or broken
    download is retried with other clients / without cookies instead of being uploaded. -> (path, health report)"""
    cur = (CLIENT_OVERRIDE[:] or None, not COOKIES_OFF[0])
    order = [cur] + [p for p in PLANS if p != cur]
    last: Optional[Exception] = None
    for n, (clients, cookies) in enumerate(order[:4]):
        CLIENT_OVERRIDE[:] = clients or []
        COOKIES_OFF[0] = not cookies
        for old in work.glob(f"{vid}.*"):
            old.unlink()
        if n and progress:
            progress(f"⬇️ دانلود دوباره با روش دیگر ({n + 1})…")
        try:
            _download_once(cfg, url, work, height, hook)
        except Exception as ex:                                     # noqa: BLE001
            last = ex
            print(f"download attempt {n + 1} failed: {ex}")
            if not is_blocked(ex) and not re.search(r"truncat|incomplete|unreachable|failed to establish|timed out|connection (reset|aborted)",
                                                     str(ex), re.I):
                raise
            time.sleep(CLIENT_PAUSE)
            continue
        video = find_video(work, vid)
        if not video:
            last = RuntimeError("Video file was not produced")
            continue
        if progress:
            progress("🔍 بررسی سلامت فایل (رمزگشایی کامل)…")
        rep = check_media(video, expected)
        if rep["ok"]:
            return video, rep
        last = RuntimeError("دانلود ناقص بود: " + "؛ ".join(rep["problems"]))
        print(f"attempt {n + 1}: {last}")
    raise last or RuntimeError("download failed")


def download_subs_only(cfg: Cfg, url: str, work: Path, keys: list) -> None:
    """One yt-dlp call per language: a 429 on one language must not abort the others (it does, in a single call).
    ignoreerrors=True turns a failed caption into a warning instead of an exception."""
    import yt_dlp
    for n, key in enumerate(keys):
        if n:
            time.sleep(SUB_PAUSE)
        opts = ydl_opts(cfg, skip_download=True, ignore_no_formats_error=True, ignoreerrors=True,
                        outtmpl=str(work / "%(id)s.%(ext)s"), writesubtitles=True, writeautomaticsub=True,
                        subtitleslangs=[key], subtitlesformat="vtt", sleep_interval_subtitles=3)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as ex:                                     # noqa: BLE001
            print(f"subtitle '{key}' failed: {ex}")


def find_video(work: Path, vid: str) -> Optional[Path]:
    c = [p for p in work.glob(f"{vid}.*") if p.suffix.lower() in VIDEO_EXTS]
    return max(c, key=lambda p: p.stat().st_size) if c else None


def collect_subs(work: Path, vid: str, keys: list) -> dict:
    out = {}
    for k in keys:
        p = work / f"{vid}.{k}.vtt"
        if p.is_file() and p.stat().st_size > 0:
            out[k] = p
    return out


def best_source(info: dict) -> Optional[tuple]:
    """The subtitle track in the video's own language: uploader's > automatic original."""
    lang = (info.get("language") or "").split("-")[0].lower()
    caps = info.get("automatic_captions") or {}
    subs = {k: v for k, v in (info.get("subtitles") or {}).items() if v and k not in SKIP_KEYS}
    if lang:
        p = pick_track(info, lang)
        if p and p[1] != "auto-translated":
            return p[0], p[1]
    origs = sorted(k for k, v in caps.items() if v and k.endswith("-orig"))
    if origs:
        return origs[0], "auto-orig"
    if subs:
        return sorted(subs)[0], "manual"
    return None


# =====================================================================  upload
def make_uploader(cfg: Cfg):
    """-> (Telegram client, size limit, using_local_server)."""
    if cfg.local_api:
        local = Telegram(cfg.bot_token, base=cfg.local_api)
        if local.reachable(tries=5, pause=4):
            return local, LOCAL_LIMIT, True
        log = clean_reason(os.environ.get("TG_LOCAL_LOG", ""), 200)
        cfg.fallback_reason = clean_reason(local.last_error + (f" | server: {log}" if log else ""), 240)
        print(f"local Bot API server not reachable ({cfg.fallback_reason}) -> using the normal API (50 MB limit, files get split)")
        cfg.fallback_used = True
    return Telegram(cfg.bot_token), CLOUD_LIMIT, False


def message_link(msg: dict) -> Optional[str]:
    chat, mid = msg.get("chat") or {}, msg.get("message_id")
    if not mid:
        return None
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{mid}"
    cid = str(chat.get("id", ""))
    return f"https://t.me/c/{cid[4:]}/{mid}" if cid.startswith("-100") else None


def send_one(up: Telegram, chat, path: Path, caption: str, meta: dict, thumb: Optional[Path]) -> dict:
    fields = {"chat_id": chat, "caption": caption[:1000], "parse_mode": "HTML"}
    try:
        return up.upload("sendVideo", "video", str(path), {
            **fields, "supports_streaming": "true", "duration": int(meta.get("duration") or 0),
            "width": meta.get("width") or None, "height": meta.get("height") or None},
            thumb=str(thumb) if thumb else None)
    except TelegramError as e:
        if e.code in (400, 415):                  # not accepted as a video -> send as a file
            print(f"sendVideo rejected ({e}); retrying as document")
            return up.upload("sendDocument", "document", str(path), fields, thumb=str(thumb) if thumb else None)
        raise


def _deliver_with(up, limit, chat, video, caption, thumb, duration, progress):
    meta = probe(video)
    meta["duration"] = meta["duration"] or duration
    parts = split_by_size(video, limit, meta["duration"]) if video.stat().st_size > limit else [video]
    sent = []
    for i, part in enumerate(parts, 1):
        cap = caption if len(parts) == 1 else f"{caption}\n\n🧩 بخش {i} از {len(parts)}"
        if progress:
            progress(f"⬆️ آپلود در تلگرام ({i}/{len(parts)})…")
        sent.append(send_one(up, chat, part, cap, probe(part) if len(parts) > 1 else meta, thumb))
    return sent


def deliver(cfg: Cfg, video: Path, caption: str, thumb: Optional[Path], duration: float, progress=None) -> list:
    up, limit, local = make_uploader(cfg)
    try:
        return _deliver_with(up, limit, cfg.storage_chat, video, caption, thumb, duration, progress)
    except TelegramError as e:
        if not local:
            raise
        cfg.fallback_reason = clean_reason(f"upload failed: {e}", 240)
        print(f"upload through the local server failed ({cfg.fallback_reason}); falling back to the normal API")
        cfg.fallback_used = True
        return _deliver_with(Telegram(cfg.bot_token), CLOUD_LIMIT, cfg.storage_chat, video, caption,
                             thumb, duration, progress)


def build_caption(title, channel, url, langs_desc, size_mb, height, duration) -> str:
    mins, secs = divmod(int(duration), 60)
    hrs, mins = divmod(mins, 60)
    dur = f"{hrs}:{mins:02d}:{secs:02d}" if hrs else f"{mins}:{secs:02d}"
    lines = [f"🎬 <b>{html.escape(title[:200])}</b>"]
    if channel:
        lines.append(f"📺 {html.escape(channel)}")
    if langs_desc:
        lines.append(f"💬 {html.escape(langs_desc)}")
    lines.append(f"📦 {height}p · {size_mb:.0f} MB · ⏱ {dur}")
    lines.append(f"🔗 {url}")
    return "\n".join(lines)


# =====================================================================  the job
LOGIN_COOKIES = {"LOGIN_INFO", "SAPISID", "APISID", "SID", "HSID", "SSID", "__Secure-1PSID", "__Secure-3PSID",
                 "__Secure-1PAPISID", "__Secure-3PAPISID"}


def cookie_report(path: str) -> dict:
    """Looks inside a Netscape cookies file WITHOUT revealing any value: counts, and the NAMES of login cookies."""
    rep = {"exists": False, "lines": 0, "valid": 0, "youtube": 0, "login": [], "expired": 0}
    if not path or not Path(path).is_file():
        return rep
    rep["exists"] = True
    now, active, expired = time.time(), set(), set()
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        rep["lines"] += 1
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        rep["valid"] += 1
        domain = parts[0].replace("#HttpOnly_", "").lstrip(".").lower()
        if domain.endswith("youtube.com"):
            rep["youtube"] += 1
        if parts[5] in LOGIN_COOKIES and (domain.endswith("youtube.com") or domain.endswith("google.com")):
            try:
                exp = int(float(parts[4]))
            except ValueError:
                exp = 0
            (expired if 0 < exp < now else active).add(parts[5])
    rep["login"] = sorted(active)
    rep["expired"] = len(expired - active)
    return rep


def cookie_diagnosis(rep: dict) -> tuple:
    """-> (what is wrong, what to do) in Persian."""
    fix = "\n\nبعد Secret زیر را در گیت‌هاب عوض کن:\nYOUTUBE_COOKIES"
    if not rep["exists"]:
        return "🔑 هنوز کوکی یوتیوب ثبت نشده است.", "Secret زیر را در گیت‌هاب بساز:\nYOUTUBE_COOKIES"
    if rep["valid"] == 0:
        return ("🔑 فایل کوکی فرمت درست ندارد. باید فرمت Netscape باشد و بین ستون‌ها کاراکتر Tab داشته باشد.",
                "دوباره Export کن و دکمهٔ Copy خود افزونه را بزن. از جای دیگری (مثل یادداشت‌ها) کپی نکن، چون Tabها خراب می‌شود." + fix)
    if rep["youtube"] == 0:
        return ("🔑 در فایل هیچ کوکی youtube.com نیست.",
                "کوکی را وقتی در صفحهٔ youtube.com/robots.txt هستی Export کن." + fix)
    if not rep["login"] and rep["expired"]:
        return "🔑 همهٔ کوکی‌های ورود منقضی شده‌اند.", "یک کوکی تازه بگیر." + fix
    if not rep["login"]:
        return ("🔑 این کوکی‌ها مال یک نشست «وارد نشده» هستند و نشانی از ورود به حساب ندارند.",
                "داخل تب خصوصی اول وارد یوتیوب شو و مطمئن شو عکس پروفایل بالای صفحه دیده می‌شود. بعد robots.txt را باز کن و Export بگیر." + fix)
    return ("🔑 کوکی ظاهراً سالم و واردشده است، ولی یوتیوب باز هم از این سرور قبولش نکرد.",
            "چند دلیل ممکن است: حساب فرعی خیلی تازه است یا یوتیوب آن را مشکوک می‌داند، یا بعد از Export همان تب خصوصی را دوباره باز کرده‌ای، "
            "یا IP این سرور فعلاً مسدود است.\n\nیک ساعت بعد دوباره امتحان کن. اگر نشد، با یک حساب فرعی قدیمی‌تر که قبلاً با آن ویدیو دیده‌ای کوکی تازه بگیر.")


def cookie_block(rep: dict) -> str:
    if not rep["exists"]:
        return ""
    login = ", ".join(rep["login"]) if rep["login"] else "ندارد"
    return ("🍪 <b>گزارش کوکی</b>\n\nتعداد کوکی‌ها:\n%d\n\nکوکی‌های youtube.com:\n%d\n\nنشست ورود:\n%s"
            % (rep["valid"], rep["youtube"], html.escape(login)))


def friendly_error(ex: Exception, cookies=False) -> str:
    """`cookies` is either a bool (cookie file present?) or the dict from cookie_report()."""
    t = str(ex)
    low = t.lower()
    if is_bot_check(ex):
        if isinstance(cookies, dict):
            why, todo = cookie_diagnosis(cookies)
            return f"یوتیوب درخواست را ربات تشخیص داد.\n\n{why}\n\n{todo}"
        why = ("🔑 کوکی ثبت‌شده را یوتیوب قبول نکرد. یا منقضی شده یا هنگام چسباندن ناقص شده است."
               if cookies else "🔑 هنوز کوکی یوتیوب ثبت نشده است.")
        return (f"یوتیوب درخواست را ربات تشخیص داد.\n\n{why}\n\n"
                "یک کوکی تازه با روش پنجرهٔ خصوصی بگیر و این Secret را در گیت‌هاب بساز یا جایگزین کن:\nYOUTUBE_COOKIES")
    if "private video" in low:
        return "این ویدئو خصوصی است."
    if "members-only" in low or "members only" in low or "join this channel" in low:
        return "این ویدئو مخصوص اعضای کانال است."
    if "video unavailable" in low or "this video is not available" in low:
        return "ویدئو در دسترس نیست (حذف شده یا محدودیت منطقه‌ای)."
    if "live event" in low or "is live" in low:
        return "پخش زندهٔ در جریان پشتیبانی نمی‌شود؛ بعد از پایان دوباره امتحان کن."
    if is_rate_limit(ex):
        return ("یوتیوب موقتاً درخواست‌های این سرور را محدود کرده (429).\n\n"
                "ربات خودش چند بار با فاصله دوباره تلاش کرد و نشد.\n\n"
                "چند دقیقه صبر کن و دوباره امتحان کن. هر اجرا معمولاً روی یک سرور تازه انجام می‌شود، پس ممکن است بار بعد همان لحظه جواب بدهد.")
    if "requested format is not available" in low:
        return "فرمت ویدئو در دسترس نیست؛ احتمالاً کوکی یا PO-Token مشکل دارد."
    return re.sub(r"\x1b\[[0-9;]*m", "", t)[:350]


def explain_error(ex: Exception, rep: dict, tech: list) -> str:
    """The full Telegram text for a failed job: what happened, a cookie report when relevant, a few technical lines."""
    low = str(ex).lower()
    body = friendly_error(ex, rep)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", str(ex))[:350]
    unknown = body == plain
    parts = ["❌ <b>خطا</b>", html.escape(body)]
    if is_bot_check(ex) and cookie_block(rep):
        parts.append(cookie_block(rep))
    if any("impersonat" in line.lower() for line in tech):
        parts.append("⚠️ <b>ابزار شبیه‌سازی مرورگر (curl_cffi) نصب نیست.</b>\n\n"
                     "بدون آن یوتیوب دانلود زیرنویس را با خطای 429 رد می‌کند. فایل download.yml در گیت‌هاب را به آخرین نسخه به‌روز کن.")
    if tech and (is_bot_check(ex) or is_rate_limit(ex) or unknown or "requested format" in low):
        parts.append("🔧 <b>جزئیات فنی</b>\n<pre>" + html.escape("\n".join(tech)) + "</pre>")
    return "\n\n".join(parts)


def slug(title: str, vid: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:60]
    return f"{s or 'video'}-{vid}"


def bar(frac: float, width: int = 10) -> str:
    n = max(0, min(width, int(frac * width)))
    return "▰" * n + "▱" * (width - n) + f" {int(frac * 100)}%"


class Progress:
    """Edits one Telegram message, throttled so we never hit rate limits."""

    def __init__(self, tg: Telegram, chat, msg_id, header: str = ""):
        self.tg, self.chat, self.msg_id, self.header = tg, chat, msg_id, header
        self._last, self._last_text = 0.0, ""

    def text(self, body: str, force: bool = True):
        txt = f"{self.header}\n\n{body}" if self.header else body
        now = time.time()
        if txt == self._last_text or (not force and now - self._last < 12):
            return
        self._last, self._last_text = now, txt
        try:
            self.tg.edit_message(self.chat, self.msg_id, txt)
        except Exception:                                           # noqa: BLE001
            pass

    def frac(self, label: str, f: float):
        self.text(f"{label}\n{bar(f)}", force=False)


# =====================================================================  the bilingual subtitle page (HTML)
def page_partner(src_lang: str) -> str:
    """The language that goes next to the spoken one: Persian for every language, English for a Persian video."""
    return "en" if base_lang(src_lang) == "fa" else "fa"


AUDIO_BUDGET = 20_000_000          # bytes of sound inside one page (the page grows by a third: base64)


def make_audio(video: Path, stem: Path, duration: float) -> Optional[dict]:
    """A small mono sound file of the video for the page (speech quality). -> {"path","mime","name"} or None.
    MP3 first (plays everywhere), AAC if this ffmpeg has no MP3 encoder. Too long for the budget (< 16 kbps) -> None."""
    dur = max(float(duration or 0), 1.0)
    kbps = int(min(40, AUDIO_BUDGET * 8 / dur / 1000))
    if kbps < 16:
        return None
    for codec, ext, mime in (("libmp3lame", "mp3", "audio/mpeg"), ("aac", "m4a", "audio/mp4")):
        out = stem.parent / f"{stem.name}.{ext}"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vn", "-sn", "-dn", "-ac", "1", "-ar", "22050",
               "-c:a", codec, "-b:a", f"{kbps}k"] + (["-movflags", "+faststart"] if ext == "m4a" else []) + [str(out)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except Exception as ex:                                     # noqa: BLE001
            print(f"audio for the page: {ex!r}")
            return None
        if r.returncode == 0 and out.is_file() and 1000 < out.stat().st_size <= AUDIO_BUDGET * 1.2:
            return {"path": out, "mime": mime, "name": f"{stem.name}.{ext}"}
        print(f"audio for the page ({codec}) failed: {clean_reason(r.stderr, 200)}")
    return None


def make_page(cfg: "Cfg", out: Path, *, title: str, src_cues: list, src_lang: str, how_src: str = "", channel: str = "",
              vid: str = "", url: str = "", duration: float = 0.0, ctx: Optional[dict] = None, ready=(), fetch=None,
              prog=None, audio: Optional[dict] = None) -> tuple:
    """Writes the bilingual HTML page: every subtitle line with the same line in the other language under it.
    ready = translations this job already made [{"lang","cues","how","aligned","auto"}]  (aligned = same cues as src_cues)
    fetch(lang) = optional function that gets a YouTube track of that language -> (cues, how) or None.
    Never raises.  -> (written?, partner language that was added or "", notes)"""
    notes: list = []
    try:
        if not src_cues:
            return False, "", notes
        partner = page_partner(src_lang)
        ai_ok = bool(cfg.ai_key)
        have = next((r for r in ready if base_lang(r["lang"]) == partner and r.get("cues")), None)
        pick = None                                                 # (cues, how, aligned)
        if have and (have.get("aligned") or not have.get("auto")):
            pick = (have["cues"], have.get("how", ""), bool(have.get("aligned")))
        if not pick and ai_ok:
            try:
                if prog:
                    prog.text(f"🌐 ساخت ترجمهٔ {LANG_NAMES.get(partner, partner)} برای صفحهٔ زیرنویس…")
                cues_b, bad = translate_cues(cfg, src_cues, src_lang, partner, ctx or {},
                                             (lambda b: prog.text(b, force=False)) if prog else None)
                pick = (cues_b, f"ترجمهٔ {AI_NAMES.get(cfg.ai_provider, '')}".strip(), True)
                if bad:
                    notes.append(f"⚠️ در صفحهٔ زیرنویس {bad} خط ترجمه نشد و به زبان اصلی ماند.")
            except AIError as ex:
                print(f"page translation failed: {ex}")
                notes.append("⚠️ ترجمهٔ صفحهٔ زیرنویس انجام نشد (" + html.escape(str(ex)[:120]) + ").")
        if not pick and have:
            pick = (have["cues"], have.get("how", ""), bool(have.get("aligned")))
        if not pick and fetch:
            try:
                got = fetch(partner)
            except Exception as ex:                                 # noqa: BLE001
                print(f"page: YouTube track failed: {ex}")
                got = None
            if got:
                pick = (got[0], got[1], False)
        rows = subpage.make_rows(src_cues, pick[0] if pick else None, aligned=pick[2] if pick else True)
        if not rows:
            return False, "", notes
        build = lambda a: subpage.build_page(rows, title=title, lang_a=src_lang, lang_b=partner if pick else "", how_a=how_src,   # noqa: E731
                                             how_b=pick[1] if pick else "", channel=channel, vid=vid, url=url,
                                             duration=duration, audio=a)
        text = build(audio)
        if audio and len(text.encode("utf-8")) > 45_000_000:        # Telegram's normal API takes 50 MB
            text, audio = build(None), None
            notes.append("ℹ️ صدای ویدیو برای قرار گرفتن در صفحه زیادی بزرگ بود؛ صفحه بدون صدا ساخته شد.")
        out.write_text(text, encoding="utf-8")
        if not pick:
            notes.append("ℹ️ صفحهٔ زیرنویس فقط با متن اصلی ساخته شد؛ ترجمه در دسترس نبود"
                         + ("" if ai_ok else " (برای ترجمه کلید هوش مصنوعی را در Secrets بساز)") + ".")
        make_page.last_audio = bool(audio)
        return True, partner if pick else "", notes
    except Exception as ex:                                         # noqa: BLE001 - the page is a bonus, the video is already delivered
        print(f"could not build the subtitle page: {ex!r}")
        notes.append("⚠️ صفحهٔ زیرنویس ساخته نشد.")
        return False, "", notes


def page_job(cfg: "Cfg", out: Path, **kw) -> tuple:
    """make_page + send it to the same chat as the video (a small file: the normal Bot API is enough).
    The sound of the video is put inside the page when `video=` is given (tap a line = play from there).
    -> (sent?, "English + فارسی", notes)"""
    video = kw.pop("video", None)
    audio = None
    if video:
        if kw.get("prog"):
            kw["prog"].text("🔊 ساخت صدای صفحهٔ زیرنویس…")
        audio = make_audio(Path(video), out.parent / (out.stem + "-audio"), kw.get("duration") or 0)
        if not audio:
            notes0 = ["ℹ️ صدا برای صفحهٔ زیرنویس ساخته نشد (ویدیو خیلی طولانی است یا صدا ندارد)."]
        else:
            notes0 = []
    else:
        notes0 = []
    make_page.last_audio = False
    ok, partner, notes = make_page(cfg, out, audio=audio, **kw)
    notes = notes0 + notes
    name = lambda c: LANG_NAMES.get(base_lang(c), c)               # noqa: E731
    label = f"{name(kw['src_lang'])} + {name(partner)}" if partner else f"{name(kw['src_lang'])} (فقط متن اصلی)"
    if not ok:
        return False, label, notes
    has_audio = bool(getattr(make_page, "last_audio", False))
    if has_audio:
        label += " + صدا"
    try:
        Telegram(cfg.bot_token).upload("sendDocument", "document", str(out), {
            "chat_id": cfg.storage_chat, "parse_mode": "HTML",
            "caption": f"🌐 <b>صفحهٔ زیرنویس</b>\n{html.escape(label)}\nفایل را در مرورگر گوشی باز کن؛ جستجو و تنظیم اندازهٔ متن دارد."
                       + ("\n🔊 روی هر جمله بزن تا صدا از همان‌جا پخش شود؛ دکمهٔ دانلود صدا هم بالای صفحه است." if has_audio else "")})
        return True, label, notes
    except Exception as ex:                                         # noqa: BLE001
        print(f"could not send the subtitle page: {ex}")
        notes.append("⚠️ صفحهٔ زیرنویس ساخته شد ولی ارسالش نشد.")
        return False, label, notes


def run_job(cfg: Cfg, tg: Telegram, job: dict) -> None:
    chat, mid, vid = job["chat_id"], job["msg_id"], job["vid"]
    langs, mode, height = job.get("langs") or [], job.get("mode", "n"), int(job.get("height") or 720)
    opts: Opts = job.get("opts") or Opts()
    src_spec = job.get("src") or ""
    url = f"https://www.youtube.com/watch?v={vid}"
    work = cfg.work_dir / vid
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    prog = Progress(tg, chat, mid)

    prog.text("🔎 دریافت اطلاعات ویدئو…")
    info = extract_info(cfg, url)
    if info.get("is_live"):
        raise RuntimeError("live event")
    title = info.get("title") or vid
    channel = info.get("channel") or info.get("uploader") or ""
    prog.header = f"🎬 <b>{html.escape(title[:120])}</b>"
    ai_ok = bool(cfg.ai_key)
    ai_name = AI_NAMES.get(cfg.ai_provider, "")
    notes: list = []

    # ---- which subtitle track is the SOURCE (the video's own language)
    srcpick = pick_for(info, src_spec) if src_spec else None
    if not srcpick or srcpick[1] == "auto-translated":
        srcpick = best_source(info) or srcpick

    # ---- what the person wants to see, and how to get it
    plan: list = []
    for spec in langs:
        code = spec.partition(":")[0]
        direct = srcpick if (srcpick and src_spec and base_lang(code) == base_lang(srcpick[0])) else pick_for(info, spec)
        if direct and direct[1] != "auto-translated":
            plan.append({"lang": code, "kind": "track", "key": direct[0], "src": direct[1]})
        elif ai_ok and srcpick:
            plan.append({"lang": code, "kind": "ai", "fallback": direct[0] if direct else None})
        elif direct:
            plan.append({"lang": code, "kind": "track", "key": direct[0], "src": "auto-translated"})
            notes.append("ℹ️ برای ترجمهٔ بهتر از گوگل، کلید ANTHROPIC_API_KEY (یا OPENAI_API_KEY) را در Secrets گیت‌هاب بساز.")
        else:
            notes.append(f"⚠️ برای این ویدئو زیرنویس «{code}» وجود ندارد.")
    dub_plan = None
    if opts.du:
        if not srcpick:
            notes.append("⚠️ دوبله ممکن نشد: ویدئو زیرنویس منبع ندارد.")
        elif base_lang(opts.du) == base_lang(srcpick[0]):
            notes.append("ℹ️ زبان دوبله همان زبان ویدئوست؛ دوبله انجام نشد.")
        elif base_lang(opts.du) not in VOICES:
            notes.append(f"⚠️ برای زبان «{opts.du}» صدای دوبله ندارم.")
        elif any(base_lang(p["lang"]) == base_lang(opts.du) for p in plan):
            dub_plan = "reuse"
        elif ai_ok:
            dub_plan = "ai"
        else:
            notes.append("⚠️ دوبله به ترجمهٔ هوش مصنوعی نیاز دارد؛ کلید ANTHROPIC_API_KEY را در Secrets گیت‌هاب بساز.")
    if not plan and mode != "n":
        mode = "n"

    keys = [p["key"] for p in plan if p["kind"] == "track"] + [p["fallback"] for p in plan if p.get("fallback")]
    if srcpick and (dub_plan == "ai" or any(p["kind"] == "ai" for p in plan) or (mode == "s" and any(p["kind"] == "ai" for p in plan))):
        keys.append(srcpick[0])
    if srcpick and dub_plan == "reuse" and not keys:
        keys.append(srcpick[0])
    keys = list(dict.fromkeys(keys))

    def hook(d):
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if tot:
                prog.frac("⬇️ دانلود از یوتیوب", d.get("downloaded_bytes", 0) / tot)

    prog.text("⬇️ دانلود از یوتیوب…")
    expected = float(info.get("duration") or 0)
    video, health = download_video(cfg, url, work, vid, height, expected, hook, prog.text)

    # ---- subtitles from YouTube: first attempt right away, failed ones (HTTP 429 ...) are retried with growing pauses
    have = collect_subs(work, vid, keys)
    for attempt, wait in enumerate((0,) + tuple(RETRY_WAITS)):
        missing = [k for k in keys if k not in have]
        if not missing:
            break
        prog.text((("💬 دریافت زیرنویس " if attempt == 0 else "💬 تلاش مجدد برای زیرنویس ") + ", ".join(missing) + "…"))
        if wait:
            time.sleep(wait)
        try:
            download_subs_only(cfg, url, work, missing)
        except Exception as ex:                                     # noqa: BLE001
            print(f"subtitle retry failed: {ex}")
        have = collect_subs(work, vid, keys)

    if srcpick and plan and mode in ("h", "s") and srcpick[0] not in have:          # the subtitle page needs the spoken-language track
        try:
            download_subs_only(cfg, url, work, [srcpick[0]])
        except Exception as ex:                                     # noqa: BLE001
            print(f"source track for the page failed: {ex}")
        have = collect_subs(work, vid, keys + [srcpick[0]])

    def load(key: str) -> list:
        return cues_from_vtt(have[key].read_text(encoding="utf-8", errors="replace")) if key in have else []

    meta = probe(video)
    duration = meta["duration"] or expected
    ctx = {"title": title, "channel": channel, "desc": re.sub(r"\s+", " ", info.get("description") or "")[:500]}
    src_cues = load(srcpick[0]) if srcpick else []
    src_lang = srcpick[0].split("-")[0] if srcpick else ""
    results: list = []                                           # {"lang","cues","how"}
    for p in plan:
        cues, how = [], ""
        if p["kind"] == "track":
            cues = load(p["key"])
            how = SOURCE_LABELS.get(p["src"], p["src"])
            if not cues:
                notes.append(f"⚠️ زیرنویس «{p['lang']}» از یوتیوب دریافت نشد.")
        else:
            if src_cues:
                try:
                    cues, bad = translate_cues(cfg, src_cues, src_lang, p["lang"], ctx, lambda b: prog.text(b, force=False))
                    how = f"ترجمهٔ {ai_name}"
                    if bad:
                        notes.append(f"⚠️ {bad} خط ترجمه نشد و به زبان اصلی ماند.")
                except AIError as ex:
                    print(f"AI translation failed: {ex}")
                    notes.append("⚠️ ترجمهٔ هوش مصنوعی انجام نشد (" + html.escape(str(ex)[:160]) + ").")
                    if p.get("fallback") and load(p["fallback"]):
                        cues, how = load(p["fallback"]), "ترجمهٔ خودکار یوتیوب"
            else:
                notes.append("⚠️ زیرنویس منبع برای ترجمه دریافت نشد.")
        if cues:
            results.append({"lang": p["lang"], "cues": cues, "how": how, "kind": p["kind"], "src": p.get("src", "")})

    # ---- subtitles -> ASS (burned in) / SRT (embedded)
    hard, soft, desc = None, [], []
    if mode in ("h", "s"):
        for i, r in enumerate(results):
            name = LANG_NAMES.get(base_lang(r["lang"]), r["lang"])
            as_hard = mode == "h" and i == 0
            if as_hard:
                ass = work / "hard.ass"
                ass.write_text(build_ass(r["cues"], r["lang"], opts, meta["width"], meta["height"]), encoding="utf-8")
                hard = ass
            else:
                path = work / f"sub_{i}.srt"
                path.write_text(to_srt(r["cues"], r["lang"], marks=True), encoding="utf-8")
                soft.append((path, r["lang"]))
            desc.append(f"{name} ({'روی تصویر' if as_hard else 'داخل فایل'}، {r['how']})")
        if mode == "s" and src_cues and any(r["kind"] == "ai" for r in results):
            path = work / "sub_orig.srt"
            path.write_text(to_srt(src_cues, src_lang, marks=True), encoding="utf-8")
            soft.append((path, src_lang))
            desc.append(f"{LANG_NAMES.get(src_lang, src_lang)} (داخل فایل، اصلی)")

    # ---- dubbing
    dub_audio = None
    if dub_plan:
        try:
            if dub_plan == "reuse":
                dcues = next((r["cues"] for r in results if base_lang(r["lang"]) == base_lang(opts.du)), None)
                if not dcues:
                    raise RuntimeError("ترجمه برای ساخت دوبله در دسترس نیست")
            else:
                dcues, bad = translate_cues(cfg, src_cues, src_lang, opts.du, ctx, lambda b: prog.text(b, force=False))
            prog.text("🎙 ساخت صدای دوبله…")
            dub_audio, dn = build_dub_audio(dcues, opts.du, opts, video, duration, work, lambda b: prog.text(b, force=False))
            notes += dn
            desc.append(f"🎙 دوبله {LANG_NAMES.get(base_lang(opts.du), opts.du)} ({'صدای زن' if opts.vc == 'f' else 'صدای مرد'})")
        except Exception as ex:                                     # noqa: BLE001
            print(f"dubbing failed: {ex!r}")
            notes.append("⚠️ دوبله انجام نشد (" + html.escape((str(ex) or type(ex).__name__)[:160]) + "). ویدیو بدون دوبله ارسال شد.")

    final = work / f"{slug(title, vid)}.mp4"
    label = "🎬 ساخت ویدیو با زیرنویس روی تصویر" if hard else "🎬 ساخت فایل نهایی"
    prog.text(label)
    render(video, final, hard=hard, soft=soft, duration=duration, on_progress=lambda f: prog.frac(label, f),
           dub_audio=dub_audio, dub_lang=opts.du)
    prog.text("🔍 بررسی فایل نهایی…")
    fin = check_media(final, duration)
    if not fin["ok"]:
        raise RuntimeError("فایل نهایی سالم نبود: " + "؛ ".join(fin["problems"]))

    size_mb = final.stat().st_size / 1_000_000
    prog.text(f"⬆️ آپلود در تلگرام ({size_mb:.0f} MB)…")
    thumb = thumbnail(final, work / "thumb.jpg", duration)
    caption = build_caption(title, channel, url, " + ".join(desc), size_mb, meta["height"] or height, duration)
    sent = deliver(cfg, final, caption, thumb, duration, progress=prog.text)

    # ---- the bilingual subtitle page (HTML): the spoken language + Persian (English for a Persian video)
    page_sent, page_label = False, ""
    if mode in ("h", "s") and results and src_cues:
        prog.text("📄 ساخت صفحهٔ زیرنویس دوزبانه…")
        ready = [{"lang": r["lang"], "cues": r["cues"], "how": r["how"], "aligned": r["kind"] == "ai",
                  "auto": r["kind"] == "track" and r.get("src") == "auto-translated"} for r in results]

        def fetch(lang: str):
            pk = pick_for(info, lang)
            if not pk:
                return None
            download_subs_only(cfg, url, work, [pk[0]])
            got = collect_subs(work, vid, [pk[0]])
            cues = cues_from_vtt(got[pk[0]].read_text(encoding="utf-8", errors="replace")) if pk[0] in got else []
            return (cues, SOURCE_LABELS.get(pk[1], pk[1])) if cues else None

        page_sent, page_label, pnotes = page_job(
            cfg, work / f"{slug(title, vid)}.html", title=title, src_cues=src_cues, src_lang=src_lang,
            how_src=SOURCE_LABELS.get(srcpick[1], srcpick[1]) if srcpick else "", channel=channel, vid=vid, url=url,
            duration=duration, ctx=ctx, ready=ready, fetch=fetch, prog=prog, video=video)
        notes += pnotes

    if cfg.fallback_used:
        notes.append("⚠️ سرور ۲ گیگابایتی در دسترس نبود؛ فایل با محدودیت ۵۰ مگابایت ارسال شد (ممکن است تکه‌تکه باشد)."
                     + (f"\nدلیل: <code>{html.escape(cfg.fallback_reason)}</code>" if cfg.fallback_reason else ""))
    links = [l for l in (message_link(m) for m in sent) if l]
    done = f"✅ <b>آپلود شد</b>\n\n🎬 <b>عنوان</b>\n{html.escape(title[:150])}"
    if desc:
        done += "\n\n💬 <b>زیرنویس و صدا</b>\n" + "\n".join(html.escape(d) for d in desc)
    if page_sent:
        done += f"\n\n🌐 <b>صفحهٔ زیرنویس</b>\n{html.escape(page_label)} (فایل HTML برای خواندن و جستجو)"
    if links:
        done += "\n\n🔗 <b>لینک پیام</b>\n" + ("\n".join(links) if len(links) == 1 else " ".join(f'<a href="{l}">{i}</a>' for i, l in enumerate(links, 1)))
    else:
        done += "\n\n📥 فایل در همین چت ارسال شد."
    mm = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"              # noqa: E731
    done += f"\n\n🔍 <b>بررسی سلامت</b>\nتصویر {mm(fin['video'])} · صدا {mm(fin['audio'])} · طول یوتیوب {mm(expected)} ✅"
    if notes:
        done += "\n\n" + "\n\n".join(dict.fromkeys(notes))
    prog.header = ""
    prog.text("✅ <b>آپلود شد</b>")
    try:
        tg.send_message(chat, done)          # a NEW message, so the phone gives a notification
    except Exception as ex:                  # noqa: BLE001
        print(f"could not send the final message: {ex}")
    shutil.rmtree(work, ignore_errors=True)


# =====================================================================  inspect: list subtitles as buttons
def track_label(code: str) -> str:
    return LANG_NAMES.get(base_lang(code), code)


def track_labels(keys: list) -> dict:
    """key -> button text; when two tracks have the same language the (short) code tells them apart."""
    count = {}
    for k in keys:
        count[base_lang(k)] = count.get(base_lang(k), 0) + 1
    return {k: track_label(k) + (f" ({k[:12]})" if count[base_lang(k)] > 1 and "-" in k else "") for k in keys}


def inspect_job(cfg: Cfg, tg: Telegram, job: dict) -> None:
    chat, mid, vid = job["chat_id"], job["msg_id"], job["vid"]
    info = extract_info(cfg, f"https://www.youtube.com/watch?v={vid}")
    if info.get("is_live"):
        raise RuntimeError("live event")
    title = info.get("title") or vid
    subs = info.get("subtitles") or {}
    caps = info.get("automatic_captions") or {}

    own = [k for k in sorted(subs) if subs[k] and k not in SKIP_KEYS][:8]
    autos = [(k[:-5], "orig") for k in sorted(caps) if k.endswith("-orig") and caps[k]]
    orig_codes = {c for c, _ in autos}
    for lang in job.get("langs") or []:                       # YouTube can machine-translate into these
        if lang not in orig_codes and pick_track(info, lang, only="auto"):
            autos.append((lang, "tr"))
    autos = autos[:6]

    rows = []
    best = best_source(info)                                  # the video's own language: translate FROM this track
    if best:
        key, how = best
        spec = f"{key}:manual" if how == "manual" else f"{key[:-5] if key.endswith('-orig') else key}:auto"
        top = [{"text": "🇮🇷 فارسی (ترجمهٔ AI)", "callback_data": f"q|{vid}|{spec}|fa.x.y.t.c.-.f.s"}]
        if base_lang(key) != "en":
            top.append({"text": "🇬🇧 English", "callback_data": f"q|{vid}|{spec}|en.x.y.t.c.-.f.s"})
        rows.append(top)
    own_names = track_labels(own)
    rows += [[{"text": f"📝 {own_names[k]} · خود ویدیو", "callback_data": f"tr|{vid}|{k}|m"}] for k in own]
    for code, kind in autos:
        tail = "خودکار" if kind == "orig" else "ترجمهٔ خودکار"
        rows.append([{"text": f"🤖 {track_label(code)} · {tail}", "callback_data": f"tr|{vid}|{code}|a"}])
    rows.append([{"text": "🎞 فقط ویدیو (بدون زیرنویس)", "callback_data": f"dl|{vid}|-|n"}])

    def lines(items) -> str:
        return "\n".join(items) if items else "ندارد"

    head = f"🎬 <b>عنوان</b>\n{html.escape(title[:150])}\n\n"
    if own or autos:
        text = (head + ("🇮🇷 <b>فارسی با هوش مصنوعی ترجمه می‌شود: دکمهٔ اول را بزن</b>\n\n" if best else "")
                + "📝 <b>زیرنویس‌های خود ویدیو</b>\n" + lines([f"• {own_names[k]}" for k in own]) + "\n\n"
                + "🤖 <b>زیرنویس‌های خودکار یوتیوب</b>\n"
                + lines([f"• {track_label(c)} ({'خودکار' if kind == 'orig' else 'ترجمهٔ خودکار'})" for c, kind in autos]) + "\n\n"
                + "👇 <b>لطفاً یکی را انتخاب کنید</b>\n\n"
                + "📝 یعنی زیرنویسی که سازندهٔ ویدیو گذاشته است\n\n"
                + "🤖 یعنی زیرنویسی که خود یوتیوب ساخته است")
    else:
        text = head + "⚠️ این ویدیو هیچ زیرنویسی ندارد.\n\nمی‌توانی آن را بدون زیرنویس دانلود کنی."
    tg.edit_message(chat, mid, text, reply_markup={"inline_keyboard": rows})


def main() -> int:
    cfg = Cfg.from_env()
    ai_setup(cfg)
    e = lambda k: (os.environ.get(k) or "").strip()                 # noqa: E731
    vid = e("VIDEO_ID")
    if not cfg.bot_token or not re.fullmatch(r"[\w-]{11}", vid) or not e("CHAT_ID") or not e("MSG_ID").isdigit():
        print("BOT_TOKEN / VIDEO_ID / CHAT_ID / MSG_ID are missing or invalid")
        return 1
    action = e("ACTION") if e("ACTION") in ("inspect", "download") else "download"
    langs = [x for x in re.split(r"[+,\s]+", e("LANGS")) if x and SPEC_RE.match(x)]
    opts = Opts.parse(e("OPTS"))
    src = e("SRC") if SPEC_RE.match(e("SRC")) else ""
    mode = e("MODE") if e("MODE") in ("h", "s", "n") else "n"
    height = int(e("HEIGHT")) if e("HEIGHT").isdigit() else 720
    job = {"chat_id": e("CHAT_ID"), "msg_id": int(e("MSG_ID")), "vid": vid, "langs": langs, "src": src, "opts": opts,
           "mode": mode if langs else "n", "height": min(max(height, 144), 2160)}
    tg = Telegram(cfg.bot_token)
    try:
        (inspect_job if action == "inspect" else run_job)(cfg, tg, job)
        return 0
    except Exception as ex:                                         # noqa: BLE001
        print(f"JOB FAILED: {ex!r}")
        try:
            rpt = cookie_report(cfg.cookies_file or e("YOUTUBE_COOKIES_FILE"))
            tg.edit_message(job["chat_id"], job["msg_id"], explain_error(ex, rpt, LOG.tail(4)))
        except Exception:                                           # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
