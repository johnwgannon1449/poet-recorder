#!/bin/bash
# Zip ./data/ (database + audio) with a date stamp into the folder you name.
#   ./backup.sh /Volumes/Backups/poet
set -e
DEST="${1:?usage: ./backup.sh <destination folder>}"
mkdir -p "$DEST"
cd "$(dirname "$0")"
OUT="$DEST/poet-recorder-$(date +%Y%m%d-%H%M).zip"
zip -rq "$OUT" data
echo "Backed up data/ to $OUT ($(du -h "$OUT" | cut -f1))"
