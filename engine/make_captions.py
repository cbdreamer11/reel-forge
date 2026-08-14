#!/usr/bin/env python3
"""
make_captions.py — Convert whisper-cli word-timestamp JSON (run with `-ml 0`)
into a styled ASS subtitle file, with brand colors and per-brand "accent"
words highlighted.

Usage:
  python3 make_captions.py --input body.json --out captions.ass \
      --brand my-profile \
      [--accent-words-file path/to/words.json]

Algorithm:
  - Group whisper word-tokens into caption blocks.
  - Flush a block on sentence-ending punctuation (.!?) or when limits hit:
      max 58 chars per block, max 10 words.
  - Within each block, identify 1-2 longest content words (skipping Spanish
    stopwords) and color them with the brand accent color.
  - Render at most 2 lines per block, each ~28 chars wide.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


SPANISH_STOPWORDS = {
    "a", "al", "algo", "algunas", "algunos", "ante", "antes", "como", "con",
    "contra", "cual", "cuales", "cuando", "de", "del", "desde", "donde",
    "durante", "e", "el", "ella", "ellas", "ellos", "en", "entre", "era",
    "erais", "eran", "eras", "eres", "es", "esa", "esas", "ese", "eso",
    "esos", "esta", "estaba", "estabais", "estaban", "estabas", "estad",
    "estada", "estadas", "estado", "estados", "estamos", "estan", "estar",
    "estara", "estaran", "estare", "estareis", "estaremos", "estaria",
    "estariais", "estariamos", "estarian", "estarias", "estas", "este",
    "esteis", "estemos", "esten", "estes", "esto", "estos", "estoy",
    "estuve", "estuviera", "estuvieran", "estuvieras", "estuvieron",
    "estuviese", "estuviesen", "estuvieses", "estuvimos", "estuviste",
    "estuvisteis", "estuvo", "fue", "fuera", "fueran", "fueras", "fueron",
    "fuese", "fuesen", "fueses", "fui", "fuimos", "fuiste", "fuisteis",
    "ha", "habeis", "haber", "habia", "habiais", "habiamos", "habian",
    "habias", "habida", "habidas", "habido", "habidos", "habiendo",
    "habra", "habran", "habras", "habre", "habreis", "habremos", "habria",
    "habriais", "habriamos", "habrian", "habrias", "han", "has", "hasta",
    "hay", "haya", "hayais", "hayamos", "hayan", "hayas", "he", "hemos",
    "hube", "hubiera", "hubieran", "hubieras", "hubieron", "hubiese",
    "hubiesen", "hubieses", "hubimos", "hubiste", "hubisteis", "hubo",
    "la", "las", "le", "les", "lo", "los", "mas", "me", "mi", "mia",
    "mias", "mio", "mios", "mis", "mucho", "muchos", "muy", "nada", "ni",
    "no", "nos", "nosotras", "nosotros", "nuestra", "nuestras", "nuestro",
    "nuestros", "o", "os", "otra", "otras", "otro", "otros", "para",
    "pero", "poco", "por", "porque", "que", "quien", "quienes", "se",
    "sea", "seais", "seamos", "sean", "seas", "ser", "sera", "seran",
    "seras", "sere", "sereis", "seremos", "seria", "seriais", "seriamos",
    "serian", "serias", "si", "sido", "siendo", "sin", "sobre", "sois",
    "somos", "son", "soy", "su", "sus", "suya", "suyas", "suyo", "suyos",
    "tambien", "tanto", "te", "tendra", "tendran", "tendras", "tendre",
    "tendreis", "tendremos", "tendria", "tendriais", "tendriamos",
    "tendrian", "tendrias", "tened", "teneis", "tenemos", "tener", "tenga",
    "tengais", "tengamos", "tengan", "tengas", "tengo", "tenia", "teniais",
    "teniamos", "tenian", "tenias", "tenida", "tenidas", "tenido",
    "tenidos", "teniendo", "ti", "tiene", "tienen", "tienes", "todo",
    "todos", "tu", "tus", "tuviera", "tuvieran", "tuvieras", "tuvieron",
    "tuviese", "tuviesen", "tuvieses", "tuvimos", "tuviste", "tuvisteis",
    "tuvo", "tuya", "tuyas", "tuyo", "tuyos", "un", "una", "uno", "unos",
    "vosotras", "vosotros", "vuestra", "vuestras", "vuestro", "vuestros",
    "y", "ya", "yo",
}


def strip_accents_for_match(s):
    repl = (
        ("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"),
        ("ú", "u"), ("ü", "u"), ("ñ", "n"),
        ("Á", "A"), ("É", "E"), ("Í", "I"), ("Ó", "O"),
        ("Ú", "U"), ("Ü", "U"), ("Ñ", "N"),
    )
    for a, b in repl:
        s = s.replace(a, b)
    return s


def hex_to_ass_bgr(hex_color):
    """#RRGGBB -> &H00BBGGRR (ASS color literal)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"expected #RRGGBB, got {hex_color!r}")
    rr = h[0:2].upper()
    gg = h[2:4].upper()
    bb = h[4:6].upper()
    return f"&H00{bb}{gg}{rr}"


