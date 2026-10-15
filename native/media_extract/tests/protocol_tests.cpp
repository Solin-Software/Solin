#include "protocol.hpp"

#include <array>
#include <iostream>
#include <limits>
#include <utility>

namespace {

using solin::media::error_header;
using solin::media::Failure;
using solin::media::Image;
using solin::media::ImagePolicy;
using solin::media::json_string;
using solin::media::parse_options;
using solin::media::Result;
using solin::media::success_header;

constexpr std::string_view replacement = "\xEF\xBF\xBD";

void require(bool condition, const std::string& description) {
    if (!condition)
        throw std::runtime_error(description);
}

template <typename Operation>
void require_failure(const Operation& operation, std::string_view code,
                     const std::string& description) {
    try {
        operation();
    } catch (const Failure& failure) {
        require(failure.kind == "permanent", description + ": wrong failure kind");
        require(failure.code == code, description + ": wrong failure code");
        require(std::string_view(failure.what()).size() != 0, description + ": missing diagnostic");
        return;
    }
    throw std::runtime_error(description + ": expected Failure");
}

void test_options() {
    const auto defaults = parse_options({"--source", u8"C:/mídia/日本語 🎵.mp4"});
    require(defaults.source == u8"C:/mídia/日本語 🎵.mp4", "Unicode source preserved");
    require(defaults.image_policy == ImagePolicy::none && defaults.width == 640 &&
                defaults.height == 360 && defaults.title && defaults.duration && !defaults.version,
            "extraction defaults");
    const auto selected =
        parse_options({"--duration", "0", "--height", "1", "--source", "a", "--width", "1",
                       "--title", "0", "--image-policy", "cover"});
    require(selected.source == "a" && selected.width == 1 && selected.height == 1 &&
                !selected.title && !selected.duration &&
                selected.image_policy == ImagePolicy::cover,
            "minimum bounds and reordered selectors");
    const auto frame =
        parse_options({"--source", "https://example.test/media?a=1&b=2", "--image-policy", "frame",
                       "--width", "640", "--height", "360", "--title", "1", "--duration", "1"});
    require(frame.image_policy == ImagePolicy::frame && frame.width == 640 && frame.height == 360 &&
                frame.title && frame.duration,
            "maximum bounds and enabled selectors");
    require(parse_options({"--source", "-media.mp4", "--image-policy", "none"}).source ==
                "-media.mp4",
            "a source beginning with a dash is text");
    const auto version = parse_options({"--version"});
    require(version.version && version.source.empty(), "standalone version requires no source");
}

void test_invalid_options() {
    const std::vector<std::vector<std::string>> invalid{
        {},
        {"--width", "1"},
        {"--source"},
        {"--source", ""},
        {"--source", std::string("a\0b", 3)},
        {"media.mp4"},
        {"--unknown", "x", "--source", "a"},
        {"--source=a"},
        {"--source", "a", "trailing"},
        {"--source", "a", "--width"},
        {"--source", "--height", "1"},
        {"--source", "--version"},
        {"--version", "--source", "a"},
        {"--source", "a", "--version"},
        {"--version", "--version"},
        {"--version", "1"},
        {"--source", "a", "--image-policy", "Cover"},
        {"--source", "a", "--image-policy", ""},
        {"--source", "a", "--image-policy", "video"},
    };
    for (const auto& arguments : invalid) {
        require_failure([&] { parse_options(arguments); }, "invalid_arguments",
                        "invalid CLI shape");
    }
    const std::array<std::pair<std::string, std::string>, 6> options{{
        {"--source", "a"},
        {"--image-policy", "none"},
        {"--width", "640"},
        {"--height", "360"},
        {"--title", "1"},
        {"--duration", "1"},
    }};
    for (const auto& option : options) {
        const std::vector<std::string> duplicate{"--source",    "a",          option.first,
                                                 option.second, option.first, option.second};
        require_failure([&] { parse_options(duplicate); }, "invalid_arguments",
                        "duplicate " + option.first);
    }
    const std::vector<std::string> invalid_numbers{
        "",
        "0",
        "-1",
        "+1",
        "1.0",
        "1x",
        " 1",
        "1 ",
        "\t1",
        "1\n",
        "2147483648",
        "999999999999999999999999999999999",
        std::string("1\0", 2),
    };
    for (const auto& name : {"--width", "--height"}) {
        for (const auto& value : invalid_numbers) {
            require_failure([&] { parse_options({"--source", "a", name, value}); },
                            "invalid_arguments", std::string("invalid dimension ") + name);
        }
    }
    require_failure([] { parse_options({"--source", "a", "--width", "641"}); }, "invalid_arguments",
                    "width above maximum");
    require_failure([] { parse_options({"--source", "a", "--height", "361"}); },
                    "invalid_arguments", "height above maximum");
    for (const auto& name : {"--title", "--duration"}) {
        for (const auto& value : {"true", "false", "2", "-1", "01", "", " 1"}) {
            require_failure([&] { parse_options({"--source", "a", name, value}); },
                            "invalid_arguments", std::string("invalid selector ") + name);
        }
    }
}

void test_utf8() {
    const std::vector<std::string> valid{
        "\x7F",
        "\xC2\x80",
        "\xDF\xBF",
        "\xE0\xA0\x80",
        "\xED\x9F\xBF",
        "\xEE\x80\x80",
        "\xEF\xBF\xBF",
        "\xF0\x90\x80\x80",
        "\xF4\x8F\xBF\xBF",
        u8"Título 日本語 🎵",
        "\xEF\xBB\xBF",
    };
    for (const auto& text : valid) {
        require(parse_options({"--source", text}).source == text, "valid UTF-8 accepted unchanged");
        require(json_string(text) == "\"" + text + "\"", "valid UTF-8 serialized unchanged");
    }
    const std::string mark(replacement);
    const std::vector<std::pair<std::string, std::string>> malformed{
        {"\x80", mark},
        {"\xBF", mark},
        {"\xC0\xAF", mark + mark},
        {"\xC1\xBF", mark + mark},
        {"\xF5\x80\x80\x80", mark + mark + mark + mark},
        {"\xFF", mark},
        {"\xE0\x80\x80", mark + mark + mark},
        {"\xED\xA0\x80", mark + mark + mark},
        {"\xF0\x80\x80\x80", mark + mark + mark + mark},
        {"\xF4\x90\x80\x80", mark + mark + mark + mark},
        {"\xC2", mark},
        {"\xE1\x80", mark},
        {"\xF1\x80\x80", mark},
        {"\xE1\x80"
         "Z",
         mark + "Z"},
        {"\xF1\x80"
         "Z",
         mark + "Z"},
        {"\xC2\xC2\xA2", mark + "\xC2\xA2"},
    };
    for (const auto& item : malformed) {
        require_failure([&] { parse_options({"--source", item.first}); }, "invalid_arguments",
                        "malformed source UTF-8");
        require(json_string(item.first) == "\"" + item.second + "\"",
                "malformed metadata uses maximal-subpart replacement");
        require(json_string("a" + item.first + u8"é") == "\"a" + item.second + u8"é\"",
                "malformed metadata preserves surrounding valid characters");
    }
}

void test_json_escaping() {
    require(json_string("") == "\"\"", "empty JSON string");
    require(json_string("\"\\/\b\f\n\r\t") == "\"\\\"\\\\\\/\\b\\f\\n\\r\\t\"",
            "JSON structural characters escaped");
    const std::string tag = u8"<tag title=\"Música\">line\nC:\\file</tag>";
    require(json_string(tag) == u8"\"<tag title=\\\"Música\\\">line\\nC:\\\\file<\\/tag>\"",
            "escaped metadata tag cannot introduce JSON or a protocol delimiter");
    require(json_string(std::string("a\0b", 3)) == "\"a\\u0000b\"", "embedded NUL escaped");
    constexpr char hex[] = "0123456789abcdef";
    for (int value = 0; value < 32; ++value) {
        std::string expected;
        switch (value) {
        case 8:
            expected = "\\b";
            break;
        case 9:
            expected = "\\t";
            break;
        case 10:
            expected = "\\n";
            break;
        case 12:
            expected = "\\f";
            break;
        case 13:
            expected = "\\r";
            break;
        default:
            expected = "\\u00";
            expected += hex[value >> 4];
            expected += hex[value & 0x0F];
        }
        require(json_string(std::string(1, static_cast<char>(value))) == "\"" + expected + "\"",
                "every JSON control byte escaped");
    }
}

void test_success_headers() {
    require(success_header(Result{}) ==
                "{\"protocol\":1,\"status\":\"ok\",\"title\":\"\",\"duration_ms\":0,"
                "\"has_video\":false,\"image\":null}",
            "exact empty success envelope");
    Result result;
    result.title = u8"Vídeo \"one\"\n<tag/>";
    result.duration_ms = std::numeric_limits<std::int64_t>::max();
    result.has_video = true;
    Image image;
    image.width = 2;
    image.height = 1;
    image.stride = 8;
    image.kind = "cover";
    image.pixels = {0, 10, 13, 34, 92, 128, 255, 0};
    result.image = image;
    const auto header = success_header(result);
    require(header ==
                u8"{\"protocol\":1,\"status\":\"ok\",\"title\":\"Vídeo \\\"one\\\"\\n<tag\\/>\","
                "\"duration_ms\":9223372036854775807,\"has_video\":true,"
                "\"image\":{\"width\":2,\"height\":1,\"stride\":8,\"kind\":\"cover\",\"bytes\":8}}",
            "exact image envelope including 64-bit duration");
    require(header.find('\n') == std::string::npos && header.find('\r') == std::string::npos &&
                header.back() == '}',
            "serializer emits no protocol delimiter");
    require(result.image->pixels == image.pixels, "serializer preserves binary payload");
    std::string wire = header + '\n';
    wire.append(reinterpret_cast<const char*>(image.pixels.data()), image.pixels.size());
    const auto delimiter = wire.find('\n');
    require(delimiter == header.size() && wire.size() - delimiter - 1 == 8,
            "binary newlines and NUL bytes do not change payload length");
    result.image->width = 640;
    result.image->height = 360;
    result.image->stride = 2560;
    result.image->kind = "frame";
    result.image->pixels.resize(921600);
    require(success_header(result).find("\"image\":{\"width\":640,\"height\":360,\"stride\":2560,"
                                        "\"kind\":\"frame\",\"bytes\":921600}") !=
                std::string::npos,
            "maximum image size advertised exactly");
}

void test_invalid_results() {
    Result negative;
    negative.duration_ms = -1;
    require_failure([&] { success_header(negative); }, "invalid_result", "negative duration");
    Result valid;
    valid.image = Image{1, 1, 4, "frame", {0, 0, 0, 255}};
    for (const auto& field : {"width", "height"}) {
        for (int value :
             {-1, 0, 641, std::numeric_limits<int>::max(), std::numeric_limits<int>::min()}) {
            auto result = valid;
            if (std::string_view(field) == "width")
                result.image->width = value;
            else
                result.image->height = value;
            require_failure([&] { success_header(result); }, "invalid_result", "invalid dimension");
        }
    }
    auto too_tall = valid;
    too_tall.image->height = 361;
    require_failure([&] { success_header(too_tall); }, "invalid_result", "height above maximum");
    for (int stride : {-1, 0, 3, 5, std::numeric_limits<int>::max()}) {
        auto result = valid;
        result.image->stride = stride;
        require_failure([&] { success_header(result); }, "invalid_result", "invalid packed stride");
    }
    for (std::size_t bytes : {0U, 3U, 5U, 8U}) {
        auto result = valid;
        result.image->pixels.resize(bytes);
        require_failure([&] { success_header(result); }, "invalid_result", "incorrect binary size");
    }
    for (const auto& kind : {"", "Cover", "video", "cover\n"}) {
        auto result = valid;
        result.image->kind = kind;
        require_failure([&] { success_header(result); }, "invalid_result", "invalid image kind");
    }
}

void test_metadata_limits() {
    Result result;
    constexpr std::size_t title_bytes = 8192;
    constexpr std::string_view prefix = "{\"protocol\":1,\"status\":\"ok\",\"title\":";
    constexpr std::string_view suffix = ",\"duration_ms\":0,\"has_video\":false,\"image\":null}";
    const auto require_title = [&](const std::string& expected) {
        require(success_header(result) ==
                    std::string(prefix) + json_string(expected) + std::string(suffix),
                "title cap preserves complete repaired UTF-8 characters");
    };
    result.title = std::string(title_bytes + 1, 'a');
    require_title(std::string(title_bytes, 'a'));
    for (const auto& character : {std::string(u8"é"), std::string(u8"日"), std::string(u8"🎵")}) {
        const auto boundary = title_bytes - character.size();
        result.title = std::string(boundary, 'a') + character + "Z";
        require_title(std::string(boundary, 'a') + character);
        result.title = std::string(boundary + 1, 'a') + character + "Z";
        require_title(std::string(boundary + 1, 'a'));
    }
    result.title = std::string(title_bytes - 3, 'a') + "\xFF" + "Z";
    require_title(std::string(title_bytes - 3, 'a') + std::string(replacement));
    result.title = std::string(title_bytes - 2, 'a') + "\xFF";
    require_title(std::string(title_bytes - 2, 'a'));
    result.title = std::string(title_bytes + 1, '\n');
    require_title(std::string(title_bytes, '\n'));
}

void test_success_header_byte_budget() {
    Result result;
    result.duration_ms = std::numeric_limits<std::int64_t>::max();
    // false takes one more byte than true in JSON.
    result.has_video = false;
    result.image = Image{640, 360, 2560, "cover", std::vector<std::uint8_t>(921600)};
    for (const char control : {'\0', '\x01', '\x1F'}) {
        // These controls each expand to six JSON bytes. The source greatly
        // exceeds the normalized title cap, exercising truncation as well.
        result.title = std::string(65536, control);
        const auto header = success_header(result);
        require(header.size() <= 65536, "maximally escaped title fits the 64 KiB header budget");
        const auto escaped_title = json_string(std::string(8192, control));
        require(escaped_title.size() == 49154, "8192 controls expand to 49152 bytes plus quotes");
        require(header.find("\"title\":" + escaped_title + ",\"duration_ms\":") !=
                    std::string::npos,
                "header preserves exactly the bounded control title");
        require(header.find('\n') == std::string::npos && header.back() == '}',
                "bounded header is complete and has no delimiter");
    }
}

void test_error_headers() {
    const Failure failure("permanent", "invalid_arguments", "bad \"source\"\nC:\\file");
    require(std::string_view(failure.what()) == "bad \"source\"\nC:\\file",
            "Failure runtime diagnostic");
    require(error_header(failure) ==
                "{\"protocol\":1,\"status\":\"error\",\"error\":{\"kind\":\"permanent\","
                "\"code\":\"invalid_arguments\",\"message\":\"bad \\\"source\\\"\\nC:\\\\file\"}}",
            "exact escaped error envelope");
    const auto require_message = [](const std::string& input, const std::string& expected) {
        require(error_header(Failure("transient", "io", input)) ==
                    "{\"protocol\":1,\"status\":\"error\",\"error\":{\"kind\":\"transient\","
                    "\"code\":\"io\","
                    "\"message\":" +
                        json_string(expected) + "}}",
                "bounded UTF-8 error message");
    };
    require_message(std::string(1025, 'a'), std::string(1024, 'a'));
    require_message(std::string(1020, 'a') + u8"🎵Z", std::string(1020, 'a') + u8"🎵");
    require_message(std::string(1021, 'a') + u8"🎵", std::string(1021, 'a'));
    require_message(std::string(1021, 'a') + "\xFF",
                    std::string(1021, 'a') + std::string(replacement));
    require_message(std::string(1022, 'a') + "\xFF", std::string(1022, 'a'));
    require_message(std::string(1025, '\n'), std::string(1024, '\n'));
    require_message(std::string("a\0b", 3), std::string("a\0b", 3));
    const auto malformed = error_header(Failure("\xFF", "\xE1\x80", "\xFF"));
    const auto mark = json_string(replacement);
    require(malformed == "{\"protocol\":1,\"status\":\"error\",\"error\":{\"kind\":" + mark +
                             ",\"code\":" + mark + ",\"message\":" + mark + "}}",
            "all error metadata repairs UTF-8");
}

} // namespace

int main() {
    const std::array<std::pair<const char*, void (*)()>, 9> tests{{
        {"options", test_options},
        {"invalid options", test_invalid_options},
        {"UTF-8", test_utf8},
        {"JSON escaping", test_json_escaping},
        {"success headers", test_success_headers},
        {"invalid results", test_invalid_results},
        {"metadata limits", test_metadata_limits},
        {"header byte budget", test_success_header_byte_budget},
        {"error headers", test_error_headers},
    }};
    int failures = 0;
    for (const auto& test : tests) {
        try {
            test.second();
        } catch (const std::exception& error) {
            std::cerr << "FAILED: " << test.first << ": " << error.what() << '\n';
            ++failures;
        }
    }
    if (failures == 0)
        std::cout << "Passed " << tests.size() << " protocol test groups\n";
    return failures == 0 ? 0 : 1;
}
