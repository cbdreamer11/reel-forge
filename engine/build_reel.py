#!/usr/bin/env python3
"""
build_reel.py — Orchestrate the full shorts pipeline.

Usage:
  python3 build_reel.py --config reel.cfg [--workdir build/] [--keep]

Config file (YAML-ish, but parsed without PyYAML):

  reel_num: 07
  section: SECTION_TAG
  title: The exact title of this piece
  brand: my-profile
  source: /path/to/synced.mov
  segments:
    - 00:01:23.4:00:01:45.0
    - 00:02:10.0:00:02:31.5

Output: <output_dir>/<NN>_<SECTION>_<slug>.mp4

Specs (locked, do not change):
  1080x1920 @ 30fps, H.264 CRF 18 preset fast
  AAC 256k 48kHz
  cover 1.0s, outro 2.5s (fade-in 0.3s, fade-out 0.5s starting at 2.0s)
  audio chain: highpass=80 -> acompressor -> alimiter=0.94 (NO loudnorm)
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

# Spoken language for whisper. "auto" detects; pin it in config once known.
LANG = os.environ.get("REELFORGE_LANG", "auto")


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = Path(__file__).resolve().parent
# WHISPER_MODEL override-able via env var (use medium when low memory)
WHISPER_MODEL = Path(os.path.expanduser(os.environ.get(
    "WHISPER_MODEL",
    str(Path.home() / "whisper-models" / "ggml-large-v3-turbo.bin"),
)))


def pick_music_for_reel(music_cfg, reel_num, body_dur, brand_dir):
    """Pick a track + offset from the brand's music pool, deterministic per reel.

    Same reel → same track + offset (reproducible).
    Different reels → different track and/or offset (variety).

    Config supports:
      - `pool`: list of paths (preferred) — rotation by `reel_num % len(pool)`
      - `path`: legacy single track
    Returns (absolute_path_or_None, offset_sec).
    """
    if not music_cfg.get("enabled", False):
        return None, 0.0
    pool = music_cfg.get("pool") or ([music_cfg["path"]] if music_cfg.get("path") else [])
    if not pool:
        return None, 0.0
    # Stable integer seed from reel_num (handles "07" / "07a" / etc.)
    try:
        rn = int(str(reel_num).strip().lstrip("0") or "0")
    except ValueError:
        rn = int(hashlib.md5(str(reel_num).encode()).hexdigest()[:8], 16)
    track_rel = pool[rn % len(pool)]
    track_path = Path(track_rel)
    if not track_path.is_absolute():
        track_path = Path(brand_dir) / track_rel
    if not track_path.exists():
        print(f"[build_reel] WARN: music track missing: {track_path}", file=sys.stderr)
        return None, 0.0
    track_dur = get_source_duration(track_path)
    # Allow offset within the track; keep at least body_dur + 1s remaining
    max_offset = max(0.0, track_dur - body_dur - 1.0)
    if max_offset > 1.0:
        # Deterministic offset per reel — gives a different slice for each reel
        offset = (rn * 7.0 + 3.0) % max_offset
    else:
        offset = 0.0
    return str(track_path), offset


def load_brand_shorts_config(brand_id):
    """Returns the brand's shorts dict from editing_defaults.json, or {}."""
    if not brand_id:
        return {}
    path = REPO_ROOT / "brands" / brand_id / "editing_defaults.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            cfg = json.load(f)
        return cfg.get("shorts", {}) or {}
    except Exception:
        return {}


def get_source_duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        text=True,
    ).strip()
    return float(out)


def apply_segment_padding(segments_sec, source_dur, pad_head, pad_tail):
    """Apply head/tail padding to (start, end) segments. Resolves overlaps
    by clipping to the midpoint of the inter-segment gap.

    segments_sec: list of (start_sec, end_sec) tuples, in temporal order.
    Returns: list of (new_start_sec, new_end_sec).
    """
    if not segments_sec:
        return []
    out = []
    n = len(segments_sec)
    for i, (s, e) in enumerate(segments_sec):
        new_s = max(0.0, s - pad_head)
        new_e = min(source_dur, e + pad_tail)
        # Clip to gap midpoints with neighbors
        if i > 0:
            prev_e_orig = segments_sec[i-1][1]
            mid = (prev_e_orig + s) / 2.0
            if new_s < mid:
                new_s = mid
        if i < n - 1:
            next_s_orig = segments_sec[i+1][0]
            mid = (e + next_s_orig) / 2.0
            if new_e > mid:
                new_e = mid
        if new_e > new_s:
            out.append((new_s, new_e))
    return out


def run(cmd, **kw):
    print("$", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, **kw)


