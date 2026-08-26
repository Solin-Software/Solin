#include "../bridge.hpp"

#include <solin/media_transport/d3d11_frame_channel_protocol.hpp>

#include <QtGui/rhi/qrhi.h>
#include <QtGui/rhi/qrhi_platform.h>
#include <QtCore/qobject.h>
#include <QtCore/qpointer.h>
#include <QtCore/qthread.h>
#include <QtMultimedia/private/qhwvideobuffer_p.h>
#include <QtMultimedia/private/qvideoframe_p.h>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <d3d11_4.h>
#include <dxgi1_2.h>
#include <objbase.h>
#include <windows.h>
#include <wrl/client.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstring>
#include <limits>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <stop_token>
#include <string>
#include <thread>
#include <utility>

namespace solin::qt_media_bridge {
namespace {

using Microsoft::WRL::ComPtr;
namespace protocol = solin::media_transport::d3d11_frame_channel;
using namespace std::chrono_literals;

class UniqueHandle final {
  public:
    explicit UniqueHandle(HANDLE handle = nullptr) noexcept : handle_(handle) {}
    ~UniqueHandle() { reset(); }

    UniqueHandle(const UniqueHandle&) = delete;
    UniqueHandle& operator=(const UniqueHandle&) = delete;
    UniqueHandle(UniqueHandle&& other) noexcept
        : handle_(std::exchange(other.handle_, nullptr)) {}
    UniqueHandle& operator=(UniqueHandle&& other) noexcept {
        if (this != &other) {
            reset();
            handle_ = std::exchange(other.handle_, nullptr);
        }
        return *this;
    }

    [[nodiscard]] HANDLE get() const noexcept { return handle_; }
    [[nodiscard]] explicit operator bool() const noexcept {
        return handle_ != nullptr && handle_ != INVALID_HANDLE_VALUE;
    }
    void reset(HANDLE replacement = nullptr) noexcept {
        if (*this) {
            static_cast<void>(CloseHandle(handle_));
        }
        handle_ = replacement;
    }

  private:
    HANDLE handle_{nullptr};
};

class UniqueView final {
  public:
    explicit UniqueView(void* view = nullptr) noexcept : view_(view) {}
    ~UniqueView() {
        if (view_ != nullptr) {
            static_cast<void>(UnmapViewOfFile(view_));
        }
    }

    UniqueView(const UniqueView&) = delete;
    UniqueView& operator=(const UniqueView&) = delete;
    UniqueView(UniqueView&&) = delete;
    UniqueView& operator=(UniqueView&&) = delete;

    [[nodiscard]] std::uint8_t* bytes() const noexcept {
        return static_cast<std::uint8_t*>(view_);
    }

  private:
    void* view_{nullptr};
};

class NamedMutexLease final {
  public:
    NamedMutexLease(HANDLE mutex, const DWORD timeout_ms) {
        const auto result = WaitForSingleObject(mutex, timeout_ms);
        if (result == WAIT_OBJECT_0 || result == WAIT_ABANDONED) {
            mutex_ = mutex;
            return;
        }
        if (result != WAIT_TIMEOUT) {
            throw std::runtime_error("d3d11_channel_mutex_failed");
        }
    }
    ~NamedMutexLease() {
        if (mutex_ != nullptr) {
            static_cast<void>(ReleaseMutex(mutex_));
        }
    }

    NamedMutexLease(const NamedMutexLease&) = delete;
    NamedMutexLease& operator=(const NamedMutexLease&) = delete;
    [[nodiscard]] explicit operator bool() const noexcept {
        return mutex_ != nullptr;
    }

  private:
    HANDLE mutex_{nullptr};
};

template <typename Value>
[[nodiscard]] Value read_value(const std::uint8_t* bytes,
                               const std::size_t offset) noexcept {
    Value value{};
    std::memcpy(&value, bytes + offset, sizeof(Value));
    return value;
}

template <typename Value>
void write_value(std::uint8_t* bytes, const std::size_t offset,
                 const Value value) noexcept {
    std::memcpy(bytes + offset, &value, sizeof(Value));
}

[[nodiscard]] std::uint64_t process_creation_time(HANDLE process) noexcept {
    FILETIME creation{};
    FILETIME exit{};
    FILETIME kernel{};
    FILETIME user{};
    if (GetProcessTimes(process, &creation, &exit, &kernel, &user) == FALSE) {
        return 0U;
    }
    return (static_cast<std::uint64_t>(creation.dwHighDateTime) << 32U) |
           creation.dwLowDateTime;
}

[[nodiscard]] bool process_identity_is_alive(
    const std::uint32_t process_id,
    const std::uint64_t expected_creation_time) noexcept {
    if (process_id == 0U || expected_creation_time == 0U) {
        return false;
    }
    UniqueHandle process{OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                                     FALSE, process_id)};
    if (!process) {
        return false;
    }
    if (WaitForSingleObject(process.get(), 0U) != WAIT_TIMEOUT) {
        return false;
    }
    return process_creation_time(process.get()) == expected_creation_time;
}

[[nodiscard]] std::uint64_t monotonic_nanoseconds() noexcept {
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch())
            .count());
}

[[nodiscard]] std::string guid_token() {
    GUID guid{};
    if (CoCreateGuid(&guid) != S_OK) {
        throw std::runtime_error("d3d11_channel_identity_unavailable");
    }
    constexpr char digits[] = "0123456789abcdef";
    const auto* bytes = reinterpret_cast<const std::uint8_t*>(&guid);
    std::string result;
    result.reserve(sizeof(GUID) * 2U);
    for (std::size_t index = 0U; index < sizeof(GUID); ++index) {
        result.push_back(digits[bytes[index] >> 4U]);
        result.push_back(digits[bytes[index] & 0x0fU]);
    }
    return result;
}

