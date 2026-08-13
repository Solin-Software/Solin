#include "solin/media_engine/control_protocol.hpp"
#include "solin/media_engine/frame_codec.hpp"
#include "solin/media_engine/media_runtime.hpp"
#include "solin/media_engine/frame_channel_output.hpp"
#include "solin/media_engine/native_window_output.hpp"
#include "solin/media_engine/scene_graph.hpp"
#include "solin/media_engine/scene_snapshot.hpp"
#include "solin/media_engine/virtual_camera_output.hpp"

#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

#ifdef _WIN32
#include "windows_virtual_camera_backend.hpp"
#include <fcntl.h>
#include <io.h>
#endif

namespace {

constexpr std::string_view kVersion = SOLIN_MEDIA_ENGINE_VERSION;

nlohmann::json local_camera_payload(const solin::media_engine::MediaRuntime& media_runtime) {
    const auto snapshot = media_runtime.local_cameras();
    auto devices = nlohmann::json::array();
    for (const auto& device : snapshot.devices) {
        auto formats = nlohmann::json::array();
        for (const auto& format : device.formats) {
            formats.push_back({
                {"media_type", format.media_type},
                {"pixel_format", format.pixel_format},
                {"width", format.width},
                {"height", format.height},
                {"fps_numerator", format.fps_numerator},
                {"fps_denominator", format.fps_denominator},
            });
        }
        devices.push_back({
            {"device_id", device.device_id},
            {"display_name", device.display_name},
            {"software_device", device.software_device},
            {"formats", std::move(formats)},
        });
    }
    return {
        {"supported", snapshot.supported},   {"ready", snapshot.ready},
        {"generation", snapshot.generation}, {"devices", std::move(devices)},
        {"error_code", snapshot.error_code},
    };
}

nlohmann::json applied_ack() {
    return {
        {"applied", true},
        {"error_code", ""},
        {"error_message", ""},
    };
}

nlohmann::json rejected_ack(const std::string_view error_code,
                            const std::string_view error_message) {
    return {
        {"applied", false},
        {"error_code", error_code},
        {"error_message", error_message},
    };
}

nlohmann::json
rejected_virtual_camera_ack(const solin::media_engine::VirtualCameraOutputController& output) {
    const auto health = output.health();
    const auto error_code = health.error_code.empty()
                                ? std::string_view{"virtual_camera_output_failed"}
                                : std::string_view{health.error_code};
    return rejected_ack(error_code, "The virtual-camera output could not be started");
}

solin::media_engine::ControlServiceReply
rejected_preparation(const std::string_view error_code, const std::string_view error_message) {
    return {
        .message_type = "error",
        .payload =
            {
                {"error_code", error_code},
                {"error_message", error_message},
            },
    };
}

solin::media_engine::OutputBus output_bus_from_text(const std::string_view value) {
    return value == "media_windows" ? solin::media_engine::OutputBus::media_windows
                                    : solin::media_engine::OutputBus::virtual_camera;
}

nlohmann::json graph_capabilities(const solin::media_engine::MediaRuntimeProbe& probe,
                                  const bool graph_available) {
    return {
        {"local_cameras", graph_available && probe.local_camera_source},
        {"rtsp_cameras", graph_available && probe.rtsp_source},
        {"hardware_compositing", graph_available && probe.d3d11_compositor},
        {"virtual_camera", graph_available && probe.virtual_camera.operational},
        {"d3d11_shared_textures", false},
    };
}

nlohmann::json
hydrate_scene_graph(solin::media_engine::SceneGraphRuntime* graph,
                    solin::media_engine::FrameChannelOutputController* preview_output,
                    solin::media_engine::FrameChannelOutputController* program_output,
                    solin::media_engine::NativeWindowOutputController* native_window_output,
                    solin::media_engine::VirtualCameraOutputController* virtual_camera_output,
                    const nlohmann::json& payload, const std::uint64_t document_revision,
                    const std::uint64_t sequence) {
    try {
        auto snapshot =
            solin::media_engine::parse_scene_hydration_snapshot(payload, document_revision);
        if (graph == nullptr) {
            return rejected_ack("media_graph_unavailable", "The native media graph is not enabled");
        }
        graph->hydrate(snapshot, sequence);
        constexpr auto media_window_index =
            static_cast<std::size_t>(solin::media_engine::OutputBus::media_windows);
        if (preview_output != nullptr &&
            (!preview_output->configure(snapshot.preview_egress,
                                        snapshot.outputs[media_window_index]) ||
             !preview_output->set_enabled(snapshot.render_enabled[media_window_index]))) {
            return rejected_ack("preview_output_failed",
                                "The preview frame output could not be started");
        }
        constexpr auto virtual_camera_index =
            static_cast<std::size_t>(solin::media_engine::OutputBus::virtual_camera);
        if (program_output != nullptr &&
            (!program_output->configure(snapshot.program_egress,
                                        snapshot.outputs[virtual_camera_index]) ||
             !program_output->set_enabled(snapshot.render_enabled[virtual_camera_index]))) {
            return rejected_ack("program_output_failed",
                                "The Program frame output could not be started");
        }
        if (native_window_output != nullptr &&
            (!native_window_output->configure(snapshot.window_targets) ||
             !native_window_output->set_enabled(!snapshot.window_targets.empty()))) {
            return rejected_ack("native_window_output_failed",
                                "The native window output could not be started");
        }
        if (virtual_camera_output != nullptr) {
            if (!virtual_camera_output->configure(snapshot.outputs[virtual_camera_index]) ||
                !virtual_camera_output->set_enabled(
                    snapshot.output_enabled[virtual_camera_index])) {
                return rejected_virtual_camera_ack(*virtual_camera_output);
            }
        }
        return applied_ack();
    } catch (const solin::media_engine::SceneGraphError& error) {
        return rejected_ack(error.error_code(), error.what());
    } catch (const std::runtime_error&) {
        return rejected_ack("invalid_scene_snapshot", "The scene snapshot was rejected");
    }
}

int run_self_test() {
    constexpr std::string_view payload = R"({"protocol_version":3,"message_type":"probe"})";
    std::stringstream stream;
    if (!solin::media_engine::write_frame(stream, payload)) {
        return 1;
    }
    const auto result = solin::media_engine::read_frame(stream);
    if (result.status != solin::media_engine::FrameReadStatus::ok || result.payload != payload) {
        return 1;
    }
    solin::media_engine::ControlSession session{"self-test-generation"};
    const solin::media_engine::ControlEnvelope hello{
        .message_type = "hello",
        .request_id = "self-test-request",
        .session_id = "self-test-session",
        .process_generation = "self-test-generation",
        .sequence = 0U,
        .document_revision = 0U,
        .deadline_monotonic_ms =
            static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()),
        .payload = {{"parent_process_id", 1U}},
    };
    const auto reply = session.handle(hello);
    if (!reply.response.has_value() || reply.response->message_type != "hello_ack") {
        return 1;
    }
    try {
        static_cast<void>(
            solin::media_engine::parse_scene_hydration_snapshot(nlohmann::json::object(), 0U));
        return 1;
    } catch (const std::runtime_error&) {
        // The boundary must reject incomplete snapshots before graph application.
    }
    solin::media_engine::MediaRuntime media_runtime;
    const auto media_probe = media_runtime.initialize();
    if (solin::media_engine::MediaRuntime::is_compiled() &&
        (!media_probe.initialized || !media_probe.local_camera_source ||
         !media_probe.rtsp_source)) {
        return 1;
    }
    return 0;
}

