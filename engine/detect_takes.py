#!/usr/bin/env python3
"""
detect_takes.py — Find take boundaries in a long video by detecting silences.

Usage:
  python3 detect_takes.py <video.mov> [--silence-threshold -30] [--min-silence 0.4]

Output: JSON with takes (start, end, duration) plus the parameters used.
"""

import argparse
import json
import re
import subprocess
import sys


def get_duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        text=True,
    ).strip()
    return float(out)


def detect_silences(path, threshold_db, min_silence):
    """Run ffmpeg silencedetect; return list of (silence_start, silence_end).

    `-vn` salta el decode del video (silencedetect es audio-only). En sources
    4K/HEVC esto es 10x speedup — sin -vn ffmpeg decodifica cada frame aunque
    el filter no los use.
    """
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", path,
            "-vn",  # output-side flag: descarta el video después del demux, no decodifica frames
            "-af",
            f"silencedetect=noise={threshold_db}dB:d={min_silence}",
            "-f", "null", "-",
        ],
        stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    log = proc.stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end: ([0-9.]+)", log)]
    pairs = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        pairs.append((s, e))
    return pairs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("video")
    p.add_argument("--silence-threshold", type=float, default=-30.0,
                   help="dB threshold (default: -30)")
    p.add_argument("--min-silence", type=float, default=0.4,
                   help="Minimum silence duration in seconds (default: 0.4)")
    p.add_argument("--min-take", type=float, default=1.0,
                   help="Drop takes shorter than this (seconds)")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    dur = get_duration(args.video)
    silences = detect_silences(args.video, args.silence_threshold,
                               args.min_silence)

    # Takes are the non-silent intervals.
    takes = []
    cursor = 0.0
    for s, e in silences:
        if s > cursor:
            takes.append((cursor, s))
        if e is not None:
            cursor = e
    if cursor < dur:
        takes.append((cursor, dur))

    takes_out = []
    for s, e in takes:
        d = e - s
        if d >= args.min_take:
            takes_out.append({
                "start": round(s, 3),
                "end": round(e, 3),
                "duration": round(d, 3),
            })

    result = {
        "takes": takes_out,
        "silence_threshold_db": args.silence_threshold,
        "min_silence_sec": args.min_silence,
        "min_take_sec": args.min_take,
        "video_duration_sec": round(dur, 3),
    }
    txt = json.dumps(result, indent=2)
    print(txt)
    if args.out:
        with open(args.out, "w") as f:
            f.write(txt + "\n")


if __name__ == "__main__":
    main()
