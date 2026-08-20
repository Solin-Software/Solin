#include "solin/media_engine/control_protocol.hpp"
#include "solin/media_engine/frame_codec.hpp"

#include <cstdint>
#include <iostream>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

namespace {

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (condition) {
        return;
    }
    ++failures;
    std::cerr << "FAILED: " << description << '\n';
}

void test_big_endian_encoding() {
    const auto frame = solin::media_engine::encode_frame("abc");
    const std::vector<std::uint8_t> expected{0U, 0U, 0U, 3U, 'a', 'b', 'c'};
    expect(frame == expected, "frame length is encoded as big-endian uint32");
}

void test_round_trip() {
    std::stringstream stream;
    expect(solin::media_engine::write_frame(stream, "hello"), "frame write succeeds");
    const auto result = solin::media_engine::read_frame(stream);
    expect(result.status == solin::media_engine::FrameReadStatus::ok, "frame read succeeds");
    expect(result.payload == "hello", "frame payload round-trips");
}

void test_truncated_payload() {
    const std::string bytes{"\0\0\0\x05"
                            "abc",
                            7U};
    std::stringstream stream{bytes};
    const auto result = solin::media_engine::read_frame(stream);
    expect(result.status == solin::media_engine::FrameReadStatus::truncated,
           "truncated payload is rejected");
}

void test_oversized_header_does_not_allocate() {
    const std::string bytes{"\0\0\x10\0", 4U};
    std::stringstream stream{bytes};
    const auto result = solin::media_engine::read_frame(stream, 128U);
    expect(result.status == solin::media_engine::FrameReadStatus::oversized,
           "oversized payload is rejected before allocation");
}

void test_clean_eof() {
    std::stringstream stream;
    const auto result = solin::media_engine::read_frame(stream);
    expect(result.status == solin::media_engine::FrameReadStatus::clean_eof,
           "empty stream is a clean EOF");
}

solin::media_engine::ControlEnvelope hello_envelope() {
    return {
        .message_type = "hello",
        .request_id = "request-1",
        .session_id = "session-1",
        .process_generation = "generation-1",
        .sequence = 0U,
        .document_revision = 0U,
        .deadline_monotonic_ms =
            static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()),
        .payload = {{"parent_process_id", 123U}},
    };
}

void test_control_envelope_round_trip() {
    const auto expected = hello_envelope();
    const auto restored = solin::media_engine::parse_control_envelope(
        solin::media_engine::serialize_control_envelope(expected));
    expect(restored.message_type == expected.message_type, "control message type round-trips");
    expect(restored.request_id == expected.request_id, "control request id round-trips");
    expect(restored.payload == expected.payload, "control payload round-trips");
}

void test_control_envelope_rejects_duplicate_keys() {
    constexpr std::string_view duplicate =
        R"({"protocol_version":3,"protocol_version":3,"message_type":"hello","request_id":"r","session_id":"s","process_generation":"g","sequence":0,"document_revision":0,"deadline_monotonic_ms":1,"payload":{}})";
    try {
        static_cast<void>(solin::media_engine::parse_control_envelope(duplicate));
        expect(false, "duplicate JSON keys are rejected");
    } catch (const std::exception&) {
        expect(true, "duplicate JSON keys are rejected");
    }
}

void test_control_envelope_rejects_the_previous_protocol_generation() {
    constexpr std::string_view previous_generation =
        R"({"protocol_version":2,"message_type":"hello","request_id":"r","session_id":"s","process_generation":"g","sequence":0,"document_revision":0,"deadline_monotonic_ms":1,"payload":{}})";
    try {
        static_cast<void>(solin::media_engine::parse_control_envelope(previous_generation));
        expect(false, "the previous control protocol generation is rejected");
    } catch (const std::exception&) {
        expect(true, "the previous control protocol generation is rejected");
    }
}

