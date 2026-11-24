#!/usr/bin/env python3
"""
Worker: add subtitles to a video that the person SENT to the Telegram bot (no YouTube involved).

Runs inside GitHub Actions (see .github/workflows/upload.yml). The Telegram bot "brain" (Google Apps Script)
starts it with: CHAT_ID, MSG_ID, FILE_ID, FILE_NAME, SRT_ID, ENGINE, TARGET, MODE, OPTS, STORAGE_CHAT.

ENGINE = "ai:<base|small|medium>:<auto|en|fa|...>"   speech recognition (faster-whisper, free, runs on the runner)
         "srt"                                          the person's own .srt / .vtt file (SRT_ID)
TARGET = "-" (keep the spoken language) or a language code (translated with the AI key from the Secrets)
MODE   = "h" burn into the picture, "s" embed as subtitle tracks

This file only IMPORTS helpers from download.py (rendering, translation, upload ...); download.py is not changed.
"""
import bisect
import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from download import (
    AI_NAMES, AIError, Cfg, FFmpegError, LANG_NAMES, LANG_RE, Opts, Progress, Telegram, TelegramError,
    CLOUD_API, ai_setup, base_lang, build_ass, check_media, clean_reason, cues_from_vtt, deliver, fix_fa, message_link,
    page_job, probe, render, thumbnail, tidy_timings, to_srt, translate_cues,
)

FILE_ID_RE = re.compile(r"^[\w-]{10,250}$")
WHISPER_SIZES = ("base", "small", "medium")
CLOUD_DOWNLOAD_LIMIT = 20 * 1024 * 1024          # getFile on the normal Bot API stops at 20 MB


class UserFacing(Exception):
    """An error whose text can be shown to the person as it is."""


def scrub(text: str, cfg: Cfg) -> str:
    """Never let the bot token reach a log or a Telegram message."""
    if cfg.bot_token:
        text = text.replace(cfg.bot_token, "***")
    return text


