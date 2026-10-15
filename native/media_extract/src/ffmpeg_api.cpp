#include "ffmpeg_api.hpp"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <sstream>
#include <vector>
#ifdef _WIN32
#include <windows.h>
#else
#include <dlfcn.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif
#endif

namespace solin::media {
namespace {
void bounded_log(void*, int level, const char* format, va_list arguments) {
    if (level > AV_LOG_WARNING)
        return;
    static std::mutex mutex;
    static std::size_t remaining = 8192;
    const std::lock_guard<std::mutex> lock(mutex);
    if (!remaining)
        return;
    char buffer[512];
    const int length = std::vsnprintf(buffer, sizeof(buffer), format, arguments);
    if (length <= 0)
        return;
    const auto count =
        std::min(remaining, std::min(sizeof(buffer) - 1, static_cast<std::size_t>(length)));
    // Logs are bytes on stderr only; no media header/payload ever passes here.
    std::fwrite(buffer, 1, count, stderr);
    remaining -= count;
}
void* load_library(const std::filesystem::path& path) {
#ifdef _WIN32
    return reinterpret_cast<void*>(
        LoadLibraryExW(path.c_str(), nullptr,
                       LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS));
#else
    return dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
#endif
}
void unload_library(void* handle) noexcept {
    if (!handle)
        return;
#ifdef _WIN32
    FreeLibrary(reinterpret_cast<HMODULE>(handle));
#else
    dlclose(handle);
#endif
}
template <typename T> void resolve(void* library, const char* name, T& function) {
#ifdef _WIN32
    const auto address = GetProcAddress(reinterpret_cast<HMODULE>(library), name);
#else
    const auto address = dlsym(library, name);
#endif
    if (!address)
        throw Failure("permanent", "ffmpeg_symbol_missing",
                      std::string("Missing FFmpeg API: ") + name);
    static_assert(sizeof(function) == sizeof(address));
    std::memcpy(&function, &address, sizeof(function));
}
template <typename T> std::string loaded_path(void* handle, T function) {
#ifdef _WIN32
    (void)function;
    std::vector<wchar_t> buffer(32768);
    const auto size = GetModuleFileNameW(reinterpret_cast<HMODULE>(handle), buffer.data(),
                                         static_cast<DWORD>(buffer.size()));
    if (!size || size >= buffer.size())
        throw Failure("permanent", "library_path_failed", "Cannot resolve loaded FFmpeg library");
    return std::filesystem::path(std::wstring(buffer.data(), size)).u8string();
#else
    (void)handle;
    void* address = nullptr;
    static_assert(sizeof(address) == sizeof(function));
    std::memcpy(&address, &function, sizeof(address));
    Dl_info info{};
    if (!dladdr(address, &info) || !info.dli_fname)
        throw Failure("permanent", "library_path_failed", "Cannot resolve loaded FFmpeg library");
    return std::filesystem::canonical(info.dli_fname).u8string();
#endif
}
std::string library_filename(const char* name, unsigned major) {
#ifdef _WIN32
    return std::string(name) + "-" + std::to_string(major) + ".dll";
#elif defined(__APPLE__)
    return std::string("lib") + name + "." + std::to_string(major) + ".dylib";
#else
    return std::string("lib") + name + ".so." + std::to_string(major);
#endif
}
} // namespace

FFmpeg::FFmpeg(const std::filesystem::path& directory) {
    constexpr const char* names[] = {"avformat", "avcodec", "avutil", "swscale"};
    // Load dependencies first, then demuxer. All paths are absolute; never PATH/CWD.
    try {
        for (const auto i : {2, 1, 3, 0}) {
            const auto index = static_cast<std::size_t>(i);
            handles_[index] = load_library(
                directory /
                library_filename(names[index], AV_VERSION_MAJOR(minimum_versions[index])));
            if (!handles_[index])
                throw Failure("permanent", "ffmpeg_library_missing",
                              std::string("Cannot load packaged ") + names[index]);
        }
#define SOLIN_VERSION(name, index) resolve(handles_[index], #name, name)
        SOLIN_VERSION(avformat_version, 0);
        SOLIN_VERSION(avcodec_version, 1);
        SOLIN_VERSION(avutil_version, 2);
        SOLIN_VERSION(swscale_version, 3);
#undef SOLIN_VERSION
        library_paths_ = {
            loaded_path(handles_[0], avformat_version), loaded_path(handles_[1], avcodec_version),
            loaded_path(handles_[2], avutil_version), loaded_path(handles_[3], swscale_version)};
        runtime_versions = {avformat_version(), avcodec_version(), avutil_version(),
                            swscale_version()};
        for (std::size_t i = 0; i < runtime_versions.size(); ++i) {
            // Also require the runtime >= SDK: later public fields may have been appended.
            if (AV_VERSION_MAJOR(runtime_versions[i]) != AV_VERSION_MAJOR(compiled_versions[i]) ||
                runtime_versions[i] < compiled_versions[i] ||
                runtime_versions[i] < minimum_versions[i]) {
                throw Failure("permanent", "ffmpeg_abi_mismatch",
                              std::string("Unsupported packaged ABI/version for ") + names[i]);
            }
        }
#define SOLIN_RESOLVE_FORMAT(name) resolve(handles_[0], #name, name);
#define SOLIN_RESOLVE_CODEC(name) resolve(handles_[1], #name, name);
#define SOLIN_RESOLVE_UTIL(name) resolve(handles_[2], #name, name);
#define SOLIN_RESOLVE_SCALE(name) resolve(handles_[3], #name, name);
        SOLIN_FORMAT_FUNCTIONS(SOLIN_RESOLVE_FORMAT)
        SOLIN_CODEC_FUNCTIONS(SOLIN_RESOLVE_CODEC)
        SOLIN_UTIL_FUNCTIONS(SOLIN_RESOLVE_UTIL)
        SOLIN_SCALE_FUNCTIONS(SOLIN_RESOLVE_SCALE)
#undef SOLIN_RESOLVE_FORMAT
#undef SOLIN_RESOLVE_CODEC
#undef SOLIN_RESOLVE_UTIL
#undef SOLIN_RESOLVE_SCALE
        av_log_set_callback(bounded_log);
        if (avformat_network_init() < 0)
            throw Failure("transient", "network_initialization_failed",
                          "Cannot initialize media networking");
        network_initialized_ = true;
    } catch (...) {
        close();
        throw;
    }
}
void FFmpeg::close() noexcept {
    if (network_initialized_)
        avformat_network_deinit();
    for (const auto i : {0, 3, 1, 2}) {
        unload_library(handles_[static_cast<std::size_t>(i)]);
        handles_[static_cast<std::size_t>(i)] = nullptr;
    }
}
FFmpeg::~FFmpeg() { close(); }
std::string FFmpeg::version_header() const {
    constexpr const char* names[] = {"avformat", "avcodec", "avutil", "swscale"};
    std::ostringstream output;
    output << "{\"protocol\":1,\"status\":\"ok\",\"abi\":{";
    for (std::size_t i = 0; i < runtime_versions.size(); ++i) {
        if (i)
            output << ',';
        output << '"' << names[i] << "\":{\"major\":" << AV_VERSION_MAJOR(runtime_versions[i])
               << ",\"compiled\":" << compiled_versions[i] << ",\"runtime\":" << runtime_versions[i]
               << ",\"minimum\":" << minimum_versions[i]
               << ",\"library\":" << json_string(library_paths_[i]) << '}';
    }
    auto header = output.str() + "}}";
    if (header.size() >= 65536)
        throw Failure("permanent", "version_header_too_large",
                      "Loaded FFmpeg paths exceed the protocol header limit");
    return header;
}

std::filesystem::path executable_directory() {
#ifdef _WIN32
    std::vector<wchar_t> buffer(32768);
    const auto size = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (!size || size >= buffer.size())
        throw Failure("permanent", "executable_path_failed", "Cannot resolve executable directory");
    return std::filesystem::path(std::wstring(buffer.data(), size)).parent_path();
#elif defined(__APPLE__)
    uint32_t size = 0;
    _NSGetExecutablePath(nullptr, &size);
    std::vector<char> buffer(size);
    if (_NSGetExecutablePath(buffer.data(), &size))
        throw Failure("permanent", "executable_path_failed", "Cannot resolve executable directory");
    return std::filesystem::canonical(buffer.data()).parent_path();
#else
    std::vector<char> buffer(4096);
    const auto size = readlink("/proc/self/exe", buffer.data(), buffer.size());
    if (size < 0 || static_cast<std::size_t>(size) == buffer.size())
        throw Failure("permanent", "executable_path_failed", "Cannot resolve executable directory");
    return std::filesystem::path(std::string(buffer.data(), static_cast<std::size_t>(size)))
        .parent_path();
#endif
}
std::filesystem::path runtime_directory() {
    return executable_directory() / SOLIN_FFMPEG_RUNTIME_SUBDIR;
}
} // namespace solin::media