void test_control_session_handshake_and_heartbeat() {
    solin::media_engine::ControlSession session{"generation-1"};
    const auto hello = session.handle(hello_envelope());
    expect(hello.response.has_value(), "hello produces a response");
    expect(hello.response.has_value() && hello.response->message_type == "hello_ack",
           "hello produces capability acknowledgement");

    auto ping = hello_envelope();
    ping.message_type = "ping";
    ping.request_id = "request-2";
    ping.sequence = 1U;
    ping.payload = nlohmann::json::object();
    const auto heartbeat = session.handle(ping);
    expect(heartbeat.response.has_value() && heartbeat.response->message_type == "heartbeat",
           "ping produces a heartbeat");

    auto list_cameras = hello_envelope();
    list_cameras.message_type = "list_local_cameras";
    list_cameras.request_id = "request-cameras";
    list_cameras.sequence = 2U;
    list_cameras.payload = nlohmann::json::object();
    const auto cameras = session.handle(list_cameras);
    expect(cameras.response.has_value() &&
               cameras.response->message_type == "local_camera_list",
           "camera discovery produces a bounded response");
    expect(cameras.response.has_value() &&
               cameras.response->payload.at("error_code") == "media_runtime_unavailable",
           "camera discovery reports an unavailable media runtime explicitly");

    auto hydrate = hello_envelope();
    hydrate.message_type = "hydrate";
    hydrate.request_id = "request-hydrate";
    hydrate.sequence = 3U;
    hydrate.document_revision = 7U;
    hydrate.payload = {
        {"document", nlohmann::json::object()},
        {"active_scenes", nlohmann::json::object()},
        {"render_enabled", nlohmann::json::object()},
        {"output_enabled", nlohmann::json::object()},
        {"content_ingress", nullptr},
        {"preview_egress", nullptr},
        {"program_egress", nullptr},
        {"window_targets", nlohmann::json::array()},
    };
    const auto hydrated = session.handle(hydrate);
    expect(hydrated.response.has_value() && hydrated.response->message_type == "ack",
           "hydration produces an acknowledgement");
    expect(hydrated.response.has_value() && !hydrated.response->payload.at("applied").get<bool>(),
           "the unavailable graph does not claim a snapshot was applied");

    auto prepare = hello_envelope();
    prepare.message_type = "prepare_scene";
    prepare.request_id = "request-3";
    prepare.sequence = 4U;
    prepare.payload = {
        {"bus_id", "media_windows"},
        {"scene_id", "scene-1"},
        {"transition", {{"kind", "cut"}, {"duration_ms", 0U}}},
    };
    const auto rejected = session.handle(prepare);
    expect(rejected.response.has_value() && rejected.response->message_type == "error",
           "unavailable media graph rejects preparation without breaking the protocol");
}

