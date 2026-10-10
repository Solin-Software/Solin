#include "solin/media_engine/scene_snapshot.hpp"
#include "solin/media_engine/local_camera_discovery.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <numeric>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace solin::media_engine {
namespace {

using Json = nlohmann::json;

constexpr std::uint64_t kSceneSchemaVersion = 11U;
constexpr std::size_t kMaximumSources = 256U;
constexpr std::size_t kMaximumScenes = 256U;
constexpr std::size_t kMaximumLayersPerScene = 128U;
constexpr std::size_t kMaximumCameraPresets = 512U;
constexpr std::size_t kMaximumWindowTargets = 32U;
constexpr std::string_view kNoSignalSourceId = "solin.source.no-signal";

struct ParsedScene {
    SceneGraphDefinition scene{};
    std::vector<std::string> preset_ids{};
};

struct ParsedPreset {
    std::string id{};
    std::string camera_source_id{};
};

[[noreturn]] void invalid(const char* field_name) {
    throw std::runtime_error(std::string{"invalid scene snapshot "} + field_name);
}

void require_exact_fields(const Json& value, const std::initializer_list<std::string_view> fields,
                          const char* field_name) {
    if (!value.is_object() || value.size() != fields.size() ||
        !std::ranges::all_of(
            fields, [&value](const std::string_view field) { return value.contains(field); })) {
        invalid(field_name);
    }
}

void require_array_size(const Json& value, const std::size_t maximum, const char* field_name,
                        const bool allow_empty = true) {
    if (!value.is_array() || value.size() > maximum || (!allow_empty && value.empty())) {
        invalid(field_name);
    }
}

[[nodiscard]] std::string bounded_text(const Json& value, const std::size_t maximum,
                                       const char* field_name, const bool allow_empty = false) {
    if (!value.is_string()) {
        invalid(field_name);
    }
    auto result = value.get<std::string>();
    const auto character_count = static_cast<std::size_t>(std::ranges::count_if(
        result, [](const unsigned char character) { return (character & 0xC0U) != 0x80U; }));
    if ((!allow_empty && result.empty()) || character_count > maximum ||
        std::ranges::any_of(result, [](const unsigned char character) {
            return character < 32U || character == 127U;
        })) {
        invalid(field_name);
    }
    return result;
}

void require_non_blank(const std::string_view value, const char* field_name) {
    if (std::ranges::all_of(value,
                            [](const unsigned char character) { return character == ' '; })) {
        invalid(field_name);
    }
}

[[nodiscard]] std::string identity(const Json& value, const char* field_name) {
    auto result = bounded_text(value, 128U, field_name);
    if (!((result.front() >= 'A' && result.front() <= 'Z') ||
          (result.front() >= 'a' && result.front() <= 'z') ||
          (result.front() >= '0' && result.front() <= '9')) ||
        std::ranges::any_of(result.substr(1U), [](const char character) {
            return !((character >= 'A' && character <= 'Z') ||
                     (character >= 'a' && character <= 'z') ||
                     (character >= '0' && character <= '9') || character == '.' ||
                     character == '_' || character == ':' || character == '-');
        })) {
        invalid(field_name);
    }
    return result;
}

[[nodiscard]] std::string runtime_identity(const Json& value, const char* field_name) {
    return bounded_text(value, 256U, field_name);
}

[[nodiscard]] std::uint64_t unsigned_integer(const Json& value, const std::uint64_t maximum,
                                             const char* field_name) {
    if (!value.is_number_integer()) {
        invalid(field_name);
    }
    if (value.is_number_unsigned()) {
        const auto result = value.get<std::uint64_t>();
        if (result > maximum) {
            invalid(field_name);
        }
        return result;
    }
    const auto signed_result = value.get<std::int64_t>();
    if (signed_result < 0 || static_cast<std::uint64_t>(signed_result) > maximum) {
        invalid(field_name);
    }
    return static_cast<std::uint64_t>(signed_result);
}

[[nodiscard]] std::int32_t signed_coordinate(const Json& value, const char* field_name) {
    if (!value.is_number_integer() || value.is_number_unsigned()) {
        if (value.is_number_unsigned() &&
            value.get<std::uint64_t>() <=
                static_cast<std::uint64_t>(std::numeric_limits<std::int32_t>::max())) {
            return static_cast<std::int32_t>(value.get<std::uint64_t>());
        }
        invalid(field_name);
    }
    const auto result = value.get<std::int64_t>();
    if (result < std::numeric_limits<std::int32_t>::min() ||
        result > std::numeric_limits<std::int32_t>::max()) {
        invalid(field_name);
    }
    return static_cast<std::int32_t>(result);
}

[[nodiscard]] double bounded_number(const Json& value, const double minimum, const double maximum,
                                    const char* field_name) {
    if (!value.is_number()) {
        invalid(field_name);
    }
    const auto result = value.get<double>();
    if (!std::isfinite(result) || result < minimum || result > maximum) {
        invalid(field_name);
    }
    return result;
}

[[nodiscard]] bool boolean(const Json& value, const char* field_name) {
    if (!value.is_boolean()) {
        invalid(field_name);
    }
    return value.get<bool>();
}

void require_one_of(const std::string_view value,
                    const std::initializer_list<std::string_view> allowed, const char* field_name) {
    if (std::ranges::find(allowed, value) == allowed.end()) {
        invalid(field_name);
    }
}

[[nodiscard]] bool is_hex_color(const std::string_view value) {
    return value.size() == 9U && value.front() == '#' &&
           std::ranges::all_of(value.substr(1U), [](const char character) {
               return (character >= '0' && character <= '9') ||
                      (character >= 'A' && character <= 'F') ||
                      (character >= 'a' && character <= 'f');
           });
}

[[nodiscard]] char lower_ascii(const char value) {
    return value >= 'A' && value <= 'Z' ? static_cast<char>(value - 'A' + 'a') : value;
}

[[nodiscard]] int hex_value(const char value) {
    if (value >= '0' && value <= '9') {
        return value - '0';
    }
    const auto lower = lower_ascii(value);
    return lower >= 'a' && lower <= 'f' ? lower - 'a' + 10 : -1;
}

[[nodiscard]] std::string decoded_query_key(const std::string_view value) {
    std::string result;
    result.reserve(value.size());
    for (std::size_t index = 0U; index < value.size(); ++index) {
        if (value[index] == '%' && index + 2U < value.size()) {
            const auto high = hex_value(value[index + 1U]);
            const auto low = hex_value(value[index + 2U]);
            if (high >= 0 && low >= 0) {
                result.push_back(lower_ascii(static_cast<char>((high << 4) | low)));
                index += 2U;
                continue;
            }
        }
        result.push_back(value[index] == '-' ? '_' : lower_ascii(value[index]));
    }
    return result;
}

void require_safe_uri(const std::string& uri, const std::initializer_list<std::string_view> schemes,
                      const char* field_name) {
    const auto scheme_end = uri.find("://");
    auto scheme = uri.substr(0U, scheme_end);
    std::ranges::transform(scheme, scheme.begin(), lower_ascii);
    if (scheme_end == std::string::npos || std::ranges::find(schemes, scheme) == schemes.end()) {
        invalid(field_name);
    }
    const auto authority_start = scheme_end + 3U;
    const auto authority_end = uri.find_first_of("/?#", authority_start);
    const auto authority = std::string_view{uri}.substr(
        authority_start,
        authority_end == std::string::npos ? std::string::npos : authority_end - authority_start);
    if (authority.empty() || authority.find('@') != std::string_view::npos ||
        decoded_query_key(authority).find('@') != std::string::npos) {
        invalid(field_name);
    }
    std::string_view host = authority;
    std::string_view port;
    if (authority.front() == '[') {
        const auto closing_bracket = authority.find(']');
        if (closing_bracket == std::string_view::npos || closing_bracket == 1U) {
            invalid(field_name);
        }
        host = authority.substr(1U, closing_bracket - 1U);
        const auto suffix = authority.substr(closing_bracket + 1U);
        if (!suffix.empty()) {
            if (suffix.front() != ':' || suffix.size() == 1U) {
                invalid(field_name);
            }
            port = suffix.substr(1U);
        }
    } else {
        const auto colon = authority.find(':');
        if (colon != std::string_view::npos) {
            if (colon + 1U == authority.size() ||
                authority.find(':', colon + 1U) != std::string_view::npos) {
                invalid(field_name);
            }
            host = authority.substr(0U, colon);
            port = authority.substr(colon + 1U);
        }
    }
    require_non_blank(host, field_name);
    if (host.empty() ||
        std::ranges::any_of(host, [](const unsigned char character) { return character == ' '; })) {
        invalid(field_name);
    }
    if (!port.empty()) {
        std::uint32_t parsed_port = 0U;
        for (const auto character : port) {
            if (character < '0' || character > '9') {
                invalid(field_name);
            }
            parsed_port = parsed_port * 10U + static_cast<std::uint32_t>(character - '0');
            if (parsed_port > 65'535U) {
                invalid(field_name);
            }
        }
        if (parsed_port == 0U) {
            invalid(field_name);
        }
    }
    const auto query_start = uri.find('?');
    if (query_start == std::string::npos) {
        return;
    }
    const auto fragment_start = uri.find('#', query_start + 1U);
    auto query = std::string_view{uri}.substr(
        query_start + 1U, fragment_start == std::string::npos ? std::string::npos
                                                              : fragment_start - query_start - 1U);
    constexpr std::array<std::string_view, 13U> sensitive{
        "password", "passwd",     "secret", "token",         "username",   "user",      "api_key",
        "apikey",   "access_key", "auth",   "authorization", "credential", "signature",
    };
    while (!query.empty()) {
        const auto separator = query.find_first_of("&;");
        const auto item = query.substr(0U, separator);
        const auto key = decoded_query_key(item.substr(0U, item.find('=')));
        if (std::ranges::any_of(sensitive, [&key](const std::string_view fragment) {
                return key.find(fragment) != std::string::npos;
            })) {
            invalid(field_name);
        }
        if (separator == std::string_view::npos) {
            break;
        }
        query.remove_prefix(separator + 1U);
    }
}

void validate_ptz_binding(const Json& value) {
    if (value.is_null()) {
        return;
    }
    if (!value.is_object() || !value.contains("protocol")) {
        invalid("PTZ binding");
    }
    const auto protocol = bounded_text(value.at("protocol"), 32U, "PTZ protocol");
    if (protocol == "onvif") {
        require_exact_fields(value, {"protocol", "endpoint", "profile_token", "credential_ref"},
                             "ONVIF binding");
        const auto endpoint = bounded_text(value.at("endpoint"), 2048U, "ONVIF endpoint");
        require_safe_uri(endpoint, {"http", "https"}, "ONVIF endpoint");
        static_cast<void>(
            bounded_text(value.at("profile_token"), 512U, "ONVIF profile token", true));
        static_cast<void>(
            bounded_text(value.at("credential_ref"), 256U, "ONVIF credential reference", true));
        return;
    }
    if (protocol == "visca_ip") {
        require_exact_fields(value, {"protocol", "host", "port", "transport"}, "VISCA IP binding");
        const auto host = bounded_text(value.at("host"), 253U, "VISCA IP host");
        require_non_blank(host, "VISCA IP host");
        const auto port = unsigned_integer(value.at("port"), 65'535U, "VISCA IP port");
        if (port == 0U) {
            invalid("VISCA IP port");
        }
        const auto transport = bounded_text(value.at("transport"), 8U, "VISCA transport");
        require_one_of(transport, {"tcp", "udp"}, "VISCA transport");
        return;
    }
    if (protocol == "visca_serial") {
        require_exact_fields(value, {"protocol", "device_id", "baud_rate", "camera_address"},
                             "VISCA serial binding");
        static_cast<void>(bounded_text(value.at("device_id"), 1024U, "VISCA serial device"));
        const auto baud_rate =
            unsigned_integer(value.at("baud_rate"), 115'200U, "VISCA serial baud rate");
        if (baud_rate != 9'600U && baud_rate != 19'200U && baud_rate != 38'400U &&
            baud_rate != 115'200U) {
            invalid("VISCA serial baud rate");
        }
        const auto address =
            unsigned_integer(value.at("camera_address"), 7U, "VISCA camera address");
        if (address == 0U) {
            invalid("VISCA camera address");
        }
        return;
    }
    invalid("PTZ protocol");
}

[[nodiscard]] LocalCameraSourceConfiguration parse_local_camera(const Json& value) {
    require_exact_fields(value,
                         {"device_id", "width", "height", "fps_numerator", "fps_denominator",
                          "media_type", "pixel_format", "ptz_binding", "keep_active"},
                         "local camera configuration");
    LocalCameraSourceConfiguration result{
        .device_id = bounded_text(value.at("device_id"), 1024U, "local camera device", true),
        .width = static_cast<std::uint32_t>(
            unsigned_integer(value.at("width"), 3'840U, "local camera width")),
        .height = static_cast<std::uint32_t>(
            unsigned_integer(value.at("height"), 3'840U, "local camera height")),
        .fps_numerator = static_cast<std::uint32_t>(
            unsigned_integer(value.at("fps_numerator"), kMaximumCameraFpsComponent,
                             "local camera FPS numerator")),
        .fps_denominator = static_cast<std::uint32_t>(unsigned_integer(
            value.at("fps_denominator"), kMaximumCameraFpsComponent,
            "local camera FPS denominator")),
        .media_type = bounded_text(value.at("media_type"), 80U, "local camera media type", true),
        .pixel_format =
            bounded_text(value.at("pixel_format"), 80U, "local camera pixel format", true),
        .has_ptz_binding = !value.at("ptz_binding").is_null(),
        .keep_active = value.at("keep_active").get<bool>(),
    };
    const bool automatic_format = result.width == 0U && result.fps_numerator == 0U &&
                                  result.media_type.empty() && result.pixel_format.empty();
    const bool exact_format = result.width > 0U && result.height > 0U &&
                              result.fps_numerator > 0U && !result.media_type.empty() &&
                              !result.pixel_format.empty();
    if ((result.width == 0U) != (result.height == 0U) || result.fps_denominator == 0U ||
        (result.fps_numerator == 0U && result.fps_denominator != 1U) ||
        (result.width > 0U &&
         (static_cast<std::uint64_t>(result.width) * result.height > 3'840ULL * 2'160ULL ||
          (std::min)(result.width, result.height) > 2'160U)) ||
        (result.fps_numerator > 0U &&
         (static_cast<std::uint64_t>(result.fps_numerator) >
              60ULL * result.fps_denominator ||
          std::gcd(result.fps_numerator, result.fps_denominator) != 1U)) ||
        result.media_type.empty() != result.pixel_format.empty() ||
        (!automatic_format && !exact_format)) {
        invalid("local camera format");
    }
    if (!result.media_type.empty()) {
        require_one_of(result.media_type, {"video/x-raw", "image/jpeg", "video/x-h264"},
                       "local camera media type");
    }
    validate_ptz_binding(value.at("ptz_binding"));
    return result;
}

[[nodiscard]] RtspCameraSourceConfiguration parse_rtsp_camera(const Json& value) {
    require_exact_fields(value, {"uri", "transport", "latency_ms", "ptz_binding",
                                 "keep_active"},
                         "RTSP camera configuration");
    auto uri = bounded_text(value.at("uri"), 2048U, "RTSP URI");
    require_safe_uri(uri, {"rtsp", "rtsps"}, "RTSP URI");
    const auto transport = bounded_text(value.at("transport"), 8U, "RTSP transport");
    require_one_of(transport, {"tcp", "udp"}, "RTSP transport");
    validate_ptz_binding(value.at("ptz_binding"));
    return {
        .uri = std::move(uri),
        .use_tcp = transport == "tcp",
        .latency_ms = static_cast<std::uint32_t>(
            unsigned_integer(value.at("latency_ms"), 10'000U, "RTSP latency")),
        .has_ptz_binding = !value.at("ptz_binding").is_null(),
        .keep_active = value.at("keep_active").get<bool>(),
    };
}

[[nodiscard]] SceneSource parse_source(const Json& value) {
    require_exact_fields(
        value, {"id", "type", "name", "enabled", "configuration", "credential_ref"}, "source");
    SceneSource result{
        .id = identity(value.at("id"), "source id"),
        .enabled = boolean(value.at("enabled"), "source enabled"),
    };
    const auto source_name = bounded_text(value.at("name"), 120U, "source name");
    require_non_blank(source_name, "source name");
    static_cast<void>(
        bounded_text(value.at("credential_ref"), 256U, "source credential reference", true));
    const auto kind = bounded_text(value.at("type"), 32U, "source kind");
    const auto& configuration = value.at("configuration");
    if (kind == "solin_content") {
        require_exact_fields(configuration, {}, "Solin content configuration");
        result.kind = SceneSourceKind::solin_content;
    } else if (kind == "idle_screen") {
        require_exact_fields(configuration, {}, "idle screen configuration");
        result.kind = SceneSourceKind::idle_screen;
    } else if (kind == "local_camera") {
        result.kind = SceneSourceKind::local_camera;
        result.configuration = parse_local_camera(configuration);
    } else if (kind == "rtsp_camera") {
        result.kind = SceneSourceKind::rtsp_camera;
        result.configuration = parse_rtsp_camera(configuration);
    } else if (kind == "image") {
        require_exact_fields(configuration, {"asset_id"}, "image configuration");
        result.kind = SceneSourceKind::image;
        result.configuration = ImageSourceConfiguration{
            .asset_id = identity(configuration.at("asset_id"), "image asset id"),
        };
    } else if (kind == "color") {
        require_exact_fields(configuration, {"color"}, "color configuration");
        auto color = bounded_text(configuration.at("color"), 9U, "source color");
        if (!is_hex_color(color)) {
            invalid("source color");
        }
        result.kind = SceneSourceKind::color;
        result.configuration = ColorSourceConfiguration{.color = std::move(color)};
    } else if (kind == "scene_reference") {
        require_exact_fields(configuration, {"target_scene_id"}, "scene reference configuration");
        result.kind = SceneSourceKind::scene_reference;
        result.configuration = SceneReferenceSourceConfiguration{
            .target_scene_id = identity(configuration.at("target_scene_id"), "referenced scene id"),
        };
    } else {
        invalid("source kind");
    }
    return result;
}

[[nodiscard]] SceneLayerDefinition parse_layer(const Json& value) {
    require_exact_fields(value,
                         {"id", "source_id", "name", "rect", "crop", "rotation_degrees", "fit_mode",
                          "opacity", "visible", "locked", "mirror_x", "mirror_y", "border_color",
                          "border_width", "corner_radius"},
                         "layer");
    const auto& rect = value.at("rect");
    const auto& crop = value.at("crop");
    require_exact_fields(rect, {"x", "y", "width", "height"}, "layer rect");
    require_exact_fields(crop, {"left", "top", "right", "bottom"}, "layer crop");
    auto fit_mode = bounded_text(value.at("fit_mode"), 16U, "layer fit mode");
    require_one_of(fit_mode, {"contain", "cover", "stretch"}, "layer fit mode");
    auto border_color = bounded_text(value.at("border_color"), 9U, "layer border color");
    if (!is_hex_color(border_color)) {
        invalid("layer border color");
    }
    const auto crop_left = bounded_number(crop.at("left"), 0.0, 0.99, "layer crop left");
    const auto crop_top = bounded_number(crop.at("top"), 0.0, 0.99, "layer crop top");
    const auto crop_right = bounded_number(crop.at("right"), 0.0, 0.99, "layer crop right");
    const auto crop_bottom = bounded_number(crop.at("bottom"), 0.0, 0.99, "layer crop bottom");
    if (crop_left + crop_right >= 1.0 || crop_top + crop_bottom >= 1.0) {
        invalid("layer crop");
    }
    const auto layer_name = bounded_text(value.at("name"), 120U, "layer name");
    require_non_blank(layer_name, "layer name");
    static_cast<void>(boolean(value.at("locked"), "layer locked"));
    return {
        .id = identity(value.at("id"), "layer id"),
        .source_id = identity(value.at("source_id"), "layer source id"),
        .geometry =
            {
                .x = bounded_number(rect.at("x"), -2.0, 2.0, "layer x"),
                .y = bounded_number(rect.at("y"), -2.0, 2.0, "layer y"),
                .width = bounded_number(rect.at("width"), 0.001, 4.0, "layer width"),
                .height = bounded_number(rect.at("height"), 0.001, 4.0, "layer height"),
                .crop_left = crop_left,
                .crop_top = crop_top,
                .crop_right = crop_right,
                .crop_bottom = crop_bottom,
                .rotation_degrees =
                    bounded_number(value.at("rotation_degrees"), -360.0, 360.0, "layer rotation"),
                .opacity = bounded_number(value.at("opacity"), 0.0, 1.0, "layer opacity"),
                .border_width =
                    bounded_number(value.at("border_width"), 0.0, 0.1, "layer border width"),
                .corner_radius =
                    bounded_number(value.at("corner_radius"), 0.0, 0.5, "layer corner radius"),
                .fit_mode = std::move(fit_mode),
                .border_color = std::move(border_color),
                .visible = boolean(value.at("visible"), "layer visible"),
                .mirror_x = boolean(value.at("mirror_x"), "layer mirror x"),
                .mirror_y = boolean(value.at("mirror_y"), "layer mirror y"),
            },
    };
}

[[nodiscard]] ParsedScene parse_scene(const Json& value) {
    require_exact_fields(
        value, {"id", "name", "layers", "entry_actions", "created_at", "updated_at"}, "scene");
    require_array_size(value.at("layers"), kMaximumLayersPerScene, "scene layers");
    require_array_size(value.at("entry_actions"), kMaximumCameraPresets, "scene entry actions");
    ParsedScene result;
    result.scene.id = identity(value.at("id"), "scene id");
    const auto scene_name = bounded_text(value.at("name"), 120U, "scene name");
    require_non_blank(scene_name, "scene name");
    static_cast<void>(bounded_text(value.at("created_at"), 64U, "scene creation time"));
    static_cast<void>(bounded_text(value.at("updated_at"), 64U, "scene update time"));
    std::unordered_set<std::string> layer_ids;
    result.scene.layers.reserve(value.at("layers").size());
    for (const auto& raw_layer : value.at("layers")) {
        auto layer = parse_layer(raw_layer);
        if (!layer_ids.insert(layer.id).second) {
            invalid("duplicate layer id");
        }
        result.scene.layers.push_back(std::move(layer));
    }
    std::unordered_set<std::string> preset_ids;
    for (const auto& action : value.at("entry_actions")) {
        require_exact_fields(action, {"type", "preset_id", "timeout_ms", "on_timeout"},
                             "scene entry action");
        if (bounded_text(action.at("type"), 32U, "scene entry action type") !=
            "recall_ptz_preset") {
            invalid("scene entry action type");
        }
        auto preset_id = identity(action.at("preset_id"), "scene entry action preset");
        if (!preset_ids.insert(preset_id).second) {
            invalid("duplicate scene entry action preset");
        }
        const auto timeout =
            unsigned_integer(action.at("timeout_ms"), 10'000U, "scene entry action timeout");
        if (timeout < 500U) {
            invalid("scene entry action timeout");
        }
        const auto policy =
            bounded_text(action.at("on_timeout"), 32U, "scene entry action timeout policy");
        require_one_of(policy, {"keep_current", "take_anyway"},
                       "scene entry action timeout policy");
        result.preset_ids.push_back(std::move(preset_id));
    }
    return result;
}

[[nodiscard]] OutputBus output_bus(const Json& value, const char* field_name) {
    const auto bus = bounded_text(value, 32U, field_name);
    if (bus == "media_windows") {
        return OutputBus::media_windows;
    }
    if (bus == "virtual_camera") {
        return OutputBus::virtual_camera;
    }
    invalid(field_name);
}

[[nodiscard]] std::size_t bus_index(const OutputBus bus) { return static_cast<std::size_t>(bus); }

[[nodiscard]] OutputVideoFormat parse_output_video_format(const Json& value) {
    require_exact_fields(value,
                         {"width", "height", "fps_numerator", "fps_denominator", "pixel_format",
                          "color_space", "color_range"},
                         "output video format");
    OutputVideoFormat result{
        .width =
            static_cast<std::uint32_t>(unsigned_integer(value.at("width"), 7'680U, "output width")),
        .height = static_cast<std::uint32_t>(
            unsigned_integer(value.at("height"), 4'320U, "output height")),
        .fps_numerator = static_cast<std::uint32_t>(
            unsigned_integer(value.at("fps_numerator"), 240'000U, "output FPS numerator")),
        .fps_denominator = static_cast<std::uint32_t>(
            unsigned_integer(value.at("fps_denominator"), 1'001U, "output FPS denominator")),
        .pixel_format = bounded_text(value.at("pixel_format"), 16U, "output pixel format"),
        .color_space = bounded_text(value.at("color_space"), 16U, "output color space"),
        .color_range = bounded_text(value.at("color_range"), 16U, "output color range"),
    };
    if (result.width < 320U || result.height < 180U || result.fps_numerator == 0U ||
        result.fps_denominator == 0U || result.fps_numerator < result.fps_denominator ||
        result.fps_numerator > 240U * result.fps_denominator) {
        invalid("output video format");
    }
    require_one_of(result.pixel_format, {"bgra", "nv12"}, "output pixel format");
    require_one_of(result.color_space, {"srgb", "bt709"}, "output color space");
    require_one_of(result.color_range, {"full", "limited"}, "output color range");
    return result;
}

[[nodiscard]] SceneOutputDefinition parse_output(const Json& value) {
    require_exact_fields(value,
                         {"bus_id", "default_scene_id", "start_with_solin", "video_format"},
                         "output");
    return {
        .bus = output_bus(value.at("bus_id"), "output bus"),
        .default_scene_id =
            bounded_text(value.at("default_scene_id"), 128U, "output default scene", true),
        .start_with_solin = boolean(value.at("start_with_solin"), "output startup state"),
        .video_format = parse_output_video_format(value.at("video_format")),
    };
}

[[nodiscard]] ParsedPreset parse_preset(const Json& value) {
    require_exact_fields(
        value,
        {"id", "camera_source_id", "name", "remote_token", "position", "created_at", "updated_at"},
        "camera preset");
    ParsedPreset result{
        .id = identity(value.at("id"), "camera preset id"),
        .camera_source_id = identity(value.at("camera_source_id"), "camera preset source"),
    };
    const auto preset_name = bounded_text(value.at("name"), 120U, "camera preset name");
    require_non_blank(preset_name, "camera preset name");
    const auto remote_token =
        bounded_text(value.at("remote_token"), 512U, "camera preset token", true);
    static_cast<void>(bounded_text(value.at("created_at"), 64U, "camera preset creation time"));
    static_cast<void>(bounded_text(value.at("updated_at"), 64U, "camera preset update time"));
    const auto& position = value.at("position");
    if (!position.is_null()) {
        require_exact_fields(position, {"pan", "tilt", "zoom"}, "camera preset position");
        static_cast<void>(bounded_number(position.at("pan"), -1.0, 1.0, "camera preset pan"));
        static_cast<void>(bounded_number(position.at("tilt"), -1.0, 1.0, "camera preset tilt"));
        static_cast<void>(bounded_number(position.at("zoom"), 0.0, 1.0, "camera preset zoom"));
    }
    if (remote_token.empty() && position.is_null()) {
        invalid("camera preset target");
    }
    return result;
}

void validate_automation(const Json& value, const std::unordered_set<std::string>& scene_ids,
                         std::array<bool, 2U>& seen_buses) {
    require_exact_fields(value, {"bus_id", "assignments"}, "automation map");
    const auto bus = output_bus(value.at("bus_id"), "automation bus");
    const auto index = bus_index(bus);
    if (seen_buses[index]) {
        invalid("duplicate automation bus");
    }
    seen_buses[index] = true;
    const auto& assignments = value.at("assignments");
    if (!assignments.is_object() || assignments.size() > 7U) {
        invalid("automation assignments");
    }
    constexpr std::array<std::string_view, 7U> categories{
        "idle", "image", "video", "timer", "browser", "external_stream", "camera",
    };
    for (const auto& [category, raw_scene_id] : assignments.items()) {
        if (std::ranges::find(categories, category) == categories.end()) {
            invalid("automation category");
        }
        const auto scene_id = identity(raw_scene_id, "automation scene");
        if (!scene_ids.contains(scene_id)) {
            invalid("automation scene reference");
        }
    }
}

[[nodiscard]] FrameChannelConfiguration
parse_frame_channel(const Json& value, const std::string_view expected_producer) {
    require_exact_fields(value,
                         {"channel_id", "generation", "producer_kind", "transport", "handle_token",
                          "width", "height", "pixel_format", "color_space", "color_range"},
                         "frame channel");
    FrameChannelConfiguration result{
        .channel_id = runtime_identity(value.at("channel_id"), "frame channel id"),
        .generation =
            unsigned_integer(value.at("generation"),
                             static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()),
                             "frame channel generation"),
        .producer_kind = bounded_text(value.at("producer_kind"), 32U, "frame channel producer"),
        .transport = bounded_text(value.at("transport"), 32U, "frame channel transport"),
        .handle_token = runtime_identity(value.at("handle_token"), "frame channel handle"),
        .width = static_cast<std::uint32_t>(
            unsigned_integer(value.at("width"), 7'680U, "frame channel width")),
        .height = static_cast<std::uint32_t>(
            unsigned_integer(value.at("height"), 4'320U, "frame channel height")),
        .pixel_format = bounded_text(value.at("pixel_format"), 16U, "frame channel pixel format"),
        .color_space = bounded_text(value.at("color_space"), 16U, "frame channel color space"),
        .color_range = bounded_text(value.at("color_range"), 16U, "frame channel color range"),
    };
    if (result.width == 0U || result.height == 0U) {
        invalid("frame channel dimensions");
    }
    require_one_of(result.producer_kind, {expected_producer}, "frame channel producer");
    require_one_of(result.transport,
                   {"d3d11_shared_texture", "shared_memory_bgra", "shared_memory_video"},
                   "frame channel transport");
    require_one_of(result.pixel_format, {"dynamic", "bgra", "nv12"}, "frame channel pixel format");
    require_one_of(result.color_space, {"srgb", "bt709"}, "frame channel color space");
    require_one_of(result.color_range, {"full", "limited"}, "frame channel color range");
    const auto fixed_bgra = result.transport == "shared_memory_bgra" &&
                            result.pixel_format == "bgra";
    const auto dynamic_video =
        (result.transport == "shared_memory_video" ||
         result.transport == "d3d11_shared_texture") &&
        result.pixel_format == "dynamic";
    const auto fixed_d3d11 =
        result.transport == "d3d11_shared_texture" &&
        (result.pixel_format == "bgra" || result.pixel_format == "nv12");
    if (!fixed_bgra && !dynamic_video && !fixed_d3d11) {
        invalid("frame channel dynamic pixel format");
    }
    return result;
}

[[nodiscard]] OutputWindowConfiguration parse_window_target(const Json& value) {
    require_exact_fields(value,
                         {"bus_id", "target_id", "screen_id", "native_handle", "x", "y",
                          "width", "height", "device_pixel_ratio", "visible"},
                         "window target");
    const auto width = unsigned_integer(value.at("width"), 32'768U, "window width");
    const auto height = unsigned_integer(value.at("height"), 32'768U, "window height");
    const auto native_handle =
        unsigned_integer(value.at("native_handle"), (std::numeric_limits<std::uint64_t>::max)(),
                         "window native handle");
    if (native_handle == 0U || width == 0U || height == 0U) {
        invalid("window dimensions");
    }
    return {
        .bus = output_bus(value.at("bus_id"), "window target bus"),
        .target_id = runtime_identity(value.at("target_id"), "window target id"),
        .screen_id = runtime_identity(value.at("screen_id"), "window screen id"),
        .native_handle = native_handle,
        .x = signed_coordinate(value.at("x"), "window x"),
        .y = signed_coordinate(value.at("y"), "window y"),
        .width = static_cast<std::uint32_t>(width),
        .height = static_cast<std::uint32_t>(height),
        .device_pixel_ratio =
            bounded_number(value.at("device_pixel_ratio"), 0.5, 8.0, "window device pixel ratio"),
        .visible = boolean(value.at("visible"), "window visibility"),
    };
}

void validate_scene_reference_graph(
    const std::vector<SceneGraphDefinition>& scenes,
    const std::unordered_map<std::string, std::string>& reference_target_by_source) {
    std::unordered_map<std::string, std::vector<std::string>> graph;
    for (const auto& scene : scenes) {
        auto& targets = graph[scene.id];
        for (const auto& layer : scene.layers) {
            const auto target = reference_target_by_source.find(layer.source_id);
            if (target != reference_target_by_source.end()) {
                targets.push_back(target->second);
            }
        }
    }
    std::unordered_map<std::string, std::uint8_t> state;
    const auto visit = [&graph, &state](const auto& self, const std::string& scene_id) -> void {
        const auto current = state[scene_id];
        if (current == 1U) {
            invalid("scene reference cycle");
        }
        if (current == 2U) {
            return;
        }
        state[scene_id] = 1U;
        for (const auto& target : graph[scene_id]) {
            self(self, target);
        }
        state[scene_id] = 2U;
    };
    for (const auto& scene : scenes) {
        visit(visit, scene.id);
    }
}

} // namespace

bool same_scene_document_source_definition(const SceneSource& left,
                                           const SceneSource& right) noexcept {
    if (left.id != right.id || left.kind != right.kind ||
        left.enabled != right.enabled || left.configuration != right.configuration) {
        return false;
    }
    if (left.kind == SceneSourceKind::solin_content &&
        left.id == kSolinContentSourceId) {
        return true;
    }
    return left.frame_channel == right.frame_channel;
}

SceneHydrationSnapshot
parse_scene_hydration_snapshot(const Json& payload,
                               const std::uint64_t expected_document_revision) {
    require_exact_fields(payload,
                         {"document", "active_scenes", "render_enabled", "output_enabled",
                          "content_ingress", "preview_egress", "program_egress",
                          "window_targets", "idle_screen"},
                         "hydrate payload");
    const auto& idle = payload.at("idle_screen");
    require_exact_fields(idle,
                         {"revision", "media_path", "yeartext_image_path", "yeartext_revision"},
                         "idle screen state");
    for (const auto key : {"revision", "yeartext_revision"}) {
        static_cast<void>(unsigned_integer(idle.at(key), std::numeric_limits<std::uint64_t>::max(),
                                           "idle screen revision"));
    }
    for (const auto key : {"media_path", "yeartext_image_path"}) {
        const auto path = bounded_text(idle.at(key), 4096U, "idle screen local path", true);
        if (path.find("://") != std::string::npos) {
            invalid("idle screen local path");
        }
    }
    const auto& document = payload.at("document");
    require_exact_fields(document,
                         {"schema_version", "document_id", "revision", "sources", "scenes",
                          "outputs", "automation", "camera_presets"},
                         "document");
    if (unsigned_integer(document.at("schema_version"), std::numeric_limits<std::uint32_t>::max(),
                         "schema version") != kSceneSchemaVersion) {
        invalid("schema version");
    }
    SceneHydrationSnapshot result{
        .document_id = identity(document.at("document_id"), "document id"),
        .document_revision =
            unsigned_integer(document.at("revision"),
                             static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()),
                             "document revision"),
    };
    if (result.document_revision != expected_document_revision) {
        invalid("document revision mismatch");
    }

    require_array_size(document.at("sources"), kMaximumSources, "sources");
    std::unordered_map<std::string, SceneSourceKind> source_kind_by_id;
    std::unordered_map<std::string, std::string> reference_target_by_source;
    result.sources.reserve(document.at("sources").size());
    std::size_t content_source_count = 0U;
    for (const auto& raw_source : document.at("sources")) {
        auto source = parse_source(raw_source);
        if (!source_kind_by_id.emplace(source.id, source.kind).second) {
            invalid("duplicate source id");
        }
        if (source.kind == SceneSourceKind::solin_content) {
            ++content_source_count;
        } else if (source.kind == SceneSourceKind::scene_reference) {
            reference_target_by_source.emplace(
                source.id,
                std::get<SceneReferenceSourceConfiguration>(source.configuration).target_scene_id);
        }
        result.sources.push_back(std::move(source));
    }
    const auto content_source = source_kind_by_id.find(std::string{kSolinContentSourceId});
    const auto no_signal_source = source_kind_by_id.find(std::string{kNoSignalSourceId});
    if (content_source_count != 1U || content_source == source_kind_by_id.end() ||
        content_source->second != SceneSourceKind::solin_content ||
        no_signal_source == source_kind_by_id.end() ||
        no_signal_source->second != SceneSourceKind::color) {
        invalid("reserved sources");
    }

    require_array_size(document.at("scenes"), kMaximumScenes, "scenes", false);
    std::unordered_set<std::string> scene_ids;
    std::vector<ParsedScene> parsed_scenes;
    parsed_scenes.reserve(document.at("scenes").size());
    result.scenes.reserve(document.at("scenes").size());
    for (const auto& raw_scene : document.at("scenes")) {
        auto parsed = parse_scene(raw_scene);
        if (!scene_ids.insert(parsed.scene.id).second) {
            invalid("duplicate scene id");
        }
        for (const auto& layer : parsed.scene.layers) {
            if (!source_kind_by_id.contains(layer.source_id)) {
                invalid("layer source reference");
            }
        }
        parsed_scenes.push_back(std::move(parsed));
        result.scenes.push_back(parsed_scenes.back().scene);
    }
    for (const auto& [source_id, target_scene_id] : reference_target_by_source) {
        static_cast<void>(source_id);
        if (!scene_ids.contains(target_scene_id)) {
            invalid("scene reference target");
        }
    }
    validate_scene_reference_graph(result.scenes, reference_target_by_source);

    require_array_size(document.at("camera_presets"), kMaximumCameraPresets, "camera presets");
    std::unordered_map<std::string, std::string> preset_camera_by_id;
    for (const auto& raw_preset : document.at("camera_presets")) {
        auto preset = parse_preset(raw_preset);
        const auto source = source_kind_by_id.find(preset.camera_source_id);
        if (source == source_kind_by_id.end() ||
            (source->second != SceneSourceKind::local_camera &&
             source->second != SceneSourceKind::rtsp_camera) ||
            !preset_camera_by_id.emplace(preset.id, preset.camera_source_id).second) {
            invalid("camera preset source");
        }
        const auto configured_source =
            std::ranges::find_if(result.sources, [&preset](const SceneSource& candidate) {
                return candidate.id == preset.camera_source_id;
            });
        const bool has_ptz =
            configured_source->kind == SceneSourceKind::local_camera
                ? std::get<LocalCameraSourceConfiguration>(configured_source->configuration)
                      .has_ptz_binding
                : std::get<RtspCameraSourceConfiguration>(configured_source->configuration)
                      .has_ptz_binding;
        if (!has_ptz) {
            invalid("camera preset PTZ binding");
        }
    }
    for (const auto& scene : parsed_scenes) {
        std::unordered_set<std::string> action_camera_ids;
        for (const auto& preset_id : scene.preset_ids) {
            const auto preset = preset_camera_by_id.find(preset_id);
            if (preset == preset_camera_by_id.end() ||
                !action_camera_ids.insert(preset->second).second) {
                invalid("scene entry action preset reference");
            }
        }
    }

    require_array_size(document.at("outputs"), 2U, "outputs", false);
    if (document.at("outputs").size() != 2U) {
        invalid("outputs");
    }
    std::array<bool, 2U> seen_output_buses{};
    for (const auto& raw_output : document.at("outputs")) {
        auto output = parse_output(raw_output);
        const auto index = bus_index(output.bus);
        if (seen_output_buses[index] ||
            (!output.default_scene_id.empty() && !scene_ids.contains(output.default_scene_id))) {
            invalid("output scene reference");
        }
        seen_output_buses[index] = true;
        result.outputs[index] = std::move(output);
    }
    if (!std::ranges::all_of(seen_output_buses, [](const bool value) { return value; })) {
        invalid("output buses");
    }

    require_array_size(document.at("automation"), 2U, "automation", false);
    if (document.at("automation").size() != 2U) {
        invalid("automation");
    }
    std::array<bool, 2U> seen_automation_buses{};
    for (const auto& mapping : document.at("automation")) {
        validate_automation(mapping, scene_ids, seen_automation_buses);
    }
    if (!std::ranges::all_of(seen_automation_buses, [](const bool value) { return value; })) {
        invalid("automation buses");
    }

    const auto& active_scenes = payload.at("active_scenes");
    const auto& render_enabled = payload.at("render_enabled");
    const auto& output_enabled = payload.at("output_enabled");
    require_exact_fields(active_scenes, {"media_windows", "virtual_camera"}, "active scenes");
    require_exact_fields(render_enabled, {"media_windows", "virtual_camera"},
                         "render enabled");
    require_exact_fields(output_enabled, {"media_windows", "virtual_camera"}, "output enabled");
    constexpr std::array<std::string_view, 2U> bus_names{"media_windows", "virtual_camera"};
    for (std::size_t index = 0U; index < bus_names.size(); ++index) {
        result.active_scene_ids[index] =
            identity(active_scenes.at(bus_names[index]), "active scene");
        if (!scene_ids.contains(result.active_scene_ids[index])) {
            invalid("active scene reference");
        }
        result.output_enabled[index] =
            boolean(output_enabled.at(bus_names[index]), "output enabled state");
        result.render_enabled[index] =
            boolean(render_enabled.at(bus_names[index]), "render enabled state");
    }

    const auto& content_ingress = payload.at("content_ingress");
    if (!content_ingress.is_null()) {
        result.content_ingress = parse_frame_channel(content_ingress, "solin_offscreen");
        const auto source = std::ranges::find_if(result.sources, [](const SceneSource& candidate) {
            return candidate.id == kSolinContentSourceId;
        });
        source->frame_channel = result.content_ingress;
    }
    const auto& preview_egress = payload.at("preview_egress");
    if (!preview_egress.is_null()) {
        result.preview_egress = parse_frame_channel(preview_egress, "native_compositor");
    }
    const auto& program_egress = payload.at("program_egress");
    if (!program_egress.is_null()) {
        result.program_egress = parse_frame_channel(program_egress, "native_compositor");
    }
    result.window_targets = parse_output_window_targets(payload.at("window_targets"));
    return result;
}

SceneLayerDefinition parse_scene_layer_definition(const Json& value) {
    return parse_layer(value);
}

std::vector<OutputWindowConfiguration> parse_output_window_targets(const Json& value) {
    require_array_size(value, kMaximumWindowTargets, "window targets");
    std::unordered_set<std::string> target_ids;
    std::vector<OutputWindowConfiguration> result;
    result.reserve(value.size());
    for (const auto& raw_target : value) {
        auto target = parse_window_target(raw_target);
        if (!target_ids.insert(target.target_id).second) {
            invalid("duplicate window target id");
        }
        result.push_back(std::move(target));
    }
    return result;
}

} // namespace solin::media_engine
