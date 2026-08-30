#pragma once

#include <algorithm>
#include <iterator>
#include <string>
#include <string_view>

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
#include <gst/gst.h>
#endif

namespace solin::media_engine {

[[nodiscard]] inline bool is_ascii_hex_digit(const char character) noexcept {
    return (character >= '0' && character <= '9') ||
           (character >= 'A' && character <= 'F') ||
           (character >= 'a' && character <= 'f');
}

[[nodiscard]] inline std::string extract_hresult_code(
    const std::string_view message) {
    constexpr std::size_t hresult_digits = 8U;
    for (std::size_t offset = 0U; offset + 2U + hresult_digits <= message.size();
         ++offset) {
        if (message[offset] != '0' ||
            (message[offset + 1U] != 'x' && message[offset + 1U] != 'X')) {
            continue;
        }
        const auto digits = message.substr(offset + 2U, hresult_digits);
        if (!std::ranges::all_of(digits, is_ascii_hex_digit) ||
            (offset + 2U + hresult_digits < message.size() &&
             is_ascii_hex_digit(message[offset + 2U + hresult_digits]))) {
            continue;
        }
        std::string result{"0x"};
        result.reserve(2U + hresult_digits);
        std::ranges::transform(digits, std::back_inserter(result),
                               [](const char character) {
                                   return character >= 'a' && character <= 'f'
                                              ? static_cast<char>(character - ('a' - 'A'))
                                              : character;
                               });
        return result;
    }
    return {};
}

#ifdef SOLIN_MEDIA_ENGINE_HAS_GSTREAMER
[[nodiscard]] inline std::string gstreamer_native_error_code(GstMessage* message) {
    if (message == nullptr || GST_MESSAGE_TYPE(message) != GST_MESSAGE_ERROR) {
        return {};
    }
    GError* error = nullptr;
    gchar* debug = nullptr;
    gst_message_parse_error(message, &error, &debug);
    std::string result;
    if (error != nullptr) {
        result = extract_hresult_code(error->message == nullptr
                                          ? std::string_view{}
                                          : std::string_view{error->message});
        if (result.empty()) {
            const auto* domain = g_quark_to_string(error->domain);
            result = std::string{domain == nullptr ? "gstreamer" : domain} + ':' +
                     std::to_string(error->code);
        }
        g_error_free(error);
    }
    g_free(debug);
    return result;
}
#endif

} // namespace solin::media_engine