[[nodiscard]] std::wstring widen_ascii(const std::string& value) {
    return std::wstring{value.begin(), value.end()};
}

[[nodiscard]] std::wstring object_name(const wchar_t* prefix,
                                       const std::string& token) {
    return std::wstring{prefix} + widen_ascii(token);
}

[[nodiscard]] std::wstring texture_name(const std::string& token,
                                        const std::uint64_t resource_generation,
                                        const std::uint32_t slot) {
    return std::wstring{protocol::kTexturePrefix} + widen_ascii(token) + L"." +
           std::to_wstring(resource_generation) + L"." + std::to_wstring(slot);
}

struct SharedTextureSlot final {
    ComPtr<ID3D11Texture2D> texture{};
    ComPtr<IDXGIKeyedMutex> mutex{};
    UniqueHandle shared_handle{};
};

struct PendingFrame final {
    QVideoFrame frame{};
    QImage image{};
    std::uint64_t session_id{0U};
    std::uint64_t playback_session_id{0U};
    std::uint64_t media_epoch{0U};
    std::uint8_t retry_attempt{0U};
    bool decoder_frame{true};
};

enum class PublishResult : std::uint8_t {
    published,
    dropped,
    fatal,
};

class WriterSlotReservation final {
  public:
    WriterSlotReservation(const std::uint32_t slot,
                          volatile LONG* lease_count) noexcept
        : slot_(slot), lease_count_(lease_count) {}
    ~WriterSlotReservation() { release(); }

    WriterSlotReservation(const WriterSlotReservation&) = delete;
    WriterSlotReservation& operator=(const WriterSlotReservation&) = delete;
    WriterSlotReservation(WriterSlotReservation&& other) noexcept
        : slot_(other.slot_),
          lease_count_(std::exchange(other.lease_count_, nullptr)) {}
    WriterSlotReservation& operator=(WriterSlotReservation&&) = delete;

    [[nodiscard]] std::uint32_t slot() const noexcept { return slot_; }
    void release() noexcept {
        if (lease_count_ != nullptr) {
            static_cast<void>(InterlockedExchange(lease_count_, 0L));
            lease_count_ = nullptr;
        }
    }

  private:
    std::uint32_t slot_{0U};
    volatile LONG* lease_count_{nullptr};
};

class OffscreenFrame final {
  public:
    explicit OffscreenFrame(QRhi& rhi) : rhi_(rhi) {
        QRhiCommandBuffer* command_buffer = nullptr;
        active_ = rhi_.beginOffscreenFrame(&command_buffer) ==
                  QRhi::FrameOpSuccess;
    }
    ~OffscreenFrame() {
        if (active_) {
            static_cast<void>(rhi_.endOffscreenFrame());
        }
    }

    OffscreenFrame(const OffscreenFrame&) = delete;
    OffscreenFrame& operator=(const OffscreenFrame&) = delete;
    [[nodiscard]] explicit operator bool() const noexcept { return active_; }

  private:
    QRhi& rhi_;
    bool active_{false};
};

class MappedFrameTextures final {
  public:
    explicit MappedFrameTextures(
        QVideoFrameTexturesUPtr& reusable_textures) noexcept
        : reusable_textures_(reusable_textures) {}
    ~MappedFrameTextures() {
        if (textures_ != nullptr) {
            textures_->onFrameEndInvoked();
            reusable_textures_ = std::move(textures_);
        }
    }

    MappedFrameTextures(const MappedFrameTextures&) = delete;
    MappedFrameTextures& operator=(const MappedFrameTextures&) = delete;

    void reset(QVideoFrameTexturesUPtr textures) noexcept {
        textures_ = std::move(textures);
    }
    [[nodiscard]] QVideoFrameTextures* get() const noexcept {
        return textures_.get();
    }
    [[nodiscard]] QVideoFrameTextures* operator->() const noexcept {
        return textures_.get();
    }

  private:
    QVideoFrameTexturesUPtr& reusable_textures_;
    QVideoFrameTexturesUPtr textures_{};
};

class D3d11FrameBridge final : public FrameBridge {
  private:
    struct SinkRelay final {
        std::mutex mutex{};
        D3d11FrameBridge* bridge{nullptr};
    };

  public:
    D3d11FrameBridge(const std::uint32_t width, const std::uint32_t height,
                     const std::uint64_t generation)
        : descriptor_{.channel_id = "solin-content-gpu-" + guid_token(),
                      .handle_token = guid_token(),
                      .generation = generation,
                      .width = width,
                      .height = height} {
        if (width == 0U || height == 0U || width > 7'680U || height > 4'320U ||
            generation == 0U) {
            throw std::invalid_argument("d3d11_channel_configuration_invalid");
        }
        initialize_channel();
        worker_ = std::jthread(
            [this](const std::stop_token stop_token) { run(stop_token); });
        std::unique_lock lock{state_mutex_};
        if (!initialized_.wait_for(lock, 5s, [this] { return initialized_done_; })) {
            lock.unlock();
            close();
            throw std::runtime_error("d3d11_bridge_initialization_timeout");
        }
        if (!available_.load()) {
            const auto error = error_code_.empty()
                                   ? std::string{"d3d11_bridge_unavailable"}
                                   : error_code_;
            lock.unlock();
            close();
            throw std::runtime_error(error);
        }
    }

    ~D3d11FrameBridge() override { close(); }

    [[nodiscard]] const BridgeDescriptor& descriptor() const noexcept override {
        return descriptor_;
    }

    [[nodiscard]] BridgeStatus status() const override {
        std::scoped_lock lock{state_mutex_};
        return {.available = available_.load(),
                .direct_submission_active = direct_submission_enabled_,
                .published_sequence = published_sequence_.load(),
                .dropped_frames = dropped_frames_.load(),
                .resource_generation = resource_generation_.load(),
                .resource_width = resource_width_.load(),
                .resource_height = resource_height_.load(),
                .error_code = error_code_};
    }

