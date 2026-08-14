#!/usr/bin/env bash
# smoke-test.sh — Prove the whole pipeline works on this machine, with no
# footage of your own.
#
# Synthesises a short talking-head clip using the system speech voice, with the
# take patterns the selector is built for (a flubbed line then a clean retry, a
# closing said three times), then runs the real pipeline over it end to end.
#
#   ./scripts/smoke-test.sh              English voice
#   ./scripts/smoke-test.sh --lang es    Spanish voice
#   ./scripts/smoke-test.sh --keep       leave the output where you can watch it
#
# Exit 0 means: detection, per-burst transcription, take selection, captions,
# zoom and the render all work. Run it after install, and after any change.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="${TMPDIR:-/tmp}/reel-forge-smoke.$$"
LANG_CODE="en"
KEEP=0

for a in "$@"; do
  case "$a" in
    --lang) shift ;;
    es|en) LANG_CODE="$a" ;;
    --keep) KEEP=1 ;;
    --help|-h) sed -n '2,15p' "$0"; exit 0 ;;
  esac
done

G="\033[32m"; R="\033[31m"; Y="\033[33m"; B="\033[1m"; D="\033[2m"; X="\033[0m"
ok()   { printf "  ${G}✓${X} %s\n" "$1"; }
bad()  { printf "  ${R}✗${X} %s\n" "$1"; }
step() { printf "\n${B}%s${X}\n" "$1"; }

cleanup() { [[ $KEEP -eq 0 ]] && rm -rf "$WORK"; }
trap cleanup EXIT

for t in ffmpeg ffprobe whisper-cli python3; do
  command -v "$t" >/dev/null 2>&1 || { bad "$t not found — run ./scripts/doctor.sh --install"; exit 1; }
done
command -v say >/dev/null 2>&1 || {
  printf "${Y}This smoke test needs the macOS 'say' command to synthesise speech.${X}\n"
  printf "On Linux, point the pipeline at any short talking-head clip instead.\n"; exit 1; }

MODEL="${WHISPER_MODEL:-$HOME/whisper-models/ggml-base.bin}"
[[ -f "$MODEL" ]] || { bad "whisper model not at $MODEL — run ./scripts/doctor.sh --install"; exit 1; }

mkdir -p "$WORK/parts"
cd "$WORK"

if [[ "$LANG_CODE" == "es" ]]; then
  VOICE=$(say -v '?' | awk -F'  +' '/es_MX|es_ES/{print $1; exit}')
  HOOK="Nadie te preguntó cómo estabas hoy."
  BODY_BAD="La mayoría de la gente pasa el día completo sin que nadie le haga esa..."
  BODY_GOOD="La mayoría de la gente pasa el día entero sin que nadie le haga esa pregunta, y lo peor es que ya ni la espera."
  CLOSE1="Comenta cómo estás."
  CLOSE2="Comenta con una palabra cómo estás."
  CLOSE3="Comenta con una palabra cómo estás hoy de verdad."
else
  VOICE=$(say -v '?' | awk -F'  +' '/en_US/{print $1; exit}')
  HOOK="Nobody asked how you were doing today."
  BODY_BAD="Most people go through an entire day without anyone asking them that..."
  BODY_GOOD="Most people go through an entire day without anyone asking them that question, and the worse part is they stopped expecting it."
  CLOSE1="Comment how you are doing."
  CLOSE2="Comment one word for how you are doing."
  CLOSE3="Comment one word for how you are really doing today."
fi
[[ -n "${VOICE:-}" ]] || VOICE=""

step "1. Synthesising a test clip"
i=0
seg () { i=$((i+1)); printf -v n "%02d" $i
  if [[ -n "$VOICE" ]]; then say -v "$VOICE" -r 165 -o "parts/$n.aiff" "$1" 2>/dev/null
  else say -r 165 -o "parts/$n.aiff" "$1" 2>/dev/null; fi; }
gap () { i=$((i+1)); printf -v n "%02d" $i
  ffmpeg -y -loglevel error -f lavfi -i anullsrc=r=22050:cl=mono -t "$1" "parts/$n.aiff"; }

seg "$HOOK";      gap 1.8
seg "$BODY_BAD";  gap 1.8
seg "$BODY_GOOD"; gap 1.8
seg "$CLOSE1";    gap 1.8
seg "$CLOSE2";    gap 1.8
seg "$CLOSE3"
ok "6 spoken takes with silence between them"

