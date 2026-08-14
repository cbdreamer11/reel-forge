#!/usr/bin/env python3
"""
take_selector.py — Curate hand-picked segments per CARDINAL RULE.

Reads a config.txt with N hand-picked segments, transcribes each one
individually with whisper-cli, applies dedup + truncated-continuation +
optional hook reorder, and emits config_curated.txt with fewer segments
in the right order.

Usage:
  python3 take_selector.py --config config.txt --out config_curated.txt \\
      [--workdir /tmp/take_sel] [--reorder-hook] \\
      [--similarity-threshold 0.5] [--cache-transcripts cache.json] [--keep]

Rules:
  - Pass 0 (cluster): merge consecutive segments whose gap < cluster_gap_sec
    into a single "logical take" spanning [first.start, last.end]. The
    individual segments were silence-cut at micro-pauses; the underlying
    take is one continuous delivery.
  - Pass 1 (truncated continuation): if logical-take A doesn't end with
    .!?… AND B's head shares ≥2 tokens with A's tail → drop A.
  - Pass 2 (similarity dedup): for each pair (i, j), i < j, with
    similarity ≥ threshold (Jaccard max of tokens / bigrams / trigrams):
      * prefer the take with terminator (.!?)
      * if both or neither, prefer the LONGER text (more complete)
      * tie → LAST take wins (CARDINAL RULE: last closing take wins).
  - Pass 3 (drop tiny fragments): drop logical takes with < min_take_words
    words that share ≥1 content word with any other surviving take
    (orphan fragments like "directores." or "cuántas personas seguirían?").
  - Pass 4 (optional hook reorder): if --reorder-hook, find segment
    matching HOOK_OPENERS regex; move LAST match to index 0.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parents[1]
sys.path.insert(0, str(TOOL_DIR))
from refine_takes import (
    similarity_score, is_continuation, has_terminator, normalize_text,
)
from build_reel import parse_config, parse_segment, ts_to_sec


def load_brand_take_selector_config(brand_id):
    """Returns the brand's shorts.take_selector dict, or {} if absent."""
    path = REPO_ROOT / "brands" / brand_id / "editing_defaults.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            cfg = json.load(f)
        return cfg.get("shorts", {}).get("take_selector", {})
    except Exception:
        return {}


WHISPER_MODEL = Path(os.path.expanduser(os.environ.get(
    "WHISPER_MODEL",
    str(Path.home() / "whisper-models" / "ggml-large-v3-turbo.bin")
)))


# Spoken language passed to whisper. "auto" lets the model detect it; pin it in
# your config once you know it — detection costs time and occasionally guesses
# wrong on a short, noisy burst.
LANG = os.environ.get("REELFORGE_LANG", "auto")

# Hook openers — the phrases YOU habitually open with.
#
# SHIPPED EMPTY ON PURPOSE. The *mechanism* (find the hook, move it to the front)
# is universal; the *phrases* are personal and language-specific. Someone else's
# openers will never match your speech, and worse, could promote the wrong take.
#
# Fill `take_selector.hook_openers` in your config with regexes for how you
# actually start a piece. Read three of your own transcripts and the list writes
# itself — most people have five or six openers and reuse them forever.
#
#   "hook_openers": ["\\bimagine\\b", "\\bthe day (that|when)\\b", "\\bhere is the thing\\b"]
#
# With the list empty, --reorder-hook is a no-op and take order is left alone.
HOOK_OPENERS = []
HOOK_RE = None


def build_hook_re(patterns):
    """Compile the profile's hook openers. Returns None when none are configured,
    which makes the hook-reorder pass a no-op rather than a wrong guess."""
    pats = [p for p in (patterns or []) if p]
    if not pats:
        return None
    return re.compile("|".join(pats), re.IGNORECASE)


