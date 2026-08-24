#include "solin/media_engine/virtual_camera_broker_protocol.hpp"

#include <algorithm>
#include <array>
#include <cstring>
#include <stdexcept>
#include <type_traits>

namespace solin::media_engine {
namespace {

constexpr std::array<std::uint8_t, 8U> kRequestMagic{
    'S', 'L', 'N', 'B', 'R', 'K', '0', '3'};
constexpr std::array<std::uint8_t, 8U> kResponseMagic{
    'S', 'L', 'N', 'B', 'R', 'P', '0', '3'};

template <typename Value, std::size_t Size>
void write_little_endian(std::array<std::uint8_t, Size>& bytes,
                         const std::size_t offset, Value value) {
    static_assert(std::is_unsigned_v<Value>);
    for (std::size_t index = 0U; index < sizeof(Value); ++index) {
        bytes[offset + index] = static_cast<std::uint8_t>(value & 0xFFU);
        value >>= 8U;
    }
}

template <typename Value>
[[nodiscard]] Value read_little_endian(const std::span<const std::uint8_t> bytes,
                                       const std::size_t offset) {
    static_assert(std::is_unsigned_v<Value>);
    Value result{0U};
    for (std::size_t index = 0U; index < sizeof(Value); ++index) {
        result |= static_cast<Value>(bytes[offset + index]) << (index * 8U);
    }
    return result;
}

void validate_exact_size(const std::span<const std::uint8_t> bytes,
                         const std::size_t expected) {
    if (bytes.size() != expected) {
        throw std::invalid_argument("virtual_camera_broker_frame_invalid");
    }
}

[[nodiscard]] VirtualCameraBrokerStatus broker_status(const std::uint32_t value) {
    if (value > static_cast<std::uint32_t>(
                    VirtualCameraBrokerStatus::shutting_down)) {
        throw std::invalid_argument("virtual_camera_broker_frame_invalid");
    }
    return static_cast<VirtualCameraBrokerStatus>(value);
}

} // namespace

std::array<std::uint8_t, kVirtualCameraBrokerRequestSize>
encode_virtual_camera_broker_request(const VirtualCameraBrokerRequest& request) {
    std::array<std::uint8_t, kVirtualCameraBrokerRequestSize> result{};
    std::copy(kRequestMagic.begin(), kRequestMagic.end(), result.begin());
    write_little_endian(result, 8U, kVirtualCameraBrokerProtocolVersion);
    write_little_endian(result, 10U,
                        static_cast<std::uint16_t>(kVirtualCameraBrokerRequestSize));
    std::copy(request.nonce.begin(), request.nonce.end(), result.begin() + 16U);
    return result;
}

VirtualCameraBrokerRequest decode_virtual_camera_broker_request(
    const std::span<const std::uint8_t> bytes) {
    validate_exact_size(bytes, kVirtualCameraBrokerRequestSize);
    if (!std::equal(kRequestMagic.begin(), kRequestMagic.end(), bytes.begin()) ||
        read_little_endian<std::uint16_t>(bytes, 8U) !=
            kVirtualCameraBrokerProtocolVersion ||
        read_little_endian<std::uint16_t>(bytes, 10U) !=
            kVirtualCameraBrokerRequestSize ||
        read_little_endian<std::uint32_t>(bytes, 12U) != 0U) {
        throw std::invalid_argument("virtual_camera_broker_frame_invalid");
    }
    VirtualCameraBrokerRequest result{};
    std::copy_n(bytes.begin() + 16U, result.nonce.size(), result.nonce.begin());
    return result;
}

std::array<std::uint8_t, kVirtualCameraBrokerResponseSize>
encode_virtual_camera_broker_response(const VirtualCameraBrokerResponse& response) {
    if (static_cast<std::uint32_t>(response.status) >
        static_cast<std::uint32_t>(VirtualCameraBrokerStatus::shutting_down)) {
        throw std::invalid_argument("virtual_camera_broker_response_invalid");
    }
    if (response.status == VirtualCameraBrokerStatus::ok &&
        (response.mapping_file_path_utf8.empty() ||
         response.mapping_file_path_utf8.size() >
             kVirtualCameraBrokerMaximumPathBytes ||
         response.mapping_file_path_utf8.find('\0') != std::string::npos ||
         response.mapping_size == 0U ||
         response.generation == 0U ||
         response.fps_numerator == 0U || response.fps_denominator == 0U ||
         response.fps_denominator > 1'001U ||
         response.fps_numerator > 240U * response.fps_denominator ||
         packed_video_frame_layout(response.layout.width, response.layout.height,
                                   response.layout.pixel_format) != response.layout)) {
        throw std::invalid_argument("virtual_camera_broker_response_invalid");
    }
    if (response.status != VirtualCameraBrokerStatus::ok &&
        (!response.mapping_file_path_utf8.empty() || response.mapping_size != 0U ||
         response.generation != 0U ||
         response.layout != PackedVideoFrameLayout{} ||
         response.fps_numerator != 0U || response.fps_denominator != 0U)) {
        throw std::invalid_argument("virtual_camera_broker_response_invalid");
    }
    std::array<std::uint8_t, kVirtualCameraBrokerResponseSize> result{};
    std::copy(kResponseMagic.begin(), kResponseMagic.end(), result.begin());
    write_little_endian(result, 8U, kVirtualCameraBrokerProtocolVersion);
    write_little_endian(result, 10U,
                        static_cast<std::uint16_t>(kVirtualCameraBrokerResponseSize));
    write_little_endian(result, 12U,
                        static_cast<std::uint32_t>(response.status));
    std::copy(response.nonce.begin(), response.nonce.end(), result.begin() + 16U);
    if (response.status != VirtualCameraBrokerStatus::ok) {
        return result;
    }
    write_little_endian(result, 32U, response.mapping_size);
    write_little_endian(result, 40U, response.generation);
    write_little_endian(result, 48U, response.layout.payload_size);
    write_little_endian(result, 56U, response.layout.width);
    write_little_endian(result, 60U, response.layout.height);
    write_little_endian(result, 64U,
                        static_cast<std::uint32_t>(response.layout.pixel_format));
    write_little_endian(result, 68U, response.layout.plane_count);
    write_little_endian(result, 72U, response.layout.plane_strides[0]);
    write_little_endian(result, 76U, response.layout.plane_strides[1]);
    write_little_endian(result, 80U, response.layout.plane_offsets[1]);
    write_little_endian(result, 88U, response.fps_numerator);
    write_little_endian(result, 92U, response.fps_denominator);
    write_little_endian(
        result, 96U,
        static_cast<std::uint16_t>(response.mapping_file_path_utf8.size()));
    std::copy(response.mapping_file_path_utf8.begin(),
              response.mapping_file_path_utf8.end(), result.begin() + 128U);
    return result;
}

VirtualCameraBrokerResponse decode_virtual_camera_broker_response(
    const std::span<const std::uint8_t> bytes) {
    validate_exact_size(bytes, kVirtualCameraBrokerResponseSize);
    if (!std::equal(kResponseMagic.begin(), kResponseMagic.end(), bytes.begin()) ||
        read_little_endian<std::uint16_t>(bytes, 8U) !=
            kVirtualCameraBrokerProtocolVersion ||
        read_little_endian<std::uint16_t>(bytes, 10U) !=
            kVirtualCameraBrokerResponseSize) {
        throw std::invalid_argument("virtual_camera_broker_frame_invalid");
    }
    VirtualCameraBrokerResponse result{
        .status = broker_status(read_little_endian<std::uint32_t>(bytes, 12U)),
        .mapping_size = read_little_endian<std::uint64_t>(bytes, 32U),
        .generation = read_little_endian<std::uint64_t>(bytes, 40U),
        .fps_numerator = read_little_endian<std::uint32_t>(bytes, 88U),
        .fps_denominator = read_little_endian<std::uint32_t>(bytes, 92U),
    };
    std::copy_n(bytes.begin() + 16U, result.nonce.size(), result.nonce.begin());
    if (result.status != VirtualCameraBrokerStatus::ok) {
        if (!std::all_of(bytes.begin() + 32U, bytes.end(),
                         [](const std::uint8_t value) { return value == 0U; })) {
            throw std::invalid_argument("virtual_camera_broker_response_invalid");
        }
        return result;
    }
    const auto path_length = read_little_endian<std::uint16_t>(bytes, 96U);
    if (path_length == 0U || path_length > kVirtualCameraBrokerMaximumPathBytes ||
        !std::all_of(bytes.begin() + 98U, bytes.begin() + 128U,
                     [](const std::uint8_t value) { return value == 0U; })) {
        throw std::invalid_argument("virtual_camera_broker_response_invalid");
    }
    result.mapping_file_path_utf8.assign(
        reinterpret_cast<const char*>(bytes.data() + 128U), path_length);
    if (result.mapping_file_path_utf8.find('\0') != std::string::npos) {
        throw std::invalid_argument("virtual_camera_broker_response_invalid");
    }
    if (!std::all_of(bytes.begin() + 128U + path_length, bytes.end(),
                     [](const std::uint8_t value) { return value == 0U; })) {
        throw std::invalid_argument("virtual_camera_broker_response_invalid");
    }
    const auto raw_format = read_little_endian<std::uint32_t>(bytes, 64U);
    if (raw_format < static_cast<std::uint32_t>(VideoFramePixelFormat::bgra) ||
        raw_format > static_cast<std::uint32_t>(VideoFramePixelFormat::yuy2)) {
        throw std::invalid_argument("virtual_camera_broker_response_invalid");
    }
    result.layout = packed_video_frame_layout(
        read_little_endian<std::uint32_t>(bytes, 56U),
        read_little_endian<std::uint32_t>(bytes, 60U),
        static_cast<VideoFramePixelFormat>(raw_format));
    if (result.mapping_size == 0U ||
        result.generation == 0U ||
        result.fps_numerator == 0U || result.fps_denominator == 0U ||
        result.fps_denominator > 1'001U ||
        result.fps_numerator > 240U * result.fps_denominator ||
        read_little_endian<std::uint64_t>(bytes, 48U) !=
            result.layout.payload_size ||
        read_little_endian<std::uint32_t>(bytes, 68U) !=
            result.layout.plane_count ||
        read_little_endian<std::uint32_t>(bytes, 72U) !=
            result.layout.plane_strides[0] ||
        read_little_endian<std::uint32_t>(bytes, 76U) !=
            result.layout.plane_strides[1] ||
        read_little_endian<std::uint64_t>(bytes, 80U) !=
            result.layout.plane_offsets[1]) {
        throw std::invalid_argument("virtual_camera_broker_response_invalid");
    }
    return result;
}

} // namespace solin::media_engine
