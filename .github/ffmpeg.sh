#!/usr/bin/env bash
# ffmpeg for the GitHub runner: a static build (pinned, cached in ~/ffmpeg-static by actions/cache),
# falling back to the newest 9.0 build and finally to apt (slow mirror: 5-15 min).
set -u
D="$HOME/ffmpeg-static"
URLS=(
  "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-11-17-12-40/ffmpeg-n9.0.2-57-gd838f9f9ae-linux64-gpl-9.0.tar.xz"
  "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n9.0-latest-linux64-gpl-9.0.tar.xz"
)
if [ ! -x "$D/ffmpeg" ]; then
  for u in "${URLS[@]}"; do
    rm -rf /tmp/ffx && mkdir -p /tmp/ffx
    if curl -fsSL --retry 3 --max-time 240 "$u" -o /tmp/ff.tar.xz && tar -xJf /tmp/ff.tar.xz -C /tmp/ffx; then
      b=$(dirname "$(find /tmp/ffx -type f -name ffmpeg -path '*/bin/*' | head -1)")
      if [ -x "$b/ffmpeg" ] && [ -x "$b/ffprobe" ]; then
        mkdir -p "$D" && cp "$b/ffmpeg" "$b/ffprobe" "$D/" && echo "static ffmpeg from $u" && break
      fi
    fi
    echo "::warning::static ffmpeg download failed: $u"
  done
fi
if [ -x "$D/ffmpeg" ] && "$D/ffmpeg" -hide_banner -version >/dev/null 2>&1; then
  echo "$D" >> "$GITHUB_PATH"
  "$D/ffmpeg" -hide_banner -version | head -1
else
  rm -rf "$D"                      # never cache a broken directory
  echo "::warning::static ffmpeg unavailable, installing from apt"
  sudo apt-get update -q && sudo apt-get install -y -q --no-install-recommends ffmpeg
fi
