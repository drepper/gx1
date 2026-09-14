#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <string_view>
#include <vector>

#include <json/json.h>

namespace knx {

  // The weekday mask starts on Monday.
  inline constexpr std::array<std::string_view, 7> weekday_names{
    "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"};

  // Character marking a day a switch time does not act on.
  inline constexpr char day_off = '-';

  // The only time type seen so far; others are passed through untranslated.
  inline constexpr std::string_view point_in_time = "pointInTime";

  // Return the days of a mask as it is read back from the device, where every
  // day carries a flag, as in "11111--" for Monday to Friday.
  std::set<unsigned> read_days(std::string_view mask);

  // Render days as the device wants them when writing, where every day carries
  // its own number, as in "01234--" for Monday to Friday.  This is deliberately
  // not the encoding reading returns; confusing the two programs other days.
  std::string write_days(const std::set<unsigned>& days);

  // One data point a scene writes.
  struct scene_action {
    unsigned point_id = 0;
    std::string value{};
    std::string urn{};
    unsigned delay = 0;
  };

  // A numbered set of values a switch time can apply.
  struct scene {
    unsigned number = 0;
    std::string name{};
    std::vector<scene_action> actions{};
  };

  // One programmed entry of a timer.
  struct switch_time {
    unsigned index = 0;
    bool active = false;
    std::string time{};
    std::string weekdays{};
    unsigned trigger_value = 0;
    std::string time_type{point_in_time};
    std::string recurrence{};
    std::string validity{};
    std::string begin_date{};

    // Return the days this entry acts on, counting Monday as zero.
    std::set<unsigned> days() const { return read_days(weekdays); }

    // Return the weekday mask as readable text such as "Mon-Fri".
    std::string day_text() const;

    // Return the time, marked up when it is not a plain time of day.
    std::string when() const;
  };

  // The timer channel of one function.
  struct timer {
    unsigned channel_id = 0;
    std::string name{};
    std::string next_trigger{};
    std::vector<switch_time> switch_times{};
    std::map<unsigned, knx::scene> scenes{};

    // Build a timer from the answer to GetConfiguration for its channel.
    static timer parse(const Json::Value& object);

    // Return whether any switch time is configured at all.
    bool programmed() const noexcept { return !switch_times.empty(); }

    // Return the scene a switch time applies, or nullptr when it is unknown.
    const knx::scene* scene_of(const switch_time& entry) const;

    // Return the entry with the given index, or nullptr when there is none.
    const switch_time* entry(unsigned index) const;

    // Return the lowest index no entry uses yet.
    unsigned free_index() const;
  };

} // namespace knx
