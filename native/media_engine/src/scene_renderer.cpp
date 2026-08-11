#include "solin/media_engine/scene_renderer.hpp"

#include <stdexcept>
#include <utility>

namespace solin::media_engine {

SceneRendererError::SceneRendererError(std::string error_code, std::string message)
    : std::runtime_error(std::move(message)), error_code_(std::move(error_code)) {
    if (error_code_.empty()) {
        throw std::invalid_argument("scene renderer error code is required");
    }
}

const std::string& SceneRendererError::error_code() const noexcept { return error_code_; }

} // namespace solin::media_engine
