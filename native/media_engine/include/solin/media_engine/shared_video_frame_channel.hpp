#pragma once

#include "solin/media_engine/video_frame.hpp"

#include <cstdint>
#include <functional>
#include <memory>
#include <string_view>

namespace solin::media_engine {

struct SharedVideoFrameChannelConfiguration final {
    std::uint64_t generation{0U};
    PackedVideoFrameLayout layout{};

    bool operator==(const SharedVideoFrameChannelConfiguration&) const = default;
};

class SharedVideoFramePublisher {
  public:
    virtual ~SharedVideoFramePublisher() = default;

    SharedVideoFramePublisher(const SharedVideoFramePublisher&) = delete;
    SharedVideoFramePublisher& operator=(const SharedVideoFramePublisher&) = delete;
    SharedVideoFramePublisher(SharedVideoFramePublisher&&) = delete;
    SharedVideoFramePublisher& operator=(SharedVideoFramePublisher&&) = delete;

    [[nodiscard]] virtual std::uint64_t publish(const VideoFrameView& frame) = 0;
    virtual void heartbeat() noexcept = 0;
    [[nodiscard]] virtual std::uintptr_t native_mapping_handle() const noexcept = 0;
    [[nodiscard]] virtual std::string_view backing_file_path_utf8() const noexcept = 0;
    [[nodiscard]] virtual std::uint64_t mapping_size() const noexcept = 0;
    [[nodiscard]] virtual const SharedVideoFrameChannelConfiguration&
    configuration() const noexcept = 0;

  protected:
    SharedVideoFramePublisher() = default;
};

class SharedVideoFrameReader {
  public:
    using FrameVisitor = std::function<void(const VideoFrameView&)>;

    virtual ~SharedVideoFrameReader() = default;

    SharedVideoFrameReader(const SharedVideoFrameReader&) = delete;
    SharedVideoFrameReader& operator=(const SharedVideoFrameReader&) = delete;
    SharedVideoFrameReader(SharedVideoFrameReader&&) = delete;
    SharedVideoFrameReader& operator=(SharedVideoFrameReader&&) = delete;

    // Returns false when no newer complete slot exists. Implementations reuse the
    // caller-owned byte buffer once it has reached the negotiated frame size.
    [[nodiscard]] virtual bool read_latest(PackedVideoFrame& destination) = 0;
    // Visits the currently published slot without an intermediate owning copy.
    // The borrowed planes remain valid only during the synchronous visitor call.
    // False means that no complete frame was available or the publisher replaced
    // the slot while the visitor was reading it.
    [[nodiscard]] virtual bool visit_current_frame(
        const FrameVisitor& visitor) = 0;
    [[nodiscard]] virtual std::uint64_t heartbeat() const noexcept = 0;
    [[nodiscard]] virtual const SharedVideoFrameChannelConfiguration&
    configuration() const noexcept = 0;

  protected:
    SharedVideoFrameReader() = default;
};

[[nodiscard]] std::unique_ptr<SharedVideoFramePublisher>
make_shared_video_frame_publisher(
    const SharedVideoFrameChannelConfiguration& configuration);

// Uses an access-controlled temporary file so a Session 0 camera source can open
// the same pages without either process requiring PROCESS_DUP_HANDLE.
[[nodiscard]] std::unique_ptr<SharedVideoFramePublisher>
make_cross_session_shared_video_frame_publisher(
    const SharedVideoFrameChannelConfiguration& configuration);

// The handle is borrowed and must remain valid for the lifetime of the reader. A broker
// duplicates it into the camera-source process; raw video never crosses the control pipe.
[[nodiscard]] std::unique_ptr<SharedVideoFrameReader>
make_shared_video_frame_reader(std::uintptr_t native_mapping_handle,
                               std::uint64_t mapping_size);

} // namespace solin::media_engine
