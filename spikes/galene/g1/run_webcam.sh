#!/bin/bash
# G1 option 1 latency run: Brio webcam + mic -> FFmpeg (x264 + Opus) -> RTP -> whippub -> Galene.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
URL=${URL:-http://127.0.0.1:8443/group/g1/.whip}
CAM=${CAM:-/dev/video0}
MIC=${MIC:-alsa_input.usb-046d_Brio_105_2445ZBE4ARA8-02.mono-fallback}

"$DIR/whippub/whippub" -url "$URL" ${TOKEN:+-token "$TOKEN"} &
PUB=$!
sleep 2
ffmpeg -hide_banner -loglevel warning -fflags nobuffer \
  -f v4l2 -input_format mjpeg -video_size 1920x1080 -framerate 30 -thread_queue_size 64 -i "$CAM" \
  -f pulse -fragment_size 1920 -thread_queue_size 64 -i "$MIC" \
  -map 0:v -vf scale=1280:720,format=yuv420p -c:v libx264 -preset veryfast -tune zerolatency \
  -profile:v baseline -bf 0 -g 60 -b:v 1700k -maxrate 1700k -bufsize 850k \
  -payload_type 102 -f rtp "rtp://127.0.0.1:5004?pkt_size=1200" \
  -map 1:a -c:a libopus -b:a 64k -ar 48000 -ac 2 -application lowdelay -frame_duration 20 \
  -payload_type 111 -f rtp "rtp://127.0.0.1:5006?pkt_size=1200" &
FF=$!
trap 'kill $FF; kill -INT $PUB; wait' INT TERM
wait $PUB
kill $FF 2>/dev/null
