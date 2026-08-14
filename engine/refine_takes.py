#!/usr/bin/env python3
"""
refine_takes.py — Apply editorial rules to scenes detected by detect_takes.

Lee scenes (output de detect_takes.py) + whisper transcript JSON + defaults,
y emite scenes refinados aplicando 3 reglas:

1. Padding: expande start/end de cada take con padding_head_sec/padding_tail_sec.
   Si causa overlap con take adyacente, recorta al midpoint del silencio.

2. Skip filler intro: descarta los primeros takes si duran <filler_intro_max_sec
   y matchean palabras técnicas comunes ("ya", "ok", "listo", "empezamos", etc).
   Itera hasta encontrar take "publicable" (no filler).

3. Repetition / autocorrection: para cada par consecutivo (A, B), calcula
   Jaccard similarity de tokens del transcript. Si > repetition_threshold,
   descarta A (la versión antigua, sin corregir).

Usage:
  python3 refine_takes.py \\
    --scenes /tmp/scenes.json \\
    --transcript /tmp/transcript.json \\
    --defaults '{"youtube":{"padding_head_sec":0.15,...}}' \\
    --format youtube_long \\
    --duration 969.8 \\
    --out /tmp/scenes-refined.json
"""

import argparse
import json
import os
import re
import subprocess
import sys


FILLER_PATTERNS = [
    r"^ya\b",
    r"^ok\b",
    r"^okey\b",
    r"^listo\b",
    r"^empezamos\b",
    r"^empieza\b",
    r"^esta(?:mos)?\s+grabando\b",
    r"^esta\s+prendido\b",
    r"^ahora\s+si\b",
    r"^vamos\b",
    r"^a\s+ver\b",
    r"^uno\s+dos\b",
    r"^probando\b",
    r"^check\b",
    r"^bueno\b\s*$",
]

FILLER_RE = re.compile("|".join(FILLER_PATTERNS), re.IGNORECASE)

# Patrones de segments NO-speech que whisper a veces emite cuando no hay habla.
# Excluirlos al construir takes desde whisper.
NON_SPEECH_PATTERNS = [
    r"^\s*\[\s*silencio\s*\]\s*$",
    r"^\s*\[\s*music(a|al)?\s*\]\s*$",
    r"^\s*\[\s*m[uú]sica\s*\]\s*$",
    r"^\s*\[\s*blank[_\s]?audio\s*\]\s*$",
    r"^\s*\(\s*silencio\s*\)\s*$",
    r"^\s*\(\s*sin\s+audio\s*\)\s*$",
    r"^\s*\[\s*sin\s+habla\s*\]\s*$",
    r"^\s*\[\s*ruido\s*\]\s*$",
]
NON_SPEECH_RE = re.compile("|".join(NON_SPEECH_PATTERNS), re.IGNORECASE)


def is_speech_segment(text):
    """True si el texto del segment parece habla real (no tag de silencio/música)."""
    if not text or not text.strip():
        return False
    if NON_SPEECH_RE.match(text):
        return False
    # Heurística: speech real tiene al menos 1 letra
    return bool(re.search(r"[a-záéíóúüñA-ZÁÉÍÓÚÜÑ]", text))


def takes_from_whisper(transcript_segments, min_take_sec=0.5, merge_gap_sec=0.0):
    """
    Construye takes desde whisper segments. Filtra non-speech.

    Por defecto (merge_gap_sec=0.0) NO fusiona — cada segment es su propio take.
    Esto preserva granularidad para que la dedup detecte autocorrecciones
    intra-bloque (cuando el hablante dice algo, se detiene, y retoma con palabras
    similares — esa pausa de 0.5-1s separa segments).

    Si merge_gap_sec > 0, fusiona segments consecutivos con gap < threshold.
    """
    if not transcript_segments:
        return []
    raw = []
    for seg in transcript_segments:
        text = seg.get("text", "").strip()
        if not is_speech_segment(text):
            continue
        s = seg.get("start", 0)
        e = seg.get("end", 0)
        if e <= s:
            continue
        raw.append({"start": float(s), "end": float(e), "text": text})

    if not raw:
        return []

    if merge_gap_sec > 0:
        merged = [raw[0]]
        for cur in raw[1:]:
            prev = merged[-1]
            gap = cur["start"] - prev["end"]
            if gap < merge_gap_sec:
                prev["end"] = cur["end"]
                prev["text"] = prev["text"] + " " + cur["text"]
            else:
                merged.append(cur)
    else:
        merged = raw

    out = []
    for m in merged:
        dur = m["end"] - m["start"]
        if dur >= min_take_sec:
            out.append({
                "start": round(m["start"], 3),
                "end": round(m["end"], 3),
                "duration": round(dur, 3),
                "text": m["text"],
            })
    return out


