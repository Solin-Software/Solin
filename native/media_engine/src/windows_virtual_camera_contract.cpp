#include "solin/media_engine/windows_virtual_camera_contract.hpp"

#ifdef _WIN32

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>
#include <sddl.h>

#include <array>
#include <cctype>
#include <cstdint>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace solin::media_engine::windows_virtual_camera {
namespace {

struct HandleCloser final {
    void operator()(void* value) const noexcept {
        if (value != nullptr && value != INVALID_HANDLE_VALUE) {
            static_cast<void>(CloseHandle(value));
        }
    }
};

struct LocalMemoryDeleter final {
    void operator()(wchar_t* value) const noexcept {
        if (value != nullptr) {
            static_cast<void>(LocalFree(value));
        }
    }
};

using UniqueHandle = std::unique_ptr<void, HandleCloser>;
using UniqueLocalString = std::unique_ptr<wchar_t, LocalMemoryDeleter>;

struct AlgorithmCloser final {
    void operator()(void* value) const noexcept {
        if (value != nullptr) {
            static_cast<void>(BCryptCloseAlgorithmProvider(
                static_cast<BCRYPT_ALG_HANDLE>(value), 0U));
        }
    }
};

using UniqueAlgorithm = std::unique_ptr<void, AlgorithmCloser>;

[[nodiscard]] std::wstring query_current_user_sid() {
    HANDLE raw_token = nullptr;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw_token) == FALSE) {
        throw std::runtime_error("virtual_camera_identity_unavailable");
    }
    UniqueHandle token{raw_token};
    DWORD required = 0U;
    static_cast<void>(GetTokenInformation(token.get(), TokenUser, nullptr, 0U,
                                          &required));
    if (GetLastError() != ERROR_INSUFFICIENT_BUFFER || required == 0U) {
        throw std::runtime_error("virtual_camera_identity_unavailable");
    }
    std::vector<std::byte> storage(required);
    if (GetTokenInformation(token.get(), TokenUser, storage.data(), required,
                            &required) == FALSE) {
        throw std::runtime_error("virtual_camera_identity_unavailable");
    }
    const auto* user = reinterpret_cast<const TOKEN_USER*>(storage.data());
    wchar_t* raw_sid = nullptr;
    if (ConvertSidToStringSidW(user->User.Sid, &raw_sid) == FALSE) {
        throw std::runtime_error("virtual_camera_identity_unavailable");
    }
    UniqueLocalString sid{raw_sid};
    std::wstring result{sid.get()};
    if (result.empty()) {
        throw std::runtime_error("virtual_camera_identity_unavailable");
    }
    return result;
}

[[nodiscard]] DWORD query_current_session_id() {
    DWORD session_id = 0U;
    if (ProcessIdToSessionId(GetCurrentProcessId(), &session_id) == FALSE) {
        throw std::runtime_error("virtual_camera_session_unavailable");
    }
    return session_id;
}

[[nodiscard]] std::wstring broker_identity_hash(const std::wstring& sid,
                                                const DWORD session_id) {
    std::wstring identity = sid + L":" + std::to_wstring(session_id);
    BCRYPT_ALG_HANDLE raw_algorithm = nullptr;
    if (BCryptOpenAlgorithmProvider(&raw_algorithm, BCRYPT_SHA256_ALGORITHM,
                                    nullptr, 0U) != 0) {
        throw std::runtime_error("virtual_camera_identity_unavailable");
    }
    UniqueAlgorithm algorithm{raw_algorithm};
    std::array<std::uint8_t, 32U> digest{};
    const auto byte_count = identity.size() * sizeof(wchar_t);
    if (byte_count > (std::numeric_limits<ULONG>::max)() ||
        BCryptHash(algorithm.get(), nullptr, 0U,
                   reinterpret_cast<PUCHAR>(identity.data()),
                   static_cast<ULONG>(byte_count), digest.data(),
                   static_cast<ULONG>(digest.size())) != 0) {
        throw std::runtime_error("virtual_camera_identity_unavailable");
    }
    constexpr std::array<wchar_t, 16U> hex{
        L'0', L'1', L'2', L'3', L'4', L'5', L'6', L'7',
        L'8', L'9', L'a', L'b', L'c', L'd', L'e', L'f'};
    std::wstring result;
    result.reserve(digest.size() * 2U);
    for (const auto byte : digest) {
        result.push_back(hex[byte >> 4U]);
        result.push_back(hex[byte & 0x0FU]);
    }
    return result;
}

} // namespace

std::wstring current_user_broker_pipe_name() {
    const auto sid = query_current_user_sid();
    const auto session_id = query_current_session_id();
    return LR"(\\.\pipe\Solin.VirtualCamera.FrameBroker.v3.)" +
           broker_identity_hash(sid, session_id);
}

std::wstring current_user_sid_string() { return query_current_user_sid(); }

unsigned long current_process_session_id() { return query_current_session_id(); }

std::string current_user_broker_pipe_name_utf8() {
    const auto wide = current_user_broker_pipe_name();
    if (wide.size() > static_cast<std::size_t>((std::numeric_limits<int>::max)())) {
        throw std::runtime_error("virtual_camera_identity_unavailable");
    }
    const auto source_size = static_cast<int>(wide.size());
    const auto required = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS,
                                               wide.data(), source_size, nullptr,
                                               0, nullptr, nullptr);
    if (required <= 0) {
        throw std::runtime_error("virtual_camera_identity_unavailable");
    }
    std::string result(static_cast<std::size_t>(required), '\0');
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wide.data(),
                            source_size, result.data(), required, nullptr,
                            nullptr) != required) {
        throw std::runtime_error("virtual_camera_identity_unavailable");
    }
    return result;
}

} // namespace solin::media_engine::windows_virtual_camera

#endif