std::optional<std::string> process_generation_from_environment() {
#ifdef _WIN32
    char* raw_value = nullptr;
    std::size_t value_size = 0U;
    if (_dupenv_s(&raw_value, &value_size, "SOLIN_MEDIA_ENGINE_PROCESS_GENERATION") != 0 ||
        raw_value == nullptr) {
        return std::nullopt;
    }
    std::string value{raw_value};
    std::free(raw_value);
    if (value.empty()) {
        return std::nullopt;
    }
    return value;
#else
    const auto* raw_value = std::getenv("SOLIN_MEDIA_ENGINE_PROCESS_GENERATION");
    if (raw_value == nullptr || std::string_view{raw_value}.empty()) {
        return std::nullopt;
    }
    return std::string{raw_value};
#endif
}

int run_protocol() {
#ifdef _WIN32
    if (_setmode(_fileno(stdin), _O_BINARY) == -1 || _setmode(_fileno(stdout), _O_BINARY) == -1) {
        return 74;
    }
#endif
    const auto generation = process_generation_from_environment();
    if (!generation.has_value()) {
        std::cerr << "Missing process generation.\n";
        return 78;
    }
    solin::media_engine::MediaRuntime media_runtime;
    const auto media_probe = media_runtime.initialize();
    if (solin::media_engine::MediaRuntime::is_compiled() && !media_probe.initialized) {
        std::cerr << "Media runtime initialization failed.\n";
        return 69;
    }
    std::unique_ptr<solin::media_engine::SceneGraphRuntime> scene_graph;
    std::unique_ptr<solin::media_engine::FrameChannelOutputController> preview_output;
    std::unique_ptr<solin::media_engine::FrameChannelOutputController> program_output;
    std::unique_ptr<solin::media_engine::NativeWindowOutputController> native_window_output;
    std::unique_ptr<solin::media_engine::VirtualCameraOutputController> virtual_camera_output;
    if (media_probe.initialized) {
        auto renderer = media_runtime.scene_renderer();
        scene_graph = std::make_unique<solin::media_engine::SceneGraphRuntime>(
            media_runtime.source_runtime_factory(), renderer);
        if (renderer != nullptr) {
            preview_output =
                std::make_unique<solin::media_engine::FrameChannelOutputController>(
                    renderer, solin::media_engine::OutputBus::media_windows);
            program_output =
                std::make_unique<solin::media_engine::FrameChannelOutputController>(
                    renderer, solin::media_engine::OutputBus::virtual_camera);
            native_window_output =
                std::make_unique<solin::media_engine::NativeWindowOutputController>(renderer);
            virtual_camera_output =
                std::make_unique<solin::media_engine::VirtualCameraOutputController>(
                    renderer, solin::media_engine::make_platform_virtual_camera_backend());
        }
    }
    solin::media_engine::ControlSession session{
        generation.value(),
        {
            .capabilities =
                [&media_probe, &scene_graph] {
                    return graph_capabilities(media_probe, scene_graph != nullptr);
                },
            .list_local_cameras = [&media_runtime] { return local_camera_payload(media_runtime); },
            .hydrate =
                [&scene_graph, &preview_output, &program_output, &native_window_output,
                 &virtual_camera_output](
                    const nlohmann::json& payload, const std::uint64_t document_revision,
                    const std::uint64_t sequence) {
                    return hydrate_scene_graph(
                        scene_graph.get(), preview_output.get(), program_output.get(),
                        native_window_output.get(), virtual_camera_output.get(), payload,
                        document_revision, sequence);
                },
            .prepare_scene =
                [&scene_graph](const std::string_view bus, const std::string_view scene_id,
                               const std::string_view transition_kind,
                               const std::uint64_t transition_duration_ms,
                               const std::uint64_t document_revision,
                               const std::string_view request_id, const std::uint64_t sequence) {
                    if (scene_graph == nullptr) {
                        return rejected_preparation("media_graph_unavailable",
                                                    "The native media graph is not enabled");
                    }
                    try {
                        const auto receipt =
                            scene_graph->prepare(output_bus_from_text(bus), scene_id,
                                                 document_revision, request_id, sequence,
                                                 {.kind = solin::media_engine::
                                                              scene_transition_kind_from_text(
                                                                  transition_kind),
                                                  .duration_ms = static_cast<std::uint32_t>(
                                                      transition_duration_ms)});
                        return solin::media_engine::ControlServiceReply{
                            .message_type = "scene_prepared",
                            .payload =
                                {
                                    {"bus_id", bus},
                                    {"scene_id", receipt.scene_id},
                                    {"preparation_token", receipt.preparation_token},
                                    {"transition",
                                     {{"kind", solin::media_engine::scene_transition_kind_text(
                                                   receipt.effective_transition.kind)},
                                      {"duration_ms",
                                       receipt.effective_transition.duration_ms}}},
                                    {"fallback_applied", receipt.fallback_applied},
                                    {"fallback_reason", receipt.fallback_reason},
                                },
                        };
                    } catch (const solin::media_engine::SceneGraphError& error) {
                        return rejected_preparation(error.error_code(), error.what());
                    }
                },
            .take_prepared =
                [&scene_graph](
                    const std::string_view bus, const std::string_view scene_id,
                    const std::string_view preparation_token,
                    const std::uint64_t document_revision, const std::uint64_t sequence) {
                    if (scene_graph == nullptr) {
                        return rejected_ack("media_graph_unavailable",
                                            "The native media graph is not enabled");
                    }
                    try {
                        scene_graph->take(output_bus_from_text(bus), scene_id, preparation_token,
                                          document_revision, sequence);
                        return applied_ack();
                    } catch (const solin::media_engine::SceneGraphError& error) {
                        return rejected_ack(error.error_code(), error.what());
                    }
                },
            .cancel_preparation =
                [&scene_graph](const std::string_view request_id) {
                    if (scene_graph != nullptr) {
                        scene_graph->cancel(request_id);
                    }
                },
            .preview_layer_geometry =
                [&scene_graph](const std::string_view bus,
                               const std::string_view scene_id,
                               const nlohmann::json& raw_layer,
                               const std::uint64_t document_revision,
                               const std::uint64_t sequence) {
                    if (scene_graph == nullptr) {
                        return rejected_ack("media_graph_unavailable",
                                            "The native media graph is not enabled");
                    }
                    try {
                        const auto layer =
                            solin::media_engine::parse_scene_layer_definition(raw_layer);
                        scene_graph->preview_layer_geometry(
                            output_bus_from_text(bus), scene_id, layer,
                            document_revision, sequence);
                        return applied_ack();
                    } catch (const solin::media_engine::SceneGraphError& error) {
                        return rejected_ack(error.error_code(), error.what());
                    } catch (const std::runtime_error&) {
                        return rejected_ack("invalid_layer_geometry",
                                            "The preview layer geometry was rejected");
                    }
                },
            .set_output_enabled =
                [&virtual_camera_output](
                    const std::string_view bus, const bool enabled,
                    const std::uint64_t document_revision, const std::uint64_t sequence) {
                    static_cast<void>(document_revision);
                    static_cast<void>(sequence);
                    try {
                        if (bus == "virtual_camera" && virtual_camera_output != nullptr &&
                            !virtual_camera_output->set_enabled(enabled)) {
                            return rejected_virtual_camera_ack(*virtual_camera_output);
                        }
                        return applied_ack();
                    } catch (const std::exception&) {
                        return rejected_ack("output_update_failed",
                                            "The output could not be updated");
                    }
                },
            .set_render_enabled =
                [&scene_graph, &preview_output, &program_output](
                    const std::string_view bus, const bool enabled,
                    const std::uint64_t document_revision, const std::uint64_t sequence) {
                    if (scene_graph == nullptr) {
                        return rejected_ack("media_graph_unavailable",
                                            "The native media graph is not enabled");
                    }
                    try {
                        scene_graph->set_output_enabled(output_bus_from_text(bus), enabled,
                                                        document_revision, sequence);
                        auto* frame_output = bus == "media_windows" ? preview_output.get()
                                                                    : program_output.get();
                        if (frame_output != nullptr && !frame_output->set_enabled(enabled)) {
                            return rejected_ack("frame_output_failed",
                                                "The frame output could not be updated");
                        }
                        return applied_ack();
                    } catch (const solin::media_engine::SceneGraphError& error) {
                        return rejected_ack(error.error_code(), error.what());
                    }
                },
        },
    };
    while (true) {
        const auto frame = solin::media_engine::read_frame(std::cin);
        if (frame.status == solin::media_engine::FrameReadStatus::clean_eof) {
            return 0;
        }
        if (frame.status != solin::media_engine::FrameReadStatus::ok) {
            std::cerr << "Invalid control frame.\n";
            return 65;
        }
        try {
            const auto request = solin::media_engine::parse_control_envelope(frame.payload);
            const auto reply = session.handle(request);
            if (reply.response.has_value() &&
                !solin::media_engine::write_frame(
                    std::cout,
                    solin::media_engine::serialize_control_envelope(reply.response.value()))) {
                return 74;
            }
            if (reply.should_stop) {
                return 0;
            }
        } catch (const std::exception&) {
            std::cerr << "Invalid control message.\n";
            return 65;
        }
    }
}

} // namespace

int main(const int argc, const char* const argv[]) {
    if (argc == 2 && std::string_view{argv[1]} == "--version") {
        std::cout << "solin-media-engine " << kVersion << '\n';
        return 0;
    }
    if (argc == 2 && std::string_view{argv[1]} == "--self-test") {
        return run_self_test();
    }
    if (argc == 1) {
        return run_protocol();
    }
    std::cerr << "Unsupported command-line arguments.\n";
    return 64;
}
