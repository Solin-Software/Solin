#include "broker_client.hpp"
#include "standby_resources.h"

#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#include <bcrypt.h>
#include <mferror.h>
#include <windows.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cwchar>
#include <condition_variable>
#include <filesystem>
#include <ranges>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace solin::media_engine::windows_virtual_camera {
namespace {

using namespace std::chrono_literals;
constexpr auto kBrokerTimeout = 500ms;
constexpr auto kReconnectInterval = 250ms;
constexpr auto kProducerStaleTimeout = 1500ms;
constexpr std::wstring_view kPipePrefix{L"\\\\.\\pipe\\Solin.VirtualCamera."};

class UniqueHandle final {
  public:
    explicit UniqueHandle(HANDLE value = nullptr) noexcept : value_(value) {}
    ~UniqueHandle() {
        if (value_ != nullptr && value_ != INVALID_HANDLE_VALUE) {
            static_cast<void>(CloseHandle(value_));
        }
    }
    UniqueHandle(const UniqueHandle&) = delete;
    UniqueHandle& operator=(const UniqueHandle&) = delete;
    UniqueHandle(UniqueHandle&& other) noexcept
        : value_(std::exchange(other.value_, nullptr)) {}
    UniqueHandle& operator=(UniqueHandle&& other) noexcept {
        if (this != &other) {
            if (value_ != nullptr && value_ != INVALID_HANDLE_VALUE) {
                static_cast<void>(CloseHandle(value_));
            }
            value_ = std::exchange(other.value_, nullptr);
        }
        return *this;
    }
    [[nodiscard]] HANDLE get() const noexcept { return value_; }
    [[nodiscard]] explicit operator bool() const noexcept {
        return value_ != nullptr && value_ != INVALID_HANDLE_VALUE;
    }

  private:
    HANDLE value_{nullptr};
};

class CoTaskString final {
  public:
    ~CoTaskString() { CoTaskMemFree(value_); }
    [[nodiscard]] wchar_t** address() noexcept { return &value_; }
    [[nodiscard]] const wchar_t* get() const noexcept { return value_; }