# =====================================================================  getting the files from Telegram
def _curl_download(url: str, dest: Path, cfg: Cfg) -> None:
    p = subprocess.run(["curl", "-sS", "-L", "--fail", "--max-time", "7200", "-o", str(dest), url],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("download failed: " + scrub(p.stderr.strip()[-200:], cfg))


def _save_local(cfg: Cfg, local: Telegram, file_path: str, dest: Path) -> None:
    """The local Bot API server answers getFile with a path on ITS disk (inside the docker container)."""
    if file_path.startswith("/"):
        if Path(file_path).is_file():
            shutil.copyfile(file_path, dest)
        else:
            p = subprocess.run(["docker", "cp", f"tgapi:{file_path}", str(dest)], capture_output=True, text=True)
            if p.returncode != 0:
                raise RuntimeError("docker cp failed: " + scrub(p.stderr.strip()[-200:], cfg))
    else:
        _curl_download(f"{local.base}/file/bot{cfg.bot_token}/{file_path}", dest, cfg)


def fetch_file(cfg: Cfg, file_id: str, dest: Path) -> Path:
    """Local Bot API server first (any size up to 2 GB), the normal API second (max 20 MB)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if cfg.local_api:
        local = Telegram(cfg.bot_token, base=cfg.local_api)
        if local.reachable(tries=5, pause=4):
            try:
                info = local.call("getFile", file_id=file_id)
                _save_local(cfg, local, info["file_path"], dest)
                if dest.is_file() and dest.stat().st_size > 0:
                    return dest
            except Exception as ex:                                 # noqa: BLE001
                print("download through the local server failed: " + scrub(repr(ex)[:200], cfg))
        else:
            print(f"local Bot API server not reachable: {local.last_error} {clean_reason(os.environ.get('TG_LOCAL_LOG', ''), 200)}")
    try:
        info = Telegram(cfg.bot_token).call("getFile", file_id=file_id)
    except TelegramError as ex:
        if "too big" in str(ex).lower():
            raise UserFacing(
                "این فایل بزرگ‌تر از ۲۰ مگابایت است و تلگرام اجازهٔ دریافتش را به ربات معمولی نمی‌دهد.\n\n"
                "راه‌حل: در گیت‌هاب (Settings ← Secrets and variables ← Actions) دو Secret بساز:\n"
                "<code>TELEGRAM_API_ID</code> و <code>TELEGRAM_API_HASH</code>\n"
                "(از my.telegram.org ← API development tools). بعد از آن فایل تا ۲ گیگابایت قبول می‌شود.\n\n"
                "یا فایل را اول با میانبر «کم کردن حجم» در ترموکس کوچک کن و دوباره بفرست.") from ex
        raise
    _curl_download(f"{CLOUD_API}/file/bot{cfg.bot_token}/{info['file_path']}", dest, cfg)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError("the downloaded file is empty")
    return dest


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1256", "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", "replace")


def guess_lang(text: str) -> str:
    """Rough guess of the language of a subtitle file (only used to tell the translator where the text comes from)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    n = len(letters)

    def share(lo: str, hi: str) -> float:
        return sum(1 for c in letters if lo <= c <= hi) / n

    if share("\u0600", "\u06ff") > 0.4:
        return "fa" if re.search(r"[پچژگکی]", text) else "ar"
    if share("\u0400", "\u04ff") > 0.4:
        return "ru"
    if share("\u3040", "\u30ff") > 0.2:
        return "ja"
    if share("\uac00", "\ud7af") > 0.3:
        return "ko"
    if share("\u4e00", "\u9fff") > 0.3:
        return "zh"
    if share("\u0900", "\u097f") > 0.4:
        return "hi"
    return "en"


# =====================================================================  speech recognition
_END = re.compile(r"[.!?؟…。]$")
_SOFT = re.compile(r"[,،;:؛]$")


def group_words(words: list, max_chars: int = 58, max_dur: float = 6.0, gap: float = 0.8) -> list:
    """[(start, end, word)] -> subtitle cues [(start, end, [text])] of a readable length."""
    cues: list = []
    cur: list = []

    def flush() -> None:
        text = "".join(w for _, _, w in cur).strip()
        if text:
            cues.append((cur[0][0], cur[-1][1], [re.sub(r"\s+", " ", text)]))

    for s, e, w in words:
        if cur:
            text = "".join(x[2] for x in cur).strip()
            if (s - cur[-1][1] > gap or len(text) + len(w.strip()) + 1 > max_chars or e - cur[0][0] > max_dur
                    or (_END.search(text) and len(text) >= 18) or (_SOFT.search(text) and len(text) >= 38)):
                flush()
                cur = []
        cur.append((s, e, w))
    if cur:
        flush()
    # the model sometimes repeats one phrase over silence: keep only the first of 3+ identical neighbours
    out: list = []
    for c in cues:
        if len(out) >= 2 and c[2] == out[-1][2] == out[-2][2]:
            continue
        out.append(c)
    return tidy_timings(out)


LEVEL_FILTER = "highpass=f=60,dynaudnorm=f=200:g=15:m=12"      # quiet parts get louder: the model (and its VAD) miss quiet speech


def extract_audio(video: Path, out: Path) -> None:
    """16 kHz mono WAV for the speech model. A phone video with a quiet voice is the usual reason for missing subtitles,
    so the loudness is evened out first; if ffmpeg cannot do that, the plain audio is used."""
    base = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(video), "-map", "0:a:0", "-vn"]
    tail = ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)]
    p = subprocess.run(base + ["-af", LEVEL_FILTER] + tail, capture_output=True, text=True)
    if p.returncode != 0 or not out.is_file() or out.stat().st_size < 1000:
        print(f"audio levelling failed, using the plain audio: {p.stderr.strip()[-200:]}")
        p = subprocess.run(base + tail, capture_output=True, text=True)
    if p.returncode != 0 or not out.is_file() or out.stat().st_size < 1000:
        raise UserFacing("این ویدیو صدایی ندارد که بشود از آن زیرنویس ساخت.")


