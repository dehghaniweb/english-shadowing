#!/usr/bin/env python3
"""
The bilingual subtitle page: ONE self-contained .html file (no internet needed, nothing to install).

  * every subtitle line of the video, with the line in the other language right under it
      - video in any language except Persian  -> original + Persian
      - video in Persian                      -> Persian + English
  * works without JavaScript; with it you also get: search, show/hide a language, text size,
    light/dark mode and a playhead bar (scroll = move through the video, drag the bar to jump)
  * every time stamp is a link to that moment on YouTube (only for videos downloaded from YouTube)

This module has no imports from download.py (download.py imports it), so it can be tested alone.
"""
from __future__ import annotations

import base64
import bisect
import html
import os
import re
import unicodedata
from pathlib import Path
from typing import Optional

RTL = {"fa", "ar", "ur", "he", "ps", "ku", "sd", "ug"}
NAMES = {
    "fa": "فارسی", "en": "English", "ar": "العربية", "tr": "Türkçe", "de": "Deutsch", "nl": "Nederlands",
    "fr": "Français", "es": "Español", "ru": "Русский", "it": "Italiano", "pt": "Português",
    "ja": "日本語", "ko": "한국어", "zh": "中文", "hi": "हिन्दी", "ur": "اردو", "uk": "Українська", "pl": "Polski",
    "he": "עברית", "id": "Indonesia", "sv": "Svenska",
}
VID_RE = re.compile(r"^[\w-]{11}$")
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def base_lang(code: str) -> str:
    return (code or "").split("-")[0].lower()


def fa_digits(x) -> str:
    return str(x).translate(_FA)