def normalize_text(text):
    """Lowercase, strip accents, remove non-alnum. Returns list of tokens (orden preservado)."""
    if not text:
        return []
    t = text.lower()
    for a, b in [("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ñ","n"),("ü","u")]:
        t = t.replace(a, b)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return [w for w in t.split() if len(w) > 1]


def normalize_tokens(text):
    """Set de tokens normalizados (sin orden)."""
    return set(normalize_text(text))


def bigrams(tokens):
    """Set de bigramas (pares consecutivos) de la lista de tokens."""
    if len(tokens) < 2:
        return set()
    return set(zip(tokens[:-1], tokens[1:]))


def trigrams(tokens):
    if len(tokens) < 3:
        return set()
    return set(zip(tokens[:-2], tokens[1:-1], tokens[2:]))


def jaccard(a, b):
    if not a and not b:
        return 0.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def has_terminator(text):
    """True if the text ends with closing punctuation (a complete idea)."""
    if not text:
        return False
    t = text.rstrip()
    if not t:
        return False
    return t[-1] in ".!?…"


def tail_tokens(text, n=4):
    """Últimos n tokens normalizados del texto."""
    return normalize_text(text)[-n:]


def head_tokens(text, n=4):
    """Primeros n tokens normalizados del texto."""
    return normalize_text(text)[:n]


def is_continuation(text_a, text_b, tail_n=4, head_n=4, min_overlap=2):
    """
    True si B retoma una idea truncada de A:
    - A NO termina con terminator (idea a medias)
    - El head de B comparte palabras con el tail de A (>= min_overlap tokens compartidos)
    """
    if has_terminator(text_a):
        return False
    tail_a = set(tail_tokens(text_a, tail_n))
    head_b = set(head_tokens(text_b, head_n))
    if not tail_a or not head_b:
        return False
    overlap = len(tail_a & head_b)
    return overlap >= min_overlap


def similarity_score(text_a, text_b):
    """Score combinado: max(jaccard_tokens, jaccard_bigramas). Atrapa re-grabaciones
    aunque las palabras estén levemente cambiadas (Jaccard tokens) o aunque sea
    una continuación con palabras compartidas en secuencia (bigramas)."""
    tok_a = normalize_text(text_a)
    tok_b = normalize_text(text_b)
    if len(tok_a) < 2 or len(tok_b) < 2:
        return 0.0
    j_tok = jaccard(set(tok_a), set(tok_b))
    j_big = jaccard(bigrams(tok_a), bigrams(tok_b))
    j_tri = jaccard(trigrams(tok_a), trigrams(tok_b))
    return max(j_tok, j_big, j_tri)


