# reel-forge

**Raw talking-head footage → finished vertical short.** Runs locally, from the
command line or as a Claude Code / Cowork skill.

Auto-syncs a separate audio recorder to camera, splits on silence, transcribes each
burst, **picks the best attempt of every repeated line**, burns word-level captions,
applies a face-anchored zoom, mixes music, and attaches your intro/outro.

---

## Why this exists

Most automatic editors cut on silence and keep everything. But nobody talking to a
camera without a teleprompter delivers a clean take — they deliver the same idea
three or four times, getting better each pass. Cut that on silence and you ship a
video where the speaker stumbles, repeats themselves, and corrects on camera.

reel-forge's core job is deciding **which attempt survives**:

> "Cancer doesn't start as a **cancer**" → *[pause]* → "Cancer doesn't start as a **tumour**"

One take per idea. Never bridge audio across takes. The full taxonomy of how real
speakers fail and retry is in [`reference/take-selection.md`](reference/take-selection.md) —
it is the part of this repo worth reading even if you never run the code.

---

## Before you record

Read this first — it costs nothing on set and changes the result a lot:
**[How to record](reference/recording.md)** · **[Cómo grabar](reference/grabacion.md)**

The headline: **don't stop the recording, just do the take again.** The selector
compares takes *inside one file*. Four attempts in one recording means it sees all
four and keeps the best on its own; four separate files means it never compares them
and you pick by hand. One recording with four takes beats four recordings with one.

---

## Install

```bash
git clone https://github.com/<you>/reel-forge.git
cd reel-forge
./scripts/doctor.sh --install
```

`doctor.sh` checks and installs ffmpeg, whisper.cpp, the whisper models, the Python
packages and the fonts. It prompts before each install; `--yes` skips the prompts.

Verify it actually works before you shoot anything:

```bash
./scripts/smoke-test.sh          # or: ./scripts/smoke-test.sh es
```

It synthesises a short talking-head clip with the system speech voice — a flubbed
line then a clean retry, a closing said three times — and runs the real pipeline over
it. Exit 0 means detection, per-burst transcription, take selection, captions, zoom
and the render all work on your machine. Add `--keep` to watch the result.

Then, once per project:

```bash
python3 scripts/setup.py --dir ~/my-project
```

It asks where your footage is, whether audio was recorded separately, whether there
is music, and how the intro/outro should be built. Writes `reelforge.json`.

### As a Claude skill

Copy the folder to `~/.claude/skills/reel-forge/` and ask Claude to
*"process my next clip"*. It reads `reelforge.json`, runs the pipeline, and stops to
let you approve the take selection before rendering.

---

## The production layer

An editor cuts one clip. A series needs to know what pieces exist, which are shot,
and where the files are. That list already exists somewhere — a spreadsheet, an HTML
dashboard, a Notion export. reel-forge reads it instead of asking you to retype it.

```bash
python3 scripts/production.py import   # your list -> pieces.json
python3 scripts/production.py vocab    # derive the word lists from that list
python3 scripts/production.py check    # preflight: paths, drives, what's missing
```

Point it at whatever you already keep — `html_table`, `csv`, `json` or
`markdown_table`, with the column mapping in config:

```json
"content_list": {
  "path": "~/plan/season-one.html",
  "format": "html_table",
  "columns": { "num": 0, "type": 1, "hook": 2, "message": 3, "closing": 4 }
}
```

**`vocab` is the useful part, and it works before you shoot anything.** The two word
lists that make take selection sharp — how you open, how you close — are already
latent in your own plan. It reads them out and tells you the threshold to use:

```
closing theme words  (field 'closing', ≥3 of 90)
    30×  comment
    30×  save
    29×  share

suggested min_theme_matches: 1
  top words cover 99% of closings, typically 1 hit(s) each.
```

That series splits its call-to-action by content type, so no single word is frequent —
but exactly one appears per piece. Judging by any single word's frequency would say
"3" and the pass would then never fire. It measures the coverage of the group instead,
and cuts the list at the frequency cliff, because at threshold 1 one ordinary word is
enough to mistake a body take for the closing.