  private:
    wchar_t* value_{nullptr};
};

[[nodiscard]] bool valid_pipe_name(const std::wstring_view value) noexcept {
    return value.starts_with(kPipePrefix) && value.size() <= 256U &&
           std::ranges::all_of(value, [](const wchar_t character) {
               return character >= 32 && character <= 126;
           });
}

[[nodiscard]] bool cancel_and_drain(HANDLE pipe, OVERLAPPED& overlapped,
                                    DWORD& transferred) noexcept {
    static_cast<void>(CancelIoEx(pipe, &overlapped));
    return GetOverlappedResult(pipe, &overlapped, &transferred, TRUE) != FALSE;
}

[[nodiscard]] bool pipe_io(HANDLE pipe, const std::span<std::uint8_t> bytes,
                           const bool write) noexcept {
    const auto deadline = std::chrono::steady_clock::now() + kBrokerTimeout;
    std::size_t offset = 0U;
    while (offset < bytes.size()) {
        const UniqueHandle event{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
        if (!event) {
            return false;
        }
        OVERLAPPED overlapped{};
        overlapped.hEvent = event.get();
        DWORD transferred = 0U;
        const auto remaining = static_cast<DWORD>(bytes.size() - offset);
        const auto started = write
                                 ? WriteFile(pipe, bytes.data() + offset, remaining,
                                             &transferred, &overlapped)
                                 : ReadFile(pipe, bytes.data() + offset, remaining,
                                            &transferred, &overlapped);
        if (started == FALSE) {
            if (GetLastError() != ERROR_IO_PENDING) {
                return false;
            }
            const auto now = std::chrono::steady_clock::now();
            if (now >= deadline) {
                return cancel_and_drain(pipe, overlapped, transferred) &&
                       transferred == remaining;
            }
            const auto wait_ms = static_cast<DWORD>(
                std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now)
                    .count());
            if (WaitForSingleObject(event.get(), wait_ms) != WAIT_OBJECT_0) {
                return cancel_and_drain(pipe, overlapped, transferred) &&
                       transferred == remaining;
            }
            if (GetOverlappedResult(pipe, &overlapped, &transferred, FALSE) == FALSE) {
                return false;
            }
        }
        if (transferred == 0U) {
            return false;
        }
        offset += transferred;
    }
    return true;
}

[[nodiscard]] HRESULT read_activation(
    IMFAttributes* attributes, std::wstring& pipe_name,
    VirtualCameraBrokerToken& token) {
    if (attributes == nullptr) {
        return E_INVALIDARG;
    }
    CoTaskString raw_pipe;
    UINT32 pipe_length = 0U;
    auto result = attributes->GetAllocatedString(kBrokerPipeAttribute,
                                                  raw_pipe.address(), &pipe_length);
    if (FAILED(result) || raw_pipe.get() == nullptr || pipe_length == 0U) {
        return MF_E_ATTRIBUTENOTFOUND;
    }
    pipe_name.assign(raw_pipe.get(), pipe_length);
    if (!valid_pipe_name(pipe_name) ||
        pipe_name != kBrokerPipeNameUtf16) {
        return E_INVALIDARG;
    }
    UINT32 token_size = 0U;
    result = attributes->GetBlobSize(kBrokerTokenAttribute, &token_size);
    if (FAILED(result) || token_size != token.size()) {
        return E_INVALIDARG;
    }
    result = attributes->GetBlob(kBrokerTokenAttribute, token.data(),
                                 static_cast<UINT32>(token.size()), &token_size);
    return SUCCEEDED(result) && token == kBrokerContractToken ? S_OK
                                                              : E_INVALIDARG;
}

[[nodiscard]] HRESULT connect_pipe(const std::wstring& pipe_name,
                                   UniqueHandle& pipe) noexcept {
    if (WaitNamedPipeW(pipe_name.c_str(), static_cast<DWORD>(kBrokerTimeout.count() *
                                                             1'000)) == FALSE) {
        return HRESULT_FROM_WIN32(GetLastError());
    }
    pipe = UniqueHandle{CreateFileW(pipe_name.c_str(), GENERIC_READ | GENERIC_WRITE,
                                    0U, nullptr, OPEN_EXISTING,
                                    FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED |
                                        SECURITY_SQOS_PRESENT |
                                        SECURITY_IDENTIFICATION,
                                    nullptr)};
    return pipe ? S_OK : HRESULT_FROM_WIN32(GetLastError());
}

[[nodiscard]] std::wstring utf8_to_utf16(const std::string_view value) {
    if (value.empty() || value.size() > static_cast<std::size_t>(INT_MAX)) {
        throw std::invalid_argument("virtual_camera_broker_path_invalid");
    }
    const auto required = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS, value.data(), static_cast<int>(value.size()),
        nullptr, 0);
    if (required <= 0) {
        throw std::invalid_argument("virtual_camera_broker_path_invalid");
    }
    std::wstring result(static_cast<std::size_t>(required), L'\0');
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value.data(),
                            static_cast<int>(value.size()), result.data(),
                            required) != required) {
        throw std::invalid_argument("virtual_camera_broker_path_invalid");
    }
    return result;
}

[[nodiscard]] std::unique_ptr<SharedVideoFrameReader> open_frame_reader(
    const VirtualCameraBrokerResponse& response) {
    const auto path = utf8_to_utf16(response.mapping_file_path_utf8);
    if (!std::filesystem::path{path}.is_absolute()) {
        throw std::invalid_argument("virtual_camera_broker_path_invalid");
    }
    const UniqueHandle file{CreateFileW(
        path.c_str(), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_RANDOM_ACCESS, nullptr)};
    if (!file) {
        throw std::runtime_error("virtual_camera_broker_file_unavailable");
    }
    LARGE_INTEGER file_size{};
    if (GetFileSizeEx(file.get(), &file_size) == FALSE || file_size.QuadPart < 0 ||
        static_cast<std::uint64_t>(file_size.QuadPart) != response.mapping_size) {
        throw std::runtime_error("virtual_camera_broker_file_invalid");
    }
    const UniqueHandle mapping{CreateFileMappingW(file.get(), nullptr, PAGE_READONLY,
                                                   0U, 0U, nullptr)};
    if (!mapping) {
        throw std::runtime_error("virtual_camera_broker_file_unavailable");
    }
    return make_shared_video_frame_reader(
        reinterpret_cast<std::uintptr_t>(mapping.get()), response.mapping_size);
}

