# Troubleshooting

## Detection

**Only 1-2 bursts detected, each very long.**
The silence threshold is wrong for your room. Re-run detection with
`-25dB` (less strict) or a shorter minimum duration (`d=0.3`). A treated room
needs a lower threshold than a live one.

**Dozens of bursts, all fragments.**
Opposite problem — threshold too sensitive, or you breathe audibly between
clauses. Raise the minimum silence duration to `0.6`.

**A burst transcribes to empty.**
That burst is noise or breath. Drop it from the candidates. This is expected.

**Captions contain sentences nobody said** ("subscribe to the channel", "thanks for
watching").
Whisper hallucinated over silence. This is exactly the failure per-burst
transcription exists to prevent — confirm you are transcribing bursts individually
and not the whole file. If it persists, edit `body.json` directly and re-render with
`REUSE_TRANSCRIPT=1`.

## Sync

**Speaker is out of sync in the final render.**
Check the offset the cross-correlation reported and its confidence. Low confidence
usually means the two recordings barely overlap in content — the camera mic was too
far away or the clap/slate was outside the analysis window. Re-run
`sync_audio.py` with a larger `--window`, or pass a better `--approx` hint.

**Sync drifts over a long take.**
The two devices ran at slightly different sample rates. Cross-correlation aligns the
start, not the slope. Fix at the source (record both at 48kHz) or split the take.

## Take selection

**The closing line arrives in three fragments.**
`take_selector.closing_consolidation.theme_words` is empty or does not match how you
actually close. Read your own transcript, note the words you always reach for, and
put those in. This is the intended knob — do not patch the code.

**Two near-identical lines both survive.**
Lower `take_selector.similarity_threshold`. It is Jaccard overlap, so `0.5` means
half the words shared; `0.4` is more aggressive deduplication.

**The selector kept the wrong take.**
It is a first pass, not a director. Edit the timestamps in `config_curated.txt` by
hand and render. Arguing with automatic selection costs more than overriding it.

## Render

**Zoom crops the face badly.**
`zoom.focal_y` lower keeps the anchor higher in frame — `0.33` puts it in the upper
third, which suits most talking-head framings. If the subject is off-center, adjust
`zoom.focal_x`. If the movement is visible as an effect, `zoom.max` is too high;
1.06 is subtle, 1.10 is assertive.

**Captions wrap to two lines.**
`captions.single_line` should be true, and `words_per_block` may be too high for
your longest words. Drop from 5 to 4.

**Audio pumps or sounds flat.**
Confirm no `loudnorm` crept into the chain. On a single short it pumps and flattens
delivery — highpass, compressor, limiter only.

**Music overwhelms the voice.**
`music.duck_db` more negative. `-18` is the safe default, `-22` for a quiet delivery.

**Concat re-encodes and takes forever.**
Your intro/outro clips do not match the body's resolution or frame rate. Match them
and the concat becomes a stream copy.

## Setup

**`doctor.sh` says whisper-cli missing but `whisper` exists.**
Those are different tools. This pipeline expects whisper.cpp, which ships
`whisper-cli`. `brew install whisper-cpp`.

**Model download fails.**
The models come from the whisper.cpp Hugging Face repo. If the URL 404s, the file
name changed upstream — download manually into `~/whisper-models/` (or set
`WHISPER_MODELS_DIR`).

**Fonts fall back to a default face.**
Only affects generated cards. Install Inter and Playfair Display, or switch
`branding.mode` to `images`/`video` and supply your own artwork.

**`setup.py` refuses to run.**
It needs an interactive terminal. Run it yourself in a shell — it cannot be answered
on your behalf.
