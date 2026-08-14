#!/usr/bin/env bash
# doctor.sh — Check (and optionally install) everything reel-forge needs.
#
#   ./doctor.sh              check only, report what's missing
#   ./doctor.sh --install    prompt to install each missing piece
#   ./doctor.sh --install --yes   install without prompting
#
# Exit code 0 = ready to render. 1 = something required is missing.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
MODELS_DIR="${WHISPER_MODELS_DIR:-$HOME/whisper-models}"

INSTALL=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    --yes|-y)  ASSUME_YES=1 ;;
    --help|-h) sed -n '2,9p' "$0"; exit 0 ;;
  esac
done

MISSING_REQUIRED=0
MISSING_OPTIONAL=0

c_ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
c_bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }
c_warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
head_()  { printf "\n\033[1m%s\033[0m\n" "$1"; }

ask() {
  # ask "prompt" -> 0 if yes
  [[ $INSTALL -eq 0 ]] && return 1
  [[ $ASSUME_YES -eq 1 ]] && return 0
  read -r -p "    → $1 [y/N] " reply </dev/tty
  [[ "$reply" =~ ^[Yy] ]]
}

OS="$(uname -s)"

head_ "Platform"
if [[ "$OS" == "Darwin" ]]; then
  c_ok "macOS"
  if command -v brew >/dev/null 2>&1; then
    c_ok "homebrew $(brew --version | head -1 | awk '{print $2}')"
    HAS_BREW=1
  else
    c_bad "homebrew not found — needed to auto-install ffmpeg/whisper"
    echo "      install it from https://brew.sh then re-run"
    HAS_BREW=0
  fi
elif [[ "$OS" == "Linux" ]]; then
  c_ok "Linux"
  HAS_BREW=0
  c_warn "auto-install targets macOS/homebrew; on Linux use your package manager"
else
  c_warn "untested platform: $OS"
  HAS_BREW=0
fi

# ── python3 ───────────────────────────────────────────────────────────────
head_ "Python"
if command -v python3 >/dev/null 2>&1; then
  c_ok "python3 $(python3 --version 2>&1 | awk '{print $2}')"
else
  c_bad "python3 not found (required)"
  MISSING_REQUIRED=1
fi

for pkg in numpy scipy soundfile PIL; do
  if python3 -c "import $pkg" >/dev/null 2>&1; then
    c_ok "python: $pkg"
  else
    c_bad "python: $pkg missing"
    if ask "pip install the python requirements?"; then
      python3 -m pip install -r "$ROOT/engine/requirements.txt" && c_ok "installed" \
        || { c_bad "pip install failed"; MISSING_REQUIRED=1; }
    else
      MISSING_REQUIRED=1
    fi
  fi
done

# ── ffmpeg ────────────────────────────────────────────────────────────────
head_ "ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
  c_ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
  # NOTE: capture to a variable first. Piping into `grep -q` under `set -o pipefail`
  # makes grep close the pipe on first match, ffmpeg dies of SIGPIPE (141), and the
  # pipeline reports failure even though the filter is present.
  FF_FILTERS="$(ffmpeg -hide_banner -filters 2>/dev/null || true)"
  if [[ "$FF_FILTERS" == *silencedetect* ]]; then
    c_ok "filter: silencedetect"
  else
    c_bad "filter silencedetect missing — take detection will not work"
    MISSING_REQUIRED=1
  fi
  FF_ENCODERS="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
  if [[ "$FF_ENCODERS" == *libx264* ]]; then
    c_ok "encoder: libx264"
  else
    c_bad "encoder libx264 missing — cannot render"
    MISSING_REQUIRED=1
  fi
else
  c_bad "ffmpeg not found (required)"
  if [[ $HAS_BREW -eq 1 ]] && ask "brew install ffmpeg?"; then
    brew install ffmpeg && c_ok "installed" || MISSING_REQUIRED=1
  else
    MISSING_REQUIRED=1
  fi
fi

# ── whisper ───────────────────────────────────────────────────────────────
head_ "Whisper (transcription)"
if command -v whisper-cli >/dev/null 2>&1; then
  c_ok "whisper-cli: $(command -v whisper-cli)"
elif command -v whisper >/dev/null 2>&1; then
  c_warn "found 'whisper' but not 'whisper-cli' — this pipeline expects whisper.cpp"
  MISSING_REQUIRED=1
else
  c_bad "whisper-cli not found (required)"
  if [[ $HAS_BREW -eq 1 ]] && ask "brew install whisper-cpp?"; then
    brew install whisper-cpp && c_ok "installed" || MISSING_REQUIRED=1
  else
    MISSING_REQUIRED=1
  fi
fi

mkdir -p "$MODELS_DIR"
# base = fast, used for take selection. large-v3-turbo = accurate, used for final captions.
check_model() {
  local name="$1" size="$2" required="$3"
  local path="$MODELS_DIR/$name"
  if [[ -f "$path" ]]; then
    c_ok "model: $name"
    return
  fi
  if [[ "$required" == "required" ]]; then
    c_bad "model missing: $name (~$size)"
  else
    c_warn "model missing: $name (~$size) — optional, better final captions"
  fi
  if ask "download $name (~$size) to $MODELS_DIR?"; then
    curl -L --fail --progress-bar \
      -o "$path" \
      "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$name" \
      && c_ok "downloaded $name" \
      || { c_bad "download failed"; rm -f "$path"; [[ "$required" == "required" ]] && MISSING_REQUIRED=1; }
  else
    [[ "$required" == "required" ]] && MISSING_REQUIRED=1 || MISSING_OPTIONAL=1
  fi
}
check_model "ggml-base.bin" "142 MB" "required"
check_model "ggml-large-v3-turbo.bin" "1.6 GB" "optional"

# ── fonts ─────────────────────────────────────────────────────────────────
head_ "Fonts (only needed for generated cover/outro cards)"
font_found() {
  local pattern="$1" hit
  # Same SIGPIPE caveat as the ffmpeg checks — capture, then test.
  hit="$(find "$HOME/Library/Fonts" /Library/Fonts /System/Library/Fonts \
           -iname "$pattern" -print -quit 2>/dev/null || true)"
  [[ -n "$hit" ]]
}
if font_found "Inter*"; then
  c_ok "Inter"
else
  c_warn "Inter not installed (captions/cards fall back to a default face)"
  if [[ $HAS_BREW -eq 1 ]] && ask "brew install --cask font-inter?"; then
    brew install --cask font-inter && c_ok "installed" || MISSING_OPTIONAL=1
  else
    MISSING_OPTIONAL=1
  fi
fi
if font_found "PlayfairDisplay*" || font_found "Playfair*"; then
  c_ok "Playfair Display"
else
  c_warn "Playfair Display not installed (title face on generated covers)"
  if [[ $HAS_BREW -eq 1 ]] && ask "brew install --cask font-playfair-display?"; then
    brew install --cask font-playfair-display && c_ok "installed" || MISSING_OPTIONAL=1
  else
    MISSING_OPTIONAL=1
  fi
fi

# ── verdict ───────────────────────────────────────────────────────────────
head_ "Verdict"
if [[ $MISSING_REQUIRED -eq 0 ]]; then
  c_ok "Ready to render."
  [[ $MISSING_OPTIONAL -eq 1 ]] && c_warn "Some optional pieces are missing — output still works, quality is lower."
  echo
  echo "  Next: python3 $ROOT/scripts/setup.py    (first-time project setup)"
  exit 0
else
  c_bad "Not ready. Re-run with --install to fix the items marked ✗."
  echo
  echo "  ./scripts/doctor.sh --install"
  exit 1
fi
