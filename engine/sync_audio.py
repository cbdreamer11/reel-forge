#!/usr/bin/env python3
"""
sync_audio.py — Cross-correlate iPhone .mov audio vs Lavalier .WAV to find
the precise offset to align the lavalier track over the camera audio.

Usage:
  python3 sync_audio.py --camera IPHONE.mov --lav LAV.wav --approx 1.0 \
      [--duration 30] [--sr 8000] [--out offset.json]

Output: JSON `{"offset": 1.234, "confidence": 0.92}` (offset = seconds the
lavalier leads the iphone; subtract from lav to align with iphone, or use
ffmpeg -itsoffset accordingly).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf
from scipy.signal import correlate


def extract(path, sr=8000, dur=30, start=0):
    """Extract `dur` seconds of mono audio at `sr` sample rate from `path`."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as t:
        out = t.name
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", str(start),
                "-t", str(dur),
                "-i", path,
                "-ar", str(sr),
                "-ac", "1",
                out,
            ],
            check=True,
        )
        a, _ = sf.read(out)
    finally:
        if os.path.exists(out):
            os.unlink(out)
    return a.astype(np.float32)


def find_offset(camera_audio, mic_audio, sr, search_window_sec):
    """Return (offset_seconds, confidence in [0,1])."""
    # Normalize
    a = camera_audio - np.mean(camera_audio)
    b = mic_audio - np.mean(mic_audio)
    a /= (np.max(np.abs(a)) + 1e-9)
    b /= (np.max(np.abs(b)) + 1e-9)

    c = correlate(a, b, mode="full")
    # lag in samples relative to start of arrays
    lags = np.arange(-len(b) + 1, len(a))

    # Limit search to +/- search_window_sec
    max_lag = int(search_window_sec * sr)
    mask = (lags >= -max_lag) & (lags <= max_lag)
    if not np.any(mask):
        mask = np.ones_like(lags, dtype=bool)
    c_masked = c.copy()
    c_masked[~mask] = -np.inf

    best_idx = int(np.argmax(c_masked))
    best_lag = lags[best_idx]
    offset_sec = best_lag / float(sr)

    # Confidence = peak / (median of abs corr in window)
    valid = c[mask]
    peak = np.max(valid)
    med = np.median(np.abs(valid)) + 1e-9
    raw = peak / med
    # Map to [0,1] with a soft squash
    confidence = float(1.0 - 1.0 / (1.0 + raw / 20.0))

    return float(offset_sec), confidence


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--camera", required=True, help="Camera file path (its own audio track)")
    p.add_argument("--lav", required=True, help="External recorder file path")
    p.add_argument("--approx", type=float, default=0.0,
                   help="Approximate offset (sec) to start search around")
    p.add_argument("--duration", type=float, default=30.0,
                   help="Seconds of audio to analyze")
    p.add_argument("--sr", type=int, default=8000, help="Sample rate for analysis")
    p.add_argument("--window", type=float, default=10.0,
                   help="Search window +/- around approx (sec)")
    p.add_argument("--out", default=None, help="Write JSON to this path")
    args = p.parse_args()

    start = max(0.0, args.approx - args.window / 2.0)

    iph = extract(args.iphone, sr=args.sr, dur=args.duration, start=start)
    lav = extract(args.lav, sr=args.sr, dur=args.duration, start=start)

    offset_local, conf = find_offset(iph, lav, args.sr, args.window)
    # offset_local is measured within the extracted windows (both started at `start`)
    # so the global offset between the original files equals offset_local.
    offset = round(offset_local, 4)

    result = {"offset": offset, "confidence": round(conf, 4)}
    out_json = json.dumps(result, indent=2)
    print(out_json)
    if args.out:
        with open(args.out, "w") as f:
            f.write(out_json + "\n")


if __name__ == "__main__":
    main()
