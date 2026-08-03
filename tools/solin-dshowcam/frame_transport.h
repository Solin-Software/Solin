// Reader for Solin's file-backed frame transport.
//
// Ported from tools/solin-mfcam (Solin's own code). Unlike the Media Foundation
// build, this runs INSIDE the consuming application — Chrome, Zoom, Teams — as
// the logged-on user. That makes the identity story easier (same user as the
// producer, no session-0 boundary) but raises the stakes on robustness: an
// unhandled fault here takes down the host's capture process, not a service.
#pragma once

#include <windows.h>
#include <intrin.h>  // _ReadBarrier

#include "solin_guids.h"

static const wchar_t kFramePath[] = L"C:\\ProgramData\\Solin\\vcam-frame.bin";
static const UINT32 kFrameMagic = 0x31435653;  // 'SVC1'
static const UINT32 kFrameVersion = 1;
static const DWORD kHeaderBytes = 64;

#pragma pack(push, 1)
struct FrameHeader {
    UINT32 magic;
    UINT32 version;
    UINT32 width;
    UINT32 height;
    UINT32 format;  // 0 = NV12
    UINT32 frame_bytes;
    UINT32 slot_count;
    volatile UINT32 active_slot;
    volatile UINT64 frame_index;
    volatile UINT64 timestamp;
    UINT8 reserved[16];
};
#pragma pack(pop)
static_assert(sizeof(FrameHeader) == kHeaderBytes, "frame header must be 64 bytes");

// SEH cannot coexist with C++ unwinding, so the copy lives in its own function.
// EXCEPTION_IN_PAGE_ERROR is reachable whenever the producer truncates the file
// underneath a mapped view.
static bool safe_copy(BYTE* dst, const BYTE* src, size_t bytes) {
    __try {
        memcpy(dst, src, bytes);
        return true;
    } __except (GetExceptionCode() == EXCEPTION_IN_PAGE_ERROR
                    ? EXCEPTION_EXECUTE_HANDLER
                    : EXCEPTION_CONTINUE_SEARCH) {
        return false;
    }
}

class FrameReader {
public:
    FrameReader()
        : file_(INVALID_HANDLE_VALUE), mapping_(nullptr), view_(nullptr), size_(0),
          last_index_(0), last_change_(0) {}
    ~FrameReader() { Close(); }

    // Lazy and re-entrant: Solin may start producing after the camera is already
    // streaming, and may stop and restart while a consumer stays connected.
    bool Ensure() {
        if (view_) return true;

        // FILE_SHARE_WRITE is mandatory — the producer holds this open for writing.
        file_ = CreateFileW(kFramePath, GENERIC_READ,
                            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                            nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file_ == INVALID_HANDLE_VALUE) return false;

        LARGE_INTEGER size{};
        if (!GetFileSizeEx(file_, &size) ||
            (ULONGLONG)size.QuadPart < (ULONGLONG)kHeaderBytes + kNv12Bytes) {
            Close();
            return false;
        }
        size_ = (size_t)size.QuadPart;

        mapping_ = CreateFileMappingW(file_, nullptr, PAGE_READONLY, 0, 0, nullptr);
        if (!mapping_) { Close(); return false; }
        view_ = (const BYTE*)MapViewOfFile(mapping_, FILE_MAP_READ, 0, 0, 0);
        if (!view_) { Close(); return false; }

        const FrameHeader* h = (const FrameHeader*)view_;
        if (h->magic != kFrameMagic || h->version != kFrameVersion ||
            h->width != (UINT32)kWidth || h->height != (UINT32)kHeight ||
            h->frame_bytes != kNv12Bytes || h->slot_count == 0 || h->slot_count > 8) {
            Close();  // producer restarted with different geometry
            return false;
        }
        return true;
    }

    // Copies the newest published NV12 frame into dst. False means "no producer" —
    // the caller must still deliver a frame (black), never stall the graph.
    bool Read(BYTE* dst) {
        if (!Ensure()) return false;
        const FrameHeader* h = (const FrameHeader*)view_;

        // Seqlock: the producer publishes by storing active_slot then bumping
        // frame_index. Re-reading the index around the copy catches the rare
        // case where it wrapped onto the slot we were reading.
        for (int attempt = 0; attempt < 2; ++attempt) {
            UINT64 before = h->frame_index;
            if (before == 0) return false;
            _ReadBarrier();

            UINT32 slot = h->active_slot;
            if (slot >= h->slot_count) return false;
            size_t offset = kHeaderBytes + (size_t)slot * kNv12Bytes;
            if (offset + kNv12Bytes > size_) return false;  // never trust the header

            if (!safe_copy(dst, view_ + offset, kNv12Bytes)) {
                Close();
                return false;
            }
            _ReadBarrier();
            if (h->frame_index == before) {
                if (before != last_index_) {
                    last_index_ = before;
                    last_change_ = GetTickCount64();
                }
                return true;
            }
        }
        return true;  // torn twice: keep what we copied rather than dropping a frame
    }

    // A producer that stopped without closing leaves a valid but frozen file.
    bool Stale() const {
        return last_change_ != 0 && (GetTickCount64() - last_change_) > 2000;
    }

    void Close() {
        if (view_) { UnmapViewOfFile(view_); view_ = nullptr; }
        if (mapping_) { CloseHandle(mapping_); mapping_ = nullptr; }
        if (file_ != INVALID_HANDLE_VALUE) { CloseHandle(file_); file_ = INVALID_HANDLE_VALUE; }
        size_ = 0;
    }

private:
    HANDLE file_;
    HANDLE mapping_;
    const BYTE* view_;
    size_t size_;
    UINT64 last_index_;
    ULONGLONG last_change_;
};
