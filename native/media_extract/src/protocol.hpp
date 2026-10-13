#pragma once

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace solin::media {

enum class ImagePolicy { none, cover, frame };

struct Options {
    std::string source;
    ImagePolicy image_policy = ImagePolicy::none;
    int width = 640;
    int height = 360;
    bool title = true;
    bool duration = true;
    bool version = false;
};

class Failure : public std::runtime_error {
  public:
    std::string kind;
    std::string code;

    Failure(std::string kind, std::string code, std::string message);

  private:
    // Keep embedded NUL characters available to the JSON serializer.
    std::string message_;
    friend std::string error_header(const Failure& failure);
};

// Arguments exclude argv[0]. --version is accepted only on its own.
Options parse_options(const std::vector<std::string>& arguments);

struct Image {
    int width = 0;
    int height = 0;
    int stride = 0;
    std::string kind;
    std::vector<std::uint8_t> pixels;
};

struct Result {
    std::string title;
    std::int64_t duration_ms = 0;
    bool has_video = false;
    std::optional<Image> image;
};

// Headers omit the newline delimiter and binary image payload.
std::string success_header(const Result& result);
std::string error_header(const Failure& failure);
// Returns a quoted JSON string, replacing malformed UTF-8 with U+FFFD.
std::string json_string(std::string_view value);

} // namespace solin::media
