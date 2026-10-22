#!/bin/bash
# G1 sanity check: FFmpeg (x264 + Opus) -> RTP -> whippub -> Galene WHIP.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
CLIP=${CLIP:-$DIR/../../hosted-meetings/s1_publish/media/testclip.mp4}
URL=${URL:-http://127.0.0.1:8443/group/g1/.whip}

"$DIR/whippub/whippub" -url "$URL" ${TOKEN:+-token "$TOKEN"} &
PUB=$!
sleep 2
ffmpeg -hide_banner -loglevel warning -re -stream_loop -1 -i "$CLIP" \
  -map 0:v -vf scale=1280:720 -r 30 -c:v libx264 -preset veryfast -tune zerolatency \
  -profile:v baseline -bf 0 -g 60 -b:v 1700k -maxrate 1700k -bufsize 850k \
  -payload_type 102 -f rtp "rtp://127.0.0.1:5004?pkt_size=1200" \
  -map 0:a -c:a libopus -b:a 64k -ar 48000 -ac 2 -application voip \
  -payload_type 111 -f rtp "rtp://127.0.0.1:5006?pkt_size=1200" &
FF=$!
trap 'kill $FF; kill -INT $PUB; wait' INT TERM
wait $PUB
kill $FF 2>/dev/null
