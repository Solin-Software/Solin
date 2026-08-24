#include "solin/media_engine/source_registry.hpp"

#include <algorithm>
#include <condition_variable>
#include <deque>
#include <exception>
#include <map>
#include <mutex>
#include <set>
#include <stdexcept>
#include <thread>
#include <tuple>
#include <utility>

namespace solin::media_engine {
namespace {

constexpr std::size_t kMaximumRuntimeIdentityBytes = 256U;
constexpr std::size_t kMaximumSources = 256U;
constexpr std::uint64_t kMaximumSourcePixels = 3'840ULL * 2'160ULL;

[[nodiscard]] bool is_runtime_identity(const std::string_view value) {
    return !value.empty() && value.size() <= kMaximumRuntimeIdentityBytes &&
           std::ranges::none_of(value, [](const unsigned char character) {
               return character < 32U || character == 127U;
           });
}

struct SourceGenerationKey final {
    std::string source_id{};
    std::uint64_t generation{0U};

    auto operator<=>(const SourceGenerationKey&) const = default;
};

struct CurrentSource final {
    SceneSource definition{};
    std::uint64_t generation{0U};

    bool operator==(const CurrentSource&) const = default;
};

enum class SlotPhase : std::uint8_t {
    creating,
    ready,
    stopping,
    stopped,
    failed,
};

struct SourceResourceCost final {
    std::uint64_t frame_pixels{0U};
    std::size_t decoder_sessions{0U};
};

[[nodiscard]] SourceResourceCost resource_cost(const SceneSource& source) {
    if (source.kind == SceneSourceKind::solin_content && source.frame_channel.has_value()) {
        return {
            .frame_pixels = static_cast<std::uint64_t>(source.frame_channel->width) *
                            source.frame_channel->height,
            .decoder_sessions = 0U,
        };
    }
    if (source.kind == SceneSourceKind::local_camera) {
        const auto* configuration =
            std::get_if<LocalCameraSourceConfiguration>(&source.configuration);
        if (configuration == nullptr) {
            return {.frame_pixels = kMaximumSourcePixels, .decoder_sessions = 1U};
        }
        const auto configured_pixels =
            static_cast<std::uint64_t>(configuration->width) * configuration->height;
        return {
            .frame_pixels = configured_pixels == 0U ? kMaximumSourcePixels
                                                     : configured_pixels,
            .decoder_sessions = configuration->media_type == "video/x-raw" ? 0U : 1U,
        };
    }
    if (source.kind == SceneSourceKind::rtsp_camera) {
        return {.frame_pixels = kMaximumSourcePixels, .decoder_sessions = 1U};
    }
    return {.frame_pixels = kMaximumSourcePixels, .decoder_sessions = 0U};
}

[[nodiscard]] bool is_camera_source(const SceneSource& source) noexcept {
    return source.kind == SceneSourceKind::local_camera ||
           source.kind == SceneSourceKind::rtsp_camera;
}

[[nodiscard]] bool keep_camera_active(const SceneSource& source) noexcept {
    if (const auto* local =
            std::get_if<LocalCameraSourceConfiguration>(&source.configuration);
        local != nullptr) {
        return local->keep_active;
    }
    if (const auto* rtsp =
            std::get_if<RtspCameraSourceConfiguration>(&source.configuration);
        rtsp != nullptr) {
        return rtsp->keep_active;
    }
    return false;
}

[[nodiscard]] bool same_runtime_definition(SceneSource left, SceneSource right) {
    const auto normalize_retention_policy = [](SceneSource& source) {
        if (auto* local =
                std::get_if<LocalCameraSourceConfiguration>(&source.configuration);
            local != nullptr) {
            local->keep_active = false;
        }
        if (auto* rtsp =
                std::get_if<RtspCameraSourceConfiguration>(&source.configuration);
            rtsp != nullptr) {
            rtsp->keep_active = false;
        }
    };
    normalize_retention_policy(left);
    normalize_retention_policy(right);
    return left == right;
}

} // namespace

namespace detail {

class SourceRuntimeSlot final {
  public:
    SourceRuntimeSlot(SceneSource source_value, const std::uint64_t generation_value)
        : source(std::move(source_value)), generation(generation_value) {}

