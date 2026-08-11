#pragma once

#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <string_view>

#include <nlohmann/json.hpp>

namespace solin::media_engine {

inline constexpr std::uint64_t kControlProtocolVersion = 3U;

struct ControlEnvelope {
    std::uint64_t protocol_version{kControlProtocolVersion};
    std::string message_type{};
    std::string request_id{};
    std::string session_id{};
    std::string process_generation{};
    std::uint64_t sequence{0U};
    std::uint64_t document_revision{0U};
    std::uint64_t deadline_monotonic_ms{0U};
    nlohmann::json payload{nlohmann::json::object()};
};

struct ProtocolReply {
    std::optional<ControlEnvelope> response{};
    bool should_stop{false};
};

struct ControlServiceReply {
    std::string message_type{};
    nlohmann::json payload{nlohmann::json::object()};
};

struct ControlSessionServices {
    std::function<nlohmann::json()> capabilities{};
    std::function<nlohmann::json()> list_local_cameras{};
    std::function<nlohmann::json(const nlohmann::json&, std::uint64_t, std::uint64_t)> hydrate{};
    std::function<ControlServiceReply(std::string_view, std::string_view, std::string_view,
                                      std::uint64_t, std::uint64_t, std::string_view,
                                      std::uint64_t)>
        prepare_scene{};
    std::function<nlohmann::json(std::string_view, std::string_view, std::string_view,
                                 std::uint64_t, std::uint64_t)>
        take_prepared{};
    std::function<void(std::string_view)> cancel_preparation{};
    std::function<nlohmann::json(std::string_view, std::string_view,
                                 const nlohmann::json&, std::uint64_t,
                                 std::uint64_t)>
        preview_layer_geometry{};
    std::function<nlohmann::json(std::string_view, bool, std::uint64_t, std::uint64_t)>
        set_output_enabled{};
    std::function<nlohmann::json(std::string_view, bool, std::uint64_t, std::uint64_t)>
        set_render_enabled{};
};

[[nodiscard]] ControlEnvelope parse_control_envelope(std::string_view payload);

[[nodiscard]] std::string serialize_control_envelope(const ControlEnvelope& envelope);

class ControlSession final {
  public:
    explicit ControlSession(std::string expected_process_generation,
                            ControlSessionServices services = {});

    [[nodiscard]] ProtocolReply handle(const ControlEnvelope& request);

  private:
    std::string expected_process_generation_;
    ControlSessionServices services_{};
    std::optional<std::string> session_id_{};
};

} // namespace solin::media_engine
