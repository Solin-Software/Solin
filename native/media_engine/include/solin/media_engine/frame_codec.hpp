#pragma once

#include <cstddef>
#include <cstdint>
#include <iosfwd>
#include <string>
#include <string_view>
#include <vector>

namespace solin::media_engine {

inline constexpr std::size_t kMaximumControlFrameBytes = 8U * 1024U * 1024U;

enum class FrameReadStatus {
    ok,
    clean_eof,
    truncated,
    oversized,
    io_error,
};

struct FrameReadResult {
    FrameReadStatus status{FrameReadStatus::io_error};
    std::string payload{};
};

[[nodiscard]] std::vector<std::uint8_t> encode_frame(std::string_view payload);

[[nodiscard]] FrameReadResult
read_frame(std::istream& input, std::size_t maximum_payload_bytes = kMaximumControlFrameBytes);

[[nodiscard]] bool write_frame(std::ostream& output, std::string_view payload,
                               std::size_t maximum_payload_bytes = kMaximumControlFrameBytes);

} // namespace solin::media_engine