def fmt_ts(t):
    """Seconds -> H:MM:SS.cs (ASS centiseconds)."""
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


# Used when no profile is configured. Rendering must work with zero branding,
# because "start with no intro" is the recommended way to begin — requiring a
# profile would make the documented first run impossible.
DEFAULT_PROFILE = {
    "profile_id": "default",
    "name": "",
    "colors": {"background": "#111111", "text": "#FAFAFA", "accent": "#4C8DFF"},
    "typography": {"sans": "Inter", "serif": "Playfair Display"},
}


def profile_dirs(brand_id):
    """Where a profile may live, in priority order. Configurable, never assumed."""
    out = []
    env = os.environ.get("REELFORGE_PROFILE_DIR")
    if env:
        out.append(Path(os.path.expanduser(env)))
    for base in (Path.cwd(), REPO_ROOT):
        out.append(base / "profiles" / brand_id)
        out.append(base / "brands" / brand_id)
    return out


def load_brand(brand_id):
    """Load a profile, or fall back to neutral defaults.

    A missing profile is not an error: it means the user has not set up branding
    yet, which is both common and recommended for a first render.
    """
    if not brand_id:
        return dict(DEFAULT_PROFILE)
    for d in profile_dirs(brand_id):
        for name in ("profile.json", "brand.json"):
            p = d / name
            if p.exists():
                with open(p) as f:
                    return json.load(f)
    print(f"[make_captions] no profile '{brand_id}' found; using defaults",
          file=sys.stderr)
    return dict(DEFAULT_PROFILE)


def load_accent_words(path_or_none, brand_id):
    """Optional word list that always gets the accent color. Absent is fine —
    the renderer then picks accent words by length, as it does by default."""
    if path_or_none:
        p = Path(path_or_none)
    elif brand_id:
        p = None
        for d in profile_dirs(brand_id):
            cand = d / "memory" / "caption_accent_words.json"
            if cand.exists():
                p = cand
                break
        if p is None:
            return set()
    else:
        return set()
    if not p.exists():
        return set()
    with open(p) as f:
        words = json.load(f)
    return {strip_accents_for_match(w.lower()) for w in words}


def extract_words(whisper_json):
    """
    Return list of {start, end, text} at WORD granularity.

    If whisper-cli was run with `-ml 0` (word-level), each entry already has
    one token + per-token timestamps — used as-is.

    If whisper-cli emitted segment-level bursts (one entry per ~3-5s burst
    with multi-word text), split each burst into individual words by
    distributing the burst's [start, end] proportionally to each word's
    character count. This gives an approximate per-word timing useful for
    captions even without true word-level whisper.
    """
    out = []
    segs = whisper_json.get("transcription") or whisper_json.get("segments") or []
    for s in segs:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if text.startswith("[") and text.endswith("]"):
            continue
        offsets = s.get("offsets") or {}
        if "from" in offsets and "to" in offsets:
            start = float(offsets["from"]) / 1000.0
            end = float(offsets["to"]) / 1000.0
        else:
            ts = s.get("timestamps") or {}
            start = parse_ts(ts.get("from", "00:00:00,000"))
            end = parse_ts(ts.get("to", "00:00:00,000"))
        words = text.split()
        if len(words) <= 1:
            out.append({"start": start, "end": end, "text": text})
            continue
        # Multi-word burst → split proportionally by char count
        char_lens = [len(w) for w in words]
        total = sum(char_lens) or 1
        dur = max(0.0, end - start)
        cur = start
        for w, cl in zip(words, char_lens):
            w_dur = dur * (cl / total)
            out.append({"start": cur, "end": cur + w_dur, "text": w})
            cur += w_dur
    return out


def parse_ts(s):
    # "HH:MM:SS,mmm"
    s = s.replace(",", ".")
    parts = s.split(":")
    if len(parts) == 3:
        h, m, sec = parts
        return int(h) * 3600 + int(m) * 60 + float(sec)
    return float(s)


def group_blocks(words, max_chars=32, max_words=5):
    """Group word-level tokens into single-line caption blocks (≤max_words).

    Defaults aim at 4-5 words per caption so each line shows briefly and
    syncs roughly with the spoken phrase.
    """
    blocks = []
    cur = []
    cur_chars = 0
    for w in words:
        token = w["text"]
        added = len(token) + (1 if cur else 0)
        if cur and (cur_chars + added > max_chars or len(cur) >= max_words):
            blocks.append(cur)
            cur, cur_chars = [], 0
        cur.append(w)
        cur_chars += added
        if re.search(r"[.!?](\s|$)|[.!?]$", token):
            blocks.append(cur)
            cur, cur_chars = [], 0
    if cur:
        blocks.append(cur)
    return blocks


