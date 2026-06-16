# Native WebView ABI

`native_webview_widget` is the Python/Qt adapter around the platform native
browser surface:

- Windows: WebView2 through `native_webview_widget.dll`
- macOS: WKWebView through `libnative_webview_widget.dylib`

The Python package and native binaries are versioned together in this
repository. The ABI source of truth is `native_webview_widget/abi.py`.

## ABI Contract

Current ABI version: `1`.

The native library must export every symbol listed in
`native_webview_widget.abi.REQUIRED_EXPORTS`. The Python backend configures
ctypes signatures for the same symbols and treats missing exports as a release
blocker.

Event ids are also part of the ABI. They must stay stable unless the ABI version
is intentionally bumped and every native implementation is rebuilt.

## Required Files

The package must contain:

- `native_webview_widget/__init__.py`
- `native_webview_widget/abi.py`
- `native_webview_widget/_backend.py`
- `native_webview_widget/widget.py`
- `native_webview_widget/native_webview_widget.dll`
- `native_webview_widget/libnative_webview_widget.dylib` or
  `native_webview_widget/native_webview_widget.dylib`

## Validation

Run this before release builds:

```powershell
.\.venv\Scripts\python.exe scripts\validate_native_webview.py
```

The validator checks package files, PE/Mach-O headers, and current-platform
exports. On unsupported platforms it still validates the versioned package files
and binary container headers, but skips loading the native library.

Use `--skip-load` only when diagnosing local dependency issues. Release builds
must keep the default export-loading validation enabled.
