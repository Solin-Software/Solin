#include "protocol.hpp"

#include <array>
#include <charconv>
#include <limits>
#include <utility>

namespace solin::media {
namespace {

constexpr int max_width = 640;
constexpr int max_height = 360;
// Cap normalized UTF-8 before JSON escaping. Each byte can expand to at most
// six JSON bytes, so 8192 title bytes use at most 49152 bytes, leaving room for
// every validated envelope field within the consumer's 64 KiB header limit.
constexpr std::size_t title_limit = 8192;
constexpr std::size_t message_limit = 1024;
constexpr std::string_view replacement = "\xEF\xBF\xBD";
constexpr std::array<std::string_view, 7> option_names{
    "--source", "--image-policy", "--width", "--height", "--title", "--duration", "--version"};

struct Utf8Unit {
    std::size_t bytes;
    bool valid;
};

// Invalid sequences consume their maximal valid prefix, preserving the next
// independent character. Restricted second bytes reject overlong encodings,
// surrogate code points, and values beyond U+10FFFF.
Utf8Unit utf8_unit(std::string_view value) {
    const auto first = static_cast<unsigned char>(value[0]);
    if (first < 0x80) {
        return {1, true};
    }
    std::size_t length = 0;
    unsigned char second_min = 0x80;
    unsigned char second_max = 0xBF;
    if (first >= 0xC2 && first <= 0xDF) {
        length = 2;
    } else if (first >= 0xE0 && first <= 0xEF) {
        length = 3;
        if (first == 0xE0)
            second_min = 0xA0;
        if (first == 0xED)
            second_max = 0x9F;
    } else if (first >= 0xF0 && first <= 0xF4) {
        length = 4;
        if (first == 0xF0)
            second_min = 0x90;
        if (first == 0xF4)
            second_max = 0x8F;
    } else {
        return {1, false};
    }
    for (std::size_t index = 1; index < length; ++index) {
        if (index == value.size())
            return {index, false};
        const auto byte = static_cast<unsigned char>(value[index]);
        const auto minimum = index == 1 ? second_min : 0x80;
        const auto maximum = index == 1 ? second_max : 0xBF;
        if (byte < minimum || byte > maximum)
            return {index, false};
    }
    return {length, true};
}

bool valid_source(std::string_view source) {
    if (source.empty() || source.find('\0') != std::string_view::npos)
        return false;
    while (!source.empty()) {
        const auto unit = utf8_unit(source);
        if (!unit.valid)
            return false;
        source.remove_prefix(unit.bytes);
    }
    return true;
}

std::string quote_json(std::string_view value, std::size_t byte_limit) {
    std::string output = "\"";
    std::size_t bytes = 0;
    constexpr char hex[] = "0123456789abcdef";
    while (!value.empty()) {
        const auto unit = utf8_unit(value);
        const auto text = unit.valid ? value.substr(0, unit.bytes) : replacement;
        if (text.size() > byte_limit - bytes)
            break;
        bytes += text.size();
        if (text.size() > 1) {
            output.append(text);
        } else {
            const auto byte = static_cast<unsigned char>(text[0]);
            switch (byte) {
            case '"':
                output += "\\\"";
                break;
            case '\\':
                output += "\\\\";
                break;
            case '/':
                output += "\\/";
                break;
            case '\b':
                output += "\\b";
                break;
            case '\f':
                output += "\\f";
                break;
            case '\n':
                output += "\\n";
                break;
            case '\r':
                output += "\\r";
                break;
            case '\t':
                output += "\\t";
                break;
            default:
                if (byte < 0x20) {
                    output += "\\u00";
                    output += hex[byte >> 4];
                    output += hex[byte & 0x0F];
                } else {
                    output += static_cast<char>(byte);
                }
            }
        }
        value.remove_prefix(unit.bytes);
    }
    output += '"';
    return output;
}

[[noreturn]] void invalid_arguments(const char* message) {
    throw Failure("permanent", "invalid_arguments", message);
}

int dimension(std::string_view value, int maximum) {
    if (value.empty())
        invalid_arguments("Image dimensions require a decimal integer");
    for (const char byte : value) {
        if (byte < '0' || byte > '9') {
            invalid_arguments("Image dimensions require a decimal integer");
        }
    }
    int result = 0;
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
    if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size() || result < 1 ||
        result > maximum) {
        invalid_arguments("Image dimensions exceed the supported bounds");
    }
    return result;
}

bool boolean(std::string_view value) {
    if (value == "0")
        return false;
    if (value == "1")
        return true;
    invalid_arguments("Field selectors must be 0 or 1");
}

void validate_result(const Result& result) {
    if (result.duration_ms < 0) {
        throw Failure("permanent", "invalid_result", "Media duration cannot be negative");
    }
    if (!result.image)
        return;
    const auto& image = *result.image;
    // Bound dimensions before multiplication, so invalid signed integers never
    // overflow while checking the packed BGRA contract.
    if (image.width < 1 || image.width > max_width || image.height < 1 ||
        image.height > max_height || image.stride != image.width * 4 ||
        image.pixels.size() !=
            static_cast<std::size_t>(image.stride) * static_cast<std::size_t>(image.height) ||
        (image.kind != "cover" && image.kind != "frame")) {
        throw Failure("permanent", "invalid_result", "Invalid packed BGRA image");
    }
}

} // namespace