def parse_config(path):
    cfg = {"segments": []}
    in_segments = False
    with open(path) as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if in_segments:
                m = re.match(r"\s*-\s*(.+)", line)
                if m:
                    cfg["segments"].append(m.group(1).strip())
                    continue
                else:
                    in_segments = False
            if line.strip().lower() == "segments:":
                in_segments = True
                continue
            if ":" in line:
                k, _, v = line.partition(":")
                cfg[k.strip()] = v.strip()
    for k in ("reel_num", "section", "title", "brand", "source"):
        if k not in cfg:
            raise ValueError(f"config missing required key: {k}")
    if not cfg["segments"]:
        raise ValueError("config: no segments listed")
    return cfg


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "untitled"


def parse_segment(seg):
    """`HH:MM:SS.s:HH:MM:SS.s` -> (start, end)"""
    # split at the middle colon (need 6 colon-separated parts since each ts has 2)
    parts = seg.split(":")
    if len(parts) != 6:
        # allow flexible: split on " "/"-"
        for sep in (" - ", "-", " "):
            if sep in seg:
                a, _, b = seg.partition(sep)
                return a.strip(), b.strip()
        raise ValueError(f"bad segment format: {seg!r}")
    start = ":".join(parts[0:3])
    end = ":".join(parts[3:6])
    return start, end


def ts_to_sec(ts):
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(ts)


def extract_segment(source, start, end, out_path):
    # `start`/`end` may be a HH:MM:SS string or a numeric seconds value
    s_sec = ts_to_sec(start) if isinstance(start, str) else float(start)
    e_sec = ts_to_sec(end) if isinstance(end, str) else float(end)
    dur = max(0.0, e_sec - s_sec)
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{s_sec:.3f}", "-t", f"{dur:.3f}",
        "-i", str(source),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
               "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1",
        "-r", "30",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ])


def concat_files(files, out_path, work):
    list_file = work / "concat.txt"
    with open(list_file, "w") as f:
        for p in files:
            f.write(f"file '{Path(p).resolve()}'\n")
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(out_path),
    ])


def extract_audio_wav(video, out_wav):
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video),
        "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le", str(out_wav),
    ])


def transcribe(wav, out_json_base):
    if not WHISPER_MODEL.exists():
        raise FileNotFoundError(f"whisper model not found at {WHISPER_MODEL}")
    out_json = Path(str(out_json_base) + ".json")
    # If a transcript already exists at this path AND REUSE_TRANSCRIPT env var
    # is set, skip whisper (lets the caller hand-edit body.json to fix
    # mishearings from the profile's caption_fixes before captions render).
    if out_json.exists() and os.environ.get("REUSE_TRANSCRIPT") == "1":
        print(f"[transcribe] reusing existing {out_json} (REUSE_TRANSCRIPT=1)")
        return out_json
    # whisper-cli writes <output>.json
    run([
        "whisper-cli",
        "-m", str(WHISPER_MODEL),
        "-l", LANG,
        "-ml", "0",
        "-oj",
        "-of", str(out_json_base),
        "-f", str(wav),
    ])
    return out_json


def make_card_videos(cover_png, outro_png, cover_mp4, outro_mp4):
    # Cover: 1.0s, silent stereo track to align with concat
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-t", "1.0", "-i", str(cover_png),
        "-f", "lavfi", "-t", "1.0", "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", "scale=1080:1920,format=yuv420p,setsar=1",
        "-r", "30",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
        "-shortest", str(cover_mp4),
    ])
    # Outro: 2.5s, fade-in 0.3s @0, fade-out 0.5s @2.0
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-t", "2.5", "-i", str(outro_png),
        "-f", "lavfi", "-t", "2.5", "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", "scale=1080:1920,format=yuv420p,setsar=1,"
               "fade=t=in:st=0:d=0.3,fade=t=out:st=2.0:d=0.5",
        "-r", "30",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
        "-shortest", str(outro_mp4),
    ])


def pick_zoom_windows_from_segments(segment_offsets, min_segment_dur=5.0,
                                    edge_trim=0.05):
    """Return zoom windows from segment boundaries within body.mp4.

    Each segment whose duration > min_segment_dur becomes ONE zoom window
    spanning [t_start + edge_trim, t_end - edge_trim]. This makes the zoom
    act as a TRANSITION: ease into the zoom early in the segment, hold while
    the speaker talks, ease out at the segment end → leading visually into the
    next cut.

    Short segments (< min_segment_dur) are skipped — abrupt zooms on
    sub-5s clips feel more like noise than emphasis.
    """
    picked = []
    for t0, t1, idx in segment_offsets:
        dur = t1 - t0
        if dur < min_segment_dur:
            continue
        picked.append((t0 + edge_trim, t1 - edge_trim))
    return picked


def parse_ass_time(s):
    """`H:MM:SS.cs` -> seconds."""
    parts = s.split(":")
    if len(parts) == 3:
        h, m, sec = parts
        return int(h) * 3600 + int(m) * 60 + float(sec)
    return float(s)