def load_samples(wav: Path):
    """The 16 kHz mono WAV made by extract_audio -> float32 numpy array.

    faster-whisper accepts an array instead of a file name. With an array it never opens the file through
    PyAV, so a PyAV update can no longer break the speech recognition (PyAV 19 did exactly that:
    "open() got an unexpected keyword argument 'metadata_errors'")."""
    import wave

    import numpy as np
    with wave.open(str(wav), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
            raise ValueError("the WAV is not 16 kHz mono 16-bit")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


SR = 16000
ARABIC_SCRIPT = {"fa", "ar", "ur", "ps", "ku", "sd", "ug"}


def reconcile_lang(lang: str, text: str) -> str:
    """The language the model announced can disagree with the text it wrote: an English speaker with a Persian accent
    called Persian, or the wrong language picked by hand. The letters of the text decide."""
    ar = sum(1 for c in text if "\u0600" <= c <= "\u06ff" or "\u0750" <= c <= "\u077f")
    la = sum(1 for c in text if c.isascii() and c.isalpha())
    if ar + la < 30:
        return lang
    if base_lang(lang) in ARABIC_SCRIPT and la > 0.85 * (ar + la):
        return "en"
    if base_lang(lang) not in ARABIC_SCRIPT and ar > 0.85 * (ar + la):
        return "fa" if re.search(r"[پچژگکی]", text) else "ar"
    return lang


def activity(samples, win: float = 0.5):
    """Loudness (RMS) of every `win` seconds of the audio -> numpy array."""
    import numpy as np
    n = int(win * SR)
    k = len(samples) // n
    if k <= 0:
        return np.zeros(0)
    x = np.asarray(samples[: k * n], dtype=np.float64).reshape(k, n)
    return np.sqrt((x * x).mean(axis=1))


def active_runs(rms, win: float = 0.5, join: float = 1.5, min_len: float = 1.0) -> list:
    """Stretches (start, end) in seconds where the sound is clearly louder than the background = probably speech."""
    import numpy as np
    if len(rms) == 0:
        return []
    thr = max(0.004, 0.2 * float(np.percentile(rms, 90)))
    runs, start, last = [], None, -1
    for i, on in enumerate(rms > thr):
        if on:
            if start is None:
                start = i
            last = i
        elif start is not None and (i - last) * win >= join:
            runs.append((start * win, (last + 1) * win))
            start = None
    if start is not None:
        runs.append((start * win, (last + 1) * win))
    return [(a, b) for a, b in runs if b - a >= min_len]


def uncovered(runs: list, cues: list, margin: float = 0.3, min_len: float = 1.5) -> list:
    """The parts of the speech-like stretches that no subtitle covers -> [(start, end)]."""
    cov = sorted((c[0] - margin, c[1] + margin) for c in cues)
    out = []
    for a, b in runs:
        cur = a
        for s0, e0 in cov:
            if e0 <= cur:
                continue
            if s0 >= b:
                break
            if s0 > cur:
                out.append((cur, min(s0, b)))
            cur = max(cur, e0)
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    return [(a, b) for a, b in out if b - a >= min_len]


def is_noise(seg) -> bool:
    """Noise, music or a repeated loop that the model wrote down as if it were speech."""
    return ((getattr(seg, "no_speech_prob", 0.0) > 0.8 and getattr(seg, "avg_logprob", 0.0) < -1.2)
            or getattr(seg, "compression_ratio", 0.0) > 2.6)


def recover_gaps(model, samples, cues: list, lang: str, main_lang: str, duration: float, prog: Progress) -> tuple:
    """Second pass over what sounds like speech but got no subtitle (a quiet voice, a window the model skipped,
    a switch of language). There is no VAD here, so nothing is thrown away before the model has heard it.
    -> (new words, seconds recovered, languages heard)"""
    gaps = [(max(0.0, a - 0.25), min(duration, b + 0.25)) for a, b in uncovered(active_runs(activity(samples)), cues)]
    starts = sorted(c[0] for c in cues)
    ends = {c[0]: c[1] for c in cues}
    budget, spent, words, heard = max(600.0, duration * 0.5), 0.0, [], set()
    print(f"second pass: {len(gaps)} stretch(es) without subtitles, {sum(b - a for a, b in gaps):.0f}s")

    def inside(t: float) -> bool:
        i = bisect.bisect_right(starts, t) - 1
        return i >= 0 and t <= ends[starts[i]] + 0.1

    for n, (a, b) in enumerate(gaps, 1):
        if spent + (b - a) > budget:
            print("second pass: time budget used up")
            break
        spent += b - a
        prog.frac("🔁 بررسی دوبارهٔ بخش‌های بی‌زیرنویس", n / len(gaps))
        seg_lang = None if lang in ("", "auto") and (b - a) >= 4.0 else (main_lang if lang in ("", "auto") else lang)
        segs, info = model.transcribe(
            samples[int(a * SR):int(b * SR)], language=seg_lang, task="transcribe", beam_size=3, vad_filter=False,
            word_timestamps=True, condition_on_previous_text=False, no_speech_threshold=None, log_prob_threshold=None)
        if seg_lang is None:
            heard.add(info.language)
        for seg in segs:
            if is_noise(seg):
                continue
            ws = [(w.start + a, w.end + a, w.word) for w in (seg.words or [])]
            if not ws and seg.text.strip():
                ws = [(seg.start + a, seg.end + a, " " + seg.text.strip())]
            words += [w for w in ws if not inside((w[0] + w[1]) / 2)]
    return words, spent, heard


def transcribe(audio: Path, model_name: str, lang: str, duration: float, prog: Progress) -> tuple:
    """-> (cues, detected language, notes for the final message)"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as ex:
        raise RuntimeError("faster-whisper is not installed (check the install step in upload.yml)") from ex
    prog.text(f"🧠 آماده‌سازی مدل تشخیص گفتار ({model_name})…")
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 2)
    try:
        samples = load_samples(audio)
    except Exception as ex:                                         # noqa: BLE001
        print(f"could not read the WAV directly ({ex!r}); faster-whisper will open the file itself")
        samples = str(audio)
    segments, info = model.transcribe(
        samples, language=None if lang in ("", "auto") else lang, task="transcribe", beam_size=3,
        vad_filter=True, vad_parameters={"threshold": 0.4, "min_silence_duration_ms": 600, "speech_pad_ms": 500},
        word_timestamps=True, condition_on_previous_text=False,
        no_speech_threshold=None)      # the default skips a whole 30 s window it believes is silence: that was the hole in the middle
    print(f"language: {info.language} ({info.language_probability:.2f})")
    words: list = []
    for seg in segments:
        prog.frac("🎙 تشخیص گفتار", min(1.0, seg.end / max(duration, 1.0)))
        if is_noise(seg):
            continue
        ws = list(seg.words or [])
        if ws:
            words += [(w.start, w.end, w.word) for w in ws]
        elif seg.text.strip():
            words.append((seg.start, seg.end, " " + seg.text.strip()))

    notes: list = []
    if not isinstance(samples, str):
        try:
            cues = group_words(words)
            extra, secs, heard = recover_gaps(model, samples, cues, lang, info.language, duration, prog)
            if extra:
                words = sorted(words + extra, key=lambda w: w[0])
                notes.append(f"ℹ️ حدود {int(secs)} ثانیه از صدا که بار اول بی‌زیرنویس مانده بود، دوباره بررسی شد و زیرنویسش اضافه شد.")
            other = sorted(h for h in heard if h and base_lang(h) != base_lang(info.language))
            if extra and other:
                notes.append("🌐 در بخشی از ویدیو زبان دیگری هم شنیده شد (" + "، ".join(other) + ")؛ همان‌طور که گفته شده نوشته شد.")
        except Exception as ex:                                     # noqa: BLE001 - the first pass result is still good
            print(f"second pass failed: {ex!r}")
        try:
            left = uncovered(active_runs(activity(samples)), group_words(words), min_len=6.0)
            if left:
                notes.append("⚠️ در این بازه‌ها صدا هست ولی گفتاری تشخیص داده نشد (موسیقی، نویز یا صدای خیلی کم): "
                             + "، ".join(f"{mmss(a)} تا {mmss(b)}" for a, b in left[:4]))
        except Exception as ex:                                     # noqa: BLE001
            print(f"coverage check failed: {ex!r}")
    cues = group_words(words)
    lang_out = reconcile_lang(info.language, " ".join(c[2][0] for c in cues))
    if lang_out != info.language:
        notes.append(f"ℹ️ مدل زبان گفتار را «{LANG_NAMES.get(base_lang(info.language), info.language)}» اعلام کرد، ولی متن نوشته‌شده "
                     f"«{LANG_NAMES.get(base_lang(lang_out), lang_out)}» است؛ بر اساس خودِ متن ادامه دادم.")
        print(f"language announced {info.language}, text is {lang_out}")
    return cues, lang_out, notes


# =====================================================================  the job
def ascii_slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:50] or "video"


def mmss(sec: float) -> str:
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


def run_upload(cfg: Cfg, tg: Telegram, job: dict) -> None:
    chat, mid = job["chat_id"], job["msg_id"]
    name = job["name"] or "video.mp4"
    stem = Path(name).stem or "video"
    opts: Opts = job["opts"]
    mode, target, engine = job["mode"], job["target"], job["engine"]
    work = cfg.work_dir / f"up{mid}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    prog = Progress(tg, chat, mid, header=f"🎞 <b>{html.escape(stem[:120])}</b>")
    ai_ok = bool(cfg.ai_key)
    ai_name = AI_NAMES.get(cfg.ai_provider, "")
    notes: list = []

    ext = Path(name).suffix.lower()
    if not re.fullmatch(r"\.\w{2,4}", ext):
        ext = ".mp4"
    video = work / ("input" + ext)
    prog.text("⬇️ دریافت ویدیو از تلگرام…")
    fetch_file(cfg, job["file_id"], video)
    meta = probe(video)
    if not meta["vcodec"] or meta["duration"] <= 0:
        raise UserFacing("این فایل یک ویدیوی سالم نیست.")
    duration = meta["duration"]

    # ---- 1) the subtitle in the language that is SPOKEN
    if engine.startswith("ai"):
        parts = engine.split(":")
        size = parts[1] if len(parts) > 1 and parts[1] in WHISPER_SIZES else "small"
        spoken = parts[2] if len(parts) > 2 else "auto"
        prog.text("🔊 جدا کردن صدا…")
        audio = work / "audio.wav"
        extract_audio(video, audio)
        src_cues, src_lang, tnotes = transcribe(audio, size, spoken, duration, prog)
        notes += tnotes
        audio.unlink(missing_ok=True)
        if not src_cues:
            raise UserFacing("در این ویدیو گفتاری تشخیص داده نشد.")
        if base_lang(src_lang) == "fa":
            src_cues = [(s, e, [fix_fa(x) for x in lines]) for s, e, lines in src_cues]
        how = f"تشخیص گفتار ({size})"
    else:
        prog.text("📄 خواندن فایل زیرنویس…")
        sub = work / "user_sub"
        fetch_file(cfg, job["srt_id"], sub)
        text = re.sub(r"\{\\[^}]*\}", "", read_text_file(sub))
        src_cues = cues_from_vtt(text)
        if not src_cues:
            raise UserFacing("در فایل زیرنویس هیچ خطی پیدا نشد. فقط .srt و .vtt پشتیبانی می‌شود.")
        src_lang = guess_lang(" ".join(" ".join(c[2]) for c in src_cues[:200]))
        how = "فایل خودت"

    # ---- 2) translate when another language was asked for
    final_cues, final_lang, final_how, translated = src_cues, src_lang, how, False
    if target != "-" and base_lang(target) != base_lang(src_lang):
        if ai_ok:
            ctx = {"title": stem, "channel": "", "desc": ""}
            try:
                final_cues, bad = translate_cues(cfg, src_cues, src_lang, target, ctx, lambda b: prog.text(b, force=False))
                final_lang, final_how, translated = target, f"ترجمهٔ {ai_name}", True
                if bad:
                    notes.append(f"⚠️ {bad} خط ترجمه نشد و به زبان اصلی ماند.")
            except AIError as ex:
                print(f"AI translation failed: {ex}")
                notes.append("⚠️ ترجمهٔ هوش مصنوعی انجام نشد (" + html.escape(str(ex)[:160]) + "). زیرنویس به زبان اصلی گفتار ساخته شد.")
        else:
            notes.append("ℹ️ برای ترجمه کلید ANTHROPIC_API_KEY (یا OPENAI_API_KEY یا GEMINI_API_KEY) را در Secrets گیت‌هاب بساز. فعلاً زیرنویس به زبان اصلی گفتار ساخته شد.")

    # ---- 3) subtitle files (burned in = ASS, embedded = SRT)
    hard, soft, desc = None, [], []
    label = LANG_NAMES.get(base_lang(final_lang), final_lang)
    if mode == "h":
        hard = work / "hard.ass"
        hard.write_text(build_ass(final_cues, final_lang, opts, meta["width"], meta["height"]), encoding="utf-8")
    else:
        p = work / "sub_0.srt"
        p.write_text(to_srt(final_cues, final_lang, marks=True), encoding="utf-8")
        soft.append((p, final_lang))
        if translated:
            p2 = work / "sub_orig.srt"
            p2.write_text(to_srt(src_cues, src_lang, marks=True), encoding="utf-8")
            soft.append((p2, src_lang))
    desc.append(f"{label} ({'روی تصویر' if hard else 'داخل فایل'}، {final_how})")
    if soft and translated:
        desc.append(f"{LANG_NAMES.get(base_lang(src_lang), src_lang)} (داخل فایل، اصلی)")

    # ---- 4) the finished video
    slug = ascii_slug(stem)
    final = work / f"{slug}-sub.mp4"
    step = "🎬 ساخت ویدیو با زیرنویس روی تصویر" if hard else "🎬 ساخت فایل نهایی"
    prog.text(step)
    render(video, final, hard=hard, soft=soft, duration=duration, on_progress=lambda f: prog.frac(step, f))
    prog.text("🔍 بررسی فایل نهایی…")
    fin = check_media(final, duration)
    if fin["video"] < duration * 0.5:
        raise RuntimeError("فایل نهایی سالم نبود: " + "؛ ".join(fin["problems"]))
    if not fin["ok"]:
        notes.append("⚠️ طول فایل نهایی کمی با ویدیوی اصلی فرق دارد (" + "؛ ".join(fin["problems"]) + ").")

    size_mb = final.stat().st_size / 1_000_000
    prog.text(f"⬆️ آپلود در تلگرام ({size_mb:.0f} MB)…")
    thumb = thumbnail(final, work / "thumb.jpg", duration)
    caption = (f"🎞 <b>{html.escape(stem[:200])}</b>\n💬 {html.escape(' + '.join(desc))}\n"
               f"📦 {meta['height'] or '?'}p · {size_mb:.0f} MB · ⏱ {mmss(duration)}")
    sent = deliver(cfg, final, caption, thumb, duration, progress=prog.text)

    # the subtitle as a separate file too (handy for a player / for editing), small -> normal API is enough
    try:
        srt_out = work / f"{slug}.{base_lang(final_lang)}.srt"
        srt_out.write_text(to_srt(final_cues, final_lang), encoding="utf-8")
        tg.upload("sendDocument", "document", str(srt_out), {"chat_id": cfg.storage_chat, "caption": f"📄 {label}"})
    except Exception as ex:                                         # noqa: BLE001
        print(f"could not send the .srt file: {scrub(repr(ex)[:200], cfg)}")

    # the bilingual subtitle page (HTML): the spoken language + Persian (English for a Persian video)
    prog.text("📄 ساخت صفحهٔ زیرنویس دوزبانه…")
    ready = [{"lang": final_lang, "cues": final_cues, "how": final_how, "aligned": True, "auto": False}] if translated else []
    page_sent, page_label, pnotes = page_job(
        cfg, work / f"{slug}-subtitles.html", title=stem, src_cues=src_cues, src_lang=src_lang, how_src=how,
        duration=duration, ctx={"title": stem, "channel": "", "desc": ""}, ready=ready, prog=prog, video=video)
    notes += pnotes

    if cfg.fallback_used:
        notes.append("⚠️ سرور ۲ گیگابایتی در دسترس نبود؛ فایل با محدودیت ۵۰ مگابایت ارسال شد (ممکن است تکه‌تکه باشد)."
                     + (f"\nدلیل: <code>{html.escape(cfg.fallback_reason)}</code>" if cfg.fallback_reason else ""))
    links = [l for l in (message_link(m) for m in sent) if l]
    done = f"✅ <b>آماده شد</b>\n\n🎞 <b>ویدیو</b>\n{html.escape(stem[:150])}\n\n💬 <b>زیرنویس</b>\n" + "\n".join(html.escape(d) for d in desc)
    if page_sent:
        done += f"\n\n🌐 <b>صفحهٔ زیرنویس</b>\n{html.escape(page_label)} (فایل HTML برای خواندن و جستجو)"
    if links:
        done += "\n\n🔗 <b>لینک پیام</b>\n" + (links[0] if len(links) == 1 else " ".join(f'<a href="{l}">{i}</a>' for i, l in enumerate(links, 1)))
    else:
        done += "\n\n📥 فایل در همین چت ارسال شد."
    done += f"\n\n🔍 <b>بررسی سلامت</b>\nتصویر {mmss(fin['video'])} · صدا {mmss(fin['audio'])} · طول اصلی {mmss(duration)} ✅"
    if notes:
        done += "\n\n" + "\n\n".join(dict.fromkeys(notes))
    prog.header = ""
    prog.text("✅ <b>آماده شد</b>")
    try:
        tg.send_message(chat, done)          # a NEW message, so the phone gives a notification
    except Exception as ex:                  # noqa: BLE001
        print(f"could not send the final message: {scrub(repr(ex)[:200], cfg)}")
    shutil.rmtree(work, ignore_errors=True)


def explain(ex: Exception, cfg: Cfg) -> str:
    if isinstance(ex, UserFacing):
        body = str(ex)
    elif isinstance(ex, AIError):
        body = "هوش مصنوعی جواب نداد: " + html.escape(str(ex)[:250])
    elif isinstance(ex, FFmpegError):
        body = "پردازش ویدیو با ffmpeg شکست خورد:\n<pre>" + html.escape(scrub(str(ex), cfg)[-400:]) + "</pre>"
    else:
        body = html.escape(scrub(str(ex) or type(ex).__name__, cfg)[:350])
    return "❌ <b>خطا</b>\n\n" + body


def main() -> int:
    cfg = Cfg.from_env()
    ai_setup(cfg)
    e = lambda k: (os.environ.get(k) or "").strip()                 # noqa: E731
    file_id, srt_id = e("FILE_ID"), e("SRT_ID")
    if not cfg.bot_token or not e("CHAT_ID") or not e("MSG_ID").isdigit() or not FILE_ID_RE.match(file_id):
        print("BOT_TOKEN / CHAT_ID / MSG_ID / FILE_ID are missing or invalid")
        return 1
    engine = e("ENGINE") or "ai:small:auto"
    if engine == "srt" and not FILE_ID_RE.match(srt_id):
        print("ENGINE=srt needs a valid SRT_ID")
        return 1
    target = e("TARGET") or "-"
    if target != "-" and not LANG_RE.match(target):
        target = "-"
    opts = Opts.parse(e("OPTS"))
    opts.du = ""                                                    # no dubbing in this part
    job = {"chat_id": e("CHAT_ID"), "msg_id": int(e("MSG_ID")), "file_id": file_id, "srt_id": srt_id,
           "name": e("FILE_NAME")[:150], "engine": engine, "target": target,
           "mode": "s" if e("MODE") == "s" else "h", "opts": opts}
    tg = Telegram(cfg.bot_token)
    try:
        run_upload(cfg, tg, job)
        return 0
    except Exception as ex:                                         # noqa: BLE001
        print(f"JOB FAILED: {scrub(repr(ex), cfg)[:400]}")
        try:
            tg.edit_message(job["chat_id"], job["msg_id"], explain(ex, cfg))
        except Exception:                                           # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
