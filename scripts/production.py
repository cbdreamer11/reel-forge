#!/usr/bin/env python3
"""
production.py — The layer above the editor: your planned content list.

An editor cuts one clip. A series needs to know what pieces exist, which are
shot, and where the files live. That list already exists somewhere — a
spreadsheet, an HTML dashboard, a Notion export. This reads it instead of
asking you to retype it.

    production.py import          read your list -> pieces.json
    production.py vocab           derive hook/closing word lists from the list
    production.py check           preflight: paths, drives, what's still missing

Nothing here is hardcoded. Every path comes from `reelforge.json`, and an
unset path is "pending", not an error — so `check` runs on day zero, before a
single frame is shot, and tells you exactly what is left to fill in.
"""

import argparse
import csv
import html as html_mod
import json
import os
import re
import shutil
import sys
import unicodedata
from collections import Counter
from pathlib import Path

G, Y, R, B, D, X = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[2m", "\033[0m"

STOPWORDS = {
    "es": set("""a al algo alguna algunas alguno algunos ante antes aqui como con
        contra cual cuales cuando de del desde donde dos el ella ellas ellos en
        entre era eran es esa esas ese eso esos esta estas este esto estos ha han
        hasta hay la las le les lo los mas me mi mis mucho muchos muy nada ni no
        nos nuestra nuestro o os otra otras otro otros para pero poco por porque
        que quien quienes se ser si sin sobre solo son su sus tambien tan tanto te
        tener tiene tienen todo todos tu tus un una uno unos y ya yo""".split()),
    "en": set("""a about after all also an and any are as at be been but by can do
        does for from get had has have how i if in into is it its just like make
        may more most my no not now of on one or our out over so some than that
        the their them then there these they this to too up us was we were what
        when which who will with would you your""".split()),
}


# ── config / paths ────────────────────────────────────────────────────────

def expand(p):
    return os.path.expanduser(p) if p else ""


def find_config(start=None):
    here = Path(start or os.getcwd()).resolve()
    for d in [here, *here.parents]:
        c = d / "reelforge.json"
        if c.exists():
            return c
    env = os.environ.get("REELFORGE_CONFIG")
    if env and Path(expand(env)).exists():
        return Path(expand(env))
    raise FileNotFoundError(
        "No reelforge.json found. Run scripts/setup.py first, or set "
        "REELFORGE_CONFIG=/path/to/reelforge.json")


class Root:
    """A path with an optional fallback. Handles the external-drive case:
    edit on the SSD when it is plugged in, keep working when it is not."""

    def __init__(self, name, spec):
        if isinstance(spec, str):
            spec = {"primary": spec}
        spec = spec or {}
        self.name = name
        self.primary = expand(spec.get("primary", ""))
        self.fallback = expand(spec.get("fallback", ""))
        self.hint = spec.get("hint", "")
        if not self.primary and not self.fallback:
            self.status, self.path = "pending", None
        elif self.primary and Path(self.primary).exists():
            self.status, self.path = "ok", Path(self.primary)
        elif self.fallback:
            self.status, self.path = "fallback", Path(self.fallback)
        else:
            self.status, self.path = "missing", None

    @property
    def degraded(self):
        return self.status == "fallback" and bool(self.primary)


class Config:
    def __init__(self, path=None):
        self.path = Path(path) if path else find_config()
        self.cfg = json.loads(self.path.read_text(encoding="utf-8"))
        self.dir = self.path.parent

    def get(self, *keys, default=None):
        node = self.cfg
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @property
    def language(self):
        return self.get("project", "language", default="auto")

    def roots(self):
        raw = self.get("roots", default=None)
        if raw:
            return {k: Root(k, v) for k, v in raw.items() if not k.startswith("_")}
        # Fall back to the flat shape written by setup.py
        src = self.get("sources", default={}) or {}
        out = {"footage": Root("footage", src.get("video_dir", ""))}
        if src.get("audio_separate"):
            out["audio"] = Root("audio", src.get("audio_dir", ""))
        music = self.get("music", default={}) or {}
        if music.get("enabled"):
            out["music"] = Root("music", music.get("pool_dir", ""))
        return out

    def pieces_json(self):
        return self.dir / "pieces.json"


# ── import ────────────────────────────────────────────────────────────────

def strip_tags(fragment):
    t = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html_mod.unescape(t)).strip()


def rows_from_html(path, columns):
    src = Path(path).read_text(encoding="utf-8", errors="replace")
    needed = max(columns.values()) if columns else 0
    out = []
    for r in re.findall(r"<tr\b.*?</tr>", src, re.S | re.I):
        cells = re.findall(r"<(t[dh])\b[^>]*>(.*?)</t[dh]>", r, re.S | re.I)
        if len(cells) <= needed:
            continue
        # Header rows are <th>-only. A long list is usually split into several
        # tables, so headers repeat — let one through and it silently displaces
        # a real row via the index fallback.
        if all(tag.lower() == "th" for tag, _ in cells):
            continue
        vals = [strip_tags(c) for _, c in cells]
        if not any(vals):
            continue
        item = {f: vals[i] for f, i in columns.items() if i < len(vals)}
        for k, v in re.findall(r'data-([a-z0-9_-]+)="([^"]*)"', r, re.I):
            item.setdefault(k, v)
        out.append(item)
    return out


def rows_from_csv(path, columns):
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not any(row):
                continue
            out.append({f: row[i] for f, i in columns.items() if i < len(row)})
    return out


def rows_from_json(path, columns):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("pieces", "items", "reels", "rows", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    # For JSON, `columns` maps our field -> their key name.
    return [{f: str(row.get(k, "")) for f, k in columns.items()}
            for row in data if isinstance(row, dict)]


def rows_from_markdown(path, columns):
    needed = max(columns.values()) if columns else 0
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) <= needed or all(set(c) <= set("-: ") for c in cells):
            continue
        out.append({f: cells[i] for f, i in columns.items() if i < len(cells)})
    return out


READERS = {"html_table": rows_from_html, "csv": rows_from_csv,
           "json": rows_from_json, "markdown_table": rows_from_markdown}


def cmd_import(cfg, args):
    spec = cfg.get("content_list", default=None)
    if not spec or not spec.get("path"):
        print("No content list configured.\n")
        print("Add this to reelforge.json and point it at the list you already keep:\n")
        print(json.dumps({"content_list": {
            "path": "~/path/to/your-list.html",
            "format": "html_table",
            "columns": {"num": 0, "type": 1, "hook": 2, "message": 3, "closing": 4},
        }}, indent=2))
        print("\nformats: " + ", ".join(sorted(READERS)))
        print("columns: field -> column index (0-based); for json, field -> key name")
        return 1

    src = Path(expand(spec["path"]))
    if not src.exists():
        print(f"{R}List not found:{X} {src}")
        return 1
    fmt = spec.get("format", "html_table")
    if fmt not in READERS:
        print(f"{R}Unknown format '{fmt}'.{X} Use one of: {', '.join(sorted(READERS))}")
        return 1

    rows = READERS[fmt](src, spec.get("columns", {}))

    # If the list numbers its own rows, trust that and drop anything unnumbered —
    # those are headers, separators or notes. Only fall back to positional
    # numbering when no row carries a number at all.
    numbered = [r for r in rows if str(r.get("num", "")).strip().isdigit()]
    if numbered and len(numbered) >= len(rows) * 0.5:
        skipped = len(rows) - len(numbered)
        rows = numbered
        if skipped:
            print(f"{D}  skipped {skipped} unnumbered row(s) (headers/separators){X}")

    pieces, seen = [], set()
    for i, it in enumerate(rows, 1):
        num = str(it.get("num", "")).strip()
        num = int(num) if num.isdigit() else i
        if num in seen:
            continue
        seen.add(num)
        it["num"] = num
        it.setdefault("status", "planned")   # planned → shot → edited → published
        it.setdefault("clip", "")
        it.setdefault("audio", "")
        pieces.append(it)

    if not pieces:
        print(f"Read {src.name} but found no usable rows.")
        print("Check content_list.columns — indices are 0-based.")
        return 1
    pieces.sort(key=lambda p: p["num"])

    doc = {"source": str(src), "format": fmt, "total": len(pieces), "pieces": pieces}
    if args.dry_run:
        print(f"{len(pieces)} pieces (#{pieces[0]['num']}–#{pieces[-1]['num']}). First 3:")
        for p in pieces[:3]:
            print(f"  #{p['num']:03d} {str(p.get('hook', ''))[:70]}")
        print(f"{D}--dry-run: nothing written{X}")
        return 0

    out = cfg.pieces_json()
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    types = Counter(str(p.get("type", "?")) for p in pieces)
    print(f"{G}✓{X} {len(pieces)} pieces  (#{pieces[0]['num']}–#{pieces[-1]['num']})")
    if len(types) > 1:
        print("  types: " + ", ".join(f"{k}={v}" for k, v in sorted(types.items())))
    print(f"  wrote {out}")
    print(f"\n  next: production.py vocab")
    return 0


# ── vocab ─────────────────────────────────────────────────────────────────

def norm(w):
    w = unicodedata.normalize("NFD", w.lower())
    return "".join(c for c in w if unicodedata.category(c) != "Mn")


def toks(text):
    return [t for t in re.findall(r"[^\W\d_]+", str(text or ""), re.UNICODE) if len(t) > 2]


def suggest_threshold(pieces, themes, field, stop, top_k=6):
    """How many theme-word hits should mark a closing?

    Returns (threshold, coverage, typical_hits). Looks at the top candidates as
    a GROUP: what fraction of closings contain at least one, and how many they
    typically contain. A series whose markers are split across content types
    gives low per-word frequency but high group coverage with ~1 hit each.
    """
    if not themes:
        return 3, 0.0, 0
    top = {w for w, _ in themes[:top_k]}
    hits = []
    for p in pieces:
        words = [norm(t) for t in toks(p.get(field, "")) if norm(t) not in stop]
        hits.append(sum(1 for w in words if w in top))
    covered = [h for h in hits if h > 0]
    coverage = len(covered) / len(pieces) if pieces else 0.0
    if not covered:
        return 3, 0.0, 0
    covered.sort()
    typical = covered[len(covered) // 2]          # median among covered
    if coverage < 0.5:
        return 3, coverage, typical               # weak signal, stay strict
    return max(1, min(typical, 3)), coverage, typical


def cmd_vocab(cfg, args):
    pj = cfg.pieces_json()
    if not pj.exists():
        print(f"No {pj.name}. Run: production.py import")
        return 1
    pieces = json.loads(pj.read_text(encoding="utf-8")).get("pieces", [])
    if not pieces:
        print("No pieces.")
        return 1

    lang = (cfg.language or "auto")[:2]
    stop = STOPWORDS.get(lang, set())
    if not stop:
        stop = set().union(*STOPWORDS.values())
        print(f"{D}  (no stopword list for '{lang}'; using the union of all){X}\n")

    # closing theme words: document frequency over the closing field
    df = Counter()
    for p in pieces:
        words = {norm(t) for t in toks(p.get(args.closing_field, "")) if norm(t) not in stop}
        df.update(words)
    themes = [(w, n) for w, n in df.most_common() if n >= args.min_docs][:args.top]

    # hook openers: repeated leading n-grams
    starts = Counter()
    for p in pieces:
        t = toks(p.get(args.hook_field, ""))
        for n in range(2, 5):
            if len(t) >= n:
                starts[" ".join(norm(x) for x in t[:n])] += 1
    cands = [(g, c) for g, c in starts.most_common() if c >= args.min_docs]
    hooks = [(g, c) for g, c in cands
             if not any(o.startswith(g + " ") and oc >= c for o, oc in cands if o != g)]

    total = len(pieces)
    print(f"{B}closing theme words{X}  {D}(field '{args.closing_field}', ≥{args.min_docs} of {total}){X}")
    for w, n in themes:
        print(f"  {n:4d}×  {w}")
    if not themes:
        print(f"{D}  none repeat enough — your closings are varied.{X}")
        print(f"{D}  Leave theme_words empty; the consolidation pass then does nothing,{X}")
        print(f"{D}  which is correct. A forced list would merge takes it should not.{X}")

    print(f"\n{B}hook openers{X}  {D}(field '{args.hook_field}', ≥{args.min_docs} of {total}){X}")
    for g, n in hooks[:15]:
        print(f"  {n:4d}×  \"{g}\"")
    if not hooks:
        print(f"{D}  none repeat — you open every piece differently. Leave it empty:{X}")
        print(f"{D}  --reorder-hook becomes a no-op and your chosen order is respected.{X}")

    # Threshold advice. Measure the COVERAGE OF THE GROUP, not of one word:
    # a series often splits its closing markers across content types ("save
    # this" / "comment" / "share"), so no single word is frequent, yet exactly
    # one appears per piece and a threshold of 1 is right.
    suggested, coverage, typical = suggest_threshold(pieces, themes,
                                                     args.closing_field, stop)
    if themes:
        print(f"\n{B}suggested min_theme_matches: {suggested}{X}")
        print(f"{D}  top words cover {coverage:.0%} of closings, typically {typical} hit(s) each.{X}")
        if suggested == 1:
            print(f"{D}  One hit is enough to mark the closing. Ties then resolve to the LAST{X}")
            print(f"{D}  take, which is the cardinal rule. A higher threshold would never be{X}")
            print(f"{D}  reached and the pass would silently never run.{X}")

    print(f"\n{D}These are candidates, not truth. Drop anything generic: a word in every{X}")
    print(f"{D}closing distinguishes nothing and will consolidate takes it should not.{X}")

    if args.write:
        # At threshold 1 a single generic word ("someone", "you") is enough to
        # mark a take as the closing — so write only the tight high-coverage
        # cluster, not the whole tail. At higher thresholds several words must
        # co-occur, so a longer list is safe and helps.
        if suggested == 1:
            # Cut at the frequency cliff. Real markers cluster near the top and
            # then the counts fall off a ledge (e.g. 30, 30, 29, then 8) — every
            # word past that ledge is ordinary language, and at threshold 1 one
            # ordinary word is enough to misidentify a body take as the closing.
            ceiling = themes[0][1]
            words = [w for w, n in themes if n >= ceiling * 0.4]
            note = ("threshold is 1, so only the marker cluster above the frequency "
                    "cliff is written — a broader list would let a body take match "
                    "on one ordinary word")
        else:
            words = [w for w, _ in themes]
            note = "review before pasting into reelforge.json; drop anything generic"

        out = {
            "_derived_from": str(pj),
            "_note": note,
            "take_selector": {
                "hook_openers": [r"\b" + r"\s+".join(map(re.escape, g.split())) + r"\b"
                                 for g, _ in hooks[:10]],
                "closing_consolidation": {
                    "theme_words": words,
                    "min_theme_matches": suggested,
                },
            },
        }
        Path(args.write).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
        print(f"\n{G}✓{X} wrote {args.write}  ({len(words)} theme word(s), min={suggested})")
    return 0


# ── check ─────────────────────────────────────────────────────────────────

def cmd_check(cfg, args):
    blockers = pending = 0
    print(f"\n{B}reel-forge preflight{X}  {D}{cfg.path}{X}")

    print(f"\n{B}Content list{X}")
    pj = cfg.pieces_json()
    if pj.exists():
        doc = json.loads(pj.read_text(encoding="utf-8"))
        pieces = doc.get("pieces", [])
        by = Counter(p.get("status", "?") for p in pieces)
        print(f"  {G}✓{X} {len(pieces)} pieces  ("
              + ", ".join(f"{k}={v}" for k, v in sorted(by.items())) + ")")
        with_file = sum(1 for p in pieces if p.get("clip"))
        if with_file:
            print(f"  {G}✓{X} {with_file}/{len(pieces)} have a file assigned")
        else:
            print(f"  {Y}!{X} no piece has a file assigned yet"); pending += 1
            print(f"{D}      expected before you shoot{X}")
    elif cfg.get("content_list", default=None):
        print(f"  {Y}!{X} pieces.json not generated — run: production.py import"); pending += 1
    else:
        print(f"  {D}no content list configured (optional){X}")

    print(f"\n{B}Paths{X}")
    roots = cfg.roots()
    if not roots:
        print(f"  {Y}!{X} no roots configured — run scripts/setup.py"); pending += 1
    for name, r in roots.items():
        if r.status == "pending":
            print(f"  {Y}!{X} {name}: not set"); pending += 1
            if r.hint:
                print(f"{D}      {r.hint}{X}")
        elif r.status == "missing":
            print(f"  {R}✗{X} {name}: set but does not exist → {r.primary}"); blockers += 1
        elif r.degraded:
            print(f"  {Y}!{X} {name}: primary unavailable → using fallback")
            print(f"{D}      primary: {r.primary}{X}")
            print(f"{D}      in use:  {r.path}{X}")
        else:
            print(f"  {G}✓{X} {name}: {r.path}")

    # External drives referenced by any root
    vols = {"/" + "/".join(r.primary.strip("/").split("/")[:2])
            for r in roots.values() if r.primary.startswith("/Volumes/")}
    if vols:
        print(f"\n{B}External drives{X}")
        for v in sorted(vols):
            if Path(v).exists():
                free = shutil.disk_usage(v).free / 1e9
                print(f"  {G}✓{X} mounted: {v}  ({free:.0f} GB free)")
            else:
                print(f"  {Y}!{X} NOT mounted: {v}")
                print(f"{D}      falling back to local paths — plug it in before rendering{X}")

    print(f"\n{B}Verdict{X}")
    if blockers:
        print(f"  {R}✗{X} {blockers} blocker(s).")
        return 1
    if pending:
        print(f"  {Y}!{X} No blockers. {pending} item(s) pending — normal before you shoot.")
        print(f"{D}      import and vocab do not need any footage.{X}")
        return 0
    print(f"  {G}✓{X} Ready.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="read your content list into pieces.json")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("vocab", help="derive hook/closing word lists from the list")
    p.add_argument("--hook-field", default="hook")
    p.add_argument("--closing-field", default="closing")
    p.add_argument("--min-docs", type=int, default=3)
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--write", default=None, help="write candidates to a JSON file")

    sub.add_parser("check", help="preflight: paths, drives, what is missing")

    args = ap.parse_args()
    try:
        cfg = Config(args.config)
    except FileNotFoundError as e:
        print(e)
        return 1
    return {"import": cmd_import, "vocab": cmd_vocab, "check": cmd_check}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