def transcript_for_window(segments, start, end):
    """Concatena texto de segments cuyo timestamp cae dentro de [start, end]."""
    if not segments:
        return ""
    parts = []
    for s in segments:
        s_start = s.get("start", s.get("t0", s.get("offsets", {}).get("from", 0) / 1000.0 if isinstance(s.get("offsets"), dict) else 0))
        s_end = s.get("end", s.get("t1", s.get("offsets", {}).get("to", 0) / 1000.0 if isinstance(s.get("offsets"), dict) else 0))
        # Overlap test
        if s_end < start or s_start > end:
            continue
        text = s.get("text", "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def load_transcript_segments(path):
    """whisper.cpp -oj output schema: {transcription: [...]} con offsets en ms."""
    if not path:
        return []
    try:
        with open(path) as f:
            j = json.load(f)
    except Exception as e:
        print(f"[refine_takes] WARN: cannot read transcript {path}: {e}", file=sys.stderr)
        return []
    # whisper.cpp -oj: {"transcription": [{"offsets":{"from":ms,"to":ms},"text":"..."}, ...]}
    if isinstance(j, dict) and "transcription" in j:
        out = []
        for seg in j["transcription"]:
            offs = seg.get("offsets") or {}
            out.append({
                "start": offs.get("from", 0) / 1000.0,
                "end": offs.get("to", 0) / 1000.0,
                "text": seg.get("text", ""),
            })
        return out
    # OpenAI whisper-cli formato alternativo: {"segments": [{"start","end","text"},...]}
    if isinstance(j, dict) and "segments" in j:
        return j["segments"]
    print(f"[refine_takes] WARN: transcript schema desconocido en {path}", file=sys.stderr)
    return []


def is_word_level(transcript_segments):
    """Detecta si los segments son word-level (segments cortos, < 2s típicamente)
    o segment-level (segments largos de 3-22s con frases completas)."""
    if not transcript_segments or len(transcript_segments) < 5:
        return False
    durs = [s.get("end", 0) - s.get("start", 0) for s in transcript_segments[:20]]
    durs = [d for d in durs if d > 0]
    if not durs:
        return False
    avg = sum(durs) / len(durs)
    return avg < 2.0  # word-level: ~0.2-1.5s per segment


def find_speech_onset_in_range(transcript_segments, t_start, t_end):
    """Para un rango de tiempo, encuentra el primer y último word-level segment
    con texto real (no silencio). Útil para recortar takes donde whisper segment
    grueso 33-55s tiene speech real solo en 38-52s."""
    speech_segs = []
    for seg in transcript_segments:
        text = seg.get("text", "").strip()
        if not is_speech_segment(text):
            continue
        s = seg.get("start", 0)
        e = seg.get("end", 0)
        if e <= t_start or s >= t_end:
            continue
        # Solo segments que CAEN dentro del rango (al menos parcialmente)
        if s >= t_start and e <= t_end:
            speech_segs.append({"start": s, "end": e, "text": text})
    if not speech_segs:
        return None
    return {
        "first_speech_start": speech_segs[0]["start"],
        "last_speech_end": speech_segs[-1]["end"],
        "concatenated_text": " ".join(s["text"] for s in speech_segs),
    }


def rescue_takes_with_whisper(silence_takes, transcript_segments, min_take_sec=0.5, min_rescue_overlap=0.3):
    """
    Approach híbrido: silencedetect manda en cortes, whisper rescata speech ignorado.

    Algoritmo:
    1. Empieza con la lista de takes de silencedetect (preserva cortes naturales).
    2. Para cada whisper segment SPEECH-real (no silencio/música):
       - Verifica si su rango está cubierto por algún silence_take.
       - Si NO está cubierto → es speech que silencedetect ignoró. Inyectar como take.
    3. **Si whisper es word-level**, recorta cada take rescued al primer y último
       word audible (elimina silencio interno antes/después del speech real).
    4. Merge: ordena por start, fusiona overlaps.
    """
    if not silence_takes and not transcript_segments:
        return []

    word_level = is_word_level(transcript_segments)

    base = [{"start": float(t["start"]), "end": float(t["end"])} for t in silence_takes]

    def is_covered(seg_start, seg_end):
        seg_dur = seg_end - seg_start
        if seg_dur <= 0:
            return True
        for t in base:
            overlap_start = max(t["start"], seg_start)
            overlap_end = min(t["end"], seg_end)
            overlap = overlap_end - overlap_start
            if overlap >= min_rescue_overlap or (overlap / seg_dur) >= 0.5:
                return True
        return False

    # Construir grupos de whisper segments NO cubiertos por silencedetect.
    # Si word-level, agrupar segments adyacentes (gap < 0.5s) en bloques antes
    # de chequear cobertura — sino chequearíamos word-by-word lo cual da takes
    # absurdamente cortos.
    if word_level:
        # Pre-agrupar por gap pequeño
        clusters = []
        cur = None
        for seg in transcript_segments:
            text = seg.get("text", "").strip()
            if not is_speech_segment(text):
                # Silencio rompe cluster
                if cur:
                    clusters.append(cur); cur = None
                continue
            s, e = seg.get("start", 0), seg.get("end", 0)
            if e <= s:
                continue
            if cur is None:
                cur = {"start": s, "end": e, "text": text}
            elif s - cur["end"] < 0.5:
                cur["end"] = e
                cur["text"] = cur["text"] + " " + text
            else:
                clusters.append(cur)
                cur = {"start": s, "end": e, "text": text}
        if cur: clusters.append(cur)
        rescue_candidates = clusters
    else:
        rescue_candidates = []
        for seg in transcript_segments or []:
            text = seg.get("text", "").strip()
            if not is_speech_segment(text):
                continue
            s = seg.get("start", 0)
            e = seg.get("end", 0)
            if e > s:
                rescue_candidates.append({"start": float(s), "end": float(e), "text": text})

    rescued = []
    for cand in rescue_candidates:
        if is_covered(cand["start"], cand["end"]):
            continue
        # Si word-level, ya está recortado a speech real. Si segment-level,
        # intenta recortar usando los word-level segments dentro (si existen).
        # En segment-level no podemos recortar porque whisper devuelve grueso.
        # Solo confiar en lo que tenemos.
        rescued.append(cand)

    if not rescued:
        return [{"start": round(t["start"], 3), "end": round(t["end"], 3),
                 "duration": round(t["end"] - t["start"], 3)} for t in silence_takes]

    all_takes = [{"start": t["start"], "end": t["end"], "text": ""} for t in silence_takes]
    all_takes.extend(rescued)
    all_takes.sort(key=lambda x: x["start"])

    merged = [all_takes[0]]
    for cur in all_takes[1:]:
        prev = merged[-1]
        if cur["start"] <= prev["end"]:
            prev["end"] = max(prev["end"], cur["end"])
            if cur.get("text"):
                prev["text"] = (prev["text"] + " " + cur["text"]).strip() if prev.get("text") else cur["text"]
        else:
            merged.append(cur)

    out = []
    for m in merged:
        dur = m["end"] - m["start"]
        if dur >= min_take_sec:
            t_out = {"start": round(m["start"], 3), "end": round(m["end"], 3),
                     "duration": round(dur, 3)}
            if m.get("text"):
                t_out["text"] = m["text"]
            out.append(t_out)
    return out


def merge_incomplete_sentences(takes, transcript_segments, max_gap_sec=2.0):
    """
    Detecta takes cuyo texto termina sin terminator (`.`, `?`, `!`, `…`) Y el
    siguiente take empieza < max_gap_sec después. Mergea ambos para evitar
    cortar frases a la mitad.

    Esto cubre el caso: el padding no fue suficiente y el cut quedó justo
    antes de la última palabra/sílaba, dejando la frase incompleta.
    """
    if not takes:
        return takes, []
    out = [dict(takes[0])]
    merged_log = []
    for cur in takes[1:]:
        prev = out[-1]
        prev_text = get_text_for_take(prev, transcript_segments)
        gap = cur["start"] - prev["end"]
        # Si previous take no termina con terminator AND gap es chico → merge
        if not has_terminator(prev_text) and gap < max_gap_sec and gap >= 0:
            # Merge: extender prev al end de cur
            new_text = (prev_text + " " + get_text_for_take(cur, transcript_segments)).strip()
            merged_log.append({
                "prev_end": prev["end"],
                "cur_start": cur["start"],
                "gap_sec": round(gap, 3),
                "merged_into_one_take": True,
            })
            prev["end"] = cur["end"]
            prev["duration"] = round(prev["end"] - prev["start"], 3)
            if new_text:
                prev["text"] = new_text
        else:
            out.append(dict(cur))
    return out, merged_log


def find_speech_onset(audio_path, t_start, t_end, threshold_db=-40.0,
                      bucket_sec=0.3, sustained_sec=0.6):
    """
    Encuentra el INSTANTE donde empieza speech audible sostenido.

    Approach: ffmpeg `astats` con buckets de bucket_sec. Para cada bucket
    calcula RMS_level. El onset es el primer bucket donde RMS > threshold_db
    Y se MANTIENE encima durante sustained_sec total (filtra micro-picos
    de ruido ambiente que silencedetect interpretaba como speech).

    Threshold -40dB:
    - Speech audible normal: RMS -26 a -32 dB → >>> threshold (detecta).
    - Speech susurrado iPhone con AGC: RMS -32 a -42 dB → ≈ threshold (detecta lo legítimo).
    - Silencio "real" con ruido ambiente: RMS -48 a -56 dB → << threshold (ignora).

    Sustained 0.6s: silencedetect fallaba con micro-picos cada ~0.5s que rompían
    la regla "silencio sostenido". RMS sostenido 0.6s filtra esos picos.

    Devuelve: offset en segundos relativo a t_start (≥0). 0 si no se detecta
    silencio inicial (speech ya está activo desde el inicio del rango).
    """
    if not audio_path or not os.path.exists(audio_path):
        return 0.0
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-nostats",
                "-ss", f"{t_start:.3f}",
                "-to", f"{t_end:.3f}",
                "-i", audio_path, "-vn",
                "-af", (f"astats=metadata=1:reset=1:length={bucket_sec},"
                        f"ametadata=print:key=lavfi.astats.Overall.RMS_level"),
                "-f", "null", "-",
            ],
            stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            timeout=30,
        )
        log = proc.stderr
        # Output viene como pares: "frame:0 ... pts_time:0.5\n[Parsed] ... RMS_level=-XX.XX"
        ptss = re.findall(r"pts_time:([\d.]+)", log)
        rmss = re.findall(r"RMS_level=(-?[\d.]+|nan|inf|-inf)", log)
        if not ptss or not rmss:
            return 0.0
        pairs = []
        for pts, rms in zip(ptss, rmss):
            try:
                pts_f = float(pts)
                rms_f = float(rms) if rms not in ("nan", "inf", "-inf") else -100.0
                pairs.append((pts_f, rms_f))
            except Exception:
                continue
        if not pairs:
            return 0.0

        # Si el PRIMER bucket ya tiene speech (> threshold), no hay silencio inicial.
        if pairs[0][1] > threshold_db:
            return 0.0

        # Buscar primer bucket con speech sostenido por sustained_sec
        needed_consecutive = max(1, int(round(sustained_sec / bucket_sec)))
        for i, (pts, rms) in enumerate(pairs):
            if rms <= threshold_db:
                continue
            # Verifica sustained
            consecutive = 1
            for j in range(i + 1, min(i + needed_consecutive, len(pairs))):
                if pairs[j][1] > threshold_db:
                    consecutive += 1
                else:
                    break
            if consecutive >= needed_consecutive:
                return max(0.0, pts)

        return 0.0
    except Exception as e:
        print(f"[find_speech_onset] WARN: {e}", file=sys.stderr)
        return 0.0