    void set_route_state(const std::uint64_t session_id,
                         const bool accepting_frames,
                         const bool demanded) override {
        std::scoped_lock lock{state_mutex_};
        if (closed_) {
            return;
        }
        if (session_id < session_id_) {
            throw std::invalid_argument("d3d11_bridge_session_reversed");
        }
        session_id_ = session_id;
        accepting_frames_ = accepting_frames;
        demanded_ = demanded;
        if (!accepting_frames || !demanded) {
            pending_.reset();
            direct_deadline_ = {};
        }
        wakeup_.notify_all();
    }

    void set_decoder_frame_gate(const std::uint64_t playback_session_id,
                                const bool accepting_frames) override {
        std::scoped_lock lock{state_mutex_};
        if (closed_) {
            return;
        }
        if (playback_session_id < playback_session_id_) {
            throw std::invalid_argument("d3d11_bridge_playback_session_reversed");
        }
        const auto session_changed = playback_session_id != playback_session_id_;
        playback_session_id_ = playback_session_id;
        decoder_accepting_frames_ = accepting_frames;
        if ((session_changed || !accepting_frames) && pending_.has_value() &&
            pending_->decoder_frame) {
            pending_.reset();
            direct_deadline_ = {};
        }
        wakeup_.notify_all();
    }

    void set_direct_submission(const bool enabled,
                               const std::uint32_t maximum_fps) override {
        if (maximum_fps == 0U || maximum_fps > 60U) {
            throw std::invalid_argument("d3d11_bridge_direct_fps_invalid");
        }
        std::scoped_lock lock{state_mutex_};
        if (closed_) {
            return;
        }
        direct_interval_ = std::chrono::nanoseconds{1'000'000'000ULL /
                                                    maximum_fps};
        direct_submission_enabled_ = enabled;
        direct_deadline_ = {};
        incompatible_direct_frames_ = 0U;
        if (!enabled && pending_.has_value() && pending_->decoder_frame) {
            pending_.reset();
        }
        wakeup_.notify_all();
    }

    void stage_media_epoch(const std::uint64_t media_epoch) override {
        std::scoped_lock lock{state_mutex_};
        if (closed_) {
            return;
        }
        if (media_epoch < media_epoch_) {
            throw std::invalid_argument("d3d11_bridge_media_epoch_reversed");
        }
        // Stage presentation identity locally. Publishing it to the channel
        // before a matching texture exists makes consumers transition to an
        // epoch that can only render black or stale pixels. publish_metadata()
        // commits both under the channel mutex after the GPU copy succeeds.
        media_epoch_ = media_epoch;
    }

    void set_image_transform(const ImageTransform& transform) override {
        if (transform.canvas_width == 0U || transform.canvas_height == 0U ||
            transform.canvas_width > descriptor_.width ||
            transform.canvas_height > descriptor_.height ||
            transform.duration_ms == 0U || transform.duration_ms > 60'000U ||
            !std::isfinite(transform.zoom) || transform.zoom < 0.1 ||
            transform.zoom > 10.0 || !std::isfinite(transform.norm_x) ||
            std::abs(transform.norm_x) > 16.0 ||
            !std::isfinite(transform.norm_y) ||
            std::abs(transform.norm_y) > 16.0) {
            throw std::invalid_argument("d3d11_bridge_transform_invalid");
        }
        std::scoped_lock lock{state_mutex_};
        if (closed_) {
            return;
        }
        ++image_transform_revision_;
        update_channel([this, &transform](std::uint8_t* bytes) {
            write_value(bytes, protocol::kImageTransformRevisionOffset,
                        image_transform_revision_);
            write_value(bytes, protocol::kImageTransformMediaEpochOffset,
                        transform.media_epoch);
            const auto flags =
                (transform.enabled ? protocol::kImageTransformEnabled : 0U) |
                (transform.animate ? protocol::kImageTransformAnimate : 0U);
            write_value(bytes, protocol::kImageTransformFlagsOffset, flags);
            write_value(bytes, protocol::kImageTransformCanvasWidthOffset,
                        transform.canvas_width);
            write_value(bytes, protocol::kImageTransformCanvasHeightOffset,
                        transform.canvas_height);
            write_value(bytes, protocol::kImageTransformDurationMsOffset,
                        transform.duration_ms);
            write_value(bytes, protocol::kImageTransformZoomOffset, transform.zoom);
            write_value(bytes, protocol::kImageTransformNormXOffset,
                        transform.norm_x);
            write_value(bytes, protocol::kImageTransformNormYOffset,
                        transform.norm_y);
        });
    }