Failure::Failure(std::string kind, std::string code, std::string message)
    : std::runtime_error(message), kind(std::move(kind)), code(std::move(code)),
      message_(std::move(message)) {}

Options parse_options(const std::vector<std::string>& arguments) {
    Options options;
    if (arguments.size() == 1 && arguments[0] == "--version") {
        options.version = true;
        return options;
    }
    std::array<bool, option_names.size()> seen{};
    for (std::size_t index = 0; index < arguments.size(); index += 2) {
        const std::string_view name = arguments[index];
        std::size_t option = 0;
        while (option < option_names.size() && option_names[option] != name)
            ++option;
        if (option == option_names.size())
            invalid_arguments("Unknown media extraction option");
        if (name == "--version")
            invalid_arguments("--version must be used alone");
        if (seen[option])
            invalid_arguments("Duplicate media extraction option");
        seen[option] = true;
        if (index + 1 == arguments.size())
            invalid_arguments("Missing media extraction option value");
        const std::string_view value = arguments[index + 1];
        for (const auto known : option_names) {
            if (value == known)
                invalid_arguments("Missing media extraction option value");
        }
        switch (option) {
        case 0:
            if (!valid_source(value))
                invalid_arguments("Media source must be nonempty UTF-8 without NUL");
            options.source = value;
            break;
        case 1:
            if (value == "none")
                options.image_policy = ImagePolicy::none;
            else if (value == "cover")
                options.image_policy = ImagePolicy::cover;
            else if (value == "frame")
                options.image_policy = ImagePolicy::frame;
            else
                invalid_arguments("Image policy must be none, cover, or frame");
            break;
        case 2:
            options.width = dimension(value, max_width);
            break;
        case 3:
            options.height = dimension(value, max_height);
            break;
        case 4:
            options.title = boolean(value);
            break;
        case 5:
            options.duration = boolean(value);
            break;
        default:
            invalid_arguments("Unknown media extraction option");
        }
    }
    if (!seen[0])
        invalid_arguments("A media source is required");
    return options;
}

std::string json_string(std::string_view value) {
    return quote_json(value, std::numeric_limits<std::size_t>::max());
}

std::string success_header(const Result& result) {
    validate_result(result);
    std::string output = "{\"protocol\":1,\"status\":\"ok\",\"title\":";
    output += quote_json(result.title, title_limit);
    output += ",\"duration_ms\":" + std::to_string(result.duration_ms);
    output += ",\"has_video\":";
    output += result.has_video ? "true" : "false";
    output += ",\"image\":";
    if (result.image) {
        const auto& image = *result.image;
        output += "{\"width\":" + std::to_string(image.width);
        output += ",\"height\":" + std::to_string(image.height);
        output += ",\"stride\":" + std::to_string(image.stride);
        output += ",\"kind\":" + json_string(image.kind);
        output += ",\"bytes\":" + std::to_string(image.pixels.size()) + "}";
    } else {
        output += "null";
    }
    output += '}';
    return output;
}

std::string error_header(const Failure& failure) {
    return "{\"protocol\":1,\"status\":\"error\",\"error\":{\"kind\":" + json_string(failure.kind) +
           ",\"code\":" + json_string(failure.code) +
           ",\"message\":" + quote_json(failure.message_, message_limit) + "}}";
}

} // namespace solin::media