void test_control_session_dispatches_validated_graph_commands() {
    bool hydrated = false;
    bool prepared = false;
    bool taken = false;
    bool cancelled = false;
    bool geometry_previewed = false;
    bool output_changed = false;
    bool render_changed = false;
    bool window_targets_changed = false;
    solin::media_engine::ControlSession session{
        "generation-1",
        {
            .capabilities = [] {
                return nlohmann::json{
                    {"local_cameras", true},
                    {"rtsp_cameras", true},
                    {"hardware_compositing", false},
                    {"virtual_camera", false},
                    {"d3d11_shared_textures", false},
                };
            },
            .hydrate = [&hydrated](const nlohmann::json&, const std::uint64_t revision,
                                   const std::uint64_t sequence) {
                hydrated = revision == 7U && sequence == 1U;
                return nlohmann::json{
                    {"applied", true},
                    {"error_code", ""},
                    {"error_message", ""},
                };
            },
            .prepare_scene = [&prepared](
                                 const std::string_view bus,
                                 const std::string_view scene_id,
                                 const std::string_view transition,
                                 const std::uint64_t duration,
                                 const std::uint64_t revision,
                                 const std::string_view request_id,
                                 const std::uint64_t sequence) {
                prepared = bus == "virtual_camera" && scene_id == "scene-2" &&
                           transition == "dissolve" && duration == 350U &&
                           revision == 7U && request_id == "prepare-request" &&
                           sequence == 2U;
                return solin::media_engine::ControlServiceReply{
                    .message_type = "scene_prepared",
                    .payload = {
                        {"bus_id", bus},
                        {"scene_id", scene_id},
                        {"preparation_token", "preparation-1"},
                        {"transition", {{"kind", "dissolve"}, {"duration_ms", 350U}}},
                        {"fallback_applied", false},
                        {"fallback_reason", ""},
                    },
                };
            },
            .take_prepared = [&taken](
                                 const std::string_view bus,
                                 const std::string_view scene_id,
                                 const std::string_view token,
                                 const std::uint64_t revision,
                                 const std::uint64_t sequence) {
                taken = bus == "virtual_camera" && scene_id == "scene-2" &&
                        token == "preparation-1" && revision == 7U && sequence == 3U;
                return nlohmann::json{
                    {"applied", true},
                    {"error_code", ""},
                    {"error_message", ""},
                };
            },
            .cancel_preparation = [&cancelled](const std::string_view request_id) {
                cancelled = request_id == "prepare-request";
            },
            .preview_layer_geometry = [&geometry_previewed](
                                          const std::string_view bus,
                                          const std::string_view scene_id,
                                          const nlohmann::json& layer,
                                          const std::uint64_t revision,
                                          const std::uint64_t sequence) {
                geometry_previewed =
                    bus == "media_windows" && scene_id == "scene-2" &&
                    layer.at("id") == "layer-1" && revision == 7U && sequence == 4U;
                return nlohmann::json{
                    {"applied", true},
                    {"error_code", ""},
                    {"error_message", ""},
                };
            },
            .set_output_enabled = [&output_changed](
                                      const std::string_view bus, const bool enabled,
                                      const std::uint64_t revision,
                                      const std::uint64_t sequence) {
                output_changed = bus == "virtual_camera" && enabled && revision == 7U &&
                                 sequence == 5U;
                return nlohmann::json{
                    {"applied", true},
                    {"error_code", ""},
                    {"error_message", ""},
                };
            },
            .set_render_enabled = [&render_changed](
                                      const std::string_view bus, const bool enabled,
                                      const std::uint64_t revision,
                                      const std::uint64_t sequence) {
                render_changed = bus == "media_windows" && enabled && revision == 7U &&
                                 sequence == 6U;
                return nlohmann::json{
                    {"applied", true},
                    {"error_code", ""},
                    {"error_message", ""},
                };
            },
            .set_window_targets = [&window_targets_changed](
                                      const nlohmann::json& targets,
                                      const std::uint64_t revision,
                                      const std::uint64_t sequence) {
                window_targets_changed = targets.empty() && revision == 7U && sequence == 7U;
                return nlohmann::json{
                    {"applied", true},
                    {"error_code", ""},
                    {"error_message", ""},
                };
            },
        },
    };
    const auto hello = session.handle(hello_envelope());
    expect(hello.response.has_value() &&
               hello.response->payload.at("local_cameras").get<bool>(),
           "hello publishes validated runtime capabilities");

    auto hydrate = hello_envelope();
    hydrate.message_type = "hydrate";
    hydrate.request_id = "hydrate-request";
    hydrate.sequence = 1U;
    hydrate.document_revision = 7U;
    hydrate.payload = {
        {"document", nlohmann::json::object()},
        {"active_scenes", nlohmann::json::object()},
        {"render_enabled", nlohmann::json::object()},
        {"output_enabled", nlohmann::json::object()},
        {"content_ingress", nullptr},
        {"preview_egress", nullptr},
        {"program_egress", nullptr},
        {"window_targets", nlohmann::json::array()},
    };
    const auto hydrate_reply = session.handle(hydrate);
    expect(hydrated && hydrate_reply.response.has_value() &&
               hydrate_reply.response->payload.at("applied").get<bool>(),
           "hydration is dispatched with revision and sequence");

    auto prepare = hello_envelope();
    prepare.message_type = "prepare_scene";
    prepare.request_id = "prepare-request";
    prepare.sequence = 2U;
    prepare.document_revision = 7U;
    prepare.payload = {
        {"bus_id", "virtual_camera"},
        {"scene_id", "scene-2"},
        {"transition", {{"kind", "dissolve"}, {"duration_ms", 350U}}},
    };
    const auto preparation_reply = session.handle(prepare);
    expect(prepared && preparation_reply.response.has_value() &&
               preparation_reply.response->message_type == "scene_prepared",
           "scene preparation is dispatched and validated");

    auto take = hello_envelope();
    take.message_type = "take_prepared";
    take.request_id = "take-request";
    take.sequence = 3U;
    take.document_revision = 7U;
    take.payload = {
        {"bus_id", "virtual_camera"},
        {"scene_id", "scene-2"},
        {"preparation_token", "preparation-1"},
    };
    const auto take_reply = session.handle(take);
    expect(taken && take_reply.response.has_value() &&
               take_reply.response->payload.at("applied").get<bool>(),
           "Take is dispatched with the consumable preparation token");

    auto cancel = hello_envelope();
    cancel.message_type = "cancel_preparation";
    cancel.request_id = "cancel-request";
    cancel.sequence = 0U;
    cancel.document_revision = 0U;
    cancel.payload = {{"cancelled_request_id", "prepare-request"}};
    static_cast<void>(session.handle(cancel));
    expect(cancelled, "preparation cancellation is dispatched");

    auto geometry = hello_envelope();
    geometry.message_type = "preview_layer_geometry";
    geometry.request_id = "geometry-request";
    geometry.sequence = 4U;
    geometry.document_revision = 7U;
    geometry.payload = {
        {"bus_id", "media_windows"},
        {"scene_id", "scene-2"},
        {"layer", {{"id", "layer-1"}}},
    };
    const auto geometry_reply = session.handle(geometry);
    expect(geometry_previewed && geometry_reply.response.has_value() &&
               geometry_reply.response->payload.at("applied").get<bool>(),
           "preview geometry is dispatched without rebuilding the graph");

    auto output = hello_envelope();
    output.message_type = "set_output_enabled";
    output.request_id = "output-request";
    output.sequence = 5U;
    output.document_revision = 7U;
    output.payload = {{"bus_id", "virtual_camera"}, {"enabled", true}};
    const auto output_reply = session.handle(output);
    expect(output_changed && output_reply.response.has_value() &&
               output_reply.response->payload.at("applied").get<bool>(),
           "output state is dispatched with revision and sequence");

    auto render = hello_envelope();
    render.message_type = "set_render_enabled";
    render.request_id = "render-request";
    render.sequence = 6U;
    render.document_revision = 7U;
    render.payload = {{"bus_id", "media_windows"}, {"enabled", true}};
    const auto render_reply = session.handle(render);
    expect(render_changed && render_reply.response.has_value() &&
               render_reply.response->payload.at("applied").get<bool>(),
           "render state is dispatched independently from destination state");

    auto window_targets = hello_envelope();
    window_targets.message_type = "set_window_targets";
    window_targets.request_id = "window-targets-request";
    window_targets.sequence = 7U;
    window_targets.document_revision = 7U;
    window_targets.payload = {{"window_targets", nlohmann::json::array()}};
    const auto window_targets_reply = session.handle(window_targets);
    expect(window_targets_changed && window_targets_reply.response.has_value() &&
               window_targets_reply.response->payload.at("applied").get<bool>(),
           "window targets are updated without hydrating the scene graph");
}