def transcribe_segment(source, start_sec, dur_sec, workdir, idx):
    wav = workdir / f"sel_{idx:02d}.wav"
    json_base = workdir / f"sel_{idx:02d}"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-nostdin",
        "-ss", f"{start_sec:.3f}", "-t", f"{dur_sec:.3f}",
        "-i", str(source),
        "-vn", "-ar", "16000", "-ac", "1",
        str(wav),
    ], check=True)
    subprocess.run([
        "whisper-cli", "-m", str(WHISPER_MODEL),
        "-l", LANG, "-np",
        "-oj", "-of", str(json_base),
        "-f", str(wav),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with open(str(json_base) + ".json") as f:
        data = json.load(f)
    parts = []
    for s in data.get("transcription", []):
        t = (s.get("text") or "").strip()
        if not t:
            continue
        if t.startswith("[") and t.endswith("]"):
            continue
        parts.append(t)
    return " ".join(parts).strip()


def _prefer_drop_idx(a_text, b_text, i, j):
    """Given two near-duplicate texts at positions i<j, return which index to DROP.
    Rules: terminator wins > longer wins > last wins (j)."""
    a_term = has_terminator(a_text)
    b_term = has_terminator(b_text)
    if a_term and not b_term:
        return j
    if b_term and not a_term:
        return i
    # both or neither: longer wins
    if len(a_text) > len(b_text) * 1.2:
        return j
    if len(b_text) > len(a_text) * 1.2:
        return i
    # last wins (CARDINAL RULE)
    return i


def _norm_word(w):
    """Lowercase + strip accents for matching against theme_words."""
    repl = (("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ñ","n"),("ü","u"))
    w = w.lower()
    for a, b in repl:
        w = w.replace(a, b)
    return w


def count_theme_matches(text, theme_words):
    """Count tokens in text matching any theme word (normalized, prefix tolerant)."""
    if not theme_words:
        return 0
    normalized_theme = {_norm_word(t) for t in theme_words}
    matched = 0
    for tok in normalize_text(text):
        if tok in normalized_theme:
            matched += 1
            continue
        # Prefix match for verb tense variations (5+ char prefix)
        if len(tok) >= 5:
            prefix = tok[:5]
            if any(th.startswith(prefix) or tok.startswith(th[:5]) for th in normalized_theme if len(th) >= 5):
                matched += 1
    return matched


def consolidate_closing(takes, keep, reasons, theme_words, min_matches=3):
    """Drop near-duplicate closing attempts.

    Identifies takes with ≥min_matches theme-word hits. Among them, the take
    with the MOST hits wins (tie → latest). All other candidates are dropped.
    Body takes with <min_matches hits are untouched.
    """
    if not theme_words:
        return
    candidates = []
    for i in range(len(takes)):
        if not keep[i]:
            continue
        c = count_theme_matches(takes[i]["text"], theme_words)
        if c >= min_matches:
            candidates.append((i, c))
    if len(candidates) <= 1:
        return
    candidates.sort(key=lambda x: (x[1], x[0]))  # asc by count, then idx
    anchor_idx, anchor_count = candidates[-1]
    for i, c in candidates:
        if i == anchor_idx:
            continue
        keep[i] = False
        reasons[i] = f"closing_consolidation_anchor_take_{anchor_idx:02d}_themes={c}_vs_{anchor_count}"


def cluster_segments(segments_text, cluster_gap_sec=1.5):
    """Merge consecutive segments with gap < threshold into logical takes.

    Each cluster spans [first.start, last.end] continuously (the inter-segment
    gaps are absorbed back into the take — the original 21 segments came from
    silence-detection at micro-pauses inside one continuous delivery).
    """
    if not segments_text:
        return []
    clusters = []
    cur = {
        "members": [segments_text[0]],
        "start_raw": segments_text[0]["start_raw"],
        "end_raw": segments_text[0]["end_raw"],
        "start_sec": segments_text[0]["start_sec"],
        "end_sec": segments_text[0]["end_sec"],
        "text": segments_text[0]["text"],
    }
    for s in segments_text[1:]:
        gap = s["start_sec"] - cur["end_sec"]
        if gap < cluster_gap_sec:
            cur["members"].append(s)
            cur["end_raw"] = s["end_raw"]
            cur["end_sec"] = s["end_sec"]
            joiner = " " if cur["text"].rstrip().endswith((",", ";", ":")) or not has_terminator(cur["text"]) else " "
            cur["text"] = cur["text"].rstrip() + joiner + s["text"].lstrip()
        else:
            clusters.append(cur)
            cur = {
                "members": [s],
                "start_raw": s["start_raw"],
                "end_raw": s["end_raw"],
                "start_sec": s["start_sec"],
                "end_sec": s["end_sec"],
                "text": s["text"],
            }
    clusters.append(cur)
    return clusters


def intra_cluster_dedup_and_split(takes, similarity_threshold=0.5):
    """Within each cluster, detect the speaker's restart pattern and drop the earlier
    attempts. Splits the cluster around dropped middle members.

    Rules (unified head-prefix detection):
      For each pair (i, j) with i < j in cluster.members:
        Let shorter = whichever of (toks_i, toks_j) is shorter, longer = the other.
        If shorter is a non-trivial prefix of longer (len(shorter) ≥ 2 tokens):
          → j is a restart of an idea begun at i (or i is a fragment of j).
          → Drop members[i..j-1] inclusive. j survives.
      Plus: fragment subset — if member i has ≤2 content tokens AND ALL its
      tokens appear within any later member's tokens → drop member i.
      Plus: Jaccard ≥ threshold between (i, j) → drop i.

    silencedetect d=0.7 misses 0.4-0.6s micro-pauses the speaker makes when he
    restarts. With d=0.4 those become member bursts within a cluster, and
    this pass dedups them.
    """
    new_takes = []
    for take in takes:
        members = take["members"]
        if len(members) < 2:
            new_takes.append(take)
            continue

        # All token lists
        tok_lists = [
            [t for t in normalize_text(m["text"]) if len(t) >= 2]
            for m in members
        ]
        keep = [True] * len(members)
        for i in range(len(members)):
            if not keep[i]:
                continue
            toks_i = tok_lists[i]
            if not toks_i:
                keep[i] = False
                continue
            for j in range(i + 1, len(members)):
                if not keep[j]:
                    continue
                toks_j = tok_lists[j]
                if not toks_j:
                    continue
                # Unified prefix rule (either direction)
                if len(toks_i) <= len(toks_j):
                    shorter, longer = toks_i, toks_j
                else:
                    shorter, longer = toks_j, toks_i
                if len(shorter) >= 2 and longer[:len(shorter)] == shorter:
                    # j is restart of i's idea (or i is fragment of j). Drop i..j-1
                    for k in range(i, j):
                        keep[k] = False
                    break
                # Fragment subset (i is ≤2 content tokens, all appear in j)
                if len(toks_i) <= 2 and set(toks_i).issubset(set(toks_j)):
                    keep[i] = False
                    break
                # Jaccard similarity
                if len(toks_i) >= 2 and len(toks_j) >= 2:
                    sim = similarity_score(members[i]["text"], members[j]["text"])
                    if sim >= similarity_threshold:
                        keep[i] = False
                        break

        # Group surviving members into runs of originally-consecutive indices.
        # Each run becomes a separate sub-take (so dropped middle members are
        # excluded from the audio extraction range).
        runs = []
        cur_run = []
        for i, m in enumerate(members):
            if keep[i]:
                cur_run.append(m)
            elif cur_run:
                runs.append(cur_run)
                cur_run = []
        if cur_run:
            runs.append(cur_run)

        if not runs:
            continue  # whole cluster dropped (very rare)
        for run in runs:
            new_takes.append({
                "members": run,
                "start_raw": run[0]["start_raw"],
                "end_raw": run[-1]["end_raw"],
                "start_sec": run[0]["start_sec"],
                "end_sec": run[-1]["end_sec"],
                "text": " ".join(m["text"].strip() for m in run),
            })
    return new_takes


def curate(segments_text, similarity_threshold=0.5, reorder_hook=False,
           cluster_gap_sec=1.5, min_fragment_words=3,
           closing_theme_words=None, closing_min_matches=3):
    # Pass 0a: cluster adjacent segments into logical takes
    raw_takes = cluster_segments(segments_text, cluster_gap_sec=cluster_gap_sec)
    # Pass 0b: within each cluster, drop prefix/duplicate members; split if drops create gaps
    takes = intra_cluster_dedup_and_split(raw_takes, similarity_threshold=similarity_threshold)
    n = len(takes)
    keep = [True] * n
    reasons = ["keep"] * n

    # Pass 1: truncated continuation
    for i in range(n - 1):
        if not keep[i]:
            continue
        j = i + 1
        while j < n and not keep[j]:
            j += 1
        if j >= n:
            continue
        if is_continuation(takes[i]["text"], takes[j]["text"]):
            keep[i] = False
            reasons[i] = f"truncated_continued_by_take_{j:02d}"

    # Pass 2: similarity dedup
    for i in range(n):
        if not keep[i]:
            continue
        for j in range(i + 1, n):
            if not keep[j]:
                continue
            a, b = takes[i]["text"], takes[j]["text"]
            if len(a.split()) < 2 or len(b.split()) < 2:
                continue
            sim = similarity_score(a, b)
            if sim < similarity_threshold:
                continue
            drop = _prefer_drop_idx(a, b, i, j)
            keep[drop] = False
            kept_idx = j if drop == i else i
            reasons[drop] = f"duplicate_of_take_{kept_idx:02d}_sim={sim:.2f}"
            if drop == i:
                break

    # Pass 2.25: trim false-start tail bursts. If a take has multiple bursts
    # AND its LAST burst's tokens are exactly the head tokens of the next
    # take, that last burst is a false start ("Un influencer." → restart →
    # "Un influencer desaparece..."). Drop the false start from the take.
    for i in range(n - 1):
        if not keep[i]:
            continue
        cur = takes[i]
        if len(cur["members"]) < 2:
            continue
        # Find next kept take
        j = i + 1
        while j < n and not keep[j]:
            j += 1
        if j >= n:
            continue
        last_burst = cur["members"][-1]
        last_toks = [t for t in normalize_text(last_burst["text"]) if len(t) > 1]
        if len(last_toks) < 2 or len(last_toks) > 5:
            continue  # only short fragments (2-5 content words) qualify
        next_toks = [t for t in normalize_text(takes[j]["text"]) if len(t) > 1]
        if len(next_toks) < len(last_toks):
            continue
        if last_toks == next_toks[:len(last_toks)]:
            # Drop last burst from cur cluster
            new_members = cur["members"][:-1]
            cur["members"] = new_members
            cur["end_raw"] = new_members[-1]["end_raw"]
            cur["end_sec"] = new_members[-1]["end_sec"]
            cur["text"] = " ".join(m["text"].strip() for m in new_members)
            reasons[i] = reasons[i] + f" + trimmed_false_start_overlap_with_take_{j:02d}"

    # Pass 2.5: closing consolidation (brand-configured theme words)
    if closing_theme_words:
        consolidate_closing(takes, keep, reasons, closing_theme_words, closing_min_matches)

    # Pass 3: drop tiny orphan fragments that share content with a surviving take
    surviving = [i for i in range(n) if keep[i]]
    for i in surviving:
        toks = [t for t in normalize_text(takes[i]["text"]) if len(t) > 3]
        if len(toks) < min_fragment_words:
            # Is there another surviving take whose content covers this fragment?
            for j in surviving:
                if j == i:
                    continue
                if not keep[j]:
                    continue
                other_toks = set(normalize_text(takes[j]["text"]))
                shared = sum(1 for t in toks if t in other_toks)
                if shared >= max(1, len(toks) - 1):
                    keep[i] = False
                    reasons[i] = f"orphan_fragment_covered_by_take_{j:02d}"
                    break

    kept = [takes[i] for i in range(n) if keep[i]]

    reorder_note = None
    # HOOK_RE is None when the profile configured no hook openers — in that case
    # this pass is skipped entirely rather than guessing at the hook.
    if reorder_hook and HOOK_RE is not None and len(kept) > 1:
        hook_idx = None
        for k, take in enumerate(kept):
            if HOOK_RE.search(take["text"]):
                hook_idx = k
        if hook_idx is not None and hook_idx > 0:
            picked = kept.pop(hook_idx)
            kept.insert(0, picked)
            reorder_note = f"reordered_hook_take_to_position_0"

    return kept, takes, reasons, reorder_note


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--workdir", default=None)
    p.add_argument("--similarity-threshold", type=float, default=0.5)
    p.add_argument("--cluster-gap-sec", type=float, default=1.5,
                   help="Merge consecutive segs with gap < this into logical take")
    p.add_argument("--reorder-hook", action="store_true")
    p.add_argument("--keep", action="store_true",
                   help="Keep workdir + transcripts after success")
    p.add_argument("--cache-transcripts", default=None,
                   help="JSON cache path (default: <workdir>/transcripts.json)")
    args = p.parse_args()

    cfg = parse_config(args.config)
    source = cfg["source"]
    brand_id = cfg.get("brand", "")
    brand_ts_cfg = load_brand_take_selector_config(brand_id) if brand_id else {}
    cluster_gap_sec = float(brand_ts_cfg.get("cluster_gap_sec", args.cluster_gap_sec))
    similarity_threshold = float(brand_ts_cfg.get("similarity_threshold", args.similarity_threshold))
    closing_cfg = brand_ts_cfg.get("closing_consolidation", {}) or {}
    closing_theme_words = closing_cfg.get("theme_words") or []
    closing_min_matches = int(closing_cfg.get("min_theme_matches", 3))

    # Compile this profile's hook openers. Empty (the default) → the hook-reorder
    # pass becomes a no-op instead of matching somebody else's phrases.
    global HOOK_RE
    HOOK_RE = build_hook_re(brand_ts_cfg.get("hook_openers"))
    if args.reorder_hook and HOOK_RE is None:
        print("[take_selector] --reorder-hook ignored: no `hook_openers` configured. "
              "Add the phrases you habitually open with to take_selector.hook_openers.",
              file=sys.stderr)

    if args.workdir:
        work = Path(args.workdir)
        work.mkdir(parents=True, exist_ok=True)
        delete_work = False
    else:
        work = Path(tempfile.mkdtemp(prefix="take_sel_"))
        delete_work = not args.keep
    print(f"[take_selector] workdir: {work}", file=sys.stderr)
    print(f"[take_selector] whisper model: {WHISPER_MODEL}", file=sys.stderr)
    if not WHISPER_MODEL.exists():
        raise FileNotFoundError(f"whisper model not found: {WHISPER_MODEL}")

    cache_path = Path(args.cache_transcripts) if args.cache_transcripts else (work / "transcripts.json")
    if cache_path.exists():
        with open(cache_path) as f:
            cache = json.load(f)
        print(f"[take_selector] loaded {len(cache)} cached transcripts", file=sys.stderr)
    else:
        cache = {}

    segments_text = []
    for idx, seg in enumerate(cfg["segments"]):
        start, end = parse_segment(seg)
        s_sec = ts_to_sec(start)
        e_sec = ts_to_sec(end)
        dur = e_sec - s_sec
        key = f"{idx:02d}_{start}_{end}"
        if key in cache:
            text = cache[key]
        else:
            print(f"[take_selector] transcribing seg {idx:02d} ({s_sec:.2f}-{e_sec:.2f}, {dur:.2f}s)...", file=sys.stderr)
            text = transcribe_segment(source, s_sec, dur, work, idx)
            cache[key] = text
        segments_text.append({
            "idx": idx,
            "start_raw": start, "end_raw": end,
            "start_sec": s_sec, "end_sec": e_sec,
            "text": text,
        })

    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    print("\n=== Segment transcripts ===", file=sys.stderr)
    for s in segments_text:
        dur = s["end_sec"] - s["start_sec"]
        print(f"[{s['idx']:02d}] {s['start_raw']}->{s['end_raw']} ({dur:.2f}s)  {s['text']!r}", file=sys.stderr)

    kept, all_takes, reasons, reorder_note = curate(
        segments_text,
        similarity_threshold=similarity_threshold,
        reorder_hook=args.reorder_hook,
        cluster_gap_sec=cluster_gap_sec,
        closing_theme_words=closing_theme_words,
        closing_min_matches=closing_min_matches,
    )

    print(f"\n=== Logical takes after clustering ({len(all_takes)}) ===", file=sys.stderr)
    kept_ids = {id(t) for t in kept}
    for i, t in enumerate(all_takes):
        members = ",".join(f"{m['idx']:02d}" for m in t["members"])
        marker = "KEEP" if id(t) in kept_ids else "DROP"
        print(f"[take {i:02d}] {marker} ({members})  {t['start_raw']}->{t['end_raw']}  "
              f"{t['text'][:90]!r}  — {reasons[i]}", file=sys.stderr)
    if reorder_note:
        print(f"  + {reorder_note}", file=sys.stderr)

    print(f"\n=== Final order ({len(kept)} takes from {len(all_takes)}) ===", file=sys.stderr)
    for i, t in enumerate(kept):
        dur = t["end_sec"] - t["start_sec"]
        print(f"  {i}. {t['start_raw']}->{t['end_raw']} ({dur:.2f}s)  {t['text'][:90]!r}", file=sys.stderr)

    out_lines = []
    for key in ("reel_num", "section", "title", "brand", "source"):
        if key in cfg:
            out_lines.append(f"{key}: {cfg[key]}")
    out_lines.append("segments:")
    for t in kept:
        out_lines.append(f"  - {t['start_raw']}:{t['end_raw']}")
    with open(args.out, "w") as f:
        f.write("\n".join(out_lines) + "\n")
    print(f"\nwrote {args.out}", file=sys.stderr)

    if delete_work:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