    void bind_video_sink(QVideoSink* sink) override {
        if (sink == nullptr || sink->thread() != QThread::currentThread()) {
            throw std::invalid_argument("d3d11_bridge_video_sink_thread_invalid");
        }
        QRhi* rhi = nullptr;
        {
            std::scoped_lock lock{state_mutex_};
            if (closed_ || !available_.load() || active_rhi_ == nullptr) {
                throw std::runtime_error("d3d11_bridge_rhi_unavailable");
            }
            if (bound_video_sink_ != nullptr && bound_video_sink_ != sink) {
                throw std::runtime_error("d3d11_bridge_video_sink_already_bound");
            }
            if (bound_video_sink_ == sink && sink_relay_ != nullptr) {
                return;
            }
            rhi = active_rhi_;
        }
        auto relay = std::make_shared<SinkRelay>();
        relay->bridge = this;
        const auto connection = QObject::connect(
            sink, &QVideoSink::videoFrameChanged, sink,
            [relay](const QVideoFrame& frame) noexcept {
                try {
                    std::scoped_lock relay_lock{relay->mutex};
                    if (relay->bridge != nullptr) {
                        static_cast<void>(relay->bridge->submit_direct(frame));
                    }
                } catch (...) {
                }
            },
            Qt::DirectConnection);
        if (!connection) {
            throw std::runtime_error("d3d11_bridge_video_sink_connect_failed");
        }
        bool bind_rejected = false;
        {
            std::scoped_lock lock{state_mutex_};
            if (closed_ || bound_video_sink_ != nullptr) {
                bind_rejected = true;
            } else {
                bound_video_sink_ = sink;
                sink_connection_ = connection;
                sink_relay_ = relay;
            }
        }
        if (bind_rejected) {
            QObject::disconnect(connection);
            std::scoped_lock relay_lock{relay->mutex};
            relay->bridge = nullptr;
            throw std::runtime_error("d3d11_bridge_video_sink_bind_raced");
        }
        sink->setRhi(rhi);
    }

    void unbind_video_sink() noexcept override {
        QPointer<QVideoSink> sink;
        QRhi* rhi = nullptr;
        QMetaObject::Connection connection;
        std::shared_ptr<SinkRelay> relay;
        try {
            {
                std::scoped_lock lock{state_mutex_};
                sink = bound_video_sink_;
                bound_video_sink_.clear();
                rhi = active_rhi_;
                connection = sink_connection_;
                sink_connection_ = {};
                relay = std::move(sink_relay_);
                direct_submission_enabled_ = false;
                direct_deadline_ = {};
                pending_.reset();
            }
            QObject::disconnect(connection);
            if (relay != nullptr) {
                std::scoped_lock relay_lock{relay->mutex};
                relay->bridge = nullptr;
            }
            if (sink == nullptr) {
                return;
            }
            const auto detach = [sink, rhi] {
                if (sink != nullptr && sink->rhi() == rhi) {
                    sink->setRhi(nullptr);
                }
            };
            if (sink->thread() == QThread::currentThread()) {
                detach();
                return;
            }
            static_cast<void>(QMetaObject::invokeMethod(
                sink.get(), detach, Qt::BlockingQueuedConnection));
        } catch (...) {
        }
    }

    [[nodiscard]] SubmitResult submit(QVideoFrame frame,
                                      const std::uint64_t session_id) override {
        if (!frame.isValid() ||
            frame.handleType() != QVideoFrame::RhiTextureHandle ||
            frame.pixelFormat() !=
                QVideoFrameFormat::PixelFormat::Format_NV12) {
            return SubmitResult::unavailable;
        }
        std::scoped_lock lock{state_mutex_};
        if (closed_ || !available_.load()) {
            return SubmitResult::unavailable;
        }
        if (!demanded_) {
            return SubmitResult::no_demand;
        }
        if (!accepting_frames_ || !decoder_accepting_frames_ ||
            session_id != session_id_) {
            return SubmitResult::session_rejected;
        }
        return enqueue_locked(std::move(frame), session_id,
                              playback_session_id_);
    }

    [[nodiscard]] SubmitResult submit_image(
        QImage image, const std::uint64_t session_id) override {
        if (image.isNull() || image.width() <= 0 || image.height() <= 0 ||
            static_cast<std::uint32_t>(image.width()) > descriptor_.width ||
            static_cast<std::uint32_t>(image.height()) > descriptor_.height) {
            return SubmitResult::unavailable;
        }
        std::scoped_lock lock{state_mutex_};
        if (closed_ || !available_.load()) {
            return SubmitResult::unavailable;
        }
        if (!demanded_) {
            return SubmitResult::no_demand;
        }
        if (!accepting_frames_ || session_id != session_id_) {
            return SubmitResult::session_rejected;
        }
        if (pending_.has_value()) {
            dropped_frames_.fetch_add(1U);
        }
        pending_ = PendingFrame{.image = std::move(image),
                                .session_id = session_id,
                                .media_epoch = media_epoch_,
                                .decoder_frame = false};
        wakeup_.notify_one();
        return SubmitResult::accepted;
    }

    void close() noexcept override {
        unbind_video_sink();
        {
            std::scoped_lock lock{state_mutex_};
            if (closed_) {
                return;
            }
            closed_ = true;
            pending_.reset();
        }
        worker_.request_stop();
        wakeup_.notify_all();
        if (worker_.joinable()) {
            worker_.join();
        }
        available_.store(false);
    }

  private:
    struct GpuState final {
        ComPtr<ID3D11Device> device{};
        ComPtr<ID3D11DeviceContext> context{};
        std::unique_ptr<QRhi> rhi{};
        QVideoFrameTexturesUPtr old_textures{};
        std::array<SharedTextureSlot, protocol::kSlotCount> texture_slots{};
        std::uint64_t resource_generation{0U};
        std::uint32_t width{0U};
        std::uint32_t height{0U};
        DXGI_FORMAT format{DXGI_FORMAT_UNKNOWN};
        std::uint64_t adapter_luid{0U};
        std::uint32_t next_slot{0U};
    };

