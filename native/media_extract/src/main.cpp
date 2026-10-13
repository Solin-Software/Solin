#include "extractor.hpp"
#include <cstdio>
#include <new>
#include <vector>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#include <windows.h>
#endif

namespace {
constexpr std::string_view resource_error =
    "{\"protocol\":1,\"status\":\"error\",\"error\":{\"kind\":\"transient\",\"code\":\"resource_"
    "exhausted\",\"message\":\"Media extraction ran out of memory\"}}";
constexpr std::string_view internal_error =
    "{\"protocol\":1,\"status\":\"error\",\"error\":{\"kind\":\"permanent\",\"code\":\"internal_"
    "error\",\"message\":\"Unexpected media extraction failure\"}}";
void signal_interrupted(int) { solin::media::interrupted = 1; }
bool write_output(std::string_view header, const solin::media::Image* image = nullptr) {
    if (std::fwrite(header.data(), 1, header.size(), stdout) != header.size() ||
        std::fputc('\n', stdout) == EOF)
        return false;
    if (image &&
        std::fwrite(image->pixels.data(), 1, image->pixels.size(), stdout) != image->pixels.size())
        return false;
    return std::fflush(stdout) == 0;
}
int run(const std::vector<std::string>& arguments) {
    using namespace solin::media;
    try {
        Deadline deadline;
        const auto options = parse_options(arguments);
        FFmpeg api(runtime_directory());
        deadline.check();
        if (options.version)
            return write_output(api.version_header()) ? 0 : 3;
        const auto result = extract(api, options, deadline);
        return write_output(success_header(result), result.image ? &*result.image : nullptr) ? 0
                                                                                             : 3;
    } catch (const Failure& failure) {
        return write_output(error_header(failure)) ? 2 : 3;
    } catch (const std::bad_alloc&) {
        return write_output(resource_error) ? 2 : 3;
    } catch (const std::exception&) {
        return write_output(internal_error) ? 2 : 3;
    }
}
void initialize_process() {
#ifdef _WIN32
    _setmode(_fileno(stdout), _O_BINARY);
    _setmode(_fileno(stderr), _O_BINARY);
#else
    std::signal(SIGPIPE, SIG_IGN);
#endif
    std::signal(SIGINT, signal_interrupted);
    std::signal(SIGTERM, signal_interrupted);
}
} // namespace
#ifdef _WIN32
int wmain(int argc, wchar_t** argv) {
    initialize_process();
    try {
        std::vector<std::string> arguments;
        for (int i = 1; i < argc; ++i) {
            const int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argv[i], -1,
                                                 nullptr, 0, nullptr, nullptr);
            if (!size)
                throw solin::media::Failure("permanent", "invalid_arguments",
                                            "Invalid Unicode argument");
            std::string text(static_cast<std::size_t>(size), '\0');
            if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argv[i], -1, text.data(), size,
                                     nullptr, nullptr))
                throw solin::media::Failure("permanent", "invalid_arguments",
                                            "Cannot convert Unicode argument");
            text.pop_back();
            arguments.push_back(std::move(text));
        }
        return run(arguments);
    } catch (const solin::media::Failure& failure) {
        return write_output(solin::media::error_header(failure)) ? 2 : 3;
    } catch (const std::exception&) {
        return write_output(resource_error) ? 2 : 3;
    }
}
#else
int main(int argc, char** argv) {
    initialize_process();
    try {
        return run(std::vector<std::string>(argv + 1, argv + argc));
    } catch (const std::exception&) {
        return write_output(resource_error) ? 2 : 3;
    }
}
#endif
