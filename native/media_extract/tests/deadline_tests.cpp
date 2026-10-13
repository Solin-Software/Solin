#include "deadline.hpp"
#include <iostream>
#include <stdexcept>
using namespace solin::media;
namespace {
void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}
void expect_error(Deadline& deadline, Deadline::Clock::time_point now, const char* code) {
    try {
        deadline.check_at(now);
    } catch (const Failure& failure) {
        require(failure.kind == "transient" && failure.code == code, "Wrong interruption error");
        return;
    }
    throw std::runtime_error("Expired deadline accepted");
}
} // namespace
int main() {
    try {
        using namespace std::chrono;
        const Deadline::Clock::time_point start{};
        Deadline probe(start);
        require(!probe.expired_at(start + seconds(14)), "Probe expired early");
        expect_error(probe, start + seconds(15), "timeout");
        Deadline extraction(start);
        extraction.begin_extraction(start + seconds(10));
        require(!extraction.expired_at(start + seconds(29)), "Extraction expired early");
        expect_error(extraction, start + seconds(30), "timeout");
        Deadline total(start);
        total.begin_extraction(start + seconds(14));
        // A repeated phase transition must never extend the total budget.
        total.begin_extraction(start + seconds(33));
        require(!total.expired_at(start + seconds(34)), "Total expired early");
        expect_error(total, start + seconds(35), "timeout");
        interrupted = 1;
        expect_error(total, start, "cancelled");
        interrupted = 0;
        return 0;
    } catch (const std::exception& error) {
        interrupted = 0;
        std::cerr << error.what() << '\n';
        return 1;
    }
}
