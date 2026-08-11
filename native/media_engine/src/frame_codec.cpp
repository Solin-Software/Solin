#include "solin/media_engine/frame_codec.hpp"

#include <array>
#include <istream>
#include <limits>
#include <ostream>
#include <stdexcept>
#include <utility>

namespace solin::media_engine {
namespace {

constexpr std::size_t kHeaderBytes = 4U;

[[nodiscard]] std::uint32_t
decode_size(const std::array<std::uint8_t, kHeaderBytes>& header) noexcept {
    return (static_cast<std::uint32_t>(header[0]) << 24U) |
           (static_cast<std::uint32_t>(header[1]) << 16U) |
           (static_cast<std::uint32_t>(header[2]) << 8U) | static_cast<std::uint32_t>(header[3]);
}

[[nodiscard]] std::array<std::uint8_t, kHeaderBytes>
encode_size(const std::uint32_t size) noexcept {
    return {
        static_cast<std::uint8_t>((size >> 24U) & 0xFFU),
        static_cast<std::uint8_t>((size >> 16U) & 0xFFU),
        static_cast<std::uint8_t>((size >> 8U) & 0xFFU),
        static_cast<std::uint8_t>(size & 0xFFU),
    };
}

} // namespace

std::vector<std::uint8_t> encode_frame(const std::string_view payload) {
    if (payload.size() > kMaximumControlFrameBytes ||
        payload.size() > std::numeric_limits<std::uint32_t>::max()) {
        throw std::length_error("control frame exceeds the protocol limit");
    }
    const auto header = encode_size(static_cast<std::uint32_t>(payload.size()));
    std::vector<std::uint8_t> frame;
    frame.reserve(header.size() + payload.size());
    frame.insert(frame.end(), header.begin(), header.end());
    frame.insert(frame.end(), payload.begin(), payload.end());
    return frame;
}

FrameReadResult read_frame(std::istream& input, const std::size_t maximum_payload_bytes) {
    std::array<std::uint8_t, kHeaderBytes> header{};
    input.read(reinterpret_cast<char*>(header.data()), static_cast<std::streamsize>(header.size()));
    const auto header_bytes = input.gcount();
    if (header_bytes == 0 && input.eof()) {
        return {.status = FrameReadStatus::clean_eof};
    }
    if (header_bytes != static_cast<std::streamsize>(header.size())) {
        return {
            .status = input.bad() ? FrameReadStatus::io_error : FrameReadStatus::truncated,
        };
    }

    const auto payload_size = static_cast<std::size_t>(decode_size(header));
    if (payload_size > maximum_payload_bytes) {
        return {.status = FrameReadStatus::oversized};
    }
    std::string payload(payload_size, '\0');
    if (payload_size != 0U) {
        input.read(payload.data(), static_cast<std::streamsize>(payload.size()));
        if (input.gcount() != static_cast<std::streamsize>(payload.size())) {
            return {
                .status = input.bad() ? FrameReadStatus::io_error : FrameReadStatus::truncated,
            };
        }
    }
    return {.status = FrameReadStatus::ok, .payload = std::move(payload)};
}

bool write_frame(std::ostream& output, const std::string_view payload,
                 const std::size_t maximum_payload_bytes) {
    if (payload.size() > maximum_payload_bytes ||
        payload.size() > std::numeric_limits<std::uint32_t>::max()) {
        return false;
    }
    const auto header = encode_size(static_cast<std::uint32_t>(payload.size()));
    output.write(reinterpret_cast<const char*>(header.data()),
                 static_cast<std::streamsize>(header.size()));
    output.write(payload.data(), static_cast<std::streamsize>(payload.size()));
    output.flush();
    return output.good();
}

} // namespace solin::media_engine