def build_zoom_expr(zoom_windows, max_zoom=1.08, ramp_sec=0.6):
    """Build a ffmpeg expression for scale factor `Z(t)` over the body.

    Animation per window [t0, t1]: ease 1.0 → max_zoom over ramp_sec at the
    start, HOLD at max_zoom for the bulk of the segment, ease back to 1.0
    over ramp_sec at the end. The closing ease-out functions as a transition
    into the next cut.

    Outside any window: factor = 1.0.
    """
    if not zoom_windows:
        return "1.0"
    parts = []
    for t0, t1 in zoom_windows:
        d = t1 - t0
        # If segment is shorter than 2*ramp_sec, compress proportionally
        actual_ramp = min(ramp_sec, d * 0.4)
        r1 = t0 + actual_ramp  # end of zoom-in
        r2 = t1 - actual_ramp  # start of zoom-out
        ramp_in = f"(1.0 + ({max_zoom}-1.0)*(t-{t0})/{actual_ramp})"
        ramp_out = f"({max_zoom} - ({max_zoom}-1.0)*(t-{r2})/{actual_ramp})"
        parts.append(
            f"if(between(t,{t0},{r1}),{ramp_in},"
            f"if(between(t,{r1},{r2}),{max_zoom},"
            f"if(between(t,{r2},{t1}),{ramp_out},1.0)))"
        )
    if len(parts) == 1:
        return parts[0]
    expr = parts[0]
    for p in parts[1:]:
        expr = f"max({expr},{p})"
    return expr


