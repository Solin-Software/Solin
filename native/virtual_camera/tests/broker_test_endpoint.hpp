#pragma once

#include <windows.h>

#include <string>

namespace solin::virtual_camera::windows_virtual_camera {

// Test DLLs and their in-process broker fixture share a process-local endpoint.
// Never route qualification graphs to the installed application's broker.
[[nodiscard]] inline std::wstring test_broker_pipe_name() {
    return L"\\\\.\\pipe\\Solin.VirtualCamera.Tests." + std::to_wstring(GetCurrentProcessId());
}

} // namespace solin::virtual_camera::windows_virtual_camera