    [[nodiscard]] SubmitResult submit_direct(const QVideoFrame& frame) {
        if (!frame.isValid()) {
            return SubmitResult::unavailable;
        }
        std::scoped_lock lock{state_mutex_};
        if (closed_ || !available_.load() || !direct_submission_enabled_) {
            return SubmitResult::unavailable;
        }
        if (!demanded_) {
            return SubmitResult::no_demand;
        }
        if (!accepting_frames_ || !decoder_accepting_frames_) {
            return SubmitResult::session_rejected;
        }
        if (frame.handleType() != QVideoFrame::RhiTextureHandle ||
            frame.pixelFormat() !=
                QVideoFrameFormat::PixelFormat::Format_NV12) {
            ++incompatible_direct_frames_;
            if (incompatible_direct_frames_ >= 3U) {
                direct_submission_enabled_ = false;
                direct_deadline_ = {};
                pending_.reset();
                error_code_ = "d3d11_bridge_frame_incompatible";
            }
            return SubmitResult::unavailable;
        }
        incompatible_direct_frames_ = 0U;
        const auto now = std::chrono::steady_clock::now();
        if (direct_deadline_ != std::chrono::steady_clock::time_point{} &&
            now < direct_deadline_) {
            dropped_frames_.fetch_add(1U);
            return SubmitResult::dropped;
        }
        if (direct_deadline_ == std::chrono::steady_clock::time_point{} ||
            now - direct_deadline_ >= direct_interval_) {
            direct_deadline_ = now + direct_interval_;
        } else {
            direct_deadline_ += direct_interval_;
        }
        return enqueue_locked(QVideoFrame{frame}, session_id_,
                              playback_session_id_);
    }

    [[nodiscard]] SubmitResult enqueue_locked(
        QVideoFrame frame, const std::uint64_t session_id,
        const std::uint64_t playback_session_id) {
        if (pending_.has_value()) {
            dropped_frames_.fetch_add(1U);
        }
        pending_ = PendingFrame{.frame = std::move(frame),
                                .session_id = session_id,
                                .playback_session_id = playback_session_id,
                                .media_epoch = media_epoch_,
                                .decoder_frame = true};
        wakeup_.notify_one();
        return SubmitResult::accepted;
    }

    void initialize_channel() {
        const auto mapping_name =
            object_name(protocol::kMappingPrefix, descriptor_.handle_token);
        mapping_.reset(CreateFileMappingW(
            INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0U,
            static_cast<DWORD>(protocol::mapping_size()), mapping_name.c_str()));
        if (!mapping_ || GetLastError() == ERROR_ALREADY_EXISTS) {
            throw std::runtime_error("d3d11_channel_mapping_unavailable");
        }
        view_ = std::make_unique<UniqueView>(MapViewOfFile(
            mapping_.get(), FILE_MAP_READ | FILE_MAP_WRITE, 0U, 0U,
            protocol::mapping_size()));
        if (view_->bytes() == nullptr) {
            throw std::runtime_error("d3d11_channel_mapping_unavailable");
        }
        mutex_.reset(CreateMutexW(
            nullptr, FALSE,
            object_name(protocol::kMutexPrefix, descriptor_.handle_token).c_str()));
        event_.reset(CreateEventW(
            nullptr, FALSE, FALSE,
            object_name(protocol::kEventPrefix, descriptor_.handle_token).c_str()));
        if (!mutex_ || !event_) {
            throw std::runtime_error("d3d11_channel_synchronization_unavailable");
        }
        std::memset(view_->bytes(), 0, protocol::mapping_size());
        std::memcpy(view_->bytes() + protocol::kMagicOffset,
                    protocol::kMagic.data(), protocol::kMagic.size());
        write_value(view_->bytes(), protocol::kVersionOffset, protocol::kVersion);
        write_value(view_->bytes(), protocol::kHeaderSizeOffset,
                    protocol::kHeaderSize);
        write_value(view_->bytes(), protocol::kSlotCountOffset,
                    protocol::kSlotCount);
        write_value(view_->bytes(), protocol::kCapacityWidthOffset,
                    descriptor_.width);
        write_value(view_->bytes(), protocol::kCapacityHeightOffset,
                    descriptor_.height);
        write_value(view_->bytes(), protocol::kGenerationOffset,
                    descriptor_.generation);
        write_value(view_->bytes(), protocol::kProducerProcessIdOffset,
                    static_cast<std::uint32_t>(GetCurrentProcessId()));
        write_value(view_->bytes(), protocol::kProducerCreationTimeOffset,
                    process_creation_time(GetCurrentProcess()));
    }

    template <typename Update>
    void update_channel(Update&& update) {
        NamedMutexLease lease{mutex_.get(), 250U};
        if (!lease) {
            throw std::runtime_error("d3d11_channel_mutex_busy");
        }
        update(view_->bytes());
        static_cast<void>(SetEvent(event_.get()));
    }

    void run(const std::stop_token stop_token) noexcept {
        std::optional<GpuState> gpu;
        try {
            gpu.emplace(initialize_gpu());
            {
                std::scoped_lock lock{state_mutex_};
                active_rhi_ = gpu->rhi.get();
                available_.store(true);
                initialized_done_ = true;
            }
            initialized_.notify_all();
            while (!stop_token.stop_requested()) {
                std::optional<PendingFrame> pending;
                {
                    std::unique_lock lock{state_mutex_};
                    wakeup_.wait(lock, stop_token, [this] {
                        return closed_ || pending_.has_value();
                    });
                    if (closed_ || stop_token.stop_requested()) {
                        break;
                    }
                    pending = std::move(pending_);
                    pending_.reset();
                    if (!pending.has_value() || !demanded_ ||
                        !accepting_frames_ ||
                        pending->session_id != session_id_ ||
                        (pending->decoder_frame &&
                         (!decoder_accepting_frames_ ||
                          pending->playback_session_id != playback_session_id_))) {
                        continue;
                    }
                }
                try {
                    const auto result = publish(gpu.value(), pending.value());
                    if (result == PublishResult::published) {
                        continue;
                    }
                    if (result == PublishResult::dropped) {
                        if (!pending->decoder_frame) {
                            retry_static_frame(std::move(pending.value()),
                                               stop_token);
                        }
                        continue;
                    }
                    set_failed("d3d11_frame_import_failed");
                } catch (const std::exception& error) {
                    set_failed(error.what());
                } catch (...) {
                    set_failed("d3d11_bridge_worker_failed");
                }
                break;
            }
        } catch (const std::exception& error) {
            set_failed(error.what());
        } catch (...) {
            set_failed("d3d11_bridge_worker_failed");
        }
        if (gpu.has_value()) {
            // QVideoSink retains this QRhi pointer. Even after an unexpected
            // worker failure, preserve the GPU state until close() has queued
            // the sink detach on its owner thread. Destroying it here would
            // leave Qt Multimedia with a dangling private-ABI resource.
            std::unique_lock lock{state_mutex_};
            wakeup_.wait(lock, stop_token,
                         [this] { return closed_; });
            active_rhi_ = nullptr;
        }
        {
            std::scoped_lock lock{state_mutex_};
            initialized_done_ = true;
        }
        initialized_.notify_all();
    }