def burn_captions_and_clean_audio(body_mp4, captions_ass, body_cap_mp4,
                                  zoom_windows=None, focal_x=0.5, focal_y=0.33,
                                  max_zoom=1.08, ramp_sec=0.6,
                                  music_path=None, music_db=-22.0,
                                  music_fade_sec=0.5, music_offset_sec=0.0):
    """Burn ASS subs + apply locked audio chain (+ optional background music).

    If `music_path` is given, plays it underneath the voice at `music_db`
    volume starting at `music_offset_sec` into the track (clip a section,
    not always from the start). Loops if track is shorter than body.
    Voice chain unchanged (highpass + compress + limit, no loudnorm).
    """
    voice_chain = (
        "highpass=f=80,"
        "adeclick=window=55:overlap=75:arorder=2:threshold=2,"
        "acompressor=threshold=-18dB:ratio=2.5:attack=5:release=80,"
        "alimiter=limit=0.94"
    )
    sub_path = str(captions_ass).replace("\\", "/").replace(":", "\\:")
    vfilters = []
    if zoom_windows:
        z = build_zoom_expr(zoom_windows, max_zoom=max_zoom, ramp_sec=ramp_sec)
        cx_off = 1080.0 * focal_x
        cy_off = 1920.0 * focal_y
        cx_expr = f"{cx_off}*(({z})-1)"
        cy_expr = f"{cy_off}*(({z})-1)"
        vfilters.append(
            f"scale=w='1080*({z})':h='1920*({z})':eval=frame,"
            f"crop=1080:1920:'{cx_expr}':'{cy_expr}'"
        )
    vfilters.append(f"subtitles='{sub_path}'")
    vf = ",".join(vfilters)

    if music_path and Path(music_path).exists():
        body_dur = get_source_duration(body_mp4)
        fade_out_start = max(0.0, body_dur - music_fade_sec)
        # Filter chain for [1:a] music:
        #   1) atrim from offset → use a SECTION of the track (clip variety per reel)
        #   2) asetpts to reset timestamps after the cut
        #   3) aloop=-1 in case (track_dur - offset) < body_dur
        #   4) atrim duration to body_dur (final cap)
        #   5) volume + fade in/out
        af = (
            f"[0:a]{voice_chain}[voice];"
            f"[1:a]atrim=start={music_offset_sec:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"aloop=loop=-1:size=2147483647,"
            f"atrim=duration={body_dur:.3f},"
            f"volume={music_db}dB,"
            f"afade=t=in:st=0:d={music_fade_sec:.3f},"
            f"afade=t=out:st={fade_out_start:.3f}:d={music_fade_sec:.3f}[bg];"
            f"[voice][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
        run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(body_mp4),
            "-i", str(music_path),
            "-filter_complex", af,
            "-map", "0:v", "-map", "[aout]",
            "-vf", vf,
            "-r", "30",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
            "-pix_fmt", "yuv420p",
            str(body_cap_mp4),
        ])
    else:
        run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(body_mp4),
            "-vf", vf,
            "-af", voice_chain,
            "-r", "30",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
            "-pix_fmt", "yuv420p",
            str(body_cap_mp4),
        ])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--workdir", default=None,
                   help="Working directory (default: ./build_<reel_num>)")
    p.add_argument("--keep", action="store_true",
                   help="Keep working files after success")
    args = p.parse_args()

    cfg = parse_config(args.config)

    work = Path(args.workdir) if args.workdir else Path(f"build_{cfg['reel_num']}")
    work.mkdir(parents=True, exist_ok=True)

    # 1. Extract segments (with brand-configured padding)
    shorts_cfg = load_brand_shorts_config(cfg.get("brand", ""))
    pad_head = float(shorts_cfg.get("padding_head_sec", 0.0))
    pad_tail = float(shorts_cfg.get("padding_tail_sec", 0.0))
    raw_segments = []
    for seg in cfg["segments"]:
        start, end = parse_segment(seg)
        raw_segments.append((ts_to_sec(start), ts_to_sec(end)))
    if pad_head > 0 or pad_tail > 0:
        source_dur = get_source_duration(cfg["source"])
        padded = apply_segment_padding(raw_segments, source_dur, pad_head, pad_tail)
        print(f"[build_reel] padding head={pad_head}s tail={pad_tail}s applied to {len(padded)} segments")
    else:
        padded = raw_segments

    seg_files = []
    segment_offsets = []  # (t_start_in_body, t_end_in_body, idx) per segment
    cursor = 0.0
    for i, (s_sec, e_sec) in enumerate(padded):
        seg_out = work / f"seg_{i:02d}.mp4"
        extract_segment(cfg["source"], s_sec, e_sec, seg_out)
        seg_files.append(seg_out)
        # Use ffprobe for actual extracted duration (frame-accurate seek may
        # differ slightly from requested e_sec-s_sec)
        actual_dur = get_source_duration(seg_out)
        segment_offsets.append((cursor, cursor + actual_dur, i))
        cursor += actual_dur

    # 2. Concat -> body.mp4
    body = work / "body.mp4"
    concat_files(seg_files, body, work)

    # 3. Audio -> wav (16k mono) for whisper
    body_wav = work / "body_clean.wav"
    extract_audio_wav(body, body_wav)

    # 4. Whisper transcript
    transcript = transcribe(body_wav, work / "body")

    # 5. Captions ASS
    captions = work / "captions.ass"
    run([
        sys.executable, str(TOOL_DIR / "make_captions.py"),
        "--input", str(transcript),
        "--out", str(captions),
        "--brand", cfg["brand"],
    ])

    # 6. Cards PNG
    cover_png = work / "cover.png"
    outro_png = work / "outro.png"
    run([
        sys.executable, str(TOOL_DIR / "make_cards.py"),
        "--brand", cfg["brand"],
        "--section", cfg["section"],
        "--title", cfg["title"],
        "--out-cover", str(cover_png),
        "--out-outro", str(outro_png),
    ])

    # 7. PNG -> MP4. Edit-layer no longer prepends a placeholder cover or
    # appends a placeholder outro — premium (branding layer) provides
    # both. The PNG outputs from step 6 are kept on disk only as preview
    # references; they're not muxed into the final edit-layer.

    # 8. Burn captions + clean audio (+ segment-based emphasis zoom)
    body_cap = work / "body_cap.mp4"
    zoom_windows = []
    if shorts_cfg.get("zoom_for_emphasis", True):
        min_seg_dur = float(shorts_cfg.get("zoom_min_segment_dur_sec", 5.0))
        zoom_windows = pick_zoom_windows_from_segments(
            segment_offsets, min_segment_dur=min_seg_dur,
        )
        if zoom_windows:
            print(f"[build_reel] zoom windows (segments >{min_seg_dur}s): {zoom_windows}")
    focal_x = float(shorts_cfg.get("zoom_focal_x", 0.5))
    focal_y = float(shorts_cfg.get("zoom_focal_y", 0.33))
    max_zoom = float(shorts_cfg.get("zoom_max", 1.08))
    ramp_sec = float(shorts_cfg.get("zoom_ramp_sec", 0.6))
    # NOTE: music is NOT applied here. The brand pool/offset config in
    # editing_defaults.json is read by `add_music.py` after the premium
    # layer runs, so music spans the WHOLE final video (cover+body+outro).
    burn_captions_and_clean_audio(body, captions, body_cap,
                                  zoom_windows=zoom_windows,
                                  focal_x=focal_x, focal_y=focal_y,
                                  max_zoom=max_zoom, ramp_sec=ramp_sec)

    # 9. Final = body_cap (no placeholder cover/outro). Premium handles wrap.
    slug = slugify(cfg["title"])
    out_dir = REPO_ROOT / "brands" / cfg["brand"] / "content" / "ready"
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"Reel_{cfg['reel_num']}_{cfg['section']}_{slug}.mp4"
    shutil.copy(body_cap, final)

    print(f"\nDONE: {final}")

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