    SceneSource source{};
    std::uint64_t generation{0U};
    mutable std::mutex mutex{};
    std::condition_variable ready{};
    SlotPhase phase{SlotPhase::creating};
    std::shared_ptr<SourceRuntime> runtime{};
    std::exception_ptr failure{};
    std::set<std::string, std::less<>> consumers{};
    bool stop_scheduled{false};
};

class SourceRegistryState final {
  public:
    SourceRegistryState(std::shared_ptr<SourceRuntimeFactory> factory_value,
                        const SourceRegistryLimits limits_value)
        : limits(limits_value), factory(std::move(factory_value)),
          stop_worker([this] { run_stop_worker(); }) {}

    ~SourceRegistryState() { close_stop_worker(); }

    void enqueue_tracked_stop(std::shared_ptr<SourceRuntimeSlot> slot) noexcept {
        if (slot == nullptr) {
            return;
        }
        bool enqueued = false;
        try {
            std::scoped_lock lock{stop_mutex};
            stop_queue.push_back(slot);
            enqueued = true;
        } catch (...) {
        }
        if (!enqueued) {
            complete_stop(std::move(slot));
            return;
        }
        stop_wakeup.notify_one();
    }

    void close_stop_worker() noexcept {
        {
            std::scoped_lock lock{stop_mutex};
            stop_worker_closed = true;
        }
        stop_wakeup.notify_all();
        if (stop_worker.joinable()) {
            stop_worker.join();
        }
    }

    [[nodiscard]] std::shared_ptr<SourceRuntimeSlot>
    claim_runtime_stop_locked(const std::shared_ptr<SourceRuntimeSlot>& slot) {
        if (slot == nullptr) {
            return {};
        }
        {
            std::scoped_lock lock{slot->mutex};
            if (slot->runtime == nullptr || slot->stop_scheduled) {
                return {};
            }
            slot->stop_scheduled = true;
            slot->phase = SlotPhase::stopping;
        }
        ++stops_in_flight;
        return slot;
    }

  private:
    void complete_stop(std::shared_ptr<SourceRuntimeSlot> slot) noexcept {
        slot->runtime->stop();
        {
            std::scoped_lock lock{mutex};
            {
                std::scoped_lock slot_lock{slot->mutex};
                slot->phase = SlotPhase::stopped;
            }
            const SourceGenerationKey key{slot->source.id, slot->generation};
            std::erase(retained_idle_cameras, key);
            const auto iterator = runtimes.find(key);
            if (iterator != runtimes.end() && iterator->second == slot) {
                runtimes.erase(iterator);
            }
            if (stops_in_flight > 0U) {
                --stops_in_flight;
            }
        }
        slot->ready.notify_all();
        lifecycle_changed.notify_all();
    }

    void run_stop_worker() noexcept {
        while (true) {
            std::shared_ptr<SourceRuntimeSlot> slot;
            {
                std::unique_lock lock{stop_mutex};
                stop_wakeup.wait(lock, [this] {
                    return stop_worker_closed || !stop_queue.empty();
                });
                if (stop_queue.empty()) {
                    if (stop_worker_closed) {
                        return;
                    }
                    continue;
                }
                slot = std::move(stop_queue.front());
                stop_queue.pop_front();
            }
            complete_stop(std::move(slot));
        }
    }

  public:

    mutable std::mutex mutex{};
    bool closed{false};
    bool shutdown_complete{false};
    bool hydrated{false};
    std::size_t creations_in_flight{0U};
    std::size_t stops_in_flight{0U};
    std::condition_variable lifecycle_changed{};
    std::string document_id{};
    std::uint64_t document_revision{0U};
    std::uint64_t next_generation{1U};
    SourceRegistryLimits limits{};
    std::shared_ptr<SourceRuntimeFactory> factory{};
    std::map<std::string, CurrentSource, std::less<>> current_sources{};
    // A runtime can fail before a lease exists (for example when Windows privacy
    // hides a configured camera from Media Foundation). Keep that source-scoped
    // failure observable across the transactional snapshot rollback.
    std::map<std::string, CurrentSource, std::less<>> failed_runtime_starts{};
    std::map<SourceGenerationKey, std::shared_ptr<SourceRuntimeSlot>> runtimes{};
    std::deque<SourceGenerationKey> retained_idle_cameras{};
    std::mutex stop_mutex{};
    std::condition_variable stop_wakeup{};
    std::deque<std::shared_ptr<SourceRuntimeSlot>> stop_queue{};
    bool stop_worker_closed{false};
    std::thread stop_worker{};
};

class SourceRegistryUpdateState final {
  public:
    std::shared_ptr<SourceRegistryState> registry{};
    std::map<std::string, CurrentSource, std::less<>> base_sources{};
    std::map<std::string, CurrentSource, std::less<>> next_sources{};
    bool base_hydrated{false};
    std::string base_document_id{};
    std::string next_document_id{};
    std::uint64_t base_revision{0U};
    std::uint64_t next_revision{0U};
    bool committed{false};
};

} // namespace detail

namespace {

[[nodiscard]] std::shared_ptr<detail::SourceRuntimeSlot> claim_runtime_stop_locked(
    detail::SourceRegistryState& state,
    const std::shared_ptr<detail::SourceRuntimeSlot>& slot) {
    return state.claim_runtime_stop_locked(slot);
}

void release_lease(const std::shared_ptr<detail::SourceRegistryState>& state,
                   const std::shared_ptr<detail::SourceRuntimeSlot>& slot,
                   const std::string_view consumer_id) noexcept {
    if (state == nullptr || slot == nullptr || consumer_id.empty()) {
        return;
    }
    std::vector<std::shared_ptr<detail::SourceRuntimeSlot>> slots_to_stop;
    {
        std::scoped_lock lock{state->mutex};
        const auto consumer = slot->consumers.find(consumer_id);
        if (consumer != slot->consumers.end()) {
            slot->consumers.erase(consumer);
        }
        if (!slot->consumers.empty()) {
            return;
        }
        const SourceGenerationKey key{slot->source.id, slot->generation};
        const auto iterator = state->runtimes.find(key);
        if (iterator != state->runtimes.end() && iterator->second == slot) {
            const auto current = state->current_sources.find(slot->source.id);
            const auto retain =
                current != state->current_sources.end() &&
                current->second.generation == slot->generation &&
                current->second.definition.enabled &&
                state->limits.maximum_retained_idle_cameras > 0U &&
                is_camera_source(current->second.definition) &&
                keep_camera_active(current->second.definition);
            if (retain) {
                std::erase(state->retained_idle_cameras, key);
                state->retained_idle_cameras.push_back(key);
                while (state->retained_idle_cameras.size() >
                       state->limits.maximum_retained_idle_cameras) {
                    const auto evicted_key =
                        state->retained_idle_cameras.front();
                    state->retained_idle_cameras.pop_front();
                    const auto evicted = state->runtimes.find(evicted_key);
                    if (evicted == state->runtimes.end() ||
                        !evicted->second->consumers.empty()) {
                        continue;
                    }
                    if (auto evicted_slot =
                            claim_runtime_stop_locked(*state, evicted->second);
                        evicted_slot != nullptr) {
                        slots_to_stop.push_back(std::move(evicted_slot));
                    }
                }
            } else if (auto released = claim_runtime_stop_locked(*state, slot);
                       released != nullptr) {
                slots_to_stop.push_back(std::move(released));
            }
        }
    }
    for (auto& stopped : slots_to_stop) {
        state->enqueue_tracked_stop(std::move(stopped));
    }
}

[[nodiscard]] std::map<std::string, SceneSource, std::less<>>
source_map(const SceneHydrationSnapshot& snapshot) {
    if (snapshot.sources.size() > kMaximumSources) {
        throw std::runtime_error("source registry capacity exceeded");
    }
    std::map<std::string, SceneSource, std::less<>> result;
    for (const auto& source : snapshot.sources) {
        if (!is_runtime_identity(source.id)) {
            throw std::runtime_error("invalid source registry identity");
        }
        const auto [_, inserted] = result.emplace(source.id, source);
        if (!inserted) {
            throw std::runtime_error("duplicate source registry identity");
        }
    }
    return result;
}

[[nodiscard]] bool same_definitions(
    const std::map<std::string, CurrentSource, std::less<>>& current,
    const std::map<std::string, SceneSource, std::less<>>& next) {
    if (current.size() != next.size()) {
        return false;
    }
    return std::ranges::equal(current, next, {}, [](const auto& entry) {
        return std::tie(entry.first, entry.second.definition);
    }, [](const auto& entry) { return std::tie(entry.first, entry.second); });
}

void ensure_capacity_for(const detail::SourceRegistryState& state,
                         const SceneSource& source) {
    if (state.runtimes.size() >= state.limits.maximum_runtime_slots) {
        throw std::runtime_error("source_runtime_slot_capacity_exceeded");
    }
    auto active_pixels = std::uint64_t{0U};
    auto decoder_sessions = std::size_t{0U};
    for (const auto& [_, slot] : state.runtimes) {
        const auto cost = resource_cost(slot->source);
        if (cost.frame_pixels > state.limits.maximum_active_frame_pixels ||
            active_pixels >
                state.limits.maximum_active_frame_pixels - cost.frame_pixels) {
            throw std::runtime_error("source_frame_pixel_budget_exceeded");
        }
        active_pixels += cost.frame_pixels;
        if (cost.decoder_sessions > state.limits.maximum_decoder_sessions ||
            decoder_sessions >
                state.limits.maximum_decoder_sessions - cost.decoder_sessions) {
            throw std::runtime_error("source_decoder_session_budget_exceeded");
        }
        decoder_sessions += cost.decoder_sessions;
    }
    const auto requested = resource_cost(source);
    if (requested.frame_pixels > state.limits.maximum_active_frame_pixels ||
        active_pixels >
            state.limits.maximum_active_frame_pixels - requested.frame_pixels) {
        throw std::runtime_error("source_frame_pixel_budget_exceeded");
    }
    if (requested.decoder_sessions > state.limits.maximum_decoder_sessions ||
        decoder_sessions >
            state.limits.maximum_decoder_sessions - requested.decoder_sessions) {
        throw std::runtime_error("source_decoder_session_budget_exceeded");
    }
}

struct RuntimeAcquisition final {
    std::shared_ptr<detail::SourceRuntimeSlot> slot{};
    std::string consumer_id{};
};

[[nodiscard]] RuntimeAcquisition acquire_runtime_slot(
    const std::shared_ptr<detail::SourceRegistryState>& state,
    const CurrentSource& selected_source, std::string consumer_key) {
    std::shared_ptr<detail::SourceRuntimeSlot> slot;
    bool creator = false;
    while (true) {
        while (true) {
            creator = false;
            {
                std::scoped_lock lock{state->mutex};
                if (state->closed) {
                    throw std::logic_error("source registry is closed");
                }
                if (!selected_source.definition.enabled) {
                    throw std::runtime_error("source is disabled");
                }
                const SourceGenerationKey key{selected_source.definition.id,
                                              selected_source.generation};
                auto iterator = state->runtimes.find(key);
                if (iterator == state->runtimes.end()) {
                    ensure_capacity_for(*state, selected_source.definition);
                    slot = std::make_shared<detail::SourceRuntimeSlot>(
                        selected_source.definition, selected_source.generation);
                    state->runtimes.emplace(key, slot);
                    creator = true;
                    ++state->creations_in_flight;
                } else {
                    slot = iterator->second;
                    std::erase(state->retained_idle_cameras, key);
                }
            }
            if (creator) {
                break;
            }
            std::unique_lock lock{slot->mutex};
            slot->ready.wait(lock, [&slot] {
                return slot->phase != SlotPhase::creating &&
                       slot->phase != SlotPhase::stopping;
            });
            if (slot->phase == SlotPhase::failed) {
                std::rethrow_exception(slot->failure);
            }
            if (slot->phase == SlotPhase::stopped) {
                continue;
            }
            break;
        }

        if (creator) {
            std::shared_ptr<SourceRuntime> runtime;
            try {
                runtime = state->factory->create(slot->source, slot->generation);
                if (runtime == nullptr) {
                    throw std::runtime_error("source runtime factory returned no runtime");
                }
                runtime->start();
                {
                    std::scoped_lock lock{slot->mutex};
                    slot->runtime = std::move(runtime);
                    slot->phase = SlotPhase::ready;
                }
            } catch (...) {
                if (runtime != nullptr) {
                    runtime->stop();
                }
                {
                    std::scoped_lock lock{slot->mutex};
                    slot->failure = std::current_exception();
                    slot->phase = SlotPhase::failed;
                }
                slot->ready.notify_all();
                {
                    std::scoped_lock lock{state->mutex};
                    const SourceGenerationKey key{slot->source.id, slot->generation};
                    const auto iterator = state->runtimes.find(key);
                    if (iterator != state->runtimes.end() && iterator->second == slot) {
                        state->runtimes.erase(iterator);
                    }
                    if (state->creations_in_flight > 0U) {
                        --state->creations_in_flight;
                    }
                }
                state->lifecycle_changed.notify_all();
                std::rethrow_exception(slot->failure);
            }
            slot->ready.notify_all();
        }

        std::shared_ptr<detail::SourceRuntimeSlot> abandoned_slot;
        std::exception_ptr consumer_failure;
        bool duplicate_consumer = false;
        bool registry_closed = false;
        bool retry_after_stop = false;
        {
            std::scoped_lock lock{state->mutex};
            if (state->closed) {
                registry_closed = true;
                abandoned_slot = claim_runtime_stop_locked(*state, slot);
            } else {
                bool stop_after_failure = false;
                {
                    std::scoped_lock slot_lock{slot->mutex};
                    if (slot->phase != SlotPhase::ready) {
                        retry_after_stop = true;
                    } else if (slot->consumers.size() >=
                               state->limits.maximum_consumers_per_runtime) {
                        consumer_failure = std::make_exception_ptr(std::runtime_error(
                            "source_consumer_capacity_exceeded"));
                    } else {
                        try {
                            duplicate_consumer =
                                !slot->consumers.emplace(consumer_key).second;
                        } catch (...) {
                            consumer_failure = std::current_exception();
                            stop_after_failure = slot->consumers.empty();
                        }
                    }
                }
                if (stop_after_failure) {
                    abandoned_slot = claim_runtime_stop_locked(*state, slot);
                }
            }
            if (creator && state->creations_in_flight > 0U) {
                --state->creations_in_flight;
            }
        }
        state->lifecycle_changed.notify_all();
        state->enqueue_tracked_stop(std::move(abandoned_slot));
        if (registry_closed) {
            throw std::logic_error("source registry is closed");
        }
        if (consumer_failure != nullptr) {
            std::rethrow_exception(consumer_failure);
        }
        if (retry_after_stop) {
            continue;
        }
        if (duplicate_consumer) {
            throw std::runtime_error("source consumer already has a lease");
        }
        return {.slot = std::move(slot), .consumer_id = std::move(consumer_key)};
    }
}

} // namespace

SourceLease::SourceLease(std::shared_ptr<detail::SourceRegistryState> state,
                         std::shared_ptr<detail::SourceRuntimeSlot> slot,
                         std::string consumer_id) noexcept
    : state_(std::move(state)), slot_(std::move(slot)),
      consumer_id_(std::move(consumer_id)) {}

SourceLease::~SourceLease() { release(); }

SourceLease::SourceLease(SourceLease&& other) noexcept
    : state_(std::move(other.state_)), slot_(std::move(other.slot_)),
      consumer_id_(std::move(other.consumer_id_)) {}

SourceLease& SourceLease::operator=(SourceLease&& other) noexcept {
    if (this == &other) {
        return *this;
    }
    release();
    state_ = std::move(other.state_);
    slot_ = std::move(other.slot_);
    consumer_id_ = std::move(other.consumer_id_);
    return *this;
}

const SceneSource& SourceLease::source() const {
    if (slot_ == nullptr) {
        throw std::logic_error("source lease is empty");
    }
    return slot_->source;
}

std::uint64_t SourceLease::generation() const noexcept {
    return slot_ == nullptr ? 0U : slot_->generation;
}

SourceRuntime& SourceLease::runtime() const {
    if (slot_ == nullptr || slot_->runtime == nullptr) {
        throw std::logic_error("source lease is empty");
    }
    return *slot_->runtime;
}

SourceLease::operator bool() const noexcept { return slot_ != nullptr; }

void SourceLease::release() noexcept {
    release_lease(state_, slot_, consumer_id_);
    consumer_id_.clear();
    slot_.reset();
    state_.reset();
}

SourceRegistryUpdate::SourceRegistryUpdate(
    std::unique_ptr<detail::SourceRegistryUpdateState> state) noexcept
    : state_(std::move(state)) {}

SourceRegistryUpdate::~SourceRegistryUpdate() = default;

SourceRegistryUpdate::SourceRegistryUpdate(SourceRegistryUpdate&& other) noexcept = default;

SourceRegistryUpdate&
SourceRegistryUpdate::operator=(SourceRegistryUpdate&& other) noexcept = default;

SourceLease SourceRegistryUpdate::acquire(const std::string_view source_id,
                                          const std::string_view consumer_id) {
    if (state_ == nullptr || state_->committed) {
        throw std::logic_error("source registry update is closed");
    }
    if (!is_runtime_identity(source_id) || !is_runtime_identity(consumer_id)) {
        throw std::invalid_argument("invalid source registry identity");
    }
    const auto source = state_->next_sources.find(source_id);
    if (source == state_->next_sources.end()) {
        throw std::out_of_range("unknown source registry identity");
    }
    RuntimeAcquisition acquired;
    try {
        acquired = acquire_runtime_slot(state_->registry, source->second,
                                        std::string{consumer_id});
    } catch (...) {
        std::scoped_lock lock{state_->registry->mutex};
        state_->registry->failed_runtime_starts.insert_or_assign(source->first,
                                                                  source->second);
        throw;
    }
    {
        std::scoped_lock lock{state_->registry->mutex};
        state_->registry->failed_runtime_starts.erase(source->first);
    }
    return SourceLease{state_->registry, std::move(acquired.slot),
                       std::move(acquired.consumer_id)};
}

void SourceRegistryUpdate::commit() {
    if (state_ == nullptr || state_->committed) {
        throw std::logic_error("source registry update is closed");
    }
    std::vector<std::shared_ptr<detail::SourceRuntimeSlot>> slots_to_stop;
    {
        std::scoped_lock lock{state_->registry->mutex};
        if (state_->registry->closed) {
            throw std::logic_error("source registry is closed");
        }
        if (state_->registry->hydrated != state_->base_hydrated ||
            state_->registry->document_id != state_->base_document_id ||
            state_->registry->document_revision != state_->base_revision ||
            state_->registry->current_sources != state_->base_sources) {
            throw std::runtime_error("source registry update conflict");
        }
        state_->registry->current_sources = std::move(state_->next_sources);
        state_->registry->document_id = std::move(state_->next_document_id);
        state_->registry->document_revision = state_->next_revision;
        state_->registry->hydrated = true;
        state_->committed = true;
        for (auto iterator =
                 state_->registry->retained_idle_cameras.begin();
             iterator != state_->registry->retained_idle_cameras.end();) {
            const auto runtime = state_->registry->runtimes.find(*iterator);
            const auto current =
                state_->registry->current_sources.find(iterator->source_id);
            const auto still_current =
                runtime != state_->registry->runtimes.end() &&
                current != state_->registry->current_sources.end() &&
                current->second.generation == iterator->generation &&
                current->second.definition.enabled &&
                keep_camera_active(current->second.definition);
            if (still_current) {
                ++iterator;
                continue;
            }
            if (runtime != state_->registry->runtimes.end() &&
                runtime->second->consumers.empty()) {
                if (auto stopped = claim_runtime_stop_locked(
                        *state_->registry, runtime->second);
                    stopped != nullptr) {
                    slots_to_stop.push_back(std::move(stopped));
                }
            }
            iterator = state_->registry->retained_idle_cameras.erase(iterator);
        }
    }
    for (auto& stopped : slots_to_stop) {
        state_->registry->enqueue_tracked_stop(std::move(stopped));
    }
}

std::uint64_t SourceRegistryUpdate::document_revision() const noexcept {
    return state_ == nullptr ? 0U : state_->next_revision;
}

SourceRegistryUpdate::operator bool() const noexcept {
    return state_ != nullptr && !state_->committed;
}

SourceRegistry::SourceRegistry(std::shared_ptr<SourceRuntimeFactory> factory,
                               const SourceRegistryLimits limits) {
    if (factory == nullptr) {
        throw std::invalid_argument("source runtime factory is required");
    }
    if (limits.maximum_runtime_slots == 0U ||
        limits.maximum_consumers_per_runtime == 0U ||
        limits.maximum_active_frame_pixels == 0U ||
        limits.maximum_retained_idle_cameras >
            limits.maximum_runtime_slots) {
        throw std::invalid_argument("source registry limits are invalid");
    }
    state_ = std::make_shared<detail::SourceRegistryState>(std::move(factory), limits);
}

SourceRegistry::~SourceRegistry() { shutdown(); }

SourceRegistryUpdate
SourceRegistry::stage_snapshot(const SceneHydrationSnapshot& snapshot) {
    auto next_sources = source_map(snapshot);
    std::scoped_lock lock{state_->mutex};
    if (state_->closed) {
        throw std::logic_error("source registry is closed");
    }
    std::erase_if(state_->failed_runtime_starts, [&next_sources](const auto& failure) {
        return !next_sources.contains(failure.first);
    });
    const bool same_document = state_->hydrated &&
                               snapshot.document_id == state_->document_id;
    if (same_document && snapshot.document_revision < state_->document_revision) {
        throw std::runtime_error("stale source registry revision");
    }
    if (same_document && snapshot.document_revision == state_->document_revision) {
        if (!same_definitions(state_->current_sources, next_sources)) {
            throw std::runtime_error("conflicting source registry revision");
        }
    }

    auto update = std::make_unique<detail::SourceRegistryUpdateState>();
    update->registry = state_;
    update->base_sources = state_->current_sources;
    update->base_hydrated = state_->hydrated;
    update->base_document_id = state_->document_id;
    update->next_document_id = snapshot.document_id;
    update->base_revision = state_->document_revision;
    update->next_revision = snapshot.document_revision;

    std::map<std::string, CurrentSource, std::less<>> reconciled;
    for (auto& [source_id, source] : next_sources) {
        const auto current = state_->current_sources.find(source_id);
        if (current != state_->current_sources.end() &&
            same_runtime_definition(current->second.definition, source)) {
            auto preserved = current->second;
            preserved.definition = std::move(source);
            reconciled.emplace(source_id, std::move(preserved));
            continue;
        }
        if (state_->next_generation == 0U) {
            throw std::overflow_error("source generation exhausted");
        }
        reconciled.emplace(source_id,
                           CurrentSource{std::move(source), state_->next_generation++});
    }
    update->next_sources = std::move(reconciled);
    return SourceRegistryUpdate{std::move(update)};
}

void SourceRegistry::replace_snapshot(const SceneHydrationSnapshot& snapshot) {
    auto update = stage_snapshot(snapshot);
    update.commit();
}

SourceLease SourceRegistry::acquire(const std::string_view source_id,
                                    const std::string_view consumer_id) {
    if (!is_runtime_identity(source_id) || !is_runtime_identity(consumer_id)) {
        throw std::invalid_argument("invalid source registry identity");
    }
    CurrentSource selected;
    {
        std::scoped_lock lock{state_->mutex};
        if (state_->closed) {
            throw std::logic_error("source registry is closed");
        }
        const auto current = state_->current_sources.find(source_id);
        if (current == state_->current_sources.end()) {
            throw std::out_of_range("unknown source registry identity");
        }
        selected = current->second;
    }
    RuntimeAcquisition acquired;
    try {
        acquired = acquire_runtime_slot(state_, selected, std::string{consumer_id});
    } catch (...) {
        std::scoped_lock lock{state_->mutex};
        state_->failed_runtime_starts.insert_or_assign(selected.definition.id, selected);
        throw;
    }
    {
        std::scoped_lock lock{state_->mutex};
        state_->failed_runtime_starts.erase(selected.definition.id);
    }
    return SourceLease{state_, std::move(acquired.slot),
                       std::move(acquired.consumer_id)};
}

std::vector<SourceRegistryEntry> SourceRegistry::entries() const {
    struct PendingEntry final {
        SourceRegistryEntry entry{};
        std::shared_ptr<detail::SourceRuntimeSlot> slot{};
    };
    std::vector<PendingEntry> pending;
    {
        std::scoped_lock lock{state_->mutex};
        pending.reserve(state_->current_sources.size() + state_->runtimes.size());
        for (const auto& [source_id, current] : state_->current_sources) {
            const SourceGenerationKey key{source_id, current.generation};
            const auto runtime = state_->runtimes.find(key);
            pending.push_back({
                .entry = {
                    .source_id = source_id,
                    .generation = current.generation,
                    .enabled = current.definition.enabled,
                    .current = true,
                    .consumer_count = runtime == state_->runtimes.end()
                                          ? 0U
                                          : runtime->second->consumers.size(),
                    .health = {},
                },
                .slot = runtime == state_->runtimes.end() ? nullptr : runtime->second,
            });
        }
        for (const auto& [key, slot] : state_->runtimes) {
            const auto current = state_->current_sources.find(key.source_id);
            if (current != state_->current_sources.end() &&
                current->second.generation == key.generation) {
                continue;
            }
            pending.push_back({
                .entry = {
                    .source_id = key.source_id,
                    .generation = key.generation,
                    .enabled = slot->source.enabled,
                    .current = false,
                    .consumer_count = slot->consumers.size(),
                    .health = {},
                },
                .slot = slot,
            });
        }
        for (const auto& [source_id, failure] : state_->failed_runtime_starts) {
            const auto existing = std::ranges::find_if(
                pending, [&source_id](const PendingEntry& item) {
                    return item.entry.source_id == source_id && item.entry.current;
                });
            if (existing != pending.end()) {
                existing->entry.enabled = failure.definition.enabled;
                existing->entry.consumer_count = 1U;
                existing->entry.health.status = SourceRuntimeStatus::failed;
                existing->entry.health.error_code = "source_runtime_start_failed";
                existing->slot.reset();
                continue;
            }
            pending.push_back({
                .entry = {
                    .source_id = source_id,
                    .generation = failure.generation,
                    .enabled = failure.definition.enabled,
                    .current = true,
                    .consumer_count = 1U,
                    .health = {
                        .status = SourceRuntimeStatus::failed,
                        .error_code = "source_runtime_start_failed",
                    },
                },
            });
        }
    }

    std::vector<SourceRegistryEntry> result;
    result.reserve(pending.size());
    for (auto& item : pending) {
        if (item.slot != nullptr) {
            std::scoped_lock lock{item.slot->mutex};
            if (item.slot->phase == SlotPhase::creating) {
                item.entry.health.status = SourceRuntimeStatus::starting;
            } else if (item.slot->phase == SlotPhase::stopping) {
                item.entry.health.status = SourceRuntimeStatus::degraded;
                item.entry.health.error_code = "source_runtime_stopping";
            } else if (item.slot->phase == SlotPhase::stopped) {
                item.entry.health.status = SourceRuntimeStatus::stopped;
            } else if (item.slot->phase == SlotPhase::failed) {
                item.entry.health.status = SourceRuntimeStatus::failed;
                item.entry.health.error_code = "source_runtime_start_failed";
            } else if (item.slot->runtime != nullptr) {
                item.entry.health = item.slot->runtime->health();
            }
        }
        result.push_back(std::move(item.entry));
    }
    std::ranges::sort(result, [](const auto& left, const auto& right) {
        return std::tie(left.source_id, left.current, left.generation) <
               std::tie(right.source_id, right.current, right.generation);
    });
    return result;
}

std::uint64_t SourceRegistry::document_revision() const {
    std::scoped_lock lock{state_->mutex};
    return state_->document_revision;
}

void SourceRegistry::shutdown() noexcept {
    if (state_ == nullptr) {
        return;
    }
    std::vector<std::shared_ptr<detail::SourceRuntimeSlot>> slots;
    {
        std::unique_lock lock{state_->mutex};
        if (state_->closed) {
            state_->lifecycle_changed.wait(
                lock, [this] { return state_->shutdown_complete; });
            return;
        }
        state_->closed = true;
        slots.reserve(state_->runtimes.size());
        for (const auto& [_, slot] : state_->runtimes) {
            auto claimed_slot = claim_runtime_stop_locked(*state_, slot);
            if (claimed_slot != nullptr) {
                slots.push_back(std::move(claimed_slot));
            }
        }
        state_->current_sources.clear();
    }
    for (const auto& slot : slots) {
        state_->enqueue_tracked_stop(slot);
    }
    {
        std::unique_lock lock{state_->mutex};
        state_->lifecycle_changed.wait(lock, [this] {
            return state_->creations_in_flight == 0U && state_->stops_in_flight == 0U;
        });
        state_->runtimes.clear();
    }
    state_->close_stop_worker();
    {
        std::scoped_lock lock{state_->mutex};
        state_->shutdown_complete = true;
    }
    state_->lifecycle_changed.notify_all();
}

} // namespace solin::media_engine
