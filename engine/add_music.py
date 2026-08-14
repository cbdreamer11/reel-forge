#!/usr/bin/env python3
"""
add_music.py — Mix brand background music UNDER the entire final reel.

Runs AFTER the premium layer (the branding layer) so the music
spans cover + body + outro continuously, faded in at the very start and
faded out at the very end.

Reads the brand's `editing_defaults.json → shorts.music` config (pool +
volume_db + fade_sec) and picks a track + offset deterministic by
reel_num (same reel always sounds the same on re-render; different reels
get different track/slice).

Usage:
  python3 add_music.py \\
      --brand my-profile \\
      --reel-num 07 \\
      --input  /path/to/Reel_NN_premium.mp4 \\
      --output /path/to/Reel_NN_premium_with_music.mp4

Video stream is copied (no re-encode). Only audio is re-muxed → fast (~5s
on a 40s reel).
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))
from build_reel import (
    pick_music_for_reel, get_source_duration, load_brand_shorts_config,
    REPO_ROOT,
)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--brand", required=True,
                   help="Brand id (e.g. my-profile)")
    p.add_argument("--reel-num", required=True,
                   help="Reel number, used to pick track + offset deterministically")
    p.add_argument("--input", required=True, help="Premium MP4 (no music)")
    p.add_argument("--output", required=True, help="Output MP4 (with music)")
    p.add_argument("--volume-db", type=float, default=None,
                   help="Override volume in dB (default: from editing_defaults)")
    args = p.parse_args()

    shorts_cfg = load_brand_shorts_config(args.brand)
    music_cfg = shorts_cfg.get("music", {}) or {}
    if not music_cfg.get("enabled", False):
        print("[add_music] music disabled for brand; copying input as-is", file=sys.stderr)
        shutil.copy(args.input, args.output)
        return

    brand_dir = REPO_ROOT / "brands" / args.brand
    in_path = Path(args.input)
    if not in_path.exists():
        print(f"[add_music] ERROR: input not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    final_dur = get_source_duration(in_path)
    music_path, music_offset = pick_music_for_reel(music_cfg, args.reel_num,
                                                    final_dur, brand_dir)
    if not music_path:
        print("[add_music] no music track resolved; copying input as-is", file=sys.stderr)
        shutil.copy(args.input, args.output)
        return

    music_db = args.volume_db if args.volume_db is not None else float(music_cfg.get("volume_db", -28.0))
    music_fade = float(music_cfg.get("fade_sec", 0.5))
    fade_out_start = max(0.0, final_dur - music_fade)

    print(f"[add_music] {Path(music_path).name} @ {music_db}dB · offset={music_offset:.2f}s · spans {final_dur:.1f}s")

    af = (
        f"[1:a]atrim=start={music_offset:.3f},"
        f"asetpts=PTS-STARTPTS,"
        f"aloop=loop=-1:size=2147483647,"
        f"atrim=duration={final_dur:.3f},"
        f"volume={music_db}dB,"
        f"afade=t=in:st=0:d={music_fade:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={music_fade:.3f}[bg];"
        f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )

    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(in_path),
        "-i", str(music_path),
        "-filter_complex", af,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
        str(args.output),
    ], check=True)

    print(f"[add_music] wrote {args.output}")


if __name__ == "__main__":
    main()
