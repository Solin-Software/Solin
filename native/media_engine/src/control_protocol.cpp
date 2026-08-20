#include "solin/media_engine/control_protocol.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_set>
#include <utility>
#include <vector>

namespace solin::media_engine {
namespace {

using Json = nlohmann::json;

constexpr std::array<std::string_view, 9U> kEnvelopeFields{
    "protocol_version",  "message_type",          "request_id",
    "session_id",        "process_generation",    "sequence",
    "document_revision", "deadline_monotonic_ms", "payload",
};

[[nodiscard]] bool has_exact_fields(const Json& value,
                                    const std::initializer_list<std::string_view> fields) {
    if (!value.is_object() || value.size() != fields.size()) {
        return false;
    }
    return std::ranges::all_of(
        fields, [&value](const std::string_view field) { return value.contains(field); });
}

[[nodiscard]] bool has_exact_envelope_fields(const Json& value) {
    if (!value.is_object() || value.size() != kEnvelopeFields.size()) {
        return false;
    }
    return std::ranges::all_of(
        kEnvelopeFields, [&value](const std::string_view field) { return value.contains(field); });
}

[[nodiscard]] std::string strict_identity(const Json& value, const char* field_name) {
    if (!value.is_string()) {
        throw std::runtime_error(std::string{"invalid "} + field_name);
    }
    auto result = value.get<std::string>();
    if (result.empty() || result.size() > 256U ||
        std::ranges::any_of(result, [](const unsigned char character) {
            return character < 33U || character > 126U;
        })) {
        throw std::runtime_error(std::string{"invalid "} + field_name);
    }
    return result;
}

[[nodiscard]] std::string strict_message_type(const Json& value) {
    auto result = strict_identity(value, "message type");
    if (result.size() > 64U || result.front() < 'a' || result.front() > 'z' ||
        std::ranges::any_of(result.substr(1U), [](const char character) {
            return !((character >= 'a' && character <= 'z') ||
                     (character >= '0' && character <= '9') || character == '_');
        })) {
        throw std::runtime_error("invalid message type");
    }
    return result;
}

[[nodiscard]] std::uint64_t strict_integer(const Json& value, const char* field_name) {
    if (!value.is_number_unsigned()) {
        throw std::runtime_error(std::string{"invalid "} + field_name);
    }
    const auto result = value.get<std::uint64_t>();
    if (result > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) {
        throw std::runtime_error(std::string{"invalid "} + field_name);
    }
    return result;
}

[[nodiscard]] std::uint64_t monotonic_milliseconds() {
    const auto now = std::chrono::steady_clock::now().time_since_epoch();
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::milliseconds>(now).count());
}

[[nodiscard]] ControlEnvelope response_for(const ControlEnvelope& request, std::string message_type,
                                           Json payload) {
    return {
        .protocol_version = kControlProtocolVersion,
        .message_type = std::move(message_type),
        .request_id = request.request_id,
        .session_id = request.session_id,
        .process_generation = request.process_generation,
        .sequence = request.sequence,
        .document_revision = request.document_revision,
        .deadline_monotonic_ms = request.deadline_monotonic_ms,
        .payload = std::move(payload),
    };
}

[[nodiscard]] Json unavailable_capabilities() {
    return Json{
        {"local_cameras", false},
        {"rtsp_cameras", false},
        {"hardware_compositing", false},
        {"virtual_camera", false},
        {"d3d11_shared_textures", false},
    };
}

[[nodiscard]] Json unavailable_ack() {
    return Json{
        {"applied", false},
        {"error_code", "media_graph_unavailable"},
        {"error_message", "The native media graph is not enabled"},
    };
}

[[nodiscard]] Json unavailable_local_cameras() {
    return Json{
        {"supported", false},
        {"ready", true},
        {"generation", 0U},
        {"devices", Json::array()},
        {"error_code", "media_runtime_unavailable"},
    };
}

void require_empty_payload(const Json& payload, const char* message_type) {
    if (!payload.is_object() || !payload.empty()) {
        throw std::runtime_error(std::string{"invalid "} + message_type + " payload");
    }
}

void require_bus(const Json& value) {
    const auto bus = strict_identity(value, "output bus");
    if (bus != "media_windows" && bus != "virtual_camera") {
        throw std::runtime_error("invalid output bus");
    }
}

void validate_unavailable_command(const ControlEnvelope& request) {
    if (request.message_type == "take_prepared") {
        if (!has_exact_fields(request.payload,
                              {"bus_id", "scene_id", "preparation_token"})) {
            throw std::runtime_error("invalid take prepared payload");
        }
        require_bus(request.payload.at("bus_id"));
        static_cast<void>(strict_identity(request.payload.at("scene_id"), "scene id"));
        static_cast<void>(
            strict_identity(request.payload.at("preparation_token"), "preparation token"));
        return;
    }
    if (!has_exact_fields(request.payload, {"bus_id", "enabled"}) ||
        !request.payload.at("enabled").is_boolean()) {
        throw std::runtime_error("invalid output state payload");
    }
    require_bus(request.payload.at("bus_id"));
}

void validate_capabilities_payload(const Json& payload) {
    if (!has_exact_fields(payload,
                          {"local_cameras", "rtsp_cameras", "hardware_compositing",
                           "virtual_camera", "d3d11_shared_textures"}) ||
        !payload.at("local_cameras").is_boolean() ||
        !payload.at("rtsp_cameras").is_boolean() ||
        !payload.at("hardware_compositing").is_boolean() ||
        !payload.at("virtual_camera").is_boolean() ||
        !payload.at("d3d11_shared_textures").is_boolean()) {
        throw std::runtime_error("invalid media graph capabilities");
    }
}

void validate_hydrate_shape(const Json& payload) {
    if (!has_exact_fields(payload, {"document", "active_scenes", "render_enabled",
                                    "output_enabled", "content_ingress", "preview_egress",
                                    "program_egress", "window_targets"}) ||
        !payload.at("document").is_object() || !payload.at("active_scenes").is_object() ||
        !payload.at("render_enabled").is_object() ||
        !payload.at("output_enabled").is_object() ||
        !(payload.at("content_ingress").is_null() ||
          payload.at("content_ingress").is_object()) ||
        !(payload.at("preview_egress").is_null() ||
          payload.at("preview_egress").is_object()) ||
        !(payload.at("program_egress").is_null() ||
          payload.at("program_egress").is_object()) ||
        !payload.at("window_targets").is_array()) {
        throw std::runtime_error("invalid hydrate payload");
    }
}

void validate_ack_payload(const Json& payload) {
    if (!has_exact_fields(payload, {"applied", "error_code", "error_message"}) ||
        !payload.at("applied").is_boolean() || !payload.at("error_code").is_string() ||
        !payload.at("error_message").is_string()) {
        throw std::runtime_error("invalid media graph acknowledgement");
    }
    const auto applied = payload.at("applied").get<bool>();
    const auto error_code = payload.at("error_code").get<std::string>();
    const auto error_message = payload.at("error_message").get<std::string>();
    if (error_code.size() > 128U || error_message.size() > 512U ||
        std::ranges::any_of(error_code, [](const unsigned char character) {
            return character < 32U || character == 127U;
        }) ||
        std::ranges::any_of(error_message, [](const unsigned char character) {
            return character < 32U || character == 127U;
        }) ||
        (applied && (!error_code.empty() || !error_message.empty())) ||
        (!applied && error_code.empty())) {
        throw std::runtime_error("invalid media graph acknowledgement");
    }
}

void validate_error_payload(const Json& payload) {
    if (!has_exact_fields(payload, {"error_code", "error_message"}) ||
        !payload.at("error_code").is_string() ||
        !payload.at("error_message").is_string()) {
        throw std::runtime_error("invalid media graph error");
    }
    const auto error_code = payload.at("error_code").get<std::string>();
    const auto error_message = payload.at("error_message").get<std::string>();
    if (error_code.empty() || error_code.size() > 128U || error_message.size() > 512U ||
        std::ranges::any_of(error_code, [](const unsigned char character) {
            return character < 32U || character == 127U;
        }) ||
        std::ranges::any_of(error_message, [](const unsigned char character) {
            return character < 32U || character == 127U;
        })) {
        throw std::runtime_error("invalid media graph error");
    }
}

void validate_preparation_payload(const Json& payload, const std::string_view expected_bus,
                                  const std::string_view expected_scene) {
    if (!has_exact_fields(payload,
                          {"bus_id", "scene_id", "preparation_token", "transition",
                           "fallback_applied", "fallback_reason"}) ||
        !payload.at("transition").is_object() ||
        !has_exact_fields(payload.at("transition"), {"kind", "duration_ms"}) ||
        !payload.at("fallback_applied").is_boolean() ||
        !payload.at("fallback_reason").is_string()) {
        throw std::runtime_error("invalid scene preparation");
    }
    require_bus(payload.at("bus_id"));
    const auto bus = strict_identity(payload.at("bus_id"), "output bus");
    const auto scene = strict_identity(payload.at("scene_id"), "scene id");
    static_cast<void>(strict_identity(payload.at("preparation_token"), "preparation token"));
    const auto transition_kind =
        strict_identity(payload.at("transition").at("kind"), "transition kind");
    const auto transition_duration = strict_integer(
        payload.at("transition").at("duration_ms"), "transition duration");
    if ((transition_kind == "cut" && transition_duration != 0U) ||
        ((transition_kind == "dissolve" || transition_kind == "fade_to_black") &&
         (transition_duration < 50U || transition_duration > 10'000U)) ||
        (transition_kind != "cut" && transition_kind != "dissolve" &&
         transition_kind != "fade_to_black")) {
        throw std::runtime_error("invalid prepared transition");
    }
    const auto fallback = payload.at("fallback_applied").get<bool>();
    const auto reason = payload.at("fallback_reason").get<std::string>();
    if (reason.size() > 128U || (fallback && reason.empty()) || (!fallback && !reason.empty())) {
        throw std::runtime_error("invalid transition fallback");
    }
    if (bus != expected_bus || scene != expected_scene) {
        throw std::runtime_error("mismatched scene preparation");
    }
}

} // namespace

ControlEnvelope parse_control_envelope(const std::string_view payload) {
    std::vector<std::unordered_set<std::string>> object_keys;
    const auto callback = [&object_keys](const int, const Json::parse_event_t event, Json& parsed) {
        if (event == Json::parse_event_t::object_start) {
            object_keys.emplace_back();
        } else if (event == Json::parse_event_t::key) {
            if (object_keys.empty() ||
                !object_keys.back().insert(parsed.get<std::string>()).second) {
                throw std::runtime_error("duplicate JSON object key");
            }
        } else if (event == Json::parse_event_t::object_end) {
            if (object_keys.empty()) {
                throw std::runtime_error("invalid JSON object nesting");
            }
            object_keys.pop_back();
        }
        return true;
    };
    const auto raw = Json::parse(payload, callback, true, false);
    if (!has_exact_envelope_fields(raw)) {
        throw std::runtime_error("invalid control envelope fields");
    }
    const auto version = strict_integer(raw.at("protocol_version"), "protocol version");
    if (version != kControlProtocolVersion) {
        throw std::runtime_error("unsupported control protocol version");
    }
    if (!raw.at("payload").is_object()) {
        throw std::runtime_error("invalid control payload");
    }
    return {
        .protocol_version = version,
        .message_type = strict_message_type(raw.at("message_type")),
        .request_id = strict_identity(raw.at("request_id"), "request id"),
        .session_id = strict_identity(raw.at("session_id"), "session id"),
        .process_generation = strict_identity(raw.at("process_generation"), "process generation"),
        .sequence = strict_integer(raw.at("sequence"), "sequence"),
        .document_revision = strict_integer(raw.at("document_revision"), "document revision"),
        .deadline_monotonic_ms = strict_integer(raw.at("deadline_monotonic_ms"), "deadline"),
        .payload = raw.at("payload"),
    };
}

std::string serialize_control_envelope(const ControlEnvelope& envelope) {
    const Json value{
        {"protocol_version", envelope.protocol_version},
        {"message_type", envelope.message_type},
        {"request_id", envelope.request_id},
        {"session_id", envelope.session_id},
        {"process_generation", envelope.process_generation},
        {"sequence", envelope.sequence},
        {"document_revision", envelope.document_revision},
        {"deadline_monotonic_ms", envelope.deadline_monotonic_ms},
        {"payload", envelope.payload},
    };
    return value.dump();
}

ControlSession::ControlSession(std::string expected_process_generation,
                               ControlSessionServices services)
    : expected_process_generation_(std::move(expected_process_generation)),
      services_(std::move(services)) {
    if (expected_process_generation_.empty() || expected_process_generation_.size() > 256U) {
        throw std::runtime_error("invalid expected process generation");
    }
}

ProtocolReply ControlSession::handle(const ControlEnvelope& request) {
    if (request.process_generation != expected_process_generation_) {
        throw std::runtime_error("process generation mismatch");
    }
    if (session_id_.has_value() && request.session_id != session_id_.value()) {
        throw std::runtime_error("session mismatch");
    }
    if (request.deadline_monotonic_ms < monotonic_milliseconds()) {
        throw std::runtime_error("request deadline expired");
    }

    if (!session_id_.has_value()) {
        if (request.message_type != "hello" || request.sequence != 0U ||
            request.document_revision != 0U ||
            !has_exact_fields(request.payload, {"parent_process_id"}) ||
            strict_integer(request.payload.at("parent_process_id"), "parent process id") == 0U) {
            throw std::runtime_error("first control message must be hello");
        }
        session_id_ = request.session_id;
        auto payload = services_.capabilities ? services_.capabilities()
                                              : unavailable_capabilities();
        validate_capabilities_payload(payload);
        return {
            .response = response_for(request, "hello_ack", std::move(payload)),
        };
    }

    if (request.message_type == "hello") {
        throw std::runtime_error("hello may only be sent once");
    }
    if (request.message_type == "ping") {
        require_empty_payload(request.payload, "ping");
        return {
            .response = response_for(request, "heartbeat",
                                     Json{{"monotonic_ms", monotonic_milliseconds()}}),
        };
    }
    if (request.message_type == "stop") {
        require_empty_payload(request.payload, "stop");
        return {.should_stop = true};
    }
    if (request.message_type == "cancel_preparation") {
        if (!has_exact_fields(request.payload, {"cancelled_request_id"})) {
            throw std::runtime_error("invalid cancel preparation payload");
        }
        const auto cancelled_request_id =
            strict_identity(request.payload.at("cancelled_request_id"),
                            "cancelled request id");
        if (services_.cancel_preparation) {
            services_.cancel_preparation(cancelled_request_id);
        }
        return {};
    }
    if (request.message_type == "list_local_cameras") {
        require_empty_payload(request.payload, "list local cameras");
        auto payload = services_.list_local_cameras ? services_.list_local_cameras()
                                                     : unavailable_local_cameras();
        if (!has_exact_fields(payload,
                              {"supported", "ready", "generation", "devices", "error_code"}) ||
            !payload.at("supported").is_boolean() || !payload.at("ready").is_boolean() ||
            !payload.at("generation").is_number_unsigned() ||
            !payload.at("devices").is_array() || !payload.at("error_code").is_string()) {
            throw std::runtime_error("invalid local camera service response");
        }
        return {
            .response = response_for(request, "local_camera_list", std::move(payload)),
        };
    }
    if (request.message_type == "hydrate") {
        validate_hydrate_shape(request.payload);
        auto payload = services_.hydrate
                           ? services_.hydrate(request.payload, request.document_revision,
                                               request.sequence)
                           : unavailable_ack();
        validate_ack_payload(payload);
        return {.response = response_for(request, "ack", std::move(payload))};
    }
    if (request.message_type == "take_prepared") {
        validate_unavailable_command(request);
        const auto bus = strict_identity(request.payload.at("bus_id"), "output bus");
        const auto scene_id = strict_identity(request.payload.at("scene_id"), "scene id");
        const auto preparation_token = strict_identity(
            request.payload.at("preparation_token"), "preparation token");
        auto payload = services_.take_prepared
                           ? services_.take_prepared(
                                 bus, scene_id, preparation_token, request.document_revision,
                                 request.sequence)
                           : unavailable_ack();
        validate_ack_payload(payload);
        return {.response = response_for(request, "ack", std::move(payload))};
    }
    if (request.message_type == "preview_layer_geometry") {
        if (!has_exact_fields(request.payload, {"bus_id", "scene_id", "layer"}) ||
            !request.payload.at("layer").is_object()) {
            throw std::runtime_error("invalid preview layer geometry payload");
        }
        require_bus(request.payload.at("bus_id"));
        const auto bus = strict_identity(request.payload.at("bus_id"), "output bus");
        const auto scene_id = strict_identity(request.payload.at("scene_id"), "scene id");
        auto payload = services_.preview_layer_geometry
                           ? services_.preview_layer_geometry(
                                 bus, scene_id, request.payload.at("layer"),
                                 request.document_revision, request.sequence)
                           : unavailable_ack();
        validate_ack_payload(payload);
        return {.response = response_for(request, "ack", std::move(payload))};
    }
    if (request.message_type == "set_output_enabled") {
        validate_unavailable_command(request);
        const auto bus = strict_identity(request.payload.at("bus_id"), "output bus");
        const auto enabled = request.payload.at("enabled").get<bool>();
        auto payload = services_.set_output_enabled
                           ? services_.set_output_enabled(
                                 bus, enabled, request.document_revision, request.sequence)
                           : unavailable_ack();
        validate_ack_payload(payload);
        return {.response = response_for(request, "ack", std::move(payload))};
    }
    if (request.message_type == "set_render_enabled") {
        validate_unavailable_command(request);
        const auto bus = strict_identity(request.payload.at("bus_id"), "render bus");
        const auto enabled = request.payload.at("enabled").get<bool>();
        auto payload = services_.set_render_enabled
                           ? services_.set_render_enabled(
                                 bus, enabled, request.document_revision, request.sequence)
                           : unavailable_ack();
        validate_ack_payload(payload);
        return {.response = response_for(request, "ack", std::move(payload))};
    }
    if (request.message_type == "set_window_targets") {
        if (!has_exact_fields(request.payload, {"window_targets"}) ||
            !request.payload.at("window_targets").is_array()) {
            throw std::runtime_error("invalid window targets payload");
        }
        auto payload = services_.set_window_targets
                           ? services_.set_window_targets(
                                 request.payload.at("window_targets"),
                                 request.document_revision, request.sequence)
                           : unavailable_ack();
        validate_ack_payload(payload);
        return {.response = response_for(request, "ack", std::move(payload))};
    }
    if (request.message_type == "prepare_scene") {
        if (!has_exact_fields(request.payload, {"bus_id", "scene_id", "transition"}) ||
            !request.payload.at("transition").is_object() ||
            !has_exact_fields(request.payload.at("transition"), {"kind", "duration_ms"})) {
            throw std::runtime_error("invalid prepare scene payload");
        }
        require_bus(request.payload.at("bus_id"));
        const auto bus = strict_identity(request.payload.at("bus_id"), "output bus");
        const auto scene_id = strict_identity(request.payload.at("scene_id"), "scene id");
        const auto transition_kind = strict_identity(
            request.payload.at("transition").at("kind"), "transition kind");
        const auto transition_duration_ms = strict_integer(
            request.payload.at("transition").at("duration_ms"), "transition duration");
        if ((transition_kind == "cut" && transition_duration_ms != 0U) ||
            ((transition_kind == "dissolve" || transition_kind == "fade_to_black") &&
             (transition_duration_ms < 50U || transition_duration_ms > 10'000U)) ||
            (transition_kind != "cut" && transition_kind != "dissolve" &&
             transition_kind != "fade_to_black")) {
            throw std::runtime_error("invalid prepare scene transition");
        }
        auto reply = services_.prepare_scene
                         ? services_.prepare_scene(
                               bus, scene_id, transition_kind, transition_duration_ms,
                               request.document_revision, request.request_id,
                               request.sequence)
                         : ControlServiceReply{
                               .message_type = "error",
                               .payload = {
                                   {"error_code", "media_graph_unavailable"},
                                   {"error_message",
                                    "The native media graph is not enabled"},
                               },
                           };
        if (reply.message_type == "scene_prepared") {
            validate_preparation_payload(reply.payload, bus, scene_id);
        } else if (reply.message_type == "error") {
            validate_error_payload(reply.payload);
        } else {
            throw std::runtime_error("invalid prepare scene service response");
        }
        return {.response =
                    response_for(request, std::move(reply.message_type),
                                 std::move(reply.payload))};
    }
    throw std::runtime_error("unsupported control message type");
}

} // namespace solin::media_engine
