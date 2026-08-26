#include "solin/media_engine/scene_snapshot.hpp"

#include <cstddef>
#include <cstdint>
#include <exception>
#include <iostream>
#include <string>
#include <tuple>
#include <utility>

#include <nlohmann/json.hpp>

namespace {

using Json = nlohmann::json;

int failures = 0;

void expect(const bool condition, const char* const description) {
    if (condition) {
        return;
    }
    ++failures;
    std::cerr << "FAILED: " << description << '\n';
}

template <typename Callback> void expect_rejected(Callback&& callback, const char* description) {
    try {
        std::forward<Callback>(callback)();
        expect(false, description);
    } catch (const std::exception&) {
        expect(true, description);
    }
}

[[nodiscard]] Json video_format(const std::string& pixel_format) {
    return {
        {"width", 1920U},
        {"height", 1080U},
        {"fps_numerator", 30U},
        {"fps_denominator", 1U},
        {"pixel_format", pixel_format},
        {"color_space", pixel_format == "bgra" ? "srgb" : "bt709"},
        {"color_range", pixel_format == "bgra" ? "full" : "limited"},
    };
}

[[nodiscard]] Json layer(const std::string& id, const std::string& source_id) {
    return {
        {"id", id},
        {"source_id", source_id},
        {"name", "Layer"},
        {"rect", {{"x", 0.0}, {"y", 0.0}, {"width", 1.0}, {"height", 1.0}}},
        {"crop", {{"left", 0.0}, {"top", 0.0}, {"right", 0.0}, {"bottom", 0.0}}},
        {"rotation_degrees", 0.0},
        {"fit_mode", "cover"},
        {"opacity", 1.0},
        {"visible", true},
        {"locked", false},
        {"mirror_x", false},
        {"mirror_y", false},
        {"border_color", "#00000000"},
        {"border_width", 0.0},
        {"corner_radius", 0.0},
    };
}

[[nodiscard]] Json source(const std::string& id, const std::string& type, Json configuration) {
    return {
        {"id", id},
        {"type", type},
        {"name", "Source"},
        {"enabled", true},
        {"configuration", std::move(configuration)},
        {"credential_ref", ""},
    };
}

[[nodiscard]] Json valid_payload() {
    Json sources = Json::array();
    sources.push_back(source("solin.content.current", "solin_content", Json::object()));
    sources.push_back(source("camera.local", "local_camera",
                             {
                                 {"device_id", "camera://stable-id"},
                                 {"width", 1920U},
                                 {"height", 1080U},
                                 {"fps_numerator", 30'000U},
                                 {"fps_denominator", 1'001U},
                                 {"media_type", "image/jpeg"},
                                 {"pixel_format", "JPEG"},
                                 {"ptz_binding", nullptr},
                                 {"keep_active", false},
                             }));
    sources.push_back(source("solin.source.no-signal", "color", {{"color", "#000000FF"}}));

    Json scenes = Json::array();
    scenes.push_back({
        {"id", "scene-1"},
        {"name", "Scene"},
        {"layers", Json::array({layer("layer-1", "solin.source.no-signal")})},
        {"entry_actions", Json::array()},
        {"created_at", "2026-08-02T12:00:00+00:00"},
        {"updated_at", "2026-08-02T12:00:00+00:00"},
    });

    Json outputs = Json::array();
    outputs.push_back({
        {"bus_id", "media_windows"},
        {"default_scene_id", "scene-1"},
        {"start_with_solin", false},
        {"video_format", video_format("bgra")},
    });
    outputs.push_back({
        {"bus_id", "virtual_camera"},
        {"default_scene_id", "scene-1"},
        {"start_with_solin", false},
        {"video_format", video_format("nv12")},
    });

    Json automation = Json::array();
    automation.push_back({{"bus_id", "media_windows"}, {"assignments", Json::object()}});
    automation.push_back({{"bus_id", "virtual_camera"}, {"assignments", Json::object()}});

    return {
        {"document",
         {
             {"schema_version", 9U},
             {"document_id", "document-1"},
             {"revision", 7U},
             {"sources", std::move(sources)},
             {"scenes", std::move(scenes)},
             {"outputs", std::move(outputs)},
             {"automation", std::move(automation)},
             {"camera_presets", Json::array()},
         }},
        {"active_scenes", {{"media_windows", "scene-1"}, {"virtual_camera", "scene-1"}}},
        {"render_enabled", {{"media_windows", true}, {"virtual_camera", false}}},
        {"output_enabled", {{"media_windows", true}, {"virtual_camera", false}}},
        {"content_ingress", nullptr},
        {"preview_egress", nullptr},
        {"program_egress", nullptr},
        {"window_targets", Json::array()},
    };
}

void test_valid_snapshot_is_normalized_into_typed_graph_state() {
    const auto snapshot = solin::media_engine::parse_scene_hydration_snapshot(valid_payload(), 7U);
    expect(snapshot.document_id == "document-1", "document identity is retained");
    expect(snapshot.sources.size() == 3U, "bounded sources are retained");
    expect(snapshot.scenes.size() == 1U, "bounded scenes are retained");
    expect(snapshot.active_scene_ids[0] == "scene-1", "active scene is retained per bus");
    const auto& local = std::get<solin::media_engine::LocalCameraSourceConfiguration>(
        snapshot.sources[1].configuration);
    expect(local.device_id == "camera://stable-id", "stable camera identity is retained");
    expect(local.fps_numerator == 30'000U && local.fps_denominator == 1'001U,
           "exact rational camera rate is retained");
}

void test_program_default_role_may_be_unassigned() {
    auto payload = valid_payload();
    for (auto& output : payload["document"]["outputs"]) {
        output["default_scene_id"] = "";
    }

    const auto snapshot = solin::media_engine::parse_scene_hydration_snapshot(payload, 7U);

    expect(snapshot.outputs[0].default_scene_id.empty() &&
               snapshot.outputs[1].default_scene_id.empty(),
           "the removable Program default role remains unassigned natively");
}

void test_unknown_nested_fields_are_rejected() {
    auto payload = valid_payload();
    payload["document"]["scenes"][0]["layers"][0]["rect"]["unexpected"] = true;
    expect_rejected(
        [&payload] {
            static_cast<void>(solin::media_engine::parse_scene_hydration_snapshot(payload, 7U));
        },
        "unknown nested scene fields are rejected");
}

void test_unicode_names_use_character_limits_instead_of_wire_byte_limits() {
    auto payload = valid_payload();
    std::string name;
    for (std::size_t index = 0U; index < 120U; ++index) {
        name += "\xC3\xA9";
    }
    payload["document"]["scenes"][0]["name"] = name;
    static_cast<void>(solin::media_engine::parse_scene_hydration_snapshot(payload, 7U));

    name += "\xC3\xA9";
    payload["document"]["scenes"][0]["name"] = name;
    expect_rejected(
        [&payload] {
            static_cast<void>(solin::media_engine::parse_scene_hydration_snapshot(payload, 7U));
        },
        "Unicode names use the same character limit as the Python domain");
}

void test_envelope_and_document_revisions_must_match() {
    const auto payload = valid_payload();
    expect_rejected(
        [&payload] {
            static_cast<void>(solin::media_engine::parse_scene_hydration_snapshot(payload, 8U));
        },
        "stale document revisions are rejected");
}

void test_sensitive_rtsp_query_parameters_are_rejected() {
    auto payload = valid_payload();
    payload["document"]["sources"].push_back(
        source("camera.network", "rtsp_camera",
               {
                   {"uri", "rtsp://camera.local/stream?access_token=secret"},
                   {"transport", "tcp"},
                   {"latency_ms", 200U},
                   {"ptz_binding", nullptr},
                   {"keep_active", false},
               }));
    expect_rejected(
        [&payload] {
            static_cast<void>(solin::media_engine::parse_scene_hydration_snapshot(payload, 7U));
        },
        "RTSP credentials cannot cross the scene snapshot boundary");
}

void test_rtsp_authority_requires_a_host_and_rejects_encoded_userinfo() {
    for (const std::string uri : {"rtsp://:554/stream", "rtsp://operator%40camera/stream"}) {
        auto payload = valid_payload();
        payload["document"]["sources"].push_back(source("camera.network", "rtsp_camera",
                                                        {
                                                            {"uri", uri},
                                                            {"transport", "tcp"},
                                                            {"latency_ms", 200U},
                                                            {"ptz_binding", nullptr},
                                                            {"keep_active", false},
                                                        }));
        expect_rejected(
            [&payload] {
                static_cast<void>(solin::media_engine::parse_scene_hydration_snapshot(payload, 7U));
            },
            "RTSP authority must contain a safe hostname");
    }
}

void test_scene_reference_cycles_are_rejected() {
    auto payload = valid_payload();
    payload["document"]["sources"].push_back(
        source("scene.reference", "scene_reference", {{"target_scene_id", "scene-1"}}));
    payload["document"]["scenes"][0]["layers"].push_back(
        layer("layer-reference", "scene.reference"));
    expect_rejected(
        [&payload] {
            static_cast<void>(solin::media_engine::parse_scene_hydration_snapshot(payload, 7U));
        },
        "scene reference cycles are rejected natively");
}

void test_content_ingress_transport_requires_a_compatible_pixel_layout() {
    struct Combination final {
        const char* transport;
        const char* pixel_format;
        bool accepted;
    };
    constexpr Combination combinations[] = {
        {"shared_memory_bgra", "bgra", true},     {"shared_memory_bgra", "nv12", false},
        {"shared_memory_video", "dynamic", true}, {"shared_memory_video", "bgra", false},
        {"d3d11_shared_texture", "bgra", true},   {"d3d11_shared_texture", "nv12", true},
        {"d3d11_shared_texture", "dynamic", true},
    };

    for (const auto& combination : combinations) {
        auto payload = valid_payload();
        payload["content_ingress"] = {
            {"channel_id", "content-1"},
            {"generation", 1U},
            {"producer_kind", "solin_offscreen"},
            {"transport", combination.transport},
            {"handle_token", "handle-1"},
            {"width", 1920U},
            {"height", 1080U},
            {"pixel_format", combination.pixel_format},
            {"color_space", "bt709"},
            {"color_range", "full"},
        };
        if (combination.accepted) {
            const auto snapshot = solin::media_engine::parse_scene_hydration_snapshot(payload, 7U);
            expect(snapshot.sources[0].frame_channel == snapshot.content_ingress,
                   "content ingress participates in the source runtime identity");
            continue;
        }
        expect_rejected(
            [&payload] {
                static_cast<void>(solin::media_engine::parse_scene_hydration_snapshot(payload, 7U));
            },
            "fixed content ingress only accepts BGRA frames");
    }
}

void test_local_camera_formats_are_either_automatic_or_within_the_exact_media_budget() {
    auto automatic = valid_payload();
    auto& automatic_configuration = automatic["document"]["sources"][1]["configuration"];
    automatic_configuration["width"] = 0U;
    automatic_configuration["height"] = 0U;
    automatic_configuration["fps_numerator"] = 0U;
    automatic_configuration["fps_denominator"] = 1U;
    automatic_configuration["media_type"] = "";
    automatic_configuration["pixel_format"] = "";
    static_cast<void>(solin::media_engine::parse_scene_hydration_snapshot(automatic, 7U));

    for (const auto& [width, height, fps] :
         {std::tuple{1'920U, 1'080U, 0U}, std::tuple{3'840U, 3'840U, 30U},
          std::tuple{2'500U, 2'500U, 30U}, std::tuple{1'920U, 1'080U, 61U}}) {
        auto payload = valid_payload();
        auto& configuration = payload["document"]["sources"][1]["configuration"];
        configuration["width"] = width;
        configuration["height"] = height;
        configuration["fps_numerator"] = fps;
        configuration["fps_denominator"] = 1U;
        expect_rejected(
            [&payload] {
                static_cast<void>(solin::media_engine::parse_scene_hydration_snapshot(payload, 7U));
            },
            "partial or over-budget local camera formats are rejected");
    }
}

} // namespace

int main() {
    test_valid_snapshot_is_normalized_into_typed_graph_state();
    test_program_default_role_may_be_unassigned();
    test_unknown_nested_fields_are_rejected();
    test_unicode_names_use_character_limits_instead_of_wire_byte_limits();
    test_envelope_and_document_revisions_must_match();
    test_sensitive_rtsp_query_parameters_are_rejected();
    test_rtsp_authority_requires_a_host_and_rejects_encoded_userinfo();
    test_scene_reference_cycles_are_rejected();
    test_content_ingress_transport_requires_a_compatible_pixel_layout();
    test_local_camera_formats_are_either_automatic_or_within_the_exact_media_budget();
    if (failures != 0) {
        std::cerr << failures << " scene snapshot test(s) failed\n";
        return 1;
    }
    return 0;
}
