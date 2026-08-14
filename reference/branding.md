# Intro, outro and look

## The recommendation first

**Start with `mode: none`.**

An intro is the last thing to add and the first thing people add. A 1.5-second
branded card in front of a piece whose cut is not working does not save the piece —
it just delays the moment the viewer leaves. Get the take selection and the captions
right, publish a few, *then* brand it.

When you do brand it, this is what the four modes mean.

---

## `none`

Straight into the content. The pipeline still renders captions, zoom and audio
treatment. Nothing else is prepended or appended.

Best for: everyone starting out, and for anything posted to a feed where the first
frame decides whether the viewer stays.

---

## `images`

You supply a still PNG/JPG for the intro and/or outro; each is held for a
configurable number of seconds and cross-faded into the body.

```json
"branding": {
  "mode": "images",
  "intro_image": "/path/to/cover.png",
  "outro_image": "/path/to/outro.png",
  "intro_sec": 1.5,
  "outro_sec": 2.5
}
```

Best for: you already have a designer, a Canva template, or a brand kit. Highest
quality-per-effort of the four, because the design work happens where design tools
are good and the pipeline just holds the frame.

Make them **1080×1920** to match the vertical canvas.

---

## `generated`

The pipeline draws the cards itself from a profile: background, text and accent
colors, an optional logo, the show name, the person name, and the contact block.

```json
"profile": {
  "name": "My Show",
  "person_name": "Jane Doe",
  "handle": "@janedoe",
  "email": "hi@example.com",
  "colors": { "background": "#111111", "text": "#FAFAFA", "accent": "#4C8DFF" },
  "logos": { "primary": "/path/to/logo.png" }
}
```

The title card carries the piece's title and section tag; the outro carries the
logo and contact details.

Best for: you have no design assets and want something consistent and clean today.
It will not look bespoke — it will look tidy, which is a real upgrade over nothing.

**The accent color does double duty:** it is also the caption highlight color, so
pick something readable against your footage, not just against the card.

---

## `video`

You supply pre-rendered intro/outro clips and the pipeline concatenates them as-is.

```json
"branding": {
  "mode": "video",
  "intro_clip": "/path/to/intro.mp4",
  "outro_clip": "/path/to/outro.mp4"
}
```

Best for: you animate elsewhere — After Effects, Remotion, Canva, whatever — and
want the pipeline to stop trying to be a motion designer. **This is the mode to use
for anything genuinely custom.** Animated logo reveals, transitions with sound
design, and anything with a texture or a 3D pass belong here.

Match the body's resolution and frame rate or the concat will re-encode.

---

## Captions

Captions are not branding, but they are the strongest visual signature the pipeline
has, so they belong in the same conversation.

| Setting | Default | What it does |
|---|---|---|
| `words_per_block` | 4 | Words shown at once. 4-5 reads well vertically; 1-2 is the "kinetic" style and is exhausting over 30s |
| `single_line` | true | Never wrap. Wrapping shifts the block's vertical center and the eye has to re-find it |
| `accent_ratio` | 0.8 | Share of blocks that get one word in the accent color. Below ~0.7 it looks accidental; at 1.0 it stops meaning anything |

**Fixing what whisper mishears.** Every speaker has words the model reliably gets
wrong: field jargon, proper nouns, product names. Put them in `caption_fixes` in
your profile as `[pattern, replacement]` pairs:

```json
"caption_fixes": [
  ["\\bkpis?\\b(?<![A-Z])", "KPI"],
  ["\\bmy compny\\b", "My Company"]
]
```

Add one entry each time you catch one in a render. After a dozen pieces the list
stops growing and your captions stop embarrassing you.

---

## Music

Deterministic by design: piece number → track and slice. Re-rendering piece 7 always
produces the same music, and different pieces use different parts of the pool, so a
run of clips does not all open on the same four bars.

`duck_db` sets how far the bed sits under the voice. `-18` is a safe default;
`-22` if your delivery is quiet, `-14` if the music is doing real work.

**Use music you have the rights to.** The pipeline does not check, and a copyright
strike is a worse outcome than a silent bed.
