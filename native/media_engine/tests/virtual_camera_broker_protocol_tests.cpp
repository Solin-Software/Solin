#include "solin/media_engine/virtual_camera_broker_protocol.hpp"

#include <array>
#include <iostream>
#include <stdexcept>
#include <string_view>

namespace {

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (!condition) {
        ++failures;
        std::cerr << "FAILED: " << description << '\n';
    }
}

void test_request_round_trip_and_strict_boundary() {
    solin::media_engine::VirtualCameraBrokerRequest request{};
    for (std::size_t index = 0U; index < request.token.size(); ++index) {
        request.token[index] = static_cast<std::uint8_t>(index + 1U);
    }
    for (std::size_t index = 0U; index < request.nonce.size(); ++index) {
        request.nonce[index] = static_cast<std::uint8_t>(0xA0U + index);
    }
    const auto encoded =
        solin::media_engine::encode_virtual_camera_broker_request(request);
    expect(solin::media_engine::decode_virtual_camera_broker_request(encoded) == request,
           "the broker request preserves its token and nonce");
    auto corrupted = encoded;
    corrupted[8U] = 0xFFU;
    try {
        static_cast<void>(
            solin::media_engine::decode_virtual_camera_broker_request(corrupted));
        expect(false, "an unknown broker version should be rejected");
    } catch (const std::invalid_argument& error) {
        expect(std::string_view{error.what()} ==
                   "virtual_camera_broker_frame_invalid",
               "invalid requests have a stable boundary error");
    }
}

void test_success_response_round_trip() {
    const auto layout = solin::media_engine::packed_video_frame_layout(
        1920U, 1080U, solin::media_engine::VideoFramePixelFormat::nv12);
    solin::media_engine::VirtualCameraBrokerResponse response{
        .status = solin::media_engine::VirtualCameraBrokerStatus::ok,
        .mapping_file_path_utf8 = "C:\\Temp\\Solin.VirtualCamera.frames",
        .mapping_size = 9'999U,
        .generation = 7U,
        .layout = layout,
        .fps_numerator = 30U,
        .fps_denominator = 1U,
    };
    response.nonce.fill(0x44U);
    const auto encoded =
        solin::media_engine::encode_virtual_camera_broker_response(response);
    expect(solin::media_engine::decode_virtual_camera_broker_response(encoded) ==
               response,
           "the broker response preserves its path, layout, generation, and nonce");
}

void test_denial_response_contains_no_transport_metadata() {
    solin::media_engine::VirtualCameraBrokerResponse response{
        .status = solin::media_engine::VirtualCameraBrokerStatus::unauthorized,
    };
    response.nonce.fill(0x55U);
    const auto decoded = solin::media_engine::decode_virtual_camera_broker_response(
        solin::media_engine::encode_virtual_camera_broker_response(response));
    expect(decoded == response && decoded.mapping_file_path_utf8.empty() &&
               decoded.mapping_size == 0U,
           "a denied handshake discloses no frame transport path");
}

void test_token_comparison_checks_every_byte() {
    solin::media_engine::VirtualCameraBrokerToken token{};
    token.fill(0xAAU);
    auto different = token;
    different.back() ^= 0x01U;
    expect(solin::media_engine::constant_time_token_equal(token, token) &&
               !solin::media_engine::constant_time_token_equal(token, different),
           "broker authentication compares the complete token");
}

} // namespace

int main() {
    test_request_round_trip_and_strict_boundary();
    test_success_response_round_trip();
    test_denial_response_contains_no_transport_metadata();
    test_token_comparison_checks_every_byte();
    return failures == 0 ? 0 : 1;
}