    [[nodiscard]] GpuState initialize_gpu() {
        GpuState gpu{};
        constexpr std::array feature_levels{
            D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};
        D3D_FEATURE_LEVEL selected_level{};
        const auto result = D3D11CreateDevice(
            nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
            feature_levels.data(), static_cast<UINT>(feature_levels.size()),
            D3D11_SDK_VERSION, &gpu.device, &selected_level, &gpu.context);
        if (FAILED(result) || gpu.device == nullptr || gpu.context == nullptr) {
            throw std::runtime_error("d3d11_device_unavailable");
        }
        ComPtr<IDXGIDevice> dxgi_device;
        ComPtr<IDXGIAdapter> adapter;
        DXGI_ADAPTER_DESC adapter_description{};
        if (FAILED(gpu.device.As(&dxgi_device)) ||
            FAILED(dxgi_device->GetAdapter(&adapter)) ||
            FAILED(adapter->GetDesc(&adapter_description))) {
            throw std::runtime_error("d3d11_adapter_unavailable");
        }
        std::memcpy(&gpu.adapter_luid, &adapter_description.AdapterLuid,
                    sizeof(adapter_description.AdapterLuid));

        QRhiD3D11InitParams init_params{};
        QRhiD3D11NativeHandles native_handles{};
        native_handles.dev = gpu.device.Get();
        native_handles.context = gpu.context.Get();
        gpu.rhi.reset(QRhi::create(QRhi::D3D11, &init_params, {},
                                   &native_handles));
        if (gpu.rhi == nullptr) {
            throw std::runtime_error("qt_rhi_d3d11_unavailable");
        }
        return gpu;
    }

    void retry_static_frame(PendingFrame pending,
                            const std::stop_token stop_token) {
        const auto backoff = std::chrono::milliseconds{
            1U << (std::min)(pending.retry_attempt, std::uint8_t{4U})};
        if (pending.retry_attempt < 4U) {
            ++pending.retry_attempt;
        }
        std::unique_lock lock{state_mutex_};
        static_cast<void>(wakeup_.wait_for(
            lock, stop_token, backoff,
            [this, &pending] {
                return closed_ || pending_.has_value() || !demanded_ ||
                       !accepting_frames_ ||
                       pending.session_id != session_id_;
            }));
        if (!closed_ && demanded_ && accepting_frames_ &&
            pending.session_id == session_id_ && !pending_.has_value()) {
            pending_ = std::move(pending);
            wakeup_.notify_one();
        }
    }

    [[nodiscard]] PublishResult publish(GpuState& gpu,
                                        const PendingFrame& pending) {
        return pending.decoder_frame ? publish_video(gpu, pending)
                                     : publish_image(gpu, pending);
    }

    [[nodiscard]] PublishResult publish_video(GpuState& gpu,
                                              const PendingFrame& pending) {
        if (pending.frame.pixelFormat() !=
                QVideoFrameFormat::PixelFormat::Format_NV12 ||
            pending.frame.width() <= 0 || pending.frame.height() <= 0 ||
            static_cast<std::uint32_t>(pending.frame.width()) > descriptor_.width ||
            static_cast<std::uint32_t>(pending.frame.height()) >
                descriptor_.height) {
            return PublishResult::fatal;
        }
        auto* hardware_buffer = QVideoFramePrivate::hwBuffer(pending.frame);
        if (hardware_buffer == nullptr) {
            return PublishResult::fatal;
        }
        // QVideoFrameTextures documents that onFrameEndInvoked() must happen
        // after QRhi::endFrame. Declare the mapped-texture owner first so
        // every return path ends the offscreen frame before releasing or
        // retaining Qt's frame resources for the next mapTextures() call.
        MappedFrameTextures textures{gpu.old_textures};
        OffscreenFrame offscreen{*gpu.rhi};
        if (!offscreen) {
            return PublishResult::fatal;
        }
        hardware_buffer->initTextureConverter(*gpu.rhi);
        textures.reset(hardware_buffer->mapTextures(*gpu.rhi, gpu.old_textures));
        if (textures.get() == nullptr || textures->texture(0U) == nullptr) {
            return PublishResult::fatal;
        }
        const auto native_texture = textures->texture(0U)->nativeTexture();
        auto* source_texture = reinterpret_cast<ID3D11Texture2D*>(
            static_cast<std::uintptr_t>(native_texture.object));
        if (source_texture == nullptr) {
            return PublishResult::fatal;
        }
        D3D11_TEXTURE2D_DESC source_description{};
        source_texture->GetDesc(&source_description);
        const auto width = static_cast<std::uint32_t>(pending.frame.width());
        const auto height = static_cast<std::uint32_t>(pending.frame.height());
        if (source_description.Format != DXGI_FORMAT_NV12 ||
            source_description.Width != width ||
            source_description.Height != height) {
            return PublishResult::fatal;
        }
        if (!ensure_ring(gpu, source_description)) {
            return PublishResult::fatal;
        }
        auto selected_slot = select_slot(gpu);
        if (!selected_slot.has_value()) {
            dropped_frames_.fetch_add(1U);
            return PublishResult::dropped;
        }
        auto& slot = gpu.texture_slots[selected_slot->slot()];
        const auto acquired = slot.mutex->AcquireSync(0U, 0U);
        if (acquired != S_OK && acquired != WAIT_ABANDONED) {
            dropped_frames_.fetch_add(1U);
            return PublishResult::dropped;
        }
        gpu.context->CopyResource(slot.texture.Get(), source_texture);
        const auto released = slot.mutex->ReleaseSync(0U);
        if (released != S_OK) {
            return PublishResult::fatal;
        }
        return publish_metadata(selected_slot.value(), pending);
    }

