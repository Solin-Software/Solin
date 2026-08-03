// NV12 (the transport's format) -> whatever the consumer negotiated.
//
// Only one output format is ever active per connection, so these run once per
// frame at most. Scalar per-row is comfortably fast enough at 720p30; profile
// before reaching for SIMD.
#pragma once

#include <windows.h>
#include "solin_guids.h"

// Studio-swing black, per format. Delivering black beats delivering nothing:
// a consumer that receives no samples shows a spinner and eventually errors,
// one that receives black shows a working-but-dark camera, which is diagnosable.
inline void fill_black_nv12(BYTE* dst) {
    memset(dst, 0x10, (size_t)kWidth * kHeight);
    memset(dst + (size_t)kWidth * kHeight, 0x80, (size_t)kWidth * kHeight / 2);
}

inline void nv12_to_i420(const BYTE* nv12, BYTE* dst) {
    const size_t luma = (size_t)kWidth * kHeight;
    memcpy(dst, nv12, luma);  // Y plane is identical
    const BYTE* uv = nv12 + luma;
    BYTE* u = dst + luma;
    BYTE* v = u + luma / 4;
    for (size_t i = 0; i < luma / 4; ++i) {
        u[i] = uv[i * 2];
        v[i] = uv[i * 2 + 1];
    }
}

inline void nv12_to_yuy2(const BYTE* nv12, BYTE* dst) {
    const BYTE* y = nv12;
    const BYTE* uv = nv12 + (size_t)kWidth * kHeight;
    for (int row = 0; row < kHeight; ++row) {
        const BYTE* y_row = y + (size_t)row * kWidth;
        const BYTE* uv_row = uv + (size_t)(row / 2) * kWidth;  // chroma is half-height
        BYTE* out = dst + (size_t)row * kWidth * 2;
        for (int col = 0; col < kWidth; col += 2) {
            out[col * 2 + 0] = y_row[col];
            out[col * 2 + 1] = uv_row[col];      // U
            out[col * 2 + 2] = y_row[col + 1];
            out[col * 2 + 3] = uv_row[col + 1];  // V
        }
    }
}

inline BYTE clamp_byte(int v) { return (BYTE)(v < 0 ? 0 : (v > 255 ? 255 : v)); }

// BT.601 limited-range, and written bottom-up: RGB24 in a VIDEOINFOHEADER is
// a bottom-up DIB, so row 0 of the image is the LAST row of the buffer.
inline void nv12_to_rgb24(const BYTE* nv12, BYTE* dst) {
    const BYTE* y_plane = nv12;
    const BYTE* uv = nv12 + (size_t)kWidth * kHeight;
    const int stride = kWidth * 3;
    for (int row = 0; row < kHeight; ++row) {
        const BYTE* y_row = y_plane + (size_t)row * kWidth;
        const BYTE* uv_row = uv + (size_t)(row / 2) * kWidth;
        BYTE* out = dst + (size_t)(kHeight - 1 - row) * stride;
        for (int col = 0; col < kWidth; ++col) {
            int c = y_row[col] - 16;
            int d = uv_row[(col & ~1)] - 128;      // U
            int e = uv_row[(col & ~1) + 1] - 128;  // V
            out[col * 3 + 0] = clamp_byte((298 * c + 516 * d + 128) >> 8);            // B
            out[col * 3 + 1] = clamp_byte((298 * c - 100 * d - 208 * e + 128) >> 8);  // G
            out[col * 3 + 2] = clamp_byte((298 * c + 409 * e + 128) >> 8);            // R
        }
    }
}
