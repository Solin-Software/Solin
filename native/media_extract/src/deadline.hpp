#pragma once
#include "protocol.hpp"
#include <algorithm>
#include <chrono>
#include <csignal>

namespace solin::media {
inline volatile std::sig_atomic_t interrupted = 0;
class Deadline final {
  public:
    using Clock = std::chrono::steady_clock;
    explicit Deadline(Clock::time_point start = Clock::now())
        : total_(start + std::chrono::seconds(35)), phase_(start + std::chrono::seconds(15)) {}
    void begin_extraction(Clock::time_point now = Clock::now()) {
        check_at(now);
        phase_ = std::min(total_, now + std::chrono::seconds(20));
    }
    bool expired_at(Clock::time_point now) const noexcept {
        return interrupted || now >= phase_ || now >= total_;
    }
    void check_at(Clock::time_point now) const {
        if (interrupted)
            throw Failure("transient", "cancelled", "Media extraction was cancelled");
        if (expired_at(now))
            throw Failure("transient", "timeout", "Media extraction deadline exceeded");
    }
    void check() const { check_at(Clock::now()); }
    static int callback(void* opaque) noexcept {
        return static_cast<Deadline*>(opaque)->expired_at(Clock::now()) ? 1 : 0;
    }

  private:
    Clock::time_point total_, phase_;
};
} // namespace solin::media
