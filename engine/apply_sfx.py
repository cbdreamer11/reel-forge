#!/usr/bin/env python3
"""
apply_sfx.py — post-process pass that mixes brand SFX (whoosh on cuts +
burn on emphasis phrases) over a finished shorts mp4.

Lo invoca `tools/video-edit-executor.js::pipelineShorts` después de que
`build_reel.py` ya produjo el reel con cover + cuts + outro. Mezcla SFX
en una sola pasada de ffmpeg sobre el mp4 final.

Por qué post-process (no inyectado en build_reel.py): build_reel encadena
varios pasos (extract → concat → caption burn → final concat con cover/outro)
que ya están entrenados y probados. Tocarlos es alto riesgo. Una pasada
extra de ~10–20s para re-encode con sfx mixed in es aceptable y aísla la
lógica nueva.

Inputs:
  --input PATH          mp4 entrada (output de build_reel)
  --output PATH         mp4 salida (con sfx)
  --db PATH             agency.db
  --brand-id STR        para query brand_assets
  --whoosh-offsets JSON lista de floats en segundos (timeline del video final)
  --transcript PATH     whisper json del body (opcional, para burns)
  --body-offset-sec F   offset (cover_dur) entre transcript timeline y video final
  --whoosh-volume-db F  default -18
  --burn-volume-db F    default -22
  --max-whoosh INT      cap pool
  --max-burn INT        cap pool
  --enable-burn BOOL    "1" o "0"

Comportamiento si la marca no tiene SFX registrados:
  log "no sfx assets found for brand X, skipping" + copia input → output.

Updates use_count + last_used_at en brand_assets para cada sfx usado.
"""

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


# ---------- DB ----------
def query_sfx_pool(db_path, brand_id, kind):
    """kind: 'whoosh' o 'burn'. Match por substring de file_path."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT id, file_path FROM brand_assets
            WHERE brand_id = ?
              AND asset_type = 'sfx'
              AND enabled = 1
              AND file_path LIKE ?
            """,
            (brand_id, f"%/{kind}/%"),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]
    finally:
        conn.close()


def bump_use_count(db_path, asset_ids):
    if not asset_ids:
        return
    conn = sqlite3.connect(db_path)
    try:
        now = int(time.time() * 1000)
        # one statement per id keeps things simple; pool is tiny (< 20)
        for aid in asset_ids:
            conn.execute(
                "UPDATE brand_assets SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                (now, aid),
            )
        conn.commit()
    finally:
        conn.close()


# ---------- transcript → burn timestamps ----------
def load_whisper_segments(path):
    if not path or not Path(path).exists():
        return []
    try:
        j = json.loads(Path(path).read_text())
    except Exception as e:
        print(f"[apply_sfx] WARN: cannot parse transcript {path}: {e}", file=sys.stderr)
        return []
    if isinstance(j, dict) and "transcription" in j:
        out = []
        for seg in j["transcription"]:
            offs = seg.get("offsets") or {}
            out.append(
                {
                    "start": offs.get("from", 0) / 1000.0,
                    "end": offs.get("to", 0) / 1000.0,
                    "text": seg.get("text", ""),
                }
            )
        return out
    if isinstance(j, dict) and "segments" in j:
        return j["segments"]
    return []


# Heurística minimalista de "high-energy phrase" sin LLM: frase corta-a-media,
# termina con puntuación fuerte, no es muletilla. Suficiente para sembrar burns
# en declaraciones/revelaciones. Si la marca quiere algo más fino, sustituir
# por una call al LLM en el caller.
FILLER_RE = re.compile(
    r"\b(eh|um|este|o\s*sea|digo|ya|ok(?:ey)?|listo|empezamos|bueno|a\s*ver|probando|check)\b",
    re.IGNORECASE,
)


def pick_burn_offsets(segments, max_n, body_offset_sec):
    if not segments or max_n <= 0:
        return []
    candidates = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if FILLER_RE.search(text):
            continue
        n = len(text)
        if n < 10 or n > 70:
            continue
        # bonus para frases que terminan en . ! ?
        ends_strong = text.rstrip().endswith((".", "!", "?"))
        candidates.append(
            (
                float(s.get("start", 0)),
                text,
                n,
                ends_strong,
            )
        )
    if not candidates:
        return []
    # Ordena por (terminación fuerte primero, luego más corta) y toma 2x max
    candidates.sort(key=lambda x: (0 if x[3] else 1, x[2]))
    pool = candidates[: max_n * 3]
    # Distribuye temporalmente: ordena por tiempo y muestrea cada N para evitar
    # que todos los burns caigan al inicio.
    pool.sort(key=lambda x: x[0])
    if len(pool) <= max_n:
        selected = pool
    else:
        step = len(pool) // max_n
        selected = [pool[i * step] for i in range(max_n)]
    return [t + body_offset_sec for (t, *_rest) in selected]