void test_transition_contract_rejects_invalid_and_legacy_payloads() {
    solin::media_engine::ControlSession session{"generation-1"};
    static_cast<void>(session.handle(hello_envelope()));

    auto invalid = hello_envelope();
    invalid.message_type = "prepare_scene";
    invalid.request_id = "invalid-transition";
    invalid.sequence = 1U;
    invalid.document_revision = 7U;
    invalid.payload = {
        {"bus_id", "virtual_camera"},
        {"scene_id", "scene-1"},
        {"transition", {{"kind", "dissolve"}, {"duration_ms", 49U}}},
    };
    try {
        static_cast<void>(session.handle(invalid));
        expect(false, "animated transition durations below 50 ms are rejected");
    } catch (const std::exception&) {
        expect(true, "animated transition durations below 50 ms are rejected");
    }

    auto legacy_take = hello_envelope();
    legacy_take.message_type = "take_prepared";
    legacy_take.request_id = "legacy-take";
    legacy_take.sequence = 2U;
    legacy_take.document_revision = 7U;
    legacy_take.payload = {
        {"bus_id", "virtual_camera"},
        {"scene_id", "scene-1"},
        {"preparation_token", "preparation-1"},
        {"transition", "fade"},
        {"transition_duration_ms", 350U},
    };
    try {
        static_cast<void>(session.handle(legacy_take));
        expect(false, "Take cannot replace the transition bound to its token");
    } catch (const std::exception&) {
        expect(true, "Take cannot replace the transition bound to its token");
    }
}

} // namespace

int main() {
    test_big_endian_encoding();
    test_round_trip();
    test_truncated_payload();
    test_oversized_header_does_not_allocate();
    test_clean_eof();
    test_control_envelope_round_trip();
    test_control_envelope_rejects_duplicate_keys();
    test_control_envelope_rejects_the_previous_protocol_generation();
    test_control_session_handshake_and_heartbeat();
    test_control_session_dispatches_validated_graph_commands();
    test_transition_contract_rejects_invalid_and_legacy_payloads();
    if (failures != 0) {
        std::cerr << failures << " native protocol test(s) failed\n";
        return 1;
    }
    return 0;
}