struct RgbPixel final {
    std::uint8_t red{0U};
    std::uint8_t green{0U};
    std::uint8_t blue{0U};
};

constexpr RgbPixel kStandbyBackground{15U, 21U, 32U};
const std::uint8_t kResourceModuleAnchor = 0U;

void render_standby_icon(std::vector<RgbPixel>& pixels,
                         const std::uint32_t width,
                         const std::uint32_t height) noexcept {
    HMODULE module = nullptr;
    if (GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&kResourceModuleAnchor), &module) == FALSE) {
        return;
    }
    const auto shorter_edge = (std::min)(width, height);
    const auto extent = static_cast<int>(
        std::clamp(shorter_edge * 3U / 10U, 128U, 512U));
    const auto icon = reinterpret_cast<HICON>(LoadImageW(
        module, MAKEINTRESOURCEW(SOLIN_STANDBY_ICON), IMAGE_ICON, extent, extent,
        LR_DEFAULTCOLOR));
    if (icon == nullptr) {
        return;
    }
    BITMAPINFO bitmap_info{};
    bitmap_info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bitmap_info.bmiHeader.biWidth = static_cast<LONG>(width);
    bitmap_info.bmiHeader.biHeight = -static_cast<LONG>(height);
    bitmap_info.bmiHeader.biPlanes = 1U;
    bitmap_info.bmiHeader.biBitCount = 32U;
    bitmap_info.bmiHeader.biCompression = BI_RGB;
    void* bitmap_bytes = nullptr;
    const auto bitmap = CreateDIBSection(nullptr, &bitmap_info, DIB_RGB_COLORS,
                                         &bitmap_bytes, nullptr, 0U);
    const auto device_context = CreateCompatibleDC(nullptr);
    if (bitmap == nullptr || bitmap_bytes == nullptr || device_context == nullptr) {
        if (device_context != nullptr) {
            static_cast<void>(DeleteDC(device_context));
        }
        if (bitmap != nullptr) {
            static_cast<void>(DeleteObject(bitmap));
        }
        static_cast<void>(DestroyIcon(icon));
        return;
    }
    auto* bgra = static_cast<std::uint8_t*>(bitmap_bytes);
    for (std::size_t index = 0U; index < pixels.size(); ++index) {
        bgra[index * 4U] = kStandbyBackground.blue;
        bgra[index * 4U + 1U] = kStandbyBackground.green;
        bgra[index * 4U + 2U] = kStandbyBackground.red;
        bgra[index * 4U + 3U] = 255U;
    }
    const auto previous_bitmap = SelectObject(device_context, bitmap);
    if (previous_bitmap == nullptr || previous_bitmap == HGDI_ERROR) {
        static_cast<void>(DeleteDC(device_context));
        static_cast<void>(DeleteObject(bitmap));
        static_cast<void>(DestroyIcon(icon));
        return;
    }
    const auto x = (static_cast<int>(width) - extent) / 2;
    const auto y = (static_cast<int>(height) - extent) / 2;
    const auto rendered = DrawIconEx(device_context, x, y, icon, extent, extent,
                                     0U, nullptr, DI_NORMAL);
    if (rendered != FALSE) {
        for (std::size_t index = 0U; index < pixels.size(); ++index) {
            pixels[index] = {
                .red = bgra[index * 4U + 2U],
                .green = bgra[index * 4U + 1U],
                .blue = bgra[index * 4U],
            };
        }
    }
    static_cast<void>(SelectObject(device_context, previous_bitmap));
    static_cast<void>(DeleteDC(device_context));
    static_cast<void>(DeleteObject(bitmap));
    static_cast<void>(DestroyIcon(icon));
}

