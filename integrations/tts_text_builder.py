"""tts_text_builder - convert narration (display/teacher text) into TTS-safe spoken Persian.

Produces `tts_text` from `narration_text` by:
  - converting math symbols/formulas to spoken Persian (no raw `⟨ ⟩`, `=`, `^2`, ...),
  - applying a pronunciation dictionary (LDA, SVM, i.i.d., ...),
  - stripping non-spoken tokens (file paths, page_0NN ids, URLs),
  - inserting lightweight pause markers at sentence/segment boundaries.

Generic: the pronunciation dict can be extended per run; nothing here is baked to
file 1/3. Returns plain text that edge-tts (and other providers) can read naturally.
"""
from __future__ import annotations

import re

# Pause token: a textual marker the TTS stage can map to a real pause; also reads
# naturally as a short break via the trailing period.
PAUSE = " … "

# Base pronunciation dictionary (extend per-run via build_tts_text(..., pron_dict=...)).
BASE_PRON = {
    "Logistic regression": "رگرسیونِ لجستیک",
    "Linear Classification": "دسته‌بندی خطی",
    "i.i.d.": "مستقل و هم‌توزیع",
    "i.i.d": "مستقل و هم‌توزیع",
    "LDA": "تحلیلِ تفکیکِ خطی",
    "SVM": "ماشینِ بردارِ پشتیبان",
    "Bibliography": "منابع",
    "Appendix": "پیوست",
    "Bishop": "بیشاپ",
    "ESL": "کتابِ ESL",
    "hinge": "لولا",
    "Logistic": "لجستیک",
}

# Ordered (longest-first) symbol/formula -> spoken Persian.
_SYMBOL_MAP = [
    ("⟨w, Φ(x)⟩", " ضربِ داخلیِ w در فیِ ایکس "),
    ("⟨w,Φ(x)⟩", " ضربِ داخلیِ w در فیِ ایکس "),
    ("⟨w, x⟩", " ضربِ داخلیِ w در x "),
    ("⟨w,x⟩", " ضربِ داخلیِ w در x "),
    ("{-1,+1}", " مجموعهٔ منفی‌یک و مثبت‌یک "),
    ("{-1, +1}", " مجموعهٔ منفی‌یک و مثبت‌یک "),
    ("f(x)=0", " اف‌ایکس برابرِ صفر "),
    ("f(x)", " اف‌ایکس "),
    ("ŷ(x)", " وای‌هتِ ایکس "),
    ("Φ(x)", " فیِ ایکس "),
    ("sign", " علامتِ "),
    ("max", " بیشینهٔ "),
    ("min", " کمینهٔ "),
    ("log", " لگاریتمِ "),
    ("exp", " نماییِ "),
    ("^2", " به‌توانِ دو "),
    ("²", " به‌توانِ دو "),
    ("R^d", " فضای آر به توانِ d "),
    ("||w||", " اندازهٔ w "),
    ("≤", " کوچک‌تر یا مساوی "),
    ("≥", " بزرگ‌تر یا مساوی "),
    ("≠", " نامساوی "),
    ("∈", " عضوِ "),
    ("Σ", " سیگما "),
    ("∫", " انتگرالِ "),
    ("Φ", " فی "),
    ("φ", " فی "),
    ("ℓ", " ال "),
    ("·", " ضرب در "),
    ("×", " ضرب در "),
    ("⟨", " "), ("⟩", " "),
]

# regex symbol conversions that need context (avoid touching word-internal chars)
_RE_EQ = re.compile(r"\s*=\s*")
_RE_PLUS = re.compile(r"\s*\+\s*")
_RE_MINUS = re.compile(r"(?<=[\w\)\dا-ی])\s*[-−]\s*(?=[\w\(\dا-ی])")
_RE_SLASH = re.compile(r"(?<=\w)\s*/\s*(?=\w)")

# non-spoken tokens to drop
_RE_PATH = re.compile(r"\b\w+/[\w/.\-]+")          # extracted/pages/...
_RE_PAGEID = re.compile(r"\bpage_0*\d+\b")
_RE_URL = re.compile(r"https?://\S+")
_RE_WS = re.compile(r"[ \t]{2,}")


def build_tts_text(narration_text: str, pron_dict: dict = None) -> str:
    if not narration_text:
        return ""
    t = narration_text
    # 1) strip non-spoken tokens
    t = _RE_URL.sub(" ", t)
    t = _RE_PAGEID.sub(" ", t)
    t = _RE_PATH.sub(" ", t)
    # 2) pronunciation dictionary (longest-first)
    pron = dict(BASE_PRON)
    if pron_dict:
        pron.update(pron_dict)
    for term in sorted(pron, key=len, reverse=True):
        t = t.replace(term, " " + pron[term] + " ")
    # 3) symbol/formula -> spoken (longest-first list)
    for sym, spoken in _SYMBOL_MAP:
        if sym in t:
            t = t.replace(sym, spoken)
    # 4) contextual operators
    t = _RE_EQ.sub(" برابرِ ", t)
    t = _RE_PLUS.sub(" به‌علاوهٔ ", t)
    t = _RE_MINUS.sub(" منهای ", t)
    t = _RE_SLASH.sub(" تقسیم بر ", t)
    # 5) pause markers at sentence/segment boundaries
    t = t.replace("؛", PAUSE).replace(":", PAUSE)
    t = re.sub(r"\.(\s|$)", PAUSE, t)
    # 6) whitespace cleanup
    t = _RE_WS.sub(" ", t).strip()
    return t


def has_raw_formula(text: str) -> bool:
    """True if TTS text still contains raw math symbols that would read badly."""
    return bool(re.search(r"[=+×·∈≤≥≠ΣΦφ∫⟨⟩²]|\^2|f\(x\)|⟨w", text or ""))
