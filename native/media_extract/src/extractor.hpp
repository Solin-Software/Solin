#pragma once
#include "deadline.hpp"
#include "ffmpeg_api.hpp"

namespace solin::media {
Result extract(FFmpeg& api, const Options& options, Deadline& deadline);
} // namespace solin::media
