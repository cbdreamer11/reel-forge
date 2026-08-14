---
name: reel-forge
description: Turn raw talking-head footage into a finished vertical short — auto-sync separate audio, split on silence, transcribe each burst, pick the best take of each repeated line, burn word-level captions, add a face-anchored zoom, mix music, and attach an intro/outro. Works for any speaker, any language, any brand. Use when the user asks to edit, cut or process a reel, short, TikTok, vertical video, talking-head clip, or raw camera footage; mentions captions/subtitles burned into video, syncing a lavalier or field recorder to camera, picking the best take, or setting up a video pipeline. Spanish triggers: "edita este reel", "procesa el raw", "corta este short", "captions al video", "sincroniza el lavalier". Runs `scripts/setup.py` on first use in a project.
license: MIT
metadata:
  version: "1.0.0"
  domain: video
  triggers: reel, short, vertical video, talking head, captions, subtitles, lavalier sync, take selection, TikTok, Instagram reel, YouTube short
---

# reel-forge

Raw talking-head footage → finished vertical short.

The pipeline's opinion, in one line: **most auto-editors fail because they cut on
silence and keep every attempt at a sentence.** This one identifies that a person
said the same idea four times and keeps the best attempt, whole.

---

## 0. Before anything — is this project set up?

Check for `reelforge.json` in the project directory.

**If it does not exist**, this is a first run. Do this, in order:

1. **Check the tools.** Run `scripts/doctor.sh`. If it exits non-zero, run
   `scripts/doctor.sh --install` and let it install what is missing. It handles
   ffmpeg, whisper.cpp, the whisper models, the python packages and the fonts.
   Never proceed with a failing doctor — every downstream error will be confusing.
2. **Run the wizard.** `python3 scripts/setup.py --dir <project>`. It asks where the
   footage is, whether audio was recorded separately, whether there is music, and
   how the intro/outro should be built. It is interactive — **tell the user to run
   it in their terminal** rather than trying to answer for them.
3. Read the resulting `reelforge.json` before doing anything else.

**If it exists**, read it and proceed to §1.

---

## 0.5 Is there a content list?

If `reelforge.json` has a `content_list`, this project is a series, not a one-off.

```bash
python3 scripts/production.py check     # always safe; run it first
python3 scripts/production.py import    # list -> pieces.json
python3 scripts/production.py vocab     # derive hook/closing word lists
python3 scripts/production.py match    # identify each clip by listening to it
```

**Run `vocab` before the user shoots anything.** The two lists that make take
selection sharp are already latent in their plan, and word lists carried over from a
previous cycle are worse than none — they consolidate the wrong takes. Paste the
result into `take_selector`, after the user prunes it.

**Use `match` instead of asking the user which clip is which.** It transcribes each
clip's opening and compares it to the planned pieces, tolerating paraphrase. It
refuses rather than guesses when two pieces are close — leave those for the user.

`check` is also the right first move whenever a path looks wrong: it reports unmounted
drives and unset roots as *pending*, not failures.

---

## 1. Sync and mux

If `sources.audio_separate` is false, skip this — the camera audio is the audio.

Otherwise, find the offset by cross-correlation and mux:

```bash
python3 engine/sync_audio.py --iphone "<camera.mov>" --lav "<audio.wav>" \
  --approx <sync_hint_sec> --window 30 --duration 30
```

Then mux with the audio treatment. **Compression and limiting, never `loudnorm`** —
loudnorm on a single short pumps and flattens the delivery:

```bash
ffmpeg -y -i "<camera.mov>" -ss <offset> -i "<audio.wav>" \
  -map 0:v -map 1:a -c:v copy \
  -af "highpass=f=80,acompressor=threshold=-18dB:ratio=2.5:attack=5:release=80,alimiter=limit=0.94" \
  -c:a pcm_s24le /tmp/<piece>/synced.mov
```

`engine/sync_and_mux.sh` wraps both steps and verifies the residual offset is ~0.

---

## 2. Detect bursts and transcribe each one

```bash
ffmpeg -i synced.mov -af "silencedetect=n=-30dB:d=0.4" -f null /dev/null 2>&1 \
  | grep silencedetect
```

Then transcribe **each burst separately**:

```bash
ffmpeg -y -ss <start> -t <dur> -i synced.mov -vn -ar 16000 -ac 1 \
  -af "highpass=f=80,acompressor=threshold=-30dB:ratio=3,volume=6dB" /tmp/b.wav
whisper-cli -m ~/whisper-models/ggml-base.bin -l <language> -f /tmp/b.wav -oj -of /tmp/b -np
```

> **Per-burst transcription is not an optimisation, it is a correctness requirement.**
> Whisper hallucinates over silence — transcribe the whole file and it invents
> sentences in the gaps. An empty burst is noise; drop it.

---

## 3. Choose the candidate segments

**Read `reference/take-selection.md` before doing this.** It is the actual craft of
this tool and the reason the output is watchable.

The short version:
- **One take per idea. Never bridge audio across takes.**
- 2-4 consecutive versions of a line → the **last** is usually right, unless one
  matches the intended script and the others paraphrase.
- A 30-60s piece is **2-4 segments**. 15+ means over-cutting.
- **Include the hook explicitly** — the selector never looks outside the range given.

Write `config.txt` (see `templates/config.txt`):

```
piece_num: 07
section: SECTION_TAG
title: The exact title
source: /tmp/<piece>/synced.mov
segments:
  - 30.00:69.62
  - 74.10:88.45
```

---

## 4. Curate the takes

```bash
WHISPER_MODEL=~/whisper-models/ggml-base.bin python3 engine/take_selector.py \
  --config /tmp/<piece>/config.txt \
  --out /tmp/<piece>/config_curated.txt \
  --workdir /tmp/<piece>/take_sel --keep
```

Clusters by gap, dedups by Jaccard similarity, and consolidates the closing line.
**Read the "Final order" in the log** before rendering. If the close is still in
fragments, the fix is `take_selector.closing_consolidation.theme_words` in
`reelforge.json` — not the code.

---

## 5. Render

```bash
WHISPER_MODEL=~/whisper-models/ggml-large-v3-turbo.bin python3 engine/build_reel.py \
  --config /tmp/<piece>/config_curated.txt \
  --workdir /tmp/<piece>/build --keep
```

Applies padding, concatenates, generates word-level captions, burns them, applies
the face-anchored zoom to segments longer than `zoom.min_segment_dur_sec`, and runs
the audio chain.

**To fix a misheard caption without re-running whisper:**

```bash
python3 engine/fix_captions_common.py /tmp/<piece>/build/body.json --profile <profile.json>
REUSE_TRANSCRIPT=1 python3 engine/build_reel.py --config ... --workdir ...
```

Add the word to `caption_fixes` in the profile so it never comes back.

---

## 6. Branding and music

Per `branding.mode` in the config — see `reference/branding.md` for all four modes.
`none` is the recommended starting point; `video` is the mode for genuinely custom
animated intros.

Music last, so it covers intro + body + outro:

```bash
python3 engine/add_music.py --brand <profile_id> --reel-num <N> \
  --input <no_music.mp4> --output <final.mp4>
```

Rotation is deterministic by piece number: the same piece always sounds the same,
different pieces get different tracks and slices. Video is stream-copied, so it is fast.

---

## 7. Hand it back

Report the output path and what to check. **Never claim a render is good without
having verified it exists and has the expected duration:**

```bash
ffprobe -v error -show_entries format=duration -of csv=p=0 <final.mp4>
```

---

## Tuning knobs

All in `reelforge.json` — no code edits.

| Symptom | Knob |
|---|---|
| Closing line arrives in fragments | `take_selector.closing_consolidation.theme_words` |
| Too many tiny cuts | `take_selector.cluster_gap_sec` up |
| Near-duplicate lines both survive | `take_selector.similarity_threshold` down |
| Zoom crops the face badly | `zoom.focal_y` (lower = higher in frame), `zoom.max` |
| Captions feel frantic | `captions.words_per_block` up |
| Highlights look random | `captions.accent_ratio` toward 0.8 |
| Words consistently misheard | `caption_fixes` in the profile |
| Music too loud/quiet | `music.duck_db` |

## Dependencies

ffmpeg (libx264, silencedetect) · whisper.cpp (`whisper-cli`) + models · python3
with numpy, scipy, soundfile, Pillow · Inter and Playfair Display fonts (only for
generated cards). `scripts/doctor.sh --install` sets all of it up.

## Not included

Publishing, scheduling, thumbnails, and anything that talks to a social platform.
This tool ends at a finished file on disk.