def two_line_wrap(words, target_line_chars=28):
    """Split words into up to two lines, balancing length."""
    text = " ".join(w["text"].strip() for w in words).strip()
    if len(text) <= target_line_chars:
        return [text]
    # find split point near middle that breaks at a space
    toks = text.split()
    if len(toks) == 1:
        return [text]
    # greedy: accumulate until > half
    total = len(text)
    line1 = ""
    i = 0
    while i < len(toks):
        trial = (line1 + " " + toks[i]).strip()
        if len(trial) > total // 2 + 2 and line1:
            break
        line1 = trial
        i += 1
    line2 = " ".join(toks[i:])
    if not line2:
        return [line1]
    return [line1, line2]


def pick_accents(words, accent_words, n=1, fallback_min_words=4,
                 fallback_min_len=5):
    """Pick up to n words to accent (accent color).

    1) Brand-listed words first.
    2) Fallback: if no brand word AND block has ≥fallback_min_words content
       words, pick the longest non-stopword (≥fallback_min_len chars) — keeps
       rhythm: long-enough lines always carry one accent word, short
       lines stay clean.
    """
    brand_cands = []
    fallback_cands = []
    for idx, w in enumerate(words):
        raw = re.sub(r"[^\wÀ-ſ]", "", w["text"]).lower()
        if not raw or raw in SPANISH_STOPWORDS:
            continue
        norm = strip_accents_for_match(raw)
        if norm in accent_words:
            brand_cands.append((idx, len(raw)))
        elif len(raw) >= fallback_min_len:
            fallback_cands.append((idx, len(raw)))
    if brand_cands:
        brand_cands.sort(key=lambda x: -x[1])
        return {c[0] for c in brand_cands[:n]}
    # Fallback only for long-enough blocks
    content_count = sum(1 for w in words
                        if re.sub(r"[^\wÀ-ſ]", "", w["text"]).lower()
                        not in SPANISH_STOPWORDS
                        and re.sub(r"[^\wÀ-ſ]", "", w["text"]))
    if content_count >= fallback_min_words and fallback_cands:
        fallback_cands.sort(key=lambda x: -x[1])
        return {c[0] for c in fallback_cands[:n]}
    return set()


def render_block_text(words, accent_idx, primary_bgr, accent_bgr,
                      target_line_chars=28):
    """Build single-line ASS text with inline color overrides for accents.

    With max_words=5 we keep one line per block (no \\N). Each accented
    word is wrapped in `{\\caccent}word{\\cprimary}` overrides.
    """
    pieces = []
    for idx, w in enumerate(words):
        w_text = w["text"].strip()
        if idx in accent_idx:
            pieces.append(f"{{\\c{accent_bgr}}}{w_text}{{\\c{primary_bgr}}}")
        else:
            pieces.append(w_text)
    return " ".join(pieces)


ASS_HEADER_TMPL = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Inter,58,{primary},&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,3,2,2,80,80,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="whisper-cli JSON (`-ml 0`)")
    p.add_argument("--out", required=True, help="Output .ass path")
    p.add_argument("--brand", required=True, help="Brand id")
    p.add_argument("--accent-words-file", default=None,
                   help="Override path for caption_accent_words.json")
    p.add_argument("--max-chars", type=int, default=32)
    p.add_argument("--max-words", type=int, default=5)
    p.add_argument("--line-chars", type=int, default=32)
    p.add_argument("--accent-n", type=int, default=1,
                   help="Accent words per block (1 = one accent word per line)")
    args = p.parse_args()

    with open(args.input) as f:
        wjson = json.load(f)

    brand = load_brand(args.brand)
    vs = brand.get("videoSpec", {})
    primary_hex = vs.get("captionPrimaryColor", "#FAFAFA")
    accent_hex = vs.get("captionAccentColor", "#4C8DFF")
    primary_bgr = hex_to_ass_bgr(primary_hex)
    accent_bgr = hex_to_ass_bgr(accent_hex)

    accent_words = load_accent_words(args.accent_words_file, args.brand)

    words = extract_words(wjson)
    if not words:
        print("[error] no words found in whisper JSON", file=sys.stderr)
        sys.exit(2)

    blocks = group_blocks(words, max_chars=args.max_chars,
                          max_words=args.max_words)

    lines = [ASS_HEADER_TMPL.format(primary=primary_bgr)]
    for block in blocks:
        if not block:
            continue
        start = fmt_ts(block[0]["start"])
        end = fmt_ts(block[-1]["end"])
        accent_idx = pick_accents(block, accent_words, n=args.accent_n)
        text = render_block_text(block, accent_idx, primary_bgr, accent_bgr,
                                 target_line_chars=args.line_chars)
        lines.append(
            f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}"
        )

    out = "\n".join(lines) + "\n"
    with open(args.out, "w") as f:
        f.write(out)
    print(f"wrote {args.out} with {len(blocks)} blocks")


if __name__ == "__main__":
    main()
