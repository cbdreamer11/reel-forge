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

## Install

```bash
git clone https://github.com/<you>/reel-forge.git
cd reel-forge
./scripts/doctor.sh --install
```

`doctor.sh` checks and installs ffmpeg, whisper.cpp, the whisper models, the Python
packages and the fonts. It prompts before each install; `--yes` skips the prompts.

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
