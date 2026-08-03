// Identifiers and geometry for the Solin Virtual Camera DirectShow filter.
#pragma once

#include <initguid.h>
#include <windows.h>

// {2A5D626F-89B9-49FC-BEB8-27A6DDF286BB} — minted for this filter.
// Never reuse OBS's {A3FCE0F5-...}: that CLSID belongs to OBS's own registration
// and writing to it would hijack a co-installed OBS Studio.
DEFINE_GUID(CLSID_SolinVirtualCamera,
            0x2a5d626f, 0x89b9, 0x49fc, 0xbe, 0xb8, 0x27, 0xa6, 0xdd, 0xf2, 0x86, 0xbb);

// I420 is absent from the Windows SDK's uuids.h (which has IYUV/YV12/NV12), so
// declare it here. Same layout as IYUV; consumers ask for it by this FOURCC.
DEFINE_GUID(MEDIASUBTYPE_I420_SOLIN,
            0x30323449, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71);

static const wchar_t kFilterName[] = L"Solin Virtual Camera";

static const int kWidth = 1280;
static const int kHeight = 720;
static const int kFps = 30;
static const REFERENCE_TIME kFrameDuration = 333333;  // 100ns units, 30fps

static const DWORD kNv12Bytes = kWidth * kHeight * 3 / 2;   // 1382400
static const DWORD kYuy2Bytes = kWidth * kHeight * 2;       // 1843200
static const DWORD kRgb24Bytes = kWidth * kHeight * 3;      // 2764800