# ---------- ffmpeg compose ----------
def build_filter_complex(whoosh_pairs, burn_pairs, whoosh_db, burn_db):
    """
    whoosh_pairs: list of (input_index, offset_sec) — input_index = ffmpeg -i index
    burn_pairs:   list of (input_index, offset_sec)
    Returns (filter_complex_str, final_audio_label).
    Voice track lives at [0:a]; sfx files are [1:a]..[N:a].
    """
    parts = []
    mix_labels = ["[0:a]"]

    for inp_idx, offset in whoosh_pairs:
        ms = max(0, int(offset * 1000))
        label = f"w{inp_idx}"
        # adelay ambos canales; volume en dB
        parts.append(
            f"[{inp_idx}:a]aformat=channel_layouts=stereo,"
            f"adelay={ms}|{ms},volume={whoosh_db}dB[{label}]"
        )
        mix_labels.append(f"[{label}]")

    for inp_idx, offset in burn_pairs:
        ms = max(0, int(offset * 1000))
        label = f"b{inp_idx}"
        parts.append(
            f"[{inp_idx}:a]aformat=channel_layouts=stereo,"
            f"adelay={ms}|{ms},volume={burn_db}dB[{label}]"
        )
        mix_labels.append(f"[{label}]")

    n_inputs = len(mix_labels)
    # amix sin normalize=0 reduce el master, lo cual no queremos. Con dropout_transition=0
    # mantenemos el bus a volumen constante incluso cuando los sfx terminan.
    parts.append(
        f"{''.join(mix_labels)}amix=inputs={n_inputs}:duration=first:"
        f"normalize=0:dropout_transition=0[aout]"
    )
    return ";".join(parts), "[aout]"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--brand-id", required=True)
    ap.add_argument(
        "--whoosh-offsets",
        default="[]",
        help="JSON list of floats (seconds, final-video timeline)",
    )
    ap.add_argument("--transcript", default="", help="whisper JSON (optional)")
    ap.add_argument(
        "--body-offset-sec",
        type=float,
        default=1.0,
        help="Seconds between transcript t=0 and final-video t=0 (usually cover_dur)",
    )
    ap.add_argument("--whoosh-volume-db", type=float, default=-18.0)
    ap.add_argument("--burn-volume-db", type=float, default=-22.0)
    ap.add_argument("--max-whoosh", type=int, default=8)
    ap.add_argument("--max-burn", type=int, default=4)
    ap.add_argument("--enable-whoosh", default="1")
    ap.add_argument("--enable-burn", default="1")
    args = ap.parse_args()

    enable_whoosh = args.enable_whoosh == "1" and args.max_whoosh > 0
    enable_burn = args.enable_burn == "1" and args.max_burn > 0

    whoosh_pool = query_sfx_pool(args.db, args.brand_id, "whoosh") if enable_whoosh else []
    burn_pool = query_sfx_pool(args.db, args.brand_id, "burn") if enable_burn else []

    if not whoosh_pool and not burn_pool:
        print(
            f"[apply_sfx] no sfx assets found for brand {args.brand_id}, skipping",
            file=sys.stderr,
        )
        shutil.copyfile(args.input, args.output)
        return 0

    # ---- pick whoosh placements ----
    whoosh_offsets = json.loads(args.whoosh_offsets) if enable_whoosh else []
    whoosh_offsets = [float(x) for x in whoosh_offsets]
    if len(whoosh_offsets) > args.max_whoosh:
        # Sample sin reemplazo, preservando orden temporal
        whoosh_offsets = sorted(random.sample(whoosh_offsets, args.max_whoosh))

    # ---- pick burn placements ----
    burn_offsets = []
    if enable_burn and burn_pool:
        segments = load_whisper_segments(args.transcript)
        burn_offsets = pick_burn_offsets(segments, args.max_burn, args.body_offset_sec)

    # ---- resolve assets (random selection con rotación) ----
    whoosh_picks = []  # (file_path, asset_id, offset_sec)
    for off in whoosh_offsets:
        if not whoosh_pool:
            break
        aid, fp = random.choice(whoosh_pool)
        whoosh_picks.append((fp, aid, off))

    burn_picks = []
    for off in burn_offsets:
        if not burn_pool:
            break
        aid, fp = random.choice(burn_pool)
        burn_picks.append((fp, aid, off))

    if not whoosh_picks and not burn_picks:
        print(
            "[apply_sfx] no whoosh or burn placements computed (no cuts and no emphasis "
            "phrases), copying input → output untouched",
            file=sys.stderr,
        )
        shutil.copyfile(args.input, args.output)
        return 0

    # ---- build ffmpeg argv ----
    cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-i", args.input]
    whoosh_pairs = []  # (input_index, offset)
    burn_pairs = []
    idx = 1
    for fp, _aid, off in whoosh_picks:
        cmd += ["-i", fp]
        whoosh_pairs.append((idx, off))
        idx += 1
    for fp, _aid, off in burn_picks:
        cmd += ["-i", fp]
        burn_pairs.append((idx, off))
        idx += 1

    filter_complex, aout = build_filter_complex(
        whoosh_pairs, burn_pairs, args.whoosh_volume_db, args.burn_volume_db
    )

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "0:v",
        "-map",
        aout,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "256k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        args.output,
    ]

    print(
        f"[apply_sfx] {len(whoosh_picks)} whoosh + {len(burn_picks)} burn → {args.output}",
        file=sys.stderr,
    )
    if whoosh_picks:
        print(
            "  whoosh: "
            + ", ".join(
                f"{Path(fp).name}@{off:.2f}s" for fp, _a, off in whoosh_picks
            ),
            file=sys.stderr,
        )
    if burn_picks:
        print(
            "  burn:   "
            + ", ".join(
                f"{Path(fp).name}@{off:.2f}s" for fp, _a, off in burn_picks
            ),
            file=sys.stderr,
        )

    res = subprocess.run(cmd)
    if res.returncode != 0:
        print(
            f"[apply_sfx] ffmpeg failed (code {res.returncode}); falling back to input",
            file=sys.stderr,
        )
        # No reventar el job entero por un fallo de SFX; conservamos el corte limpio.
        shutil.copyfile(args.input, args.output)
        return 0

    used_ids = [aid for _f, aid, _o in whoosh_picks] + [aid for _f, aid, _o in burn_picks]
    bump_use_count(args.db, used_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
