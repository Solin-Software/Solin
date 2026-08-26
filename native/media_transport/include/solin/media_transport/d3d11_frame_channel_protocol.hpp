#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>

namespace solin::media_transport::d3d11_frame_channel {

inline constexpr std::array<std::uint8_t, 8U> kMagic{
    'S', 'L', 'N', 'G', 'P', 'U', '0', '1'};
inline constexpr std::uint16_t kVersion = 1U;
inline constexpr std::uint16_t kHeaderSize = 256U;
inline constexpr std::uint16_t kSlotHeaderSize = 128U;
inline constexpr std::uint32_t kSlotCount = 3U;
inline constexpr std::uint64_t kMaximumSequence =
    (std::numeric_limits<std::int64_t>::max)() / 2U;

inline constexpr std::size_t kMagicOffset = 0U;
inline constexpr std::size_t kVersionOffset = 8U;
inline constexpr std::size_t kHeaderSizeOffset = 10U;
inline constexpr std::size_t kSlotCountOffset = 12U;
inline constexpr std::size_t kCapacityWidthOffset = 16U;
inline constexpr std::size_t kCapacityHeightOffset = 20U;
inline constexpr std::size_t kGenerationOffset = 24U;
inline constexpr std::size_t kProducerProcessIdOffset = 32U;
inline constexpr std::size_t kProducerCreationTimeOffset = 40U;
inline constexpr std::size_t kPublishedSequenceOffset = 48U;
inline constexpr std::size_t kPublishedSlotOffset = 56U;
inline constexpr std::size_t kResourceGenerationOffset = 64U;
inline constexpr std::size_t kResourceWidthOffset = 72U;
inline constexpr std::size_t kResourceHeightOffset = 76U;
inline constexpr std::size_t kResourceFormatOffset = 80U;
inline constexpr std::size_t kAdapterLuidOffset = 88U;
inline constexpr std::size_t kMediaEpochOffset = 96U;
inline constexpr std::size_t kImageTransformRevisionOffset = 104U;
inline constexpr std::size_t kImageTransformMediaEpochOffset = 112U;
inline constexpr std::size_t kImageTransformFlagsOffset = 120U;
inline constexpr std::size_t kImageTransformCanvasWidthOffset = 124U;
inline constexpr std::size_t kImageTransformCanvasHeightOffset = 128U;
inline constexpr std::size_t kImageTransformDurationMsOffset = 132U;
inline constexpr std::size_t kImageTransformZoomOffset = 136U;
inline constexpr std::size_t kImageTransformNormXOffset = 144U;
inline constexpr std::size_t kImageTransformNormYOffset = 152U;

inline constexpr std::uint32_t kImageTransformEnabled = 1U << 0U;
inline constexpr std::uint32_t kImageTransformAnimate = 1U << 1U;
inline constexpr std::uint32_t kImageTransformKnownFlags =
    kImageTransformEnabled | kImageTransformAnimate;

inline constexpr std::size_t kSlotMarkerOffset = 0U;
inline constexpr std::size_t kSlotSequenceOffset = 8U;
inline constexpr std::size_t kSlotPresentationTimestampOffset = 16U;
inline constexpr std::size_t kSlotDurationOffset = 24U;
inline constexpr std::size_t kSlotProducedMonotonicOffset = 32U;
inline constexpr std::size_t kSlotMediaEpochOffset = 40U;
inline constexpr std::size_t kSlotLeaseCountOffset = 48U;
inline constexpr std::size_t kSlotLeaseOwnerProcessIdOffset = 52U;
inline constexpr std::size_t kSlotLeaseOwnerCreationTimeOffset = 56U;

inline constexpr wchar_t kMappingPrefix[] = L"Local\\SolinD3D11Frame.";
inline constexpr wchar_t kMutexPrefix[] = L"Local\\SolinD3D11FrameMutex.";
inline constexpr wchar_t kEventPrefix[] = L"Local\\SolinD3D11FrameEvent.";
inline constexpr wchar_t kTexturePrefix[] = L"Local\\SolinD3D11Texture.";

[[nodiscard]] constexpr std::size_t mapping_size() noexcept {
    return kHeaderSize + kSlotCount * kSlotHeaderSize;
}

[[nodiscard]] constexpr std::size_t slot_offset(
    const std::uint32_t slot) noexcept {
    return kHeaderSize + static_cast<std::size_t>(slot) * kSlotHeaderSize;
}

static_assert(kImageTransformNormYOffset + sizeof(double) <= kHeaderSize);
static_assert(kSlotLeaseOwnerCreationTimeOffset + sizeof(std::uint64_t) <=
              kSlotHeaderSize);
static_assert(kSlotLeaseCountOffset % alignof(std::int32_t) == 0U);
static_assert(kHeaderSize % alignof(std::uint64_t) == 0U);
static_assert(mapping_size() == 640U);

} // namespace solin::media_transport::d3d11_frame_channel