[[nodiscard]] std::uint8_t limited_luma(const RgbPixel pixel) noexcept {
    return static_cast<std::uint8_t>(std::clamp(
        16.0 + 0.182586 * pixel.red + 0.614231 * pixel.green +
            0.062007 * pixel.blue,
        16.0, 235.0));
}

[[nodiscard]] std::uint8_t limited_chroma_blue(const RgbPixel pixel) noexcept {
    return static_cast<std::uint8_t>(std::clamp(
        128.0 - 0.100644 * pixel.red - 0.338572 * pixel.green +
            0.439216 * pixel.blue,
        16.0, 240.0));
}

[[nodiscard]] std::uint8_t limited_chroma_red(const RgbPixel pixel) noexcept {
    return static_cast<std::uint8_t>(std::clamp(
        128.0 + 0.439216 * pixel.red - 0.398942 * pixel.green -
            0.040274 * pixel.blue,
        16.0, 240.0));
}

[[nodiscard]] PackedVideoFrame standby_frame(
    const VirtualCameraBrokerResponse& response) {
    PackedVideoFrame frame{
        .sequence = 0U,
        .duration_ns =
            1'000'000'000ULL * response.fps_denominator / response.fps_numerator,
        .discontinuity = true,
        .width = response.layout.width,
        .height = response.layout.height,
        .pixel_format = response.layout.pixel_format,
        .plane_strides = response.layout.plane_strides,
        .plane_offsets = response.layout.plane_offsets,
        .bytes = std::vector<std::uint8_t>(
            static_cast<std::size_t>(response.layout.payload_size), 0U),
    };
    if (frame.pixel_format == VideoFramePixelFormat::nv12) {
        const auto width = frame.width;
        const auto height = frame.height;
        std::vector<RgbPixel> pixels(static_cast<std::size_t>(width) * height,
                                     kStandbyBackground);
        render_standby_icon(pixels, width, height);
        for (std::uint32_t row = 0U; row < height; ++row) {
            for (std::uint32_t column = 0U; column < width; ++column) {
                const auto index = static_cast<std::size_t>(row) * width + column;
                frame.bytes[index] = limited_luma(pixels[index]);
            }
        }
        const auto chroma_offset = static_cast<std::size_t>(frame.plane_offsets[1]);
        for (std::uint32_t row = 0U; row < height; row += 2U) {
            for (std::uint32_t column = 0U; column < width; column += 2U) {
                std::uint32_t blue_sum = 0U;
                std::uint32_t red_sum = 0U;
                for (std::uint32_t dy = 0U; dy < 2U; ++dy) {
                    for (std::uint32_t dx = 0U; dx < 2U; ++dx) {
                        const auto pixel = pixels[
                            static_cast<std::size_t>(row + dy) * width + column + dx];
                        blue_sum += limited_chroma_blue(pixel);
                        red_sum += limited_chroma_red(pixel);
                    }
                }
                const auto index = chroma_offset +
                                   static_cast<std::size_t>(row / 2U) * width + column;
                frame.bytes[index] = static_cast<std::uint8_t>(blue_sum / 4U);
                frame.bytes[index + 1U] = static_cast<std::uint8_t>(red_sum / 4U);
            }
        }
    }
    return frame;
}

[[nodiscard]] VirtualCameraBrokerResponse fallback_response(
    IMFAttributes* attributes) {
    UINT32 width = 0U;
    UINT32 height = 0U;
    UINT32 fps_numerator = 0U;
    UINT32 fps_denominator = 0U;
    if (FAILED(attributes->GetUINT32(kFrameWidthAttribute, &width)) ||
        FAILED(attributes->GetUINT32(kFrameHeightAttribute, &height)) ||
        FAILED(attributes->GetUINT32(kFrameRateNumeratorAttribute,
                                     &fps_numerator)) ||
        FAILED(attributes->GetUINT32(kFrameRateDenominatorAttribute,
                                     &fps_denominator)) ||
        fps_numerator == 0U || fps_denominator == 0U) {
        throw std::invalid_argument("virtual_camera_broker_response_invalid");
    }
    return {
        .status = VirtualCameraBrokerStatus::ok,
        .generation = 0U,
        .layout = packed_video_frame_layout(width, height,
                                             VideoFramePixelFormat::nv12),
        .fps_numerator = fps_numerator,
        .fps_denominator = fps_denominator,
    };
}

