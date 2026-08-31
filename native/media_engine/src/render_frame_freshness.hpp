#pragma once

#include <cstdint>
#include <optional>

namespace solin::media_engine {

// Protected by the owning graph's frame mutex. Output callbacks can outlive a
// suspended activation, so publication requires the causal timestamp of a new
// input submission, not merely a callback received after resume.
class RenderFrameFreshness final {
  public:
    void invalidate() noexcept {
        ++generation_;
        minimum_timestamp_.reset();
    }

    void submitted(const std::uint64_t timestamp) noexcept {
        if (!minimum_timestamp_.has_value()) {
            minimum_timestamp_ = timestamp;
        }
    }

    [[nodiscard]] bool acknowledge(const std::uint64_t timestamp) noexcept {
        if (!minimum_timestamp_.has_value() || timestamp < minimum_timestamp_.value()) {
            return false;
        }
        published_generation_ = generation_;
        return true;
    }

    [[nodiscard]] std::uint64_t generation() const noexcept { return generation_; }
    [[nodiscard]] bool ready(const std::uint64_t generation) const noexcept {
        return generation == generation_ && published_generation_ == generation_;
    }

  private:
    std::uint64_t generation_{1U};
    std::uint64_t published_generation_{0U};
    std::optional<std::uint64_t> minimum_timestamp_{0U};
};

} // namespace solin::media_engine