### Matching clips to pieces by listening to them

```bash
python3 scripts/production.py match
```

Name your files however you like. `match` transcribes the first 30 seconds of each
clip and compares it against every planned piece, so the mapping comes from **what
you actually said**, not from a filename you have to keep straight on a shoot day.

It tolerates paraphrase, because nobody delivers their script verbatim:

```
✓ C0042.MOV  →  #042  (0.68)
✓ C0043.MOV  →  #051  (0.68)
? C0044.MOV: ambiguous between #12 (0.41) and #37 (0.38)
```

Two guards, because a wrong mapping edits the wrong piece: the match must clear an
absolute score **and** clearly beat the runner-up. Anything short of that is left
alone for you to set by hand — it refuses rather than guesses. Mic checks, slates and
false starts score near zero and are skipped.

Re-run it any time; results are cached per file, and renaming a clip just re-matches it.

### Paths that survive an unplugged drive

Video lives on external disks, and external disks are not always mounted. Every root
takes a `primary` and a `fallback`:

```json
"roots": {
  "footage": { "primary": "/Volumes/Rig/footage", "fallback": "~/Movies/footage" },
  "output":  { "primary": "/Volumes/Rig/out",     "fallback": "~/Movies/out" }
}
```

`check` reports the degradation loudly instead of failing later with a confusing
error. An unset path is *pending*, not an error — so `check` runs on day zero and
tells you exactly what is left to fill in.

---

## Requirements

- macOS or Linux (auto-install targets macOS/Homebrew)
- ffmpeg with `libx264` and `silencedetect`
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) providing `whisper-cli`
- Python 3 with numpy, scipy, soundfile, Pillow
- Inter + Playfair Display fonts *(only if you use generated cards)*

Everything runs locally. No API keys, no uploads, no per-minute billing.

---

## Pipeline

```
camera file  +  separate audio (optional)
        │
   sync + mux ............ cross-correlation, then compress/limit (never loudnorm)
        │
   silence split ......... bursts, transcribed one at a time
        │
   TAKE SELECTION ........ ← the part that matters
        │
   render ................ padding, word-level captions, face-anchored zoom
        │
   branding + music ...... intro/outro, deterministic music rotation
        │
   finished .mp4
```

---

## Configuration

One file, `reelforge.json`, written by the wizard and edited by hand after that.

| Knob | Does |
|---|---|
| `take_selector.closing_consolidation.theme_words` | Words that signal your closing line. **The single highest-leverage setting** — it collapses 3-4 attempts at the punchline into one |
| `take_selector.cluster_gap_sec` | Merge segments closer than this into one logical take |
| `captions.words_per_block` | Words on screen at once. 4-5 reads well vertically |
| `captions.accent_ratio` | Share of blocks with one highlighted word |
| `zoom.focal_y` | Vertical anchor for the zoom. `0.33` keeps a face in the upper third |
| `caption_fixes` | `[pattern, replacement]` pairs for words whisper reliably mangles |
| `music.duck_db` | How far the bed sits under the voice |

---

## Design notes

**Per-burst transcription is a correctness requirement, not an optimisation.**
Whisper hallucinates over silence — transcribe a whole file and it invents sentences
in the gaps, including stock phrases nobody said.

**No `loudnorm`.** On a single short it pumps and flattens delivery. Highpass,
compressor, limiter.

**Deterministic music.** Piece number → track and slice. Re-rendering piece 7 always
sounds identical, and a run of clips does not all open on the same four bars.

**Zoom is a transition, not an effect.** Only on segments over ~5s, max 1.08, with
ease-in / hold / ease-out. If you can see it happening, it is too strong.

**Start with no intro.** A branded card in front of a piece whose cut is not working
just delays the moment the viewer leaves.

---

## Not included

Publishing, scheduling, thumbnails, analytics, or anything that talks to a social
platform. This ends at a finished file on disk.

## License

MIT.