def trim_silence_in_takes(takes, audio_path, max_trim_sec=20.0, min_remaining_sec=1.0):
    """
    Para cada take, detecta silencio audible al INICIO y recorta sincrónicamente.
    Audio y video se recortan al mismo offset (modificamos solo el start del take;
    el filter_complex de ffmpeg usa los mismos start/end para trim y atrim).

    Aplica a todos los takes (silencio inicial puede aparecer en cualquiera tras
    whisper rescue o silencedetect imperfecto). Solo evita takes muy cortos donde
    el costo de la llamada ffmpeg no se justifica.

    max_trim_sec=20: cubre el caso reportado (silencio inicial de 15-16s).
    min_remaining_sec=1: si recortar dejara take <1s, mantener original.
    """
    if not audio_path or not os.path.exists(audio_path):
        return takes, []
    trimmed = []
    log = []
    for i, t in enumerate(takes):
        dur = t["end"] - t["start"]
        # Skip takes muy cortos donde el recorte no aporta y el costo de ffmpeg
        # se acumula
        if dur < 1.5:
            trimmed.append(t)
            continue
        offset = find_speech_onset(audio_path, t["start"], t["end"])
        if offset <= 0.05:
            trimmed.append(t)
            continue
        offset = min(offset, max_trim_sec)
        new_start = t["start"] + offset
        new_dur = t["end"] - new_start
        if new_dur < min_remaining_sec:
            trimmed.append(t)
            continue
        new_take = dict(t)
        new_take["start"] = round(new_start, 3)
        new_take["duration"] = round(new_dur, 3)
        trimmed.append(new_take)
        log.append({
            "take_idx": i,
            "original_start": t["start"],
            "new_start": new_take["start"],
            "offset_sec": round(offset, 3),
            "took_text_field": "text" in t,
        })
    return trimmed, log


