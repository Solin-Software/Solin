#pragma once

#include "solin/media_engine/video_frame.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string>

namespace solin::media_engine {

inline constexpr std::uint16_t kVirtualCameraBrokerProtocolVersion = 2U;
inline constexpr std::size_t kVirtualCameraBrokerTokenSize = 32U;
inline constexpr std::size_t kVirtualCameraBrokerNonceSize = 16U;
inline constexpr std::size_t kVirtualCameraBrokerRequestSize = 64U;
inline constexpr std::size_t kVirtualCameraBrokerResponseSize = 640U;
inline constexpr std::size_t kVirtualCameraBrokerMaximumPathBytes = 512U;

using VirtualCameraBrokerToken =
    std::array<std::uint8_t, kVirtualCameraBrokerTokenSize>;
using VirtualCameraBrokerNonce =
    std::array<std::uint8_t, kVirtualCameraBrokerNonceSize>;

enum class VirtualCameraBrokerStatus : std::uint32_t {
    ok = 0U,
    invalid_request = 1U,
    protocol_mismatch = 2U,
    unauthorized = 3U,
    transport_unavailable = 4U,
    shutting_down = 5U,
};

struct VirtualCameraBrokerRequest final {
    VirtualCameraBrokerToken token{};
    VirtualCameraBrokerNonce nonce{};

    bool operator==(const VirtualCameraBrokerRequest&) const = default;
};

struct VirtualCameraBrokerResponse final {
    VirtualCameraBrokerStatus status{VirtualCameraBrokerStatus::invalid_request};
    VirtualCameraBrokerNonce nonce{};
    std::string mapping_file_path_utf8{};
    std::uint64_t mapping_size{0U};
    std::uint64_t generation{0U};
    PackedVideoFrameLayout layout{};
    std::uint32_t fps_numerator{0U};
    std::uint32_t fps_denominator{0U};

    bool operator==(const VirtualCameraBrokerResponse&) const = default;
};

[[nodiscard]] std::array<std::uint8_t, kVirtualCameraBrokerRequestSize>
encode_virtual_camera_broker_request(const VirtualCameraBrokerRequest& request);
[[nodiscard]] VirtualCameraBrokerRequest
decode_virtual_camera_broker_request(std::span<const std::uint8_t> bytes);
[[nodiscard]] std::array<std::uint8_t, kVirtualCameraBrokerResponseSize>
encode_virtual_camera_broker_response(const VirtualCameraBrokerResponse& response);
[[nodiscard]] VirtualCameraBrokerResponse
decode_virtual_camera_broker_response(std::span<const std::uint8_t> bytes);

[[nodiscard]] bool constant_time_token_equal(
    const VirtualCameraBrokerToken& left,
    const VirtualCameraBrokerToken& right) noexcept;

} // namespace solin::media_engine
