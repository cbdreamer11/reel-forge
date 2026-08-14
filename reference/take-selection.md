# Take selection — the part that matters

Everything else in this pipeline is plumbing. **This is the actual craft**, and it is
the reason the output does not look like every other auto-generated clip.

When a person talks to a camera without a teleprompter, they do not deliver a clean
take. They deliver **the same idea three or four times, getting better each time.**
Most auto-editors treat that as one continuous recording and cut it on silence.
The result is a video where the speaker says the same thing twice, stumbles, and
corrects themselves on camera.

Take selection is the step that picks **which attempt survives.**

---

## THE CARDINAL RULE

> **One take per idea. Never bridge audio across takes.**

If you splice the first half of attempt #2 to the second half of attempt #3, the
room tone shifts, the breath lands wrong, and the viewer feels it even if they can
not name it. Pick one attempt, whole. If no single attempt is usable, the idea does
not go in the piece.

---

## The five failure patterns

These are the ways a real person fails and retries. Learn to recognise them in the
transcript and the choice becomes mechanical.

### 1. Wrong word, restart
> "Cancer doesn't start as a **cancer**" → *[pause]* → "Cancer doesn't start as a **tumour**"

The speaker reached for a word, got the wrong one, and went back. **Use the second.**

### 2. Trailing off
> "...that conver—" → *[pause]* → "...that difficult conversation..."

Started, lost it, restarted. **Use the complete one.**

### 3. Wording escalation
> "they operate **well**" → "they operate **excellently**" → "they operate **exceptionally**"

Each attempt is more precise than the last. This is the speaker refining live.
**The last one is almost always the most polished.**

### 4. The closing line, 3-4 attempts
The punchline is where people try hardest and retry most. **The last take wins** —
it is the one that best matches what they meant to say.

This pattern is common enough that the pipeline handles it specially: see
*Closing consolidation* below.

### 5. Fragment then full version
> brief fragment → *long pause* → complete version

**Use the complete one.**

---

## Decision rules

- **2-4 consecutive versions of the same sentence → the LAST is usually right.**
- **One version matches your script/title and the others paraphrase → use the one
  that matches.** This overrides "use the last."
- **All takes are partial → use the most complete one and accept the imperfection.**
  A slightly rough real take beats a Frankenstein splice.
- **If you are looking at 15+ segments, you are over-cutting.** A 30-60s piece is
  **2-4 curated segments**, rarely more.

---

## Re-transcribing every burst is non-negotiable

Whisper hallucinates over silence. Transcribe the **whole file** and it will invent
sentences in the gaps — including stock phrases like "subscribe to the channel"
that were never said.

The pipeline therefore: detects silence → splits into bursts → **transcribes each
burst separately**. Slower, and the only way the transcript is trustworthy.

If a burst comes back empty, that burst is noise. Drop it.

---

## Structure of a short piece

| Section | Position | Segments |
|---|---|---|
| **Hook** | 0-3s | 1 |
| **Body** | 3-25s | 1-2 |
| **Close** | 25-30s | 1 |

**Include the hook explicitly** in your candidate list. The selector never looks
outside the range you give it — if the hook is before the first detected burst
(common: people start talking before they settle), it will not be found for you.

---

## Closing consolidation

Because pattern #4 is so reliable, the selector can collapse several attempts at
the closing line into one. It needs to know **which words signal your closing.**

In `reelforge.json`:

```json
"take_selector": {
  "closing_consolidation": {
    "theme_words": ["question", "ask yourself", "so", "the point"],
    "min_theme_matches": 3
  }
}
```

Any take containing at least `min_theme_matches` of those words becomes a candidate
anchor for the close; the last such take wins and the earlier attempts drop.

**Leave it empty on your first runs.** Do 3-5 pieces, read your own closing lines,
and you will see the words you always reach for. Fill it in then — it is the single
highest-leverage setting in the file.

---

## How to read the selector output

The log prints a **"Final order"** showing which takes survived and why. Read it
before rendering. If the close still arrives in fragments, your `theme_words` are
wrong or too narrow — that is the knob, not the code.

---

## When to override the machine

The selector is a good first pass, not a director. Override it when:

- The best-worded take has bad delivery (rushed, flat, looking away).
- An earlier take has a gesture or expression the later one lacks.
- The "complete" take is complete but boring, and a fragment is electric.

The config file is plain text on purpose. **Edit the timestamps by hand and move
on** — arguing with an automatic selection costs more than fixing it.
