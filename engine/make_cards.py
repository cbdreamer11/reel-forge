#!/usr/bin/env python3
"""
make_cards.py — Generate Cover and Outro PNG cards (1080x1920) parameterized by brand.

Usage:
  python3 make_cards.py --brand my-profile \
      --section "SECTION_TAG" --title "The title of this piece" \
      --out-cover cover.png --out-outro outro.png \
      [--system-name "MY SHOW"]

Reads brand config from <repo>/brands/<id>/brand.json. Pulls colors, logos,
display name and tagline from the brand. Fonts are loaded from
~/Library/Fonts/ with /Library/Fonts/ as fallback.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


REPO_ROOT = Path(__file__).resolve().parents[2]
FONT_DIRS = [
    Path.home() / "Library" / "Fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
]

# Inter on this machine ships as .otf; Playfair may not be installed at all.
FONT_CANDIDATES = {
    "playfair_regular": ["PlayfairDisplay-Regular.ttf", "PlayfairDisplay-Regular.otf",
                         "Playfair Display.ttc", "PlayfairDisplay.ttf"],
    "inter_bold":       ["Inter-Bold.ttf", "Inter-Bold.otf", "InterDisplay-Bold.otf"],
    "inter_semibold":   ["Inter-SemiBold.ttf", "Inter-SemiBold.otf", "InterDisplay-SemiBold.otf"],
    "inter_regular":    ["Inter-Regular.ttf", "Inter-Regular.otf", "InterDisplay-Regular.otf"],
}


def find_font(role):
    for name in FONT_CANDIDATES[role]:
        for d in FONT_DIRS:
            p = d / name
            if p.exists():
                return str(p)
    return None


def load_font(role, size, missing_ok=False):
    p = find_font(role)
    if p is None:
        if missing_ok:
            print(f"[warn] font role '{role}' not found; falling back to default",
                  file=sys.stderr)
            return ImageFont.load_default()
        raise FileNotFoundError(
            f"Could not locate a font for role '{role}'. Looked for "
            f"{FONT_CANDIDATES[role]} in {[str(d) for d in FONT_DIRS]}."
        )
    return ImageFont.truetype(p, size=size)


def hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def load_brand(brand_id):
    path = REPO_ROOT / "brands" / brand_id / "brand.json"
    if not path.exists():
        raise FileNotFoundError(f"brand.json not found at {path}")
    with open(path) as f:
        return json.load(f), path.parent


def text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def wrap_text(draw, text, font, max_width):
    """Greedy word-wrap respecting pixel width."""
    words = text.split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for w in words[1:]:
        trial = current + " " + w
        if text_size(draw, trial, font)[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines


def draw_centered_lines(draw, lines, font, y_start, fill, line_spacing=14, width=1080):
    y = y_start
    for line in lines:
        w, h = text_size(draw, line, font)
        draw.text(((width - w) // 2, y), line, font=font, fill=fill)
        y += h + line_spacing
    return y


def spaced_section(text):
    """'SECTION' -> 'S · E · C · T · I · O · N'."""
    chars = [c for c in text if not c.isspace()]
    return " · ".join(chars)


def paste_logo(canvas, logo_path, target_w, target_h, cx, cy):
    if not logo_path or not os.path.exists(logo_path):
        print(f"[warn] logo not found at {logo_path}; skipping", file=sys.stderr)
        return
    img = Image.open(logo_path).convert("RGBA")
    # Preserve aspect ratio, fit inside box
    iw, ih = img.size
    scale = min(target_w / iw, target_h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(iw * scale) * ih // max(1, iw))
    # safer: recompute nh from scale to avoid stretch
    nh = max(1, int(ih * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    x = int(cx - nw / 2)
    y = int(cy - nh / 2)
    canvas.alpha_composite(img, (x, y))


def make_cover(brand, brand_dir, section, title, system_name, out_path):
    W, H = 1080, 1920
    colors = brand.get("colors", {})
    bg = hex_to_rgb(colors.get("background", "#111111"))
    accent = hex_to_rgb(colors.get("accent", "#4C8DFF"))
    text_color = hex_to_rgb(colors.get("text", "#FAFAFA"))

    canvas = Image.new("RGBA", (W, H), bg + (255,))
    draw = ImageDraw.Draw(canvas)

    # Border 2px accent
    draw.rectangle([(0, 0), (W - 1, H - 1)], outline=accent, width=2)

    # Section tag (Inter SemiBold 36)
    section_font = load_font("inter_semibold", 36)
    section_text = spaced_section(section)
    pw, ph = text_size(draw, section_text, section_font)
    section_y = 240
    draw.text(((W - pw) // 2, section_y), section_text, font=section_font, fill=accent)
    # Underline 100px wide, 3px, centered ~20px below text
    ul_w = 100
    ul_y = section_y + ph + 20
    draw.rectangle(
        [((W - ul_w) // 2, ul_y), ((W + ul_w) // 2, ul_y + 3)],
        fill=accent,
    )

    # Title (Playfair Regular 110) centered y~750, wrap at W-200
    title_font = load_font("playfair_regular", 110, missing_ok=True)
    title_lines = wrap_text(draw, title, title_font, W - 200)
    # Compute total height to center around y=750
    line_heights = [text_size(draw, ln, title_font)[1] for ln in title_lines]
    total_h = sum(line_heights) + 14 * (len(title_lines) - 1)
    y_start = 750 - total_h // 2
    draw_centered_lines(draw, title_lines, title_font, y_start, text_color,
                        line_spacing=14, width=W)

    # Bottom block
    name_font = load_font("inter_bold", 42)
    sys_font = load_font("inter_regular", 30)

    # Person name comes from the profile — never hardcoded.
    person_name = (brand.get("person_name")
                   or " ".join(filter(None, [brand.get("person_name_primary"),
                                             brand.get("person_name_secondary")]))
                   or brand.get("name", "")).upper()
    display_name = system_name or brand.get("name", "")
    y_name = 1620
    nw, nh = text_size(draw, person_name, name_font)
    draw.text(((W - nw) // 2, y_name), person_name, font=name_font, fill=text_color)

    y_sys = y_name + nh + 16
    sw, sh = text_size(draw, display_name, sys_font)
    draw.text(((W - sw) // 2, y_sys), display_name, font=sys_font, fill=accent)

    # Horizontal logo 240x90 centered, below the names
    logos = brand.get("logos", {})
    logo_rel = logos.get("primary")
    if logo_rel:
        logo_path = str(brand_dir / logo_rel)
        paste_logo(canvas, logo_path, 240, 90, W // 2, y_sys + sh + 90)

    canvas.convert("RGB").save(out_path, "PNG")


def make_outro(brand, brand_dir, out_path):
    W, H = 1080, 1920
    colors = brand.get("colors", {})
    bg = hex_to_rgb(colors.get("background", "#111111"))
    accent = hex_to_rgb(colors.get("accent", "#4C8DFF"))
    text_color = hex_to_rgb(colors.get("text", "#FAFAFA"))

    canvas = Image.new("RGBA", (W, H), bg + (255,))
    draw = ImageDraw.Draw(canvas)

    # Thin accent border
    draw.rectangle([(0, 0), (W - 1, H - 1)], outline=accent, width=2)

    # Vertical logo 600x900 at y=460
    logos = brand.get("logos", {})
    vlogo = logos.get("vertical") or logos.get("primary")
    if vlogo:
        logo_path = str(brand_dir / vlogo)
        paste_logo(canvas, logo_path, 600, 900, W // 2, 460 + 450)

    # Tagline (if present) Inter color=accent y=1430
    tagline = brand.get("tagline")
    tag_font = load_font("inter_regular", 32)
    if tagline:
        tw, th = text_size(draw, tagline, tag_font)
        draw.text(((W - tw) // 2, 1430), tagline, font=tag_font, fill=accent)

    # Email Inter Bold color=text y=1530 — comes from the profile, never hardcoded
    email = brand.get("email") or brand.get("contact", {}).get("email", "")
    if email:
        email_font = load_font("inter_bold", 36)
        ew, eh = text_size(draw, email, email_font)
        draw.text(((W - ew) // 2, 1530), email, font=email_font, fill=text_color)

    canvas.convert("RGB").save(out_path, "PNG")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--brand", required=True, help="Brand id (matches brands/<id>/)")
    p.add_argument("--section", required=True, help="Section tag shown on the cover card, e.g. EPISODE_01")
    p.add_argument("--title", required=True, help="Cover title text")
    p.add_argument("--out-cover", required=True, help="Output path for cover PNG")
    p.add_argument("--out-outro", required=True, help="Output path for outro PNG")
    p.add_argument("--system-name", default=None,
                   help="Override the system/show name shown under the person name")
    args = p.parse_args()

    brand, brand_dir = load_brand(args.brand)

    make_cover(brand, brand_dir, args.section, args.title, args.system_name,
               args.out_cover)
    make_outro(brand, brand_dir, args.out_outro)
    print(f"wrote {args.out_cover} and {args.out_outro}")


if __name__ == "__main__":
    main()
