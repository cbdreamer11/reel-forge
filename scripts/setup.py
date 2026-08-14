#!/usr/bin/env python3
"""
setup.py — First-run wizard for reel-forge.

Asks where your footage lives, whether audio is recorded separately, whether
you want music, and how the intro/outro should be built. Writes everything to
`reelforge.json` in the project directory.

    python3 setup.py                  # interactive, writes ./reelforge.json
    python3 setup.py --dir ~/MyShow   # set up a specific project directory
    python3 setup.py --show           # print the current config and exit

Re-running is safe: existing answers become the defaults.
"""

import argparse
import json
import os
import sys
from pathlib import Path

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mkv"}
AUDIO_EXT = {".wav", ".m4a", ".mp3", ".aif", ".aiff", ".flac"}

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


def head(text):
    print(f"\n{BOLD}{text}{RESET}")


def note(text):
    print(f"{DIM}  {text}{RESET}")


def ask(prompt, default=None, required=False):
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        try:
            raw = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            raw = ""
        if not raw and default is not None:
            return default
        if raw:
            return raw
        if not required:
            return ""
        print(f"{YELLOW}    required{RESET}")


def ask_yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    raw = ask(f"{prompt} ({d})", default="")
    if not raw:
        return default
    return raw.lower().startswith("y")


def ask_choice(prompt, options, default=1):
    print(f"\n  {prompt}")
    for i, (_, label, desc) in enumerate(options, 1):
        print(f"    {i}) {BOLD}{label}{RESET}")
        if desc:
            print(f"{DIM}       {desc}{RESET}")
    while True:
        raw = ask("choose", default=str(default))
        try:
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        except ValueError:
            pass
        print(f"{YELLOW}    pick 1-{len(options)}{RESET}")


def ask_dir(prompt, default=None, must_exist=True, allow_empty=False):
    while True:
        raw = ask(prompt, default=default)
        if not raw and allow_empty:
            return ""
        p = Path(os.path.expanduser(raw)).resolve() if raw else None
        if p is None:
            print(f"{YELLOW}    required{RESET}")
            continue
        if must_exist and not p.exists():
            print(f"{YELLOW}    not found: {p}{RESET}")
            if ask_yes("    use it anyway?", default=False):
                return str(p)
            continue
        return str(p)


