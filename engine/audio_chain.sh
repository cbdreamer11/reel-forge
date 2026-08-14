#!/usr/bin/env bash
# audio_chain.sh — Apply the locked vocal cleanup chain.
#
# Usage:
#   ./audio_chain.sh <input> <output.wav>
#
# Chain (no loudnorm):
#   highpass=80
#   acompressor threshold=-18dB ratio=2.5 attack=5 release=80
#   alimiter limit=0.94
# Output: 24-bit PCM WAV (mastering-friendly intermediate).

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <input> <output.wav>" >&2
  exit 1
fi

INPUT="$1"
OUTPUT="$2"

ffmpeg -y -i "$INPUT" \
  -af "highpass=f=80,acompressor=threshold=-18dB:ratio=2.5:attack=5:release=80,alimiter=limit=0.94" \
  -c:a pcm_s24le "$OUTPUT"

echo "wrote $OUTPUT"
