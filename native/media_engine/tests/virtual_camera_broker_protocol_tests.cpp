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
    for (std::size_t index = 0U; index < request.nonce.size(); ++index) {
        request.nonce[index] = static_cast<std::uint8_t>(0xA0U + index);
    }
    const auto encoded =
        solin::media_engine::encode_virtual_camera_broker_request(request);
    expect(solin::media_engine::decode_virtual_camera_broker_request(encoded) == request,
           "the broker request preserves its nonce");
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
    corrupted = encoded;
    corrupted[12U] = 1U;
    try {
        static_cast<void>(
            solin::media_engine::decode_virtual_camera_broker_request(corrupted));
        expect(false, "non-zero request reserved bytes should be rejected");
    } catch (const std::invalid_argument&) {
        expect(true, "request reserved bytes are validated");
    }
    try {
        static_cast<void>(solin::media_engine::decode_virtual_camera_broker_request(
            std::span<const std::uint8_t>{encoded}.first(encoded.size() - 1U)));
        expect(false, "a truncated broker request should be rejected");
    } catch (const std::invalid_argument&) {
        expect(true, "request length is pointer-size-independent and strict");
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
    auto corrupted = encoded;
    corrupted[8U] = 4U;
    try {
        static_cast<void>(
            solin::media_engine::decode_virtual_camera_broker_response(corrupted));
        expect(false, "a future response protocol version should be rejected");
    } catch (const std::invalid_argument&) {
        expect(true, "response versions are decoded strictly");
    }
    corrupted = encoded;
    corrupted[100U] = 1U;
    try {
        static_cast<void>(
            solin::media_engine::decode_virtual_camera_broker_response(corrupted));
        expect(false, "non-zero response reserved bytes should be rejected");
    } catch (const std::invalid_argument&) {
        expect(true, "response reserved bytes are validated");
    }
    try {
        static_cast<void>(solin::media_engine::decode_virtual_camera_broker_response(
            std::span<const std::uint8_t>{encoded}.first(encoded.size() - 1U)));
        expect(false, "a truncated broker response should be rejected");
    } catch (const std::invalid_argument&) {
        expect(true, "response length is strict across pointer sizes");
    }
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
    auto corrupted =
        solin::media_engine::encode_virtual_camera_broker_response(response);
    corrupted[32U] = 1U;
    try {
        static_cast<void>(
            solin::media_engine::decode_virtual_camera_broker_response(corrupted));
        expect(false, "denial responses cannot carry hidden metadata");
    } catch (const std::invalid_argument&) {
        expect(true, "denial metadata is rejected at the protocol boundary");
    }
}

} // namespace

int main() {
    const auto run = [](const char* const name, const auto& test) {
        try {
            test();
        } catch (const std::exception& error) {
            std::cerr << "FAILED: unexpected " << name
                      << " exception: " << error.what() << '\n';
            ++failures;
        }
    };
    run("request", test_request_round_trip_and_strict_boundary);
    run("success response", test_success_response_round_trip);
    run("denial response", test_denial_response_contains_no_transport_metadata);
    return failures == 0 ? 0 : 1;
}
