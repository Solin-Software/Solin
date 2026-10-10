#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

#include <nlohmann/json.hpp>

namespace solin::media_engine {

inline constexpr std::string_view kSolinContentSourceId = "solin.content.current";

enum class OutputBus : std::uint8_t {
    media_windows = 0U,
    virtual_camera = 1U,
};

enum class SceneSourceKind : std::uint8_t {
    solin_content,
    local_camera,
    rtsp_camera,
    image,
    color,
    scene_reference,
    idle_screen,
};

struct FrameChannelConfiguration {
    std::string channel_id{};
    std::uint64_t generation{0U};
    std::string producer_kind{};
    std::string transport{};
    std::string handle_token{};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    std::string pixel_format{};
    std::string color_space{};
    std::string color_range{};

    bool operator==(const FrameChannelConfiguration&) const = default;
};

struct LocalCameraSourceConfiguration {
    std::string device_id{};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    std::uint32_t fps_numerator{0U};
    std::uint32_t fps_denominator{1U};
    std::string media_type{};
    std::string pixel_format{};
    bool has_ptz_binding{false};
    bool keep_active{false};

    bool operator==(const LocalCameraSourceConfiguration&) const = default;
};

struct RtspCameraSourceConfiguration {
    std::string uri{};
    bool use_tcp{true};
    std::uint32_t latency_ms{200U};
    bool has_ptz_binding{false};
    bool keep_active{false};

    bool operator==(const RtspCameraSourceConfiguration&) const = default;
};

struct ImageSourceConfiguration {
    std::string asset_id{};

    bool operator==(const ImageSourceConfiguration&) const = default;
};

struct ColorSourceConfiguration {
    std::string color{};

    bool operator==(const ColorSourceConfiguration&) const = default;
};

struct SceneReferenceSourceConfiguration {
    std::string target_scene_id{};

    bool operator==(const SceneReferenceSourceConfiguration&) const = default;
};

using SceneSourceConfiguration =
    std::variant<std::monostate, LocalCameraSourceConfiguration, RtspCameraSourceConfiguration,
                 ImageSourceConfiguration, ColorSourceConfiguration,
                 SceneReferenceSourceConfiguration>;

struct SceneSource {
    std::string id{};
    SceneSourceKind kind{SceneSourceKind::solin_content};
    bool enabled{true};
    SceneSourceConfiguration configuration{};
    std::optional<FrameChannelConfiguration> frame_channel{};

    bool operator==(const SceneSource&) const = default;
};

struct SceneLayerGeometry {
    double x{0.0};
    double y{0.0};
    double width{1.0};
    double height{1.0};
    double crop_left{0.0};
    double crop_top{0.0};
    double crop_right{0.0};
    double crop_bottom{0.0};
    double rotation_degrees{0.0};
    double opacity{1.0};
    double border_width{0.0};
    double corner_radius{0.0};
    std::string fit_mode{};
    std::string border_color{};
    bool visible{true};
    bool mirror_x{false};
    bool mirror_y{false};

    bool operator==(const SceneLayerGeometry&) const = default;
};

struct SceneLayerDefinition {
    std::string id{};
    std::string source_id{};
    SceneLayerGeometry geometry{};

    bool operator==(const SceneLayerDefinition&) const = default;
};

struct SceneGraphDefinition {
    std::string id{};
    std::vector<SceneLayerDefinition> layers{};

    bool operator==(const SceneGraphDefinition&) const = default;
};

struct OutputVideoFormat {
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    std::uint32_t fps_numerator{0U};
    std::uint32_t fps_denominator{1U};
    std::string pixel_format{};
    std::string color_space{};
    std::string color_range{};

    bool operator==(const OutputVideoFormat&) const = default;
};

struct SceneOutputDefinition {
    OutputBus bus{OutputBus::media_windows};
    std::string default_scene_id{};
    bool start_with_solin{false};
    OutputVideoFormat video_format{};

    bool operator==(const SceneOutputDefinition&) const = default;
};

struct OutputWindowConfiguration {
    OutputBus bus{OutputBus::media_windows};
    std::string target_id{};
    std::string screen_id{};
    std::uint64_t native_handle{0U};
    std::int32_t x{0};
    std::int32_t y{0};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    double device_pixel_ratio{1.0};
    bool visible{true};

    bool operator==(const OutputWindowConfiguration&) const = default;
};

struct SceneHydrationSnapshot {
    std::string document_id{};
    std::uint64_t document_revision{0U};
    std::vector<SceneSource> sources{};
    std::vector<SceneGraphDefinition> scenes{};
    std::array<SceneOutputDefinition, 2U> outputs{};
    std::array<std::string, 2U> active_scene_ids{};
    std::array<bool, 2U> render_enabled{};
    std::array<bool, 2U> output_enabled{};
    std::optional<FrameChannelConfiguration> content_ingress{};
    std::optional<FrameChannelConfiguration> preview_egress{};
    std::optional<FrameChannelConfiguration> program_egress{};
    std::vector<OutputWindowConfiguration> window_targets{};

    bool operator==(const SceneHydrationSnapshot&) const = default;
};

// The Solin content frame channel is runtime transport state injected into a
// hydration snapshot, not part of the versioned scene document. All persisted
// source fields remain revision-bound.
[[nodiscard]] bool same_scene_document_source_definition(
    const SceneSource& left, const SceneSource& right) noexcept;

[[nodiscard]] SceneHydrationSnapshot
parse_scene_hydration_snapshot(const nlohmann::json& payload,
                               std::uint64_t expected_document_revision);

[[nodiscard]] SceneLayerDefinition
parse_scene_layer_definition(const nlohmann::json& value);

[[nodiscard]] std::vector<OutputWindowConfiguration>
parse_output_window_targets(const nlohmann::json& value);

} // namespace solin::media_engine
