#pragma once

#ifdef _WIN32

#include <guiddef.h>

#include <string>

namespace solin::media_engine::windows_virtual_camera {

inline constexpr wchar_t kFriendlyName[] = L"Solin Virtual Camera";

inline constexpr GUID kDirectShowFilterClassId{
    0x08AFA2E5,
    0x0293,
    0x4E56,
    {0x9F, 0xE1, 0x2A, 0x79, 0xDA, 0xE8, 0xE2, 0x8F},
};

[[nodiscard]] std::wstring current_user_broker_pipe_name();
[[nodiscard]] std::string current_user_broker_pipe_name_utf8();
[[nodiscard]] std::wstring current_user_sid_string();
[[nodiscard]] unsigned long current_process_session_id();

} // namespace solin::media_engine::windows_virtual_camera

#endif
