#!/usr/bin/env python3
"""
fix_captions_common.py — Auto-fix common whisper mishearings in body.json
before captions are rendered. Invoked between build_reel.py runs:

  WHISPER_MODEL=~/whisper-models/ggml-base.bin python3 build_reel.py ...
  python3 fix_captions_common.py /tmp/cXXX/build/body.json
  REUSE_TRANSCRIPT=1 python3 build_reel.py ...   # re-render with fixed captions

Add per-profile overrides via --profile to apply your own vocabulary fixes.

Pure JSON edit — no whisper rerun. Idempotent.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Global fixes — applied to every profile.
#
# SHIPPED EMPTY ON PURPOSE. Mishearings are specific to a language, an accent and
# a vocabulary; someone else's list is noise in your captions and can corrupt
# words you actually said.
#
# Build your own instead: every time a render shows a word whisper got wrong, add
# one (pattern, replacement) pair to `caption_fixes` in your profile.json. After a
# dozen pieces the list stops growing.
#
# See templates/caption-fixes-example.json for the format.
COMMON_FIXES = []


# Per-profile overrides — applied after global fixes.
#
# These are NOT hardcoded: each profile declares its own vocabulary under
# `caption_fixes` in its profile.json, e.g.
#
#   "caption_fixes": [
#     ["\\bteh\\b", "the"],
#     ["\\bmy compny\\b", "My Company"]
#   ]
#
# Put here the words whisper reliably gets wrong for YOUR vocabulary: jargon of
# your field, proper nouns, brand names, and any term you say often that comes
# back mangled. Grows one entry at a time, every time you catch one in a render.
PROFILE_FIXES = {}


def load_profile_fixes(profile_path):
    """Read `caption_fixes` from a profile.json and register them.

    Returns the list of (pattern, replacement) tuples for that profile.
    Missing file or missing key is not an error — it just means no overrides.
    """
    import json
    import os

    if not profile_path or not os.path.exists(profile_path):
        return []
    try:
        with open(profile_path, "r", encoding="utf-8") as fh:
            profile = json.load(fh)
    except (ValueError, OSError):
        return []

    pairs = [(p, r) for p, r in profile.get("caption_fixes", []) if p]
    PROFILE_FIXES[profile.get("profile_id", "default")] = pairs
    return pairs


def apply_fixes(text, fixes):
    """Apply each (pattern, replacement) and return (new_text, changes_count)."""
    n = 0
    for pat, repl in fixes:
        new = re.sub(pat, repl, text)
        if new != text:
            n += new != text  # any change = +1
        text = new
    return text, n


def fix_body_json(path, brand=None, profile=None, dry_run=False, verbose=False):
    """Walk body.json `transcription[*].text` and apply fixes. Returns total
    changes.

    `profile` is a path to a profile.json whose `caption_fixes` are applied
    after the global ones. `brand` is kept as a backwards-compatible alias for
    an already-registered profile id.
    """
    with open(path) as f:
        data = json.load(f)

    fixes = list(COMMON_FIXES)
    if profile:
        fixes.extend(load_profile_fixes(profile))
    if brand and brand in PROFILE_FIXES:
        fixes.extend(PROFILE_FIXES[brand])

    total_changes = 0
    for seg in data.get("transcription", []):
        orig = seg.get("text", "")
        new, n = apply_fixes(orig, fixes)
        if n > 0:
            total_changes += n
            if verbose:
                print(f"  − {orig.strip()}", file=sys.stderr)
                print(f"  + {new.strip()}", file=sys.stderr)
            if not dry_run:
                seg["text"] = new

    if not dry_run and total_changes > 0:
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    return total_changes


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("body_json", type=Path,
                   help="Path to build_reel's body.json")
    p.add_argument("--profile", default=None,
                   help="Path to profile.json — its `caption_fixes` are applied after the global ones")
    p.add_argument("--brand", default=None,
                   help="(deprecated alias) id of an already-registered profile")
    p.add_argument("--dry-run", action="store_true",
                   help="Show changes without writing")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress per-line diffs")
    args = p.parse_args()

    if not args.body_json.exists():
        print(f"ERROR: {args.body_json} not found", file=sys.stderr)
        sys.exit(1)

    n = fix_body_json(args.body_json, brand=args.brand, profile=args.profile,
                      dry_run=args.dry_run, verbose=not args.quiet)
    action = "would fix" if args.dry_run else "fixed"
    print(f"[fix_captions] {action} {n} caption segment(s) in {args.body_json}")


if __name__ == "__main__":
    main()