    [[nodiscard]] PublishResult publish_image(GpuState& gpu,
                                              const PendingFrame& pending) {
        auto image = pending.image.convertToFormat(QImage::Format_ARGB32);
        if (image.isNull() || image.width() <= 0 || image.height() <= 0 ||
            image.bytesPerLine() <= 0 || image.constBits() == nullptr) {
            return PublishResult::fatal;
        }
        D3D11_TEXTURE2D_DESC description{};
        description.Width = static_cast<UINT>(image.width());
        description.Height = static_cast<UINT>(image.height());
        description.MipLevels = 1U;
        description.ArraySize = 1U;
        description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        description.SampleDesc.Count = 1U;
        description.Usage = D3D11_USAGE_DEFAULT;
        description.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        if (!ensure_ring(gpu, description)) {
            return PublishResult::fatal;
        }
        auto selected_slot = select_slot(gpu);
        if (!selected_slot.has_value()) {
            dropped_frames_.fetch_add(1U);
            return PublishResult::dropped;
        }
        auto& slot = gpu.texture_slots[selected_slot->slot()];
        const auto acquired = slot.mutex->AcquireSync(0U, 0U);
        if (acquired != S_OK && acquired != WAIT_ABANDONED) {
            dropped_frames_.fetch_add(1U);
            return PublishResult::dropped;
        }
        gpu.context->UpdateSubresource(
            slot.texture.Get(), 0U, nullptr, image.constBits(),
            static_cast<UINT>(image.bytesPerLine()), 0U);
        const auto released = slot.mutex->ReleaseSync(0U);
        if (released != S_OK) {
            return PublishResult::fatal;
        }
        return publish_metadata(selected_slot.value(), pending);
    }

    [[nodiscard]] bool ensure_ring(GpuState& gpu,
                                   const D3D11_TEXTURE2D_DESC& source) {
        if (gpu.width == source.Width && gpu.height == source.Height &&
            gpu.format == source.Format && gpu.resource_generation != 0U) {
            return true;
        }
        std::array<SharedTextureSlot, protocol::kSlotCount> replacement{};
        const auto resource_generation = gpu.resource_generation + 1U;
        auto description = source;
        description.MipLevels = 1U;
        description.ArraySize = 1U;
        description.SampleDesc.Count = 1U;
        description.SampleDesc.Quality = 0U;
        description.Usage = D3D11_USAGE_DEFAULT;
        description.CPUAccessFlags = 0U;
        description.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        description.MiscFlags = D3D11_RESOURCE_MISC_SHARED_NTHANDLE |
                                D3D11_RESOURCE_MISC_SHARED_KEYEDMUTEX;
        for (std::uint32_t index = 0U; index < protocol::kSlotCount; ++index) {
            auto& slot = replacement[index];
            if (FAILED(gpu.device->CreateTexture2D(
                    &description, nullptr, &slot.texture)) ||
                FAILED(slot.texture.As(&slot.mutex))) {
                return false;
            }
            ComPtr<IDXGIResource1> resource;
            HANDLE handle = nullptr;
            const auto name = texture_name(descriptor_.handle_token,
                                           resource_generation, index);
            if (FAILED(slot.texture.As(&resource)) ||
                FAILED(resource->CreateSharedHandle(
                    nullptr, DXGI_SHARED_RESOURCE_READ, name.c_str(), &handle)) ||
                handle == nullptr) {
                return false;
            }
            slot.shared_handle.reset(handle);
        }
        NamedMutexLease lease{mutex_.get(), 250U};
        if (!lease) {
            return false;
        }
        gpu.texture_slots = std::move(replacement);
        gpu.resource_generation = resource_generation;
        gpu.width = source.Width;
        gpu.height = source.Height;
        gpu.format = source.Format;
        gpu.next_slot = 0U;
        write_value(view_->bytes(), protocol::kResourceWidthOffset, gpu.width);
        write_value(view_->bytes(), protocol::kResourceHeightOffset, gpu.height);
        write_value(view_->bytes(), protocol::kResourceFormatOffset,
                    static_cast<std::uint32_t>(gpu.format));
        write_value(view_->bytes(), protocol::kAdapterLuidOffset,
                    gpu.adapter_luid);
        write_value(view_->bytes(), protocol::kResourceGenerationOffset,
                    gpu.resource_generation);
        resource_width_.store(gpu.width);
        resource_height_.store(gpu.height);
        resource_generation_.store(gpu.resource_generation);
        static_cast<void>(SetEvent(event_.get()));
        return true;
    }

