#pragma once

#include "solin/media_engine/scene_snapshot.hpp"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stop_token>
#include <string>
#include <string_view>
#include <vector>

namespace solin::media_engine {

enum class SourceRuntimeStatus : std::uint8_t {
    starting,
    ready,
    degraded,
    failed,
    stopped,
};

enum class SourceFrameMemory : std::uint8_t {
    system_memory,
    d3d11,
};

class SourceFramePayload {
  public:
    virtual ~SourceFramePayload() = default;

    SourceFramePayload(const SourceFramePayload&) = delete;
    SourceFramePayload& operator=(const SourceFramePayload&) = delete;
    SourceFramePayload(SourceFramePayload&&) = delete;
    SourceFramePayload& operator=(SourceFramePayload&&) = delete;

  protected:
    SourceFramePayload() = default;
};

struct SourceFrame {
    std::uint64_t sequence{0U};
    std::uint64_t stream_epoch{0U};
    // Projection identity carried only by the canonical Solin content source.
    // It is intentionally distinct from stream_epoch, which tracks source
    // reconnect/device generations.
    std::uint64_t media_epoch{0U};
    bool discontinuity{false};
    std::uint64_t presentation_timestamp_ns{0U};
    std::uint64_t duration_ns{0U};
    std::uint64_t received_monotonic_ns{0U};
    std::uint32_t width{0U};
    std::uint32_t height{0U};
    std::string pixel_format{};
    SourceFrameMemory memory{SourceFrameMemory::system_memory};
    std::shared_ptr<const SourceFramePayload> payload{};
};

struct SourceRuntimeHealth {
    SourceRuntimeStatus status{SourceRuntimeStatus::stopped};
    std::string error_code{};
    std::uint64_t frame_sequence{0U};
    std::uint64_t dropped_frames{0U};
    std::uint64_t reconnect_count{0U};

    bool operator==(const SourceRuntimeHealth&) const = default;
};

class SourceRuntime {
  public:
    virtual ~SourceRuntime() = default;

    SourceRuntime(const SourceRuntime&) = delete;
    SourceRuntime& operator=(const SourceRuntime&) = delete;
    SourceRuntime(SourceRuntime&&) = delete;
    SourceRuntime& operator=(SourceRuntime&&) = delete;

    virtual void start() = 0;
    virtual void stop() noexcept = 0;
    [[nodiscard]] virtual SourceRuntimeHealth health() const = 0;
    [[nodiscard]] virtual std::shared_ptr<const SourceFrame> latest_frame() const {
        return {};
    }
    [[nodiscard]] virtual bool wait_for_frame(
        std::uint64_t after_sequence, std::stop_token stop_token,
        std::chrono::steady_clock::time_point deadline) const noexcept {
        static_cast<void>(after_sequence);
        static_cast<void>(stop_token);
        static_cast<void>(deadline);
        return false;
    }
    virtual void wake_frame_waiters() noexcept {}

  protected:
    SourceRuntime() = default;
};

class SourceRuntimeFactory {
  public:
    virtual ~SourceRuntimeFactory() = default;

    SourceRuntimeFactory(const SourceRuntimeFactory&) = delete;
    SourceRuntimeFactory& operator=(const SourceRuntimeFactory&) = delete;
    SourceRuntimeFactory(SourceRuntimeFactory&&) = delete;
    SourceRuntimeFactory& operator=(SourceRuntimeFactory&&) = delete;

    [[nodiscard]] virtual std::shared_ptr<SourceRuntime>
    create(const SceneSource& source, std::uint64_t generation) = 0;

  protected:
    SourceRuntimeFactory() = default;
};

struct SourceRegistryEntry {
    std::string source_id{};
    std::uint64_t generation{0U};
    bool enabled{false};
    bool current{true};
    std::size_t consumer_count{0U};
    SourceRuntimeHealth health{};

    bool operator==(const SourceRegistryEntry&) const = default;
};

struct SourceRegistryLimits {
    std::size_t maximum_runtime_slots{32U};
    std::size_t maximum_consumers_per_runtime{64U};
    std::uint64_t maximum_active_frame_pixels{8ULL * 3'840ULL * 2'160ULL};
    std::size_t maximum_decoder_sessions{8U};
    std::size_t maximum_retained_idle_cameras{0U};

    bool operator==(const SourceRegistryLimits&) const = default;
};

namespace detail {
class SourceRegistryState;
class SourceRuntimeSlot;
class SourceRegistryUpdateState;
} // namespace detail

class SourceLease final {
  public:
    ~SourceLease();

    SourceLease(const SourceLease&) = delete;
    SourceLease& operator=(const SourceLease&) = delete;
    SourceLease(SourceLease&& other) noexcept;
    SourceLease& operator=(SourceLease&& other) noexcept;

    [[nodiscard]] const SceneSource& source() const;
    [[nodiscard]] std::uint64_t generation() const noexcept;
    [[nodiscard]] SourceRuntime& runtime() const;
    [[nodiscard]] explicit operator bool() const noexcept;

  private:
    friend class SourceRegistry;
    friend class SourceRegistryUpdate;

    SourceLease(std::shared_ptr<detail::SourceRegistryState> state,
                std::shared_ptr<detail::SourceRuntimeSlot> slot,
                std::string consumer_id) noexcept;
    void release() noexcept;

    std::shared_ptr<detail::SourceRegistryState> state_{};
    std::shared_ptr<detail::SourceRuntimeSlot> slot_{};
    std::string consumer_id_{};
};

class SourceRegistryUpdate final {
  public:
    ~SourceRegistryUpdate();

    SourceRegistryUpdate(const SourceRegistryUpdate&) = delete;
    SourceRegistryUpdate& operator=(const SourceRegistryUpdate&) = delete;
    SourceRegistryUpdate(SourceRegistryUpdate&& other) noexcept;
    SourceRegistryUpdate& operator=(SourceRegistryUpdate&& other) noexcept;

    [[nodiscard]] SourceLease acquire(std::string_view source_id,
                                      std::string_view consumer_id);
    void commit();
    [[nodiscard]] std::uint64_t document_revision() const noexcept;
    [[nodiscard]] explicit operator bool() const noexcept;

  private:
    friend class SourceRegistry;

    explicit SourceRegistryUpdate(
        std::unique_ptr<detail::SourceRegistryUpdateState> state) noexcept;

    std::unique_ptr<detail::SourceRegistryUpdateState> state_{};
};

class SourceRegistry final {
  public:
    explicit SourceRegistry(std::shared_ptr<SourceRuntimeFactory> factory,
                            SourceRegistryLimits limits = {});
    ~SourceRegistry();

    SourceRegistry(const SourceRegistry&) = delete;
    SourceRegistry& operator=(const SourceRegistry&) = delete;
    SourceRegistry(SourceRegistry&&) = delete;
    SourceRegistry& operator=(SourceRegistry&&) = delete;

    [[nodiscard]] SourceRegistryUpdate
    stage_snapshot(const SceneHydrationSnapshot& snapshot);
    void replace_snapshot(const SceneHydrationSnapshot& snapshot);
    [[nodiscard]] SourceLease acquire(std::string_view source_id,
                                      std::string_view consumer_id);
    [[nodiscard]] bool is_current(const SourceLease& lease) const noexcept;
    [[nodiscard]] std::vector<SourceRegistryEntry> entries() const;
    [[nodiscard]] std::uint64_t document_revision() const;
    void shutdown() noexcept;

  private:
    std::shared_ptr<detail::SourceRegistryState> state_{};
};

} // namespace solin::media_engine