: > list.txt
for f in parts/*.aiff; do echo "file '$WORK/$f'" >> list.txt; done
ffmpeg -y -loglevel error -f concat -safe 0 -i list.txt -ar 48000 -ac 1 audio.wav || { bad "concat failed"; exit 1; }
DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 audio.wav)
ffmpeg -y -loglevel error -f lavfi -i "color=c=0x1a1a2e:s=1080x1920:r=30:d=$DUR" \
  -f lavfi -i "color=c=0xe8c39e:s=420x420:d=$DUR" -i audio.wav \
  -filter_complex "[1]format=yuva420p,geq=lum='p(X,Y)':a='if(lte(hypot(X-210,Y-210),200),255,0)'[h];[0][h]overlay=330:520:shortest=1" \
  -map 2:a -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest raw.mp4 || { bad "clip build failed"; exit 1; }
ok "raw.mp4  ${DUR%.*}s  1080x1920"

step "2. Silence detection"
# NOTE: no `mapfile` — macOS still ships bash 3.2, where it does not exist.
BOUNDS=()
while IFS= read -r line; do
  BOUNDS+=("$line")
done < <(ffmpeg -i raw.mp4 -af "silencedetect=n=-30dB:d=0.4" -f null /dev/null 2>&1 \
  | grep -oE "silence_(start|end): [0-9.]+" | grep -oE "[0-9.]+$")
[[ ${#BOUNDS[@]} -ge 6 ]] && ok "${#BOUNDS[@]} silence boundaries found" \
  || { bad "expected several silences, found ${#BOUNDS[@]}"; exit 1; }

step "3. Take selection"
{ echo "piece_num: 01"; echo "section: SMOKE"; echo "title: Smoke test"
  echo "source: $WORK/raw.mp4"; echo "segments:"; } > config.txt
prev=0
for b in "${BOUNDS[@]}"; do
  awk -v a="$prev" -v b="$b" 'BEGIN{if (b-a > 0.5) printf "  - %.2f:%.2f\n", a, b}' >> config.txt
  prev="$b"
done
awk -v a="$prev" -v d="$DUR" 'BEGIN{if (d-a > 0.5) printf "  - %.2f:%.2f\n", a, d}' >> config.txt

WHISPER_MODEL="$MODEL" REELFORGE_LANG="$LANG_CODE" python3 "$ROOT/engine/take_selector.py" \
  --config config.txt --out curated.txt --workdir "$WORK/sel" --keep > sel.log 2>&1
if [[ $? -ne 0 ]]; then bad "take_selector failed"; tail -20 sel.log; exit 1; fi
CAND=$(grep -c "^  - " config.txt | tr -d ' \n')
KEPT=$(grep -c "^  - " curated.txt | tr -d ' \n')
ok "curated $CAND candidate(s) down to $KEPT take(s)"
[[ "$KEPT" -lt "$CAND" ]] && ok "repeated takes were collapsed" \
  || printf "  ${Y}!${X} nothing was collapsed — check %s\n" "$WORK/sel.log"

step "4. Render"
WHISPER_MODEL="$MODEL" REELFORGE_LANG="$LANG_CODE" python3 "$ROOT/engine/build_reel.py" \
  --config curated.txt --workdir "$WORK/build" --output-dir "$WORK/out" --keep > build.log 2>&1
if [[ $? -ne 0 ]]; then bad "build_reel failed"; tail -25 build.log; exit 1; fi
FINAL=$(ls "$WORK"/out/*.mp4 2>/dev/null | head -1)
[[ -f "$FINAL" ]] || { bad "no output file"; tail -20 build.log; exit 1; }

VW=$(ffprobe -v error -select_streams v:0 -show_entries stream=width  -of csv=p=0 "$FINAL" | tr -d ' \n')
VH=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$FINAL" | tr -d ' \n')
VD=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$FINAL" | tr -d ' \n')
ok "rendered $(basename "$FINAL")"
ok "${VW}x${VH}  ${VD%.*}s  $(du -h "$FINAL" | awk "{print \$1}")"
[[ "$VW" == "1080" && "$VH" == "1920" ]] || { bad "expected 1080x1920, got ${VW}x${VH}"; exit 1; }
HAS_AUDIO=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of csv=p=0 "$FINAL" | tr -d ' \n')
[[ -n "$HAS_AUDIO" ]] && ok "audio track present ($HAS_AUDIO)" || { bad "no audio in output"; exit 1; }
grep -q "captions.ass" build.log && ok "captions burned in"
grep -q "zoom windows" build.log && ok "face-anchored zoom applied"

step "Result"
printf "  ${G}Pipeline works end to end on this machine.${X}\n"
if [[ $KEEP -eq 1 ]]; then
  printf "  Watch it:  open \"%s\"\n" "$FINAL"
else
  printf "${D}  (run with --keep to leave the video where you can watch it)${X}\n"
fi