[[nodiscard]] bool response_matches(
    const VirtualCameraBrokerResponse& response,
    const PackedVideoFrameLayout& layout, const std::uint32_t fps_numerator,
    const std::uint32_t fps_denominator) noexcept {
    return response.status == VirtualCameraBrokerStatus::ok &&
           response.layout == layout &&
           response.fps_numerator == fps_numerator &&
           response.fps_denominator == fps_denominator;
}

[[nodiscard]] HRESULT open_broker_generation(
    const std::wstring& pipe_name, const VirtualCameraBrokerToken& token,
    VirtualCameraBrokerResponse& response,
    std::unique_ptr<SharedVideoFrameReader>& reader) noexcept {
    try {
        VirtualCameraBrokerRequest request{.token = token};
        if (BCryptGenRandom(nullptr, request.nonce.data(),
                            static_cast<ULONG>(request.nonce.size()),
                            BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0) {
            return E_FAIL;
        }
        UniqueHandle pipe;
        auto result = connect_pipe(pipe_name, pipe);
        if (FAILED(result)) {
            return result;
        }
        auto request_bytes = encode_virtual_camera_broker_request(request);
        std::array<std::uint8_t, kVirtualCameraBrokerResponseSize> response_bytes{};
        if (!pipe_io(pipe.get(), request_bytes, true) ||
            !pipe_io(pipe.get(), response_bytes, false)) {
            return HRESULT_FROM_WIN32(ERROR_TIMEOUT);
        }
        auto decoded = decode_virtual_camera_broker_response(response_bytes);
        if (decoded.nonce != request.nonce ||
            decoded.status != VirtualCameraBrokerStatus::ok) {
            return E_ACCESSDENIED;
        }
        auto opened_reader = open_frame_reader(decoded);
        response = std::move(decoded);
        reader = std::move(opened_reader);
        return S_OK;
    } catch (const std::invalid_argument&) {
        return E_INVALIDARG;
    } catch (...) {
        return E_FAIL;
    }
}

} // namespace

BrokerFrameProvider::BrokerFrameProvider(
    std::wstring pipe_name, const VirtualCameraBrokerToken token,
    PackedVideoFrameLayout layout, const std::uint32_t fps_numerator,
    const std::uint32_t fps_denominator)
    : pipe_name_(std::move(pipe_name)), token_(token), layout_(std::move(layout)),
      fps_numerator_(fps_numerator), fps_denominator_(fps_denominator) {
    if (pipe_name_.empty() || layout_.pixel_format != VideoFramePixelFormat::nv12 ||
        fps_numerator_ == 0U || fps_denominator_ == 0U) {
        throw std::invalid_argument("virtual_camera_broker_response_invalid");
    }
    VirtualCameraBrokerResponse response{
        .status = VirtualCameraBrokerStatus::ok,
        .layout = layout_,
        .fps_numerator = fps_numerator_,
        .fps_denominator = fps_denominator_,
    };
    standby_frame_ = standby_frame(response);
    last_progress_ = std::chrono::steady_clock::now();
    reconnect_worker_ = std::thread([this]() noexcept { reconnect_loop(); });
}

BrokerFrameProvider::~BrokerFrameProvider() {
    stop_requested_.store(true);
    reconnect_wakeup_.notify_all();
    if (reconnect_worker_.joinable()) {
        reconnect_worker_.join();
    }
}