    [[nodiscard]] std::optional<WriterSlotReservation> select_slot(
        GpuState& gpu) {
        NamedMutexLease channel_lease{mutex_.get(), 0U};
        if (!channel_lease) {
            return std::nullopt;
        }
        for (std::uint32_t attempt = 0U; attempt < protocol::kSlotCount;
             ++attempt) {
            const auto slot = (gpu.next_slot + attempt) % protocol::kSlotCount;
            const auto offset = protocol::slot_offset(slot);
            auto* lease_count = reinterpret_cast<volatile LONG*>(
                view_->bytes() + offset + protocol::kSlotLeaseCountOffset);
            auto leases = InterlockedCompareExchange(lease_count, 0L, 0L);
            if (leases > 0L) {
                const auto owner_pid = read_value<std::uint32_t>(
                    view_->bytes(),
                    offset + protocol::kSlotLeaseOwnerProcessIdOffset);
                const auto owner_creation = read_value<std::uint64_t>(
                    view_->bytes(),
                    offset + protocol::kSlotLeaseOwnerCreationTimeOffset);
                if (!process_identity_is_alive(owner_pid, owner_creation)) {
                    InterlockedExchange(lease_count, 0L);
                    leases = 0L;
                }
            }
            if (leases == 0L &&
                InterlockedCompareExchange(lease_count, -1L, 0L) == 0L) {
                gpu.next_slot = (slot + 1U) % protocol::kSlotCount;
                return WriterSlotReservation{slot, lease_count};
            }
        }
        return std::nullopt;
    }

    [[nodiscard]] PublishResult publish_metadata(
        WriterSlotReservation& reservation, const PendingFrame& pending) {
        NamedMutexLease lease{mutex_.get(), 0U};
        if (!lease) {
            dropped_frames_.fetch_add(1U);
            return PublishResult::dropped;
        }
        const auto next_sequence = published_sequence_.load() + 1U;
        if (next_sequence == 0U || next_sequence > protocol::kMaximumSequence) {
            return PublishResult::fatal;
        }
        const auto offset = protocol::slot_offset(reservation.slot());
        write_value(view_->bytes(), offset + protocol::kSlotMarkerOffset,
                    next_sequence * 2U + 1U);
        write_value(view_->bytes(), offset + protocol::kSlotSequenceOffset,
                    next_sequence);
        const auto start_us =
            pending.decoder_frame ? pending.frame.startTime() : -1LL;
        const auto end_us =
            pending.decoder_frame ? pending.frame.endTime() : -1LL;
        const auto timestamp =
            start_us < 0 ? 0U : static_cast<std::uint64_t>(start_us) * 1'000U;
        const auto duration =
            end_us <= start_us
                ? 0U
                : static_cast<std::uint64_t>(end_us - start_us) * 1'000U;
        write_value(view_->bytes(),
                    offset + protocol::kSlotPresentationTimestampOffset,
                    timestamp);
        write_value(view_->bytes(), offset + protocol::kSlotDurationOffset,
                    duration);
        write_value(view_->bytes(),
                    offset + protocol::kSlotProducedMonotonicOffset,
                    monotonic_nanoseconds());
        write_value(view_->bytes(), offset + protocol::kSlotMediaEpochOffset,
                    pending.media_epoch);
        write_value(view_->bytes(), offset + protocol::kSlotMarkerOffset,
                    next_sequence * 2U);
        // Make the completed texture available while the channel mutex still
        // excludes readers. Publishing the sequence afterwards is the release
        // boundary: a reader can never lease a slot that the producer has
        // already selected for overwrite.
        reservation.release();
        write_value(view_->bytes(), protocol::kMediaEpochOffset,
                    pending.media_epoch);
        write_value(view_->bytes(), protocol::kPublishedSlotOffset,
                    reservation.slot());
        write_value(view_->bytes(), protocol::kPublishedSequenceOffset,
                    next_sequence);
        published_sequence_.store(next_sequence);
        static_cast<void>(SetEvent(event_.get()));
        return PublishResult::published;
    }

    void set_failed(const std::string& error) noexcept {
        try {
            std::scoped_lock lock{state_mutex_};
            available_.store(false);
            error_code_ = error.empty() ? "d3d11_bridge_worker_failed" : error;
            pending_.reset();
        } catch (...) {
            available_.store(false);
        }
        wakeup_.notify_all();
    }

    BridgeDescriptor descriptor_{};
    UniqueHandle mapping_{};
    std::unique_ptr<UniqueView> view_{};
    UniqueHandle mutex_{};
    UniqueHandle event_{};
    mutable std::mutex state_mutex_{};
    std::condition_variable_any wakeup_{};
    std::condition_variable initialized_{};
    std::optional<PendingFrame> pending_{};
    std::jthread worker_{};
    std::atomic_bool available_{false};
    std::atomic_uint64_t published_sequence_{0U};
    std::atomic_uint64_t dropped_frames_{0U};
    std::atomic_uint64_t resource_generation_{0U};
    std::atomic_uint32_t resource_width_{0U};
    std::atomic_uint32_t resource_height_{0U};
    std::uint64_t session_id_{0U};
    std::uint64_t playback_session_id_{0U};
    std::uint64_t media_epoch_{0U};
    std::uint64_t image_transform_revision_{0U};
    bool accepting_frames_{false};
    bool decoder_accepting_frames_{false};
    bool demanded_{false};
    bool direct_submission_enabled_{false};
    bool initialized_done_{false};
    bool closed_{false};
    std::uint32_t incompatible_direct_frames_{0U};
    std::chrono::nanoseconds direct_interval_{33'333'333ns};
    std::chrono::steady_clock::time_point direct_deadline_{};
    QRhi* active_rhi_{nullptr};
    QPointer<QVideoSink> bound_video_sink_{};
    QMetaObject::Connection sink_connection_{};
    std::shared_ptr<SinkRelay> sink_relay_{};
    std::string error_code_{};
};

} // namespace

std::unique_ptr<FrameBridge> make_frame_bridge(const std::uint32_t width,
                                               const std::uint32_t height,
                                               const std::uint64_t generation) {
    return std::make_unique<D3d11FrameBridge>(width, height, generation);
}

} // namespace solin::qt_media_bridge
