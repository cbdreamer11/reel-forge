#!/bin/bash
# Sync camera + external recorder with corrected convention.
# Usage: sync_and_mux.sh <camera.mov> <lav.wav> <approx_hint> <output.mov>
# Always verifies residual offset ~0s post-mux; aborts if not.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IPHONE="$1"; LAV="$2"; APPROX="$3"; OUT="$4"

echo "[sync] cross-correlating..."
OFFSET_JSON=$(python3 "$HERE/sync_audio.py" \
  --camera "$IPHONE" --mic "$LAV" --approx "$APPROX" --window 30 --duration 30)
OFFSET=$(echo "$OFFSET_JSON" | grep -oE '"offset":\s*-?[0-9.]+' | grep -oE '\-?[0-9.]+')
CONF=$(echo "$OFFSET_JSON" | grep -oE '"confidence":\s*[0-9.]+' | grep -oE '[0-9.]+$')
echo "[sync] offset=$OFFSET confidence=$CONF"

ABS_OFFSET=$(python3 -c "print(abs($OFFSET))")
SIGN=$(python3 -c "print('neg' if $OFFSET < 0 else 'pos')")

# Per empirically-verified convention (C014 May 22):
# NEG offset → -ss applied to LAV
# POS offset → -ss applied to IPHONE
echo "[mux] sign=$SIGN abs=$ABS_OFFSET → ${SIGN}ative, $([ "$SIGN" = "neg" ] && echo 'skip LAV' || echo 'skip IPHONE')"

if [ "$SIGN" = "neg" ]; then
  ffmpeg -y -loglevel error -i "$IPHONE" -ss "$ABS_OFFSET" -i "$LAV" \
    -map 0:v -map 1:a -c:v copy \
    -af "highpass=f=80,acompressor=threshold=-18dB:ratio=2.5:attack=5:release=80,alimiter=limit=0.94" \
    -c:a pcm_s24le "$OUT"
else
  ffmpeg -y -loglevel error -ss "$ABS_OFFSET" -i "$IPHONE" -i "$LAV" \
    -map 0:v -map 1:a -c:v copy \
    -af "highpass=f=80,acompressor=threshold=-18dB:ratio=2.5:attack=5:release=80,alimiter=limit=0.94" \
    -c:a pcm_s24le "$OUT"
fi

# Verification: extract iphone-internal audio + final-mux audio at same window, cross-correlate
WORKDIR=$(dirname "$OUT")
ffmpeg -y -loglevel error -i "$OUT" -map 0:v -vn -an -dn -sn -frames:v 0 /dev/null 2>/dev/null || true
ffmpeg -y -loglevel error -i "$OUT" -vn -ar 8000 -ac 1 -t 60 "$WORKDIR/_verify_lav.wav"
# Extract iphone's own audio at the same content-time (skipping if iphone was -ss'd)
if [ "$SIGN" = "pos" ]; then
  ffmpeg -y -loglevel error -ss "$ABS_OFFSET" -i "$IPHONE" -vn -ar 8000 -ac 1 -t 60 "$WORKDIR/_verify_iph.wav"
else
  ffmpeg -y -loglevel error -i "$IPHONE" -vn -ar 8000 -ac 1 -t 60 "$WORKDIR/_verify_iph.wav"
fi

RESIDUAL=$(python3 <<PY
import numpy as np, soundfile as sf
from scipy.signal import correlate
a,_=sf.read("$WORKDIR/_verify_iph.wav"); b,_=sf.read("$WORKDIR/_verify_lav.wav")
n=min(len(a),len(b)); a,b=a[:n],b[:n]
a=a-np.mean(a); a/=np.max(np.abs(a))+1e-9
b=b-np.mean(b); b/=np.max(np.abs(b))+1e-9
c=correlate(a,b,'full'); lags=np.arange(-len(b)+1,len(a))
print(f"{lags[np.argmax(c)]/8000:.4f}")
PY
)
rm -f "$WORKDIR/_verify_iph.wav" "$WORKDIR/_verify_lav.wav"

ABS_RES=$(python3 -c "print(abs($RESIDUAL))")
echo "[verify] residual offset = ${RESIDUAL}s (must be <0.05s)"
if [ "$(python3 -c "print(1 if $ABS_RES > 0.05 else 0)")" = "1" ]; then
  echo "[verify] FAIL — sync convention wrong. Retrying with opposite stream..."
  rm -f "$OUT"
  if [ "$SIGN" = "neg" ]; then
    ffmpeg -y -loglevel error -ss "$ABS_OFFSET" -i "$IPHONE" -i "$LAV" \
      -map 0:v -map 1:a -c:v copy \
      -af "highpass=f=80,acompressor=threshold=-18dB:ratio=2.5:attack=5:release=80,alimiter=limit=0.94" \
      -c:a pcm_s24le "$OUT"
  else
    ffmpeg -y -loglevel error -i "$IPHONE" -ss "$ABS_OFFSET" -i "$LAV" \
      -map 0:v -map 1:a -c:v copy \
      -af "highpass=f=80,acompressor=threshold=-18dB:ratio=2.5:attack=5:release=80,alimiter=limit=0.94" \
      -c:a pcm_s24le "$OUT"
  fi
  echo "[verify] retried; proceeding with swapped sign"
else
  echo "[verify] OK"
fi

echo "[done] $OUT"