bool BrokerFrameProvider::visit_frame(
    const SharedVideoFrameReader::FrameVisitor& visitor) {
    if (!visitor) {
        return false;
    }
    std::unique_lock lock{mutex_};
    const auto now = std::chrono::steady_clock::now();
    if (reader_ != nullptr) {
        try {
            const auto heartbeat = reader_->heartbeat();
            if (heartbeat != 0U && heartbeat != last_heartbeat_) {
                last_heartbeat_ = heartbeat;
                last_progress_ = now;
            }
            if (now - last_progress_ >= kProducerStaleTimeout) {
                reader_.reset();
                generation_.store(0U);
                last_heartbeat_ = 0U;
                reconnect_requested_ = true;
                reconnect_wakeup_.notify_one();
            }
            bool visited_current = false;
            if (reader_ != nullptr && reader_->visit_current_frame(
                    [&visitor, &visited_current](const VideoFrameView& frame) {
                        visited_current = true;
                        visitor(frame);
                    })) {
                return true;
            }
            if (visited_current) {
                return false;
            }
        } catch (...) {
            reader_.reset();
            generation_.store(0U);
            last_heartbeat_ = 0U;
            reconnect_requested_ = true;
            reconnect_wakeup_.notify_one();
        }
    }
    visitor(video_frame_view(standby_frame_));
    return true;
}

const PackedVideoFrameLayout& BrokerFrameProvider::layout() const noexcept {
    return layout_;
}

std::uint32_t BrokerFrameProvider::fps_numerator() const noexcept {
    return fps_numerator_;
}

std::uint32_t BrokerFrameProvider::fps_denominator() const noexcept {
    return fps_denominator_;
}

std::uint64_t BrokerFrameProvider::generation() const noexcept {
    return generation_.load();
}

void BrokerFrameProvider::reconnect_loop() noexcept {
    while (!stop_requested_.load()) {
        {
            std::unique_lock lock{mutex_};
            reconnect_wakeup_.wait_for(
                lock, kReconnectInterval,
                [this]() { return stop_requested_.load() || reconnect_requested_; });
            if (stop_requested_.load()) {
                return;
            }
            if (!reconnect_requested_ && reader_ != nullptr) {
                continue;
            }
            reconnect_requested_ = false;
        }

        VirtualCameraBrokerResponse response{};
        std::unique_ptr<SharedVideoFrameReader> reader;
        const auto result = open_broker_generation(pipe_name_, token_, response,
                                                   reader);
        if (SUCCEEDED(result) &&
            response_matches(response, layout_, fps_numerator_,
                             fps_denominator_)) {
            std::scoped_lock lock{mutex_};
            if (!stop_requested_.load()) {
                reader_ = std::move(reader);
                generation_.store(response.generation);
                last_heartbeat_ = reader_->heartbeat();
                last_progress_ = std::chrono::steady_clock::now();
            }
        } else {
            std::unique_lock lock{mutex_};
            reconnect_requested_ = false;
            reconnect_wakeup_.wait_for(
                lock, kReconnectInterval,
                [this]() { return stop_requested_.load(); });
            reconnect_requested_ = true;
        }
    }
}

HRESULT connect_to_frame_broker(
    IMFAttributes* activation_attributes,
    std::shared_ptr<BrokerFrameProvider>& provider) noexcept {
    try {
        provider.reset();
        std::wstring pipe_name;
        VirtualCameraBrokerToken token{};
        auto result = read_activation(activation_attributes, pipe_name, token);
        if (FAILED(result)) {
            return result;
        }
        const auto fallback = fallback_response(activation_attributes);
        provider = std::make_shared<BrokerFrameProvider>(
            std::move(pipe_name), token, fallback.layout,
            fallback.fps_numerator, fallback.fps_denominator);
        return S_OK;
    } catch (const std::invalid_argument&) {
        return E_INVALIDARG;
    } catch (...) {
        return E_FAIL;
    }
}

HRESULT validate_frame_broker_activation(
    IMFAttributes* activation_attributes) noexcept {
    try {
        std::wstring pipe_name;
        VirtualCameraBrokerToken token{};
        return read_activation(activation_attributes, pipe_name, token);
    } catch (...) {
        return E_FAIL;
    }
}

} // namespace solin::media_engine::windows_virtual_camera