def apply_padding(takes, padding_head, padding_tail, duration):
    """Expand each take with padding; clip to neighbors via midpoint."""
    if not takes:
        return []
    out = []
    n = len(takes)
    for i, t in enumerate(takes):
        new_start = max(0.0, t["start"] - padding_head)
        new_end = min(duration, t["end"] + padding_tail)
        # Clip start si overlap con take anterior
        if i > 0:
            prev = out[-1]
            silence_start = takes[i-1]["end"]
            silence_end = t["start"]
            silence_mid = (silence_start + silence_end) / 2.0
            # prev.end ya está padded; new_start ya está padded
            if new_start < prev["end"]:
                # Reparte el silencio al midpoint
                prev["end"] = min(prev["end"], silence_mid)
                new_start = max(new_start, silence_mid)
        # Clip end si overlap con take siguiente
        if i < n - 1:
            next_silence_start = t["end"]
            next_silence_end = takes[i+1]["start"]
            next_silence_mid = (next_silence_start + next_silence_end) / 2.0
            if new_end > next_silence_mid:
                new_end = next_silence_mid
        new_dur = new_end - new_start
        if new_dur > 0.01:
            out.append({
                "start": round(new_start, 3),
                "end": round(new_end, 3),
                "duration": round(new_dur, 3),
            })
    return out


