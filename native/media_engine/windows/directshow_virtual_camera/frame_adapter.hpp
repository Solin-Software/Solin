#pragma once

#include "media_profiles.hpp"
#include "solin/media_engine/video_frame.hpp"

#include <cstdint>
#include <span>
#include <vector>

namespace solin::media_engine::windows_virtual_camera {

class DirectShowFrameAdapter final {
  public:
    explicit DirectShowFrameAdapter(DirectShowMediaProfile profile);

    [[nodiscard]] const DirectShowMediaProfile& profile() const noexcept;
    [[nodiscard]] bool write_direct_nv12(
        const VideoFrameView& source,
        std::span<std::uint8_t> destination) const noexcept;
    [[nodiscard]] bool adapt(const VideoFrameView& source,
                             std::span<std::uint8_t> destination) noexcept;
    [[nodiscard]] bool write_standby(
        std::span<std::uint8_t> destination) const noexcept;

  private:
    [[nodiscard]] bool write_nv12(const VideoFrameView& source,
                                  std::span<std::uint8_t> destination) noexcept;
    [[nodiscard]] bool convert_nv12_to_output(
        std::span<const std::uint8_t> nv12,
        std::span<std::uint8_t> destination) noexcept;

    DirectShowMediaProfile profile_{};
    std::vector<std::uint8_t> scaled_nv12_{};
    std::vector<std::uint8_t> i420_scratch_{};
    std::vector<std::uint8_t> standby_output_{};
};

} // namespace solin::media_engine::windows_virtual_camera