def fmt_time(t: float) -> str:
    t = max(0, int(t))
    h, rest = divmod(t, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# =====================================================================  pairing the two languages
def _text(lines) -> str:
    return re.sub(r"\s+", " ", " ".join(lines or [])).strip()


def pair_by_time(primary: list, other: list) -> list:
    """Two subtitle tracks that were cut differently (for example the uploader's English and the uploader's Persian).
    -> one text per `primary` cue: every `other` line goes to the primary cue it overlaps most (by its middle)."""
    order = sorted(range(len(primary)), key=lambda i: primary[i][0])
    starts = [primary[i][0] for i in order]
    buckets = [[] for _ in primary]
    for s, e, lines in sorted(other, key=lambda c: c[0]):
        txt = _text(lines)
        if not txt or not order:
            continue
        mid = (s + e) / 2
        k = max(0, bisect.bisect_right(starts, mid) - 1)
        if k + 1 < len(order) and mid >= primary[order[k]][1]:            # in a gap: take the nearer neighbour
            if primary[order[k + 1]][0] - mid < mid - primary[order[k]][1]:
                k += 1
        buckets[order[k]].append(txt)
    return [" ".join(b) for b in buckets]


def make_rows(cues_a: list, cues_b: Optional[list] = None, aligned: bool = True) -> list:
    """-> [(start, end, text_a, text_b)].  aligned=True: cues_b[i] is the translation of cues_a[i] (same timing,
    made by translate_cues). aligned=False: cues_b is another track -> paired by time."""
    if not cues_b:
        others = [""] * len(cues_a)
    elif aligned and len(cues_b) == len(cues_a):
        others = [_text(c[2]) for c in cues_b]
    else:
        others = pair_by_time(cues_a, cues_b)
    rows = []
    for (s, e, lines), b in zip(cues_a, others):
        a = _text(lines)
        if a or b:
            rows.append((float(s), float(e), a, b))
    return rows


# =====================================================================  the Persian font (embedded when the runner has it)
_FONT_ROOTS = ("/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts", "~/.local/share/fonts")
_FONT_FMT = {".woff2": ("font/woff2", "woff2"), ".woff": ("font/woff", "woff"),
             ".ttf": ("font/ttf", "truetype"), ".otf": ("font/otf", "opentype")}
_FONT_RANK = {".woff2": 0, ".woff": 1, ".ttf": 2, ".otf": 3}


def embed_fonts(roots=_FONT_ROOTS, max_bytes: int = 900_000) -> str:
    """@font-face rules with Vazirmatn inside the page (base64), or "" when the font is not installed or too big.
    Without it the page simply uses the fonts of the phone (Noto Sans Arabic on Android, which looks fine too)."""
    try:
        found = []
        for root in roots:
            for dirpath, _dirs, files in os.walk(os.path.expanduser(root)):
                for f in files:
                    p = Path(dirpath) / f
                    if "vazirmatn" in f.lower() and p.suffix.lower() in _FONT_FMT and "code" not in f.lower():
                        found.append(p)
        if not found:
            return ""

        def pick(pred):
            c = sorted((p for p in found if pred(p.stem.lower())), key=lambda p: (_FONT_RANK[p.suffix.lower()], p.stat().st_size))
            return c[0] if c else None

        faces = []
        var = pick(lambda s: "[" in s or "variable" in s or "wght" in s)
        if var:
            faces.append((var, "100 900"))
        else:
            reg = pick(lambda s: s.endswith("regular") or s == "vazirmatn")
            bold = pick(lambda s: s.endswith("-bold") or s == "vazirmatn-bold")
            faces += [(p, w) for p, w in ((reg, "400"), (bold, "700")) if p]
        out, used = [], 0
        for p, weight in faces:
            size = p.stat().st_size
            if used + size > max_bytes:
                continue
            used += size
            mime, fmt = _FONT_FMT[p.suffix.lower()]
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            out.append('@font-face{font-family:"Vazirmatn";font-weight:%s;font-style:normal;font-display:swap;'
                       'src:url(data:%s;base64,%s) format("%s")}' % (weight, mime, data, fmt))
        return "\n".join(out)
    except Exception:                                               # noqa: BLE001 - a missing font must never stop the page
        return ""


# =====================================================================  look
_LIGHT = """--bg:#F4F6FB;--ink:#16254D;--mut:#5A6584;--line:rgba(22,37,77,.11);
--tr:#0A6B70;--trbar:#2AA7A0;--chip:#FBE8B8;--chipink:#5B3C00;--mark:#FFE27A;
--pill:#16254D;--pillink:#F6F8FF;--fill:#F0B232;--ctl:#E5EAF5;--ctlink:#16254D;--cta:#16254D;--ctaink:#FFFFFF;--onbg:rgba(240,178,50,.18);color-scheme:light;"""
_DARK = """--bg:#0B1126;--ink:#E7EBF7;--mut:#95A1C2;--line:rgba(231,235,247,.12);
--tr:#7DD8D0;--trbar:#3FB6AD;--chip:#3A2D0C;--chipink:#F4BE48;--mark:#6B5416;
--pill:#1B2650;--pillink:#F6F8FF;--fill:#F4BE48;--ctl:#18213F;--ctlink:#E7EBF7;--cta:#F4BE48;--ctaink:#2A1F05;--onbg:rgba(244,190,72,.15);color-scheme:dark;"""

CSS = r"""
:root{--font:"Vazirmatn","Noto Sans Arabic","Noto Naskh Arabic","Segoe UI",Tahoma,system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;
--fs:18px;--top:64px;%%LIGHT%%}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){%%DARK%%}}
:root[data-theme="dark"]{%%DARK%%}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-padding-top:var(--top)}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);font-size:var(--fs);line-height:1.7}
.page{max-width:46rem;margin:0 auto;padding:0 1rem 7rem}
a{color:inherit}
:focus-visible{outline:2px solid var(--fill);outline-offset:2px}

.hero{padding:2.3rem 0 1.2rem}
.hero h1{margin:0 0 .55rem;font-size:clamp(1.4rem,5.4vw,2.05rem);line-height:1.55;font-weight:800;text-wrap:balance;text-align:right}
.facts{margin:0 0 1.15rem;color:var(--mut);font-size:.86em}
.legend{display:grid;gap:.7rem;margin:0 0 1.3rem;padding:0;list-style:none}
.legend li{display:grid;grid-template-columns:auto 1fr;column-gap:.7rem;align-items:start}
.sw{width:3px;align-self:stretch;border-radius:3px;background:var(--ink)}
.legend .lt .sw{background:var(--trbar)}
.legend b{display:block;font-size:1.02em;line-height:1.5}
.legend .lt b{color:var(--tr)}
.legend span.d{display:block;color:var(--mut);font-size:.8em;line-height:1.6}
.go{display:inline-block;font-weight:700;font-size:.86em;text-decoration:none;color:var(--ctaink);background:var(--cta);padding:.5em 1.1em;border-radius:999px}

.bar{position:sticky;top:0;z-index:30;margin:0 -1rem;padding:.55rem 1rem .6rem;background:var(--bg);border-block-end:1px solid var(--line)}
@supports (background:color-mix(in srgb,red,blue)){.bar{background:color-mix(in srgb,var(--bg) 88%,transparent);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px)}}
.find{display:flex;gap:.5rem}
#q{flex:1;min-width:0;font:inherit;font-size:.88em;color:var(--ink);background:var(--ctl);border:1px solid transparent;border-radius:.75rem;padding:.5rem .85rem}
#q::placeholder{color:var(--mut)}
.btn{font:inherit;font-size:.8em;font-weight:700;color:var(--ctlink);background:var(--ctl);border:0;border-radius:.75rem;padding:.45rem .85rem;cursor:pointer;white-space:nowrap}
.btn[aria-expanded="true"]{background:var(--ink);color:var(--bg)}
.tools{display:grid;gap:.45rem;padding-top:.5rem}
.tools[hidden]{display:none}
.row{display:flex;flex-wrap:wrap;gap:.45rem;align-items:center}
.chk{display:inline-flex;align-items:center;gap:.4rem;font-size:.8em;font-weight:700;background:var(--ctl);border-radius:.75rem;padding:.45rem .8rem;cursor:pointer}
.chk input{accent-color:var(--tr);width:1.05em;height:1.05em;margin:0}
.gap{flex:1}
.found{margin:.35rem 0 0;font-size:.78em;color:var(--mut);min-height:0}
.found:empty{display:none}

.cue{padding:.9em 0;border-block-end:1px solid var(--line)}
.cue[hidden]{display:none}
.cue p{margin:0;text-wrap:pretty}
.o{font-weight:600;line-height:1.75}
.x{margin-top:.45em;padding-inline-start:.75em;border-inline-start:3px solid var(--trbar);color:var(--tr);line-height:1.75}
p[dir="rtl"]{line-height:1.95}
.tc{display:inline-block;font-size:.66em;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.7;padding:0 .55em;margin-inline-end:.75em;border-radius:.55em;background:var(--chip);color:var(--chipink);text-decoration:none;vertical-align:.12em}
a.tc:hover{text-decoration:underline}
.x .tc{display:none}
.no-o .x .tc{display:inline-block}
.no-o .o,.no-x .x{display:none}
mark{background:var(--mark);color:inherit;border-radius:.2em;padding:0 .08em}

.foot{padding:1.6rem 0 0;color:var(--mut);font-size:.76em;line-height:1.9}

.ph{position:fixed;inset-inline:0;bottom:0;z-index:40;display:flex;justify-content:center;padding:0 .8rem calc(.7rem + env(safe-area-inset-bottom,0px));pointer-events:none}
.ph[hidden]{display:none}
.pill{pointer-events:auto;direction:ltr;display:flex;align-items:center;gap:.75rem;width:min(100%,34rem);padding:.45rem 1rem;border-radius:999px;background:var(--pill);color:var(--pillink);box-shadow:0 8px 26px rgba(8,14,36,.3)}
.now{font-weight:800;font-size:.9em;min-width:3.3em;font-variant-numeric:tabular-nums}
.tot{font-size:.78em;opacity:.72;font-variant-numeric:tabular-nums}
.track{position:relative;flex:1;height:1.8rem;touch-action:none;cursor:pointer}
.track::before{content:"";position:absolute;left:0;right:0;top:50%;height:4px;margin-top:-2px;border-radius:4px;background:rgba(255,255,255,.24)}
.track i{position:absolute;left:0;top:50%;height:4px;margin-top:-2px;border-radius:4px;background:var(--fill);width:0}
.track b{position:absolute;top:50%;left:0;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;background:var(--fill)}

.go.alt{background:transparent;color:var(--ink);box-shadow:inset 0 0 0 2px var(--ink);margin-inline-start:.5rem}
.hint{margin:.2rem 0 1.1rem;color:var(--mut);font-size:.8em}
.has-audio .cue{cursor:pointer;-webkit-tap-highlight-color:transparent}
.cue.on{background:var(--onbg);box-shadow:0 0 0 .55em var(--onbg);border-radius:.4em}
.pp{flex:none;width:2.2rem;height:2.2rem;border-radius:50%;border:0;padding:0;display:grid;place-items:center;cursor:pointer;background:var(--fill);color:#2A1F05}
.pp svg{width:.95rem;height:.95rem;fill:currentColor}
.pp .i-pause{display:none}.pp.playing .i-play{display:none}.pp.playing .i-pause{display:block}
@media (max-width:420px){.page{padding-inline:.85rem}.bar{margin-inline:-.85rem;padding-inline:.85rem}}
@media print{.bar,.ph,.go{display:none!important}body{background:#fff;color:#000}.cue{break-inside:avoid}.x{color:#064}.tc{background:none;border:1px solid #999;color:#000}}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
"""

JS = r"""
(function(){
"use strict";
var D=document,R=D.documentElement,B=D.body;
R.classList.add("js");
var $=function(i){return D.getElementById(i)};
var store={get:function(k){try{return localStorage.getItem(k)}catch(e){return null}},set:function(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
[].forEach.call(D.querySelectorAll(".js-only"),function(e){e.hidden=false});
var rows=[].slice.call(D.querySelectorAll(".cue"));
var bar=$("bar"),q=$("q"),found=$("found"),ph=$("ph"),track=$("ph-track"),fill=$("ph-fill"),knob=$("ph-knob"),now=$("ph-now"),pp=$("ph-play"),aud=$("aud"),dl=$("dl");
var total=ph?(+ph.getAttribute("data-total")||0):0;
var starts=rows.map(function(r){return +r.getAttribute("data-s")});
if(aud)B.classList.add("has-audio");
function faNum(n){return String(n).replace(/\d/g,function(d){return "۰۱۲۳۴۵۶۷۸۹".charAt(d)})}
function fmt(t){t=Math.max(0,Math.floor(t));var h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;return (h?h+":"+(m<10?"0":"")+m:m)+":"+(s<10?"0":"")+s}
function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
function norm(s){return s.toLowerCase().replace(/[يى]/g,"ی").replace(/ك/g,"ک").replace(/ة/g,"ه")
  .replace(/[٠-٩]/g,function(c){return String.fromCharCode(c.charCodeAt(0)-1584)})
  .replace(/[۰-۹]/g,function(c){return String.fromCharCode(c.charCodeAt(0)-1728)})}
function topLine(){return bar.getBoundingClientRect().bottom+6}
function fit(){R.style.setProperty("--top",(bar.offsetHeight+6)+"px")}

/* ---- the audio: decoded from the page itself on first use (nothing is downloaded, nothing leaves the phone) */
var audio=null,blobUrl="",playing=false,scrollDriven=true,followUntil=0,lastUser=0,searching=false,activeIdx=-1;
function ensureAudio(){
  if(!aud)return null;
  if(audio)return audio;
  var b64=aud.textContent,parts=[],CH=1<<20,i,j,bin,arr;
  for(i=0;i<b64.length;i+=CH){bin=atob(b64.slice(i,i+CH));arr=new Uint8Array(bin.length);for(j=0;j<bin.length;j++)arr[j]=bin.charCodeAt(j);parts.push(arr)}
  blobUrl=URL.createObjectURL(new Blob(parts,{type:aud.getAttribute("data-mime")||"audio/mpeg"}));
  audio=new Audio();audio.preload="auto";audio.src=blobUrl;
  audio.addEventListener("timeupdate",onTime);
  audio.addEventListener("play",function(){playing=true;scrollDriven=false;ui()});
  audio.addEventListener("pause",function(){playing=false;ui()});
  audio.addEventListener("ended",function(){playing=false;ui()});
  audio.addEventListener("error",function(){playing=false;ui();found.textContent="پخش صدا در این مرورگر ممکن نشد؛ دکمهٔ دانلود صدا را بزن."});
  return audio;
}
function ui(){if(pp){pp.classList.toggle("playing",playing);pp.setAttribute("aria-label",playing?"توقف":"پخش")}}
function seekTo(t){
  var a=ensureAudio();if(!a)return;
  var go=function(){try{a.currentTime=t}catch(e){}};
  if(a.readyState>=1)go();else{var f=function(){a.removeEventListener("loadedmetadata",f);go()};a.addEventListener("loadedmetadata",f)}
}
function playAt(t){var a=ensureAudio();if(!a)return;scrollDriven=false;seekTo(t);display(t);mark(t);var p=a.play();if(p&&p.catch)p.catch(function(){})}
function idxAt(t){var lo=0,hi=starts.length-1;while(lo<hi){var m=(lo+hi+1)>>1;if(starts[m]<=t+0.12)lo=m;else hi=m-1}return lo}
function follow(r){
  var b=r.getBoundingClientRect(),top=topLine()+8,bot=window.innerHeight-(ph?ph.offsetHeight:0)-28;
  if(b.top<top||b.bottom>bot)window.scrollTo({top:b.top+window.pageYOffset-top-24,behavior:"smooth"});
}
function mark(t){
  var i=idxAt(t);if(i===activeIdx)return;
  if(activeIdx>-1)rows[activeIdx].classList.remove("on");
  activeIdx=i;rows[i].classList.add("on");
  if(playing&&Date.now()>followUntil&&!rows[i].hidden)follow(rows[i]);
}
function onTime(){var t=audio.currentTime;display(t);mark(t)}

/* ---- the playhead: the reading position, or the playing position */
function display(t){
  if(!ph)return;var f=total?Math.min(1,t/total):0;
  now.textContent=fmt(t);fill.style.width=(f*100)+"%";knob.style.left=(f*100)+"%";track.setAttribute("aria-valuenow",Math.round(t));
}
function curIdx(){var y=topLine(),lo=0,hi=rows.length-1;
  while(lo<hi){var m=(lo+hi)>>1;if(rows[m].getBoundingClientRect().bottom>y)hi=m;else lo=m+1}
  return lo}
function tick(){
  if(!ph||ph.hidden||!rows.length)return;
  if(searching||!scrollDriven){display(audio?audio.currentTime:0);return}
  display(starts[curIdx()]);
}
function jump(f){
  var t=f*total,lo=0,hi=starts.length-1;
  while(lo<hi){var m=(lo+hi)>>1;if(starts[m]<t)lo=m+1;else hi=m}
  if(!searching)window.scrollTo(0,rows[lo].getBoundingClientRect().top+window.pageYOffset-topLine()+6);
  if(aud){scrollDriven=false;seekTo(starts[lo]);display(starts[lo]);mark(starts[lo])}else tick();
}
if(ph&&rows.length){
  var drag=false,pos=function(e){var b=track.getBoundingClientRect();return Math.min(1,Math.max(0,(e.clientX-b.left)/b.width))};
  track.addEventListener("pointerdown",function(e){drag=true;try{track.setPointerCapture(e.pointerId)}catch(x){}jump(pos(e))});
  track.addEventListener("pointermove",function(e){if(drag)jump(pos(e))});
  ["pointerup","pointercancel"].forEach(function(n){track.addEventListener(n,function(){drag=false})});
  track.addEventListener("keydown",function(e){
    var i=aud&&!scrollDriven?idxAt(audio?audio.currentTime:0):curIdx(),d=e.key==="ArrowLeft"||e.key==="ArrowDown"?-1:e.key==="ArrowRight"||e.key==="ArrowUp"?1:0;
    if(!d)return;e.preventDefault();jump(starts[Math.min(rows.length-1,Math.max(0,i+d*5))]/(total||1));
  });
  ["touchstart","touchmove","wheel","keydown","mousedown"].forEach(function(n){D.addEventListener(n,function(e){
    if(e.target&&e.target.closest&&e.target.closest("#ph"))return;lastUser=Date.now()},{passive:true})});
  var raf=0;window.addEventListener("scroll",function(){if(!raf)raf=requestAnimationFrame(function(){raf=0;
    if(Date.now()-lastUser<800){lastUser=Date.now();if(playing)followUntil=Date.now()+6000;else if(aud)scrollDriven=true}
    tick()})},{passive:true});
}
if(aud&&pp){
  pp.addEventListener("click",function(){
    var a=ensureAudio();if(!a)return;
    if(playing){a.pause();return}
    if(scrollDriven&&!searching)playAt(starts[curIdx()]);else{scrollDriven=false;var p=a.play();if(p&&p.catch)p.catch(function(){})}
  });
  rows.forEach(function(r){r.addEventListener("click",function(e){
    if(e.target.closest&&e.target.closest("a"))return;
    var sel=window.getSelection&&String(window.getSelection());if(sel)return;
    playAt(+r.getAttribute("data-s"));
  })});
}
if(dl)dl.addEventListener("click",function(){ensureAudio();if(blobUrl)dl.href=blobUrl});

/* ---- search (Persian/Arabic letter variants and digits are treated as the same) */
rows.forEach(function(r){r._t=[].slice.call(r.querySelectorAll(".t"));r._r=r._t.map(function(e){return e.textContent});r._n=r._r.map(norm)});
var last="",timer=0;
function search(){
  var nq=norm(q.value.trim());if(nq===last)return;last=nq;searching=!!nq;var shown=0;
  rows.forEach(function(r){
    var hit=!nq||r._n.some(function(n){return n.indexOf(nq)>-1});
    r.hidden=!hit;if(hit)shown++;
    r._t.forEach(function(e,i){
      var raw=r._r[i],n=r._n[i];
      if(!nq||!hit||n.length!==raw.length||n.indexOf(nq)<0){if(e._m){e.textContent=raw;e._m=0}return}
      var out="",at=0,k;
      while((k=n.indexOf(nq,at))>-1){out+=esc(raw.slice(at,k))+"<mark>"+esc(raw.slice(k,k+nq.length))+"</mark>";at=k+nq.length}
      e.innerHTML=out+esc(raw.slice(at));e._m=1;
    });
  });
  found.textContent=nq?(shown?faNum(shown)+" مورد پیدا شد":"موردی پیدا نشد"):"";
  if(ph){if(!aud)ph.hidden=!!nq;tick()}
  fit();
}
q.addEventListener("input",function(){clearTimeout(timer);timer=setTimeout(search,120)});
q.addEventListener("keydown",function(e){if(e.key==="Escape"){q.value="";search()}});

/* ---- settings */
var tools=$("tools"),tb=$("tools-btn");
tb.addEventListener("click",function(){var open=tools.hidden;tools.hidden=!open;tb.setAttribute("aria-expanded",open?"true":"false");fit()});
var so=$("show-o"),sx=$("show-x");
function vis(){B.classList.toggle("no-o",!so.checked);B.classList.toggle("no-x",!sx.checked);tick()}
if(so&&sx){so.addEventListener("change",function(){if(!so.checked&&!sx.checked)sx.checked=true;vis()});
  sx.addEventListener("change",function(){if(!so.checked&&!sx.checked)so.checked=true;vis()})}
var fs=+store.get("ts-fs")||18;
function setFs(v){fs=Math.min(30,Math.max(14,v));R.style.setProperty("--fs",fs+"px");store.set("ts-fs",String(fs));fit();tick()}
if(store.get("ts-fs"))setFs(fs);
$("fs-down").addEventListener("click",function(){setFs(fs-2)});
$("fs-up").addEventListener("click",function(){setFs(fs+2)});
var tbtn=$("theme");
function cur(){return R.getAttribute("data-theme")||(window.matchMedia&&matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light")}
function lab(){tbtn.textContent=cur()==="dark"?"حالت روشن":"حالت تیره"}
if(store.get("ts-th"))R.setAttribute("data-theme",store.get("ts-th"));
tbtn.addEventListener("click",function(){var n=cur()==="dark"?"light":"dark";R.setAttribute("data-theme",n);store.set("ts-th",n);lab()});
lab();fit();ui();tick();
window.addEventListener("resize",function(){fit();tick()});
window.addEventListener("load",function(){fit();tick()});
})();
"""


# =====================================================================  the page
def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def text_dir(text: str, fallback: str) -> str:
    """rtl / ltr of ONE paragraph, decided by its own letters (not by the language it is supposed to be in)."""
    rtl = ltr = 0
    for ch in text:
        k = unicodedata.bidirectional(ch)
        if k in ("R", "AL"):
            rtl += 1
        elif k == "L":
            ltr += 1
    if not rtl and not ltr:
        return fallback
    return "rtl" if rtl > ltr else "ltr"


def _attrs(text: str, lang: str, declared_dir: str) -> str:
    d = text_dir(text, declared_dir)
    code = base_lang(lang) if d == declared_dir else ("fa" if d == "rtl" else "en")
    return f'lang="{_e(code)}" dir="{d}"'


def _lang_name(code: str) -> str:
    return NAMES.get(base_lang(code), code)


def build_page(rows: list, *, title: str, lang_a: str, lang_b: str = "", how_a: str = "", how_b: str = "",
               channel: str = "", url: str = "", vid: str = "", duration: float = 0.0,
               brand: str = "TubeSub", font_css: Optional[str] = None, audio: Optional[dict] = None) -> str:
    """rows = make_rows(...). lang_b="" means there is no second language.
    audio = {"path": Path, "mime": "audio/mpeg", "name": "x.mp3"} puts the sound inside the page: tap a line to play from there.
    -> the whole HTML file as text."""
    has_b = bool(lang_b) and any(r[3] for r in rows)
    vid = vid if VID_RE.match(vid or "") else ""
    dir_a = "rtl" if base_lang(lang_a) in RTL else "ltr"
    dir_b = "rtl" if base_lang(lang_b) in RTL else "ltr"
    last_end = max((r[1] for r in rows), default=0.0)
    total = max(float(duration or 0), last_end)

    items = []
    for i, (s, _e_, a, b) in enumerate(rows):
        t = fmt_time(s)
        chip = (f'<a class="tc" href="https://youtu.be/{vid}?t={int(s)}" target="_blank" rel="noopener noreferrer" '
                f'title="پخش از {t} در یوتیوب">{t}</a>' if vid else f'<span class="tc">{t}</span>')
        parts = [f'<article class="cue" id="c{i}" data-s="{s:.2f}">']
        if a:
            parts.append(f'<p class="o" {_attrs(a, lang_a, dir_a)}>{chip}<span class="t">{_e(a)}</span></p>')
        if has_b and b:
            dup = f'<span class="tc" aria-hidden="true">{t}</span>' if a else chip
            parts.append(f'<p class="x" {_attrs(b, lang_b, dir_b)}>{dup}<span class="t">{_e(b)}</span></p>')
        parts.append("</article>")
        items.append("".join(parts))

    facts = []
    if channel:
        facts.append(f"کانال {_e(channel)}")
    if total:
        facts.append(f"مدت {fa_digits(fmt_time(total))}")
    facts.append(f"{fa_digits(len(rows))} خط زیرنویس")

    def legend(cls, name, role, how):
        d = _e(how if how.startswith(role) else f"{role}، {how}" if how else role)     # "ترجمهٔ Claude" already says "ترجمه"
        return f'<li class="{cls}"><i class="sw"></i><div><b>{_e(name)}</b><span class="d">{d}</span></div></li>'

    leg = legend("lo", _lang_name(lang_a), "متن اصلی", how_a)
    if has_b:
        leg += legend("lt", _lang_name(lang_b), "ترجمه", how_b)
    go = ""
    if vid and url.startswith("https://"):
        go = f'<a class="go" href="{_e(url)}" target="_blank" rel="noopener noreferrer">دیدن ویدیو در یوتیوب</a>'

    toggles = ""
    if has_b:
        toggles = ('<label class="chk"><input type="checkbox" id="show-o" checked>متن اصلی</label>'
                   '<label class="chk"><input type="checkbox" id="show-x" checked>ترجمه</label>')
    else:                                               # the script expects these two ids to exist
        toggles = '<input type="checkbox" id="show-o" checked hidden><input type="checkbox" id="show-x" checked hidden>'

    audio_block = hint = dl_btn = play_btn = ""
    if audio and Path(audio.get("path", "")).is_file():
        raw = Path(audio["path"]).read_bytes()
        size = fa_digits(f"{len(raw) / 1_000_000:.1f}").replace(".", "٫")
        audio_block = (f'<script type="text/plain" id="aud" data-mime="{_e(audio.get("mime", "audio/mpeg"))}">'
                       f'{base64.b64encode(raw).decode("ascii")}</script>')
        hint = '<p class="hint js-only" hidden>روی هر جمله بزن تا صدا از همان‌جا پخش شود.</p>'
        dl_btn = f'<a class="go alt js-only" id="dl" href="#" download="{_e(audio.get("name", "audio.mp3"))}" hidden>دانلود صدا ({size} مگابایت)</a>'
        play_btn = ('<button class="pp" id="ph-play" type="button" aria-label="پخش">'
                    '<svg class="i-play" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5v15l13-7.5z"/></svg>'
                    '<svg class="i-pause" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4.5h4v15H6zM14 4.5h4v15h-4z"/></svg></button>')
    fonts = embed_fonts() if font_css is None else font_css
    css = CSS.replace("%%LIGHT%%", _LIGHT).replace("%%DARK%%", _DARK)
    disclaimer = "ترجمهٔ ماشینی ممکن است اشتباه داشته باشد." if has_b else ""
    page_title = f"زیرنویس: {title}"

    return f"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex">
<title>{_e(page_title)}</title>
<style>
{fonts}
{css}
</style>
</head>
<body>
<div class="page">
<header class="hero">
<h1 dir="auto">{_e(title)}</h1>
<p class="facts">{"، ".join(facts)}</p>
<ul class="legend">{leg}</ul>
{hint}
{go}{dl_btn}
</header>
<div class="bar js-only" id="bar" hidden>
<div class="find"><input id="q" type="search" placeholder="جستجو در زیرنویس…" autocomplete="off" enterkeyhint="search" aria-label="جستجو در زیرنویس"><button class="btn" id="tools-btn" type="button" aria-expanded="false" aria-controls="tools">تنظیمات</button></div>
<div class="tools" id="tools" hidden><div class="row">{toggles}</div><div class="row"><button class="btn" id="fs-down" type="button" aria-label="کوچک‌تر کردن متن">A−</button><button class="btn" id="fs-up" type="button" aria-label="بزرگ‌تر کردن متن">A+</button><span class="gap"></span><button class="btn" id="theme" type="button"></button></div></div>
<p class="found" id="found" aria-live="polite"></p>
</div>
<main id="cues">
{chr(10).join(items)}
</main>
<footer class="foot">ساخته‌شده با {_e(brand)}. {disclaimer}</footer>
</div>
<div class="ph js-only" id="ph" data-total="{total:.1f}" hidden><div class="pill">{play_btn}<span class="now" id="ph-now">0:00</span><div class="track" id="ph-track" role="slider" tabindex="0" aria-label="جای فعلی در ویدیو" aria-valuemin="0" aria-valuemax="{int(total)}" aria-valuenow="0"><i id="ph-fill"></i><b id="ph-knob"></b></div><span class="tot">{fmt_time(total)}</span></div></div>
{audio_block}
<script>{JS}</script>
</body>
</html>
"""