def count_media(path, exts):
    p = Path(path)
    if not p.exists():
        return 0
    return sum(1 for f in p.rglob("*") if f.suffix.lower() in exts)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".", help="project directory (default: cwd)")
    ap.add_argument("--show", action="store_true", help="print current config and exit")
    args = ap.parse_args()

    proj = Path(os.path.expanduser(args.dir)).resolve()
    proj.mkdir(parents=True, exist_ok=True)
    cfg_path = proj / "reelforge.json"

    old = {}
    if cfg_path.exists():
        try:
            old = json.loads(cfg_path.read_text(encoding="utf-8"))
        except ValueError:
            print(f"{YELLOW}warning: existing reelforge.json is not valid JSON; ignoring{RESET}")

    if args.show:
        if not old:
            print("No reelforge.json here. Run without --show to create one.")
            return 1
        print(json.dumps(old, indent=2, ensure_ascii=False))
        return 0

    if not sys.stdin.isatty():
        print("setup.py needs an interactive terminal.")
        print(f"Run it yourself:\n\n    python3 {Path(__file__).resolve()} --dir {proj}\n")
        return 1

    print(f"\n{BOLD}reel-forge setup{RESET}  ·  {proj}")
    note("Enter accepts the default in brackets. Re-run any time to change answers.")

    cfg = {"version": 1, "project_dir": str(proj)}

    # ── 1. identity ───────────────────────────────────────────────────────
    head("1. Project")
    old_p = old.get("project", {})
    cfg["project"] = {
        "name": ask("Project or show name", default=old_p.get("name") or proj.name),
        "language": ask("Spoken language (ISO code: es, en, pt...)",
                        default=old_p.get("language", "es")),
    }

    # ── 2. footage ────────────────────────────────────────────────────────
    head("2. Where is your footage?")
    note("The folder holding the raw camera files. Subfolders are searched too.")
    old_s = old.get("sources", {})
    video_dir = ask_dir("Video folder", default=old_s.get("video_dir") or str(proj / "raw"))
    n_vid = count_media(video_dir, VIDEO_EXT)
    print(f"{GREEN}    found {n_vid} video file(s){RESET}" if n_vid
          else f"{YELLOW}    no video files found yet — that's fine, add them later{RESET}")

    head("3. Audio")
    separate = ask_yes(
        "Did you record audio separately (lavalier, field recorder, podcast mic)?",
        default=bool(old_s.get("audio_separate", False)))
    audio_dir, sync_hint = "", 0.0
    if separate:
        note("reel-forge auto-syncs by cross-correlating the two waveforms.")
        audio_dir = ask_dir("Audio folder", default=old_s.get("audio_dir") or video_dir)
        n_aud = count_media(audio_dir, AUDIO_EXT)
        print(f"{GREEN}    found {n_aud} audio file(s){RESET}" if n_aud
              else f"{YELLOW}    no audio files found yet{RESET}")
        raw_hint = ask("Roughly how many seconds apart do they start? (0 if you don't know)",
                       default=str(old_s.get("sync_hint_sec", 0)))
        try:
            sync_hint = float(raw_hint)
        except ValueError:
            sync_hint = 0.0
    else:
        note("Using the camera's own audio track.")

    cfg["sources"] = {
        "video_dir": video_dir,
        "audio_separate": separate,
        "audio_dir": audio_dir,
        "sync_hint_sec": sync_hint,
    }

    # ── 4. music ──────────────────────────────────────────────────────────
    head("4. Background music")
    old_m = old.get("music", {})
    use_music = ask_yes("Do your videos carry background music?",
                        default=bool(old_m.get("enabled", False)))
    music = {"enabled": use_music}
    if use_music:
        note("Point at a folder of tracks. Rotation is deterministic: the same "
             "piece number always gets the same track and the same slice, so a "
             "re-render sounds identical.")
        music["pool_dir"] = ask_dir("Music folder", default=old_m.get("pool_dir") or "")
        music["duck_db"] = float(ask("How far under the voice, in dB (more negative = quieter)",
                                     default=str(old_m.get("duck_db", -18))) or -18)
    cfg["music"] = music

    # ── 5. intro / outro ──────────────────────────────────────────────────
    head("5. Intro and outro")
    old_b = old.get("branding", {})
    mode = ask_choice(
        "How should the intro/outro be made?",
        [
            ("none", "None",
             "Straight to the content. Best default — start here, add branding once the edit is right."),
            ("images", "I'll supply still images",
             "PNG/JPG you already designed. Held for a set number of seconds."),
            ("generated", "Generate cards from my colors and logo",
             "reel-forge draws the title/outro cards from a profile you fill in below."),
            ("video", "I'll supply my own intro/outro video clips",
             "Pre-rendered .mp4 files, concatenated as-is. Use this if you animate elsewhere."),
        ],
        default={"none": 1, "images": 2, "generated": 3, "video": 4}.get(old_b.get("mode"), 1),
    )
    branding = {"mode": mode}

    if mode == "images":
        branding["intro_image"] = ask_dir("Intro image file (blank = no intro)",
                                          default=old_b.get("intro_image", ""), allow_empty=True)
        branding["outro_image"] = ask_dir("Outro image file (blank = no outro)",
                                          default=old_b.get("outro_image", ""), allow_empty=True)
        branding["intro_sec"] = float(ask("Intro seconds", default=str(old_b.get("intro_sec", 1.5))) or 1.5)
        branding["outro_sec"] = float(ask("Outro seconds", default=str(old_b.get("outro_sec", 2.5))) or 2.5)
    elif mode == "video":
        branding["intro_clip"] = ask_dir("Intro clip (blank = none)",
                                         default=old_b.get("intro_clip", ""), allow_empty=True)
        branding["outro_clip"] = ask_dir("Outro clip (blank = none)",
                                         default=old_b.get("outro_clip", ""), allow_empty=True)
    elif mode == "generated":
        note("These feed the generated cards and the caption accent color.")
        old_pr = old.get("profile", {})
        colors = old_pr.get("colors", {})
        branding["intro_sec"] = float(ask("Intro seconds", default=str(old_b.get("intro_sec", 1.5))) or 1.5)
        branding["outro_sec"] = float(ask("Outro seconds", default=str(old_b.get("outro_sec", 2.5))) or 2.5)
        cfg["profile"] = {
            "profile_id": old_pr.get("profile_id") or proj.name.lower().replace(" ", "-"),
            "name": ask("Show name as it appears on screen", default=old_pr.get("name", cfg["project"]["name"])),
            "person_name": ask("Person name on the card (blank to omit)",
                               default=old_pr.get("person_name", "")),
            "handle": ask("Social handle (blank to omit)", default=old_pr.get("handle", "")),
            "email": ask("Contact email (blank to omit)", default=old_pr.get("email", "")),
            "website": ask("Website (blank to omit)", default=old_pr.get("website", "")),
            "colors": {
                "background": ask("Background hex", default=colors.get("background", "#111111")),
                "text": ask("Text hex", default=colors.get("text", "#FAFAFA")),
                "accent": ask("Accent hex (also the caption highlight)",
                              default=colors.get("accent", "#4C8DFF")),
            },
            "logos": {"primary": ask("Logo file (blank to omit)",
                                     default=old_pr.get("logos", {}).get("primary", ""))},
            "caption_fixes": old_pr.get("caption_fixes", []),
        }
    cfg["branding"] = branding

    # ── 6. editing defaults ───────────────────────────────────────────────
    cfg["editing"] = old.get("editing", {
        "padding_head_sec": 0.05,
        "padding_tail_sec": 0.10,
        "captions": {"words_per_block": 4, "single_line": True, "accent_ratio": 0.8},
        "zoom": {"enabled": True, "min_segment_dur_sec": 5.0,
                 "focal_x": 0.5, "focal_y": 0.33, "max": 1.08, "ramp_sec": 0.6},
        "take_selector": {"cluster_gap_sec": 1.5, "similarity_threshold": 0.5,
                          "hook_openers": [],
                          "closing_consolidation": {"theme_words": [], "min_theme_matches": 3}},
    })

    # ── write ─────────────────────────────────────────────────────────────
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    head("Done")
    print(f"{GREEN}  wrote {cfg_path}{RESET}")
    if not cfg["editing"]["take_selector"]["closing_consolidation"]["theme_words"]:
        note("Tip: after your first few renders, fill `take_selector.closing_consolidation."
             "theme_words` with the words you habitually use in your closing line. That is "
             "what collapses 3-4 attempts at the punchline into one.")
    print()
    print("  Next:")
    print(f"    ./scripts/doctor.sh          verify tools are installed")
    print(f"    (then ask Claude: \"process my next clip\")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