def get_text_for_take(take, transcript_segments):
    """Si el take ya tiene .text (vino de whisper), úsalo. Sino, busca en transcript."""
    if take.get("text"):
        return take["text"]
    return transcript_for_window(transcript_segments, take["start"], take["end"])


def skip_filler_intro(takes, transcript_segments, max_sec):
    """Descarta primeros takes que son técnicos (filler o muy cortos sin sustancia)."""
    dropped = []
    while takes:
        first = takes[0]
        dur = first["end"] - first["start"]
        text = get_text_for_take(first, transcript_segments).strip()
        is_short = dur < max_sec
        is_filler = bool(text and FILLER_RE.search(text))
        is_empty_short = is_short and len(text) < 8
        if is_filler or is_empty_short:
            dropped.append({"take": first, "text": text, "reason": "filler" if is_filler else "empty_short"})
            takes = takes[1:]
            continue
        break
    return takes, dropped


def remove_repetitions(takes, transcript_segments, threshold):
    """
    Para cada par consecutivo (A,B), aplica DOS detecciones:

    1. **Similarity score** (max de Jaccard tokens / bigramas / trigramas) ≥ threshold
       → re-grabación con palabras cambiadas pero idea similar. DROP A.

    2. **Truncated continuation**: A NO termina con terminator (idea cortada)
       Y el head de B comparte ≥2 palabras con el tail de A. → DROP A (idea a medias).

    Devuelve takes filtrados + lista de descartados con razón.
    """
    if not takes or threshold <= 0:
        return takes, []
    out = []
    dropped = []
    i = 0
    while i < len(takes):
        cur = takes[i]
        if i + 1 < len(takes):
            nxt = takes[i+1]
            text_cur = get_text_for_take(cur, transcript_segments)
            text_nxt = get_text_for_take(nxt, transcript_segments)
            tok_cur = normalize_text(text_cur)
            tok_nxt = normalize_text(text_nxt)

            if len(tok_cur) >= 3 and len(tok_nxt) >= 3:
                sim = similarity_score(text_cur, text_nxt)
                if sim >= threshold:
                    dropped.append({
                        "take": cur,
                        "reason": "similarity",
                        "sim": round(sim, 3),
                        "text_a": text_cur,
                        "text_b": text_nxt,
                    })
                    i += 1
                    continue

                # Detección de continuación (idea a medias)
                if is_continuation(text_cur, text_nxt):
                    dropped.append({
                        "take": cur,
                        "reason": "truncated_continuation",
                        "text_a": text_cur,
                        "text_b": text_nxt,
                    })
                    i += 1
                    continue
        out.append(cur)
        i += 1
    return out, dropped


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenes", required=True, help="Path to scenes.json from detect_takes")
    p.add_argument("--transcript", default=None, help="Path to whisper transcript JSON (optional)")
    p.add_argument("--defaults", default="{}", help="JSON string with editing defaults")
    p.add_argument("--format", default="youtube_long", help="Format key in defaults (youtube/shorts)")
    p.add_argument("--duration", type=float, default=0.0, help="Video duration sec (for padding clamp)")
    p.add_argument("--take-source", default=None,
                   choices=["silencedetect", "whisper", "silencedetect_plus_whisper_rescue", "auto"],
                   help="Origen de takes: silencedetect (legacy), whisper (transcript-based), "
                        "silencedetect_plus_whisper_rescue (silencedetect base + inyecta whisper segments "
                        "no cubiertos para rescatar speech susurrado), auto (whisper si transcript, else silencedetect)")
    p.add_argument("--audio-path", default=None,
                   help="Path al WAV/MP3 extraído del source (16kHz mono típico). Si se da, refine_takes "
                        "hace silencedetect interno para recortar silencio audible al inicio de cada take.")
    p.add_argument("--out", required=True, help="Output path for refined scenes JSON")
    args = p.parse_args()

    with open(args.scenes) as f:
        scenes = json.load(f)
    raw_silence_takes = scenes.get("takes", [])
    duration = args.duration or scenes.get("video_duration_sec", 0.0)

    defaults = json.loads(args.defaults) if args.defaults else {}
    # Defaults can be: full structure {youtube: {...}} or already format-scoped
    # Si trae 'youtube' o 'shorts' como key, dive in
    if args.format in defaults:
        cfg = defaults[args.format]
    elif "youtube" in defaults and args.format.startswith("youtube"):
        cfg = defaults["youtube"]
    else:
        cfg = defaults

    padding_head = float(cfg.get("padding_head_sec", 0.15))
    padding_tail = float(cfg.get("padding_tail_sec", 0.35))
    filler_intro_max = float(cfg.get("filler_intro_max_sec", 3.0))
    # Threshold bajo (0.45) por default — Jaccard/bigramas/trigramas atrapan
    # autocorrecciones aunque las palabras cambien levemente.
    repetition_threshold = float(cfg.get("repetition_threshold", 0.45))
    min_take_sec = float(cfg.get("min_take_sec", 0.5))
    # Por default NO mergeamos whisper segments — preservamos granularidad
    # para que la dedup detecte autocorrecciones entre segments adyacentes.
    merge_gap_sec = float(cfg.get("whisper_merge_gap_sec", 0.0))

    transcript_segments = load_transcript_segments(args.transcript)

    # Resolver take_source con precedencia: CLI flag > defaults > auto
    take_source = args.take_source or cfg.get("take_source") or "auto"
    if take_source == "auto":
        take_source = "whisper" if transcript_segments else "silencedetect"

    if take_source in ("whisper", "silencedetect_plus_whisper_rescue") and not transcript_segments:
        print(f"[refine_takes] WARN: take_source={take_source} pero sin transcript; cayendo a silencedetect", file=sys.stderr)
        take_source = "silencedetect"

    if take_source == "whisper":
        takes = takes_from_whisper(transcript_segments, min_take_sec=min_take_sec, merge_gap_sec=merge_gap_sec)
    elif take_source == "silencedetect_plus_whisper_rescue":
        # Approach híbrido: silencedetect manda en cortes (preserva ritmo natural),
        # whisper se usa solo para inyectar takes adicionales donde silencedetect
        # ignoró speech (típicamente intro susurrado del iPhone con AGC).
        takes = rescue_takes_with_whisper(
            raw_silence_takes, transcript_segments,
            min_take_sec=min_take_sec,
        )
    else:
        takes = list(raw_silence_takes)

    original_count = len(takes)

    # Step 1: skip filler intro (requiere transcript para mejor detección)
    takes, dropped_intro = skip_filler_intro(takes, transcript_segments, filler_intro_max)

    # Step 2: remove repetitions
    takes, dropped_repetitions = remove_repetitions(takes, transcript_segments, repetition_threshold)

    # Step 2.5: merge incomplete sentences (frases sin terminator que continúan
    # en el siguiente take dentro de gap chico). Evita frases cortadas a media
    # palabra que el usuario reportó.
    incomplete_merge_gap = float(cfg.get("incomplete_merge_gap_sec", 2.0))
    takes, merged_sentences = merge_incomplete_sentences(takes, transcript_segments, incomplete_merge_gap)

    # Step 2.75: trim silencio audible interno al INICIO de cada take rescued.
    # iPhone con AGC produce speech susurrado donde whisper marca segment 33-55s
    # pero el speech REAL audible empieza ~38s. silencedetect interno con
    # threshold -45dB detecta el primer onset preciso y ajusta sincrónicamente
    # (audio + video se recortan al mismo offset).
    trim_log = []
    if args.audio_path:
        takes, trim_log = trim_silence_in_takes(takes, args.audio_path)

    # Step 3: padding (último, para no perder takes antes de evaluar overlap)
    takes_padded = apply_padding(takes, padding_head, padding_tail, duration)

    result = {
        "takes": takes_padded,
        "applied": {
            "take_source": take_source,
            "padding_head_sec": padding_head,
            "padding_tail_sec": padding_tail,
            "filler_intro_max_sec": filler_intro_max,
            "repetition_threshold": repetition_threshold,
            "min_take_sec": min_take_sec,
            "whisper_merge_gap_sec": merge_gap_sec,
            "transcript_provided": bool(transcript_segments),
        },
        "summary": {
            "take_source": take_source,
            "silencedetect_takes": len(raw_silence_takes),
            "whisper_takes": len(takes_from_whisper(transcript_segments, min_take_sec=min_take_sec, merge_gap_sec=merge_gap_sec)) if transcript_segments else 0,
            "word_level_transcript": is_word_level(transcript_segments),
            "source_takes": original_count,
            "original_takes": original_count,  # compat con executor log
            "dropped_intro": len(dropped_intro),
            "dropped_repetitions": len(dropped_repetitions),
            "merged_incomplete_sentences": len(merged_sentences),
            "trimmed_silence_in_takes": len(trim_log),
            "final_takes": len(takes_padded),
        },
        "dropped_intro_detail": dropped_intro,
        "dropped_repetitions_detail": dropped_repetitions,
        "merged_sentences_detail": merged_sentences,
        "trimmed_silence_detail": trim_log,
        "video_duration_sec": duration,
    }

    txt = json.dumps(result, indent=2, ensure_ascii=False)
    print(txt)
    with open(args.out, "w") as f:
        f.write(txt + "\n")


if __name__ == "__main__":
    main()
