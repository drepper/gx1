#include "timer.hh"

#include <algorithm>
#include <cctype>
#include <charconv>
#include <chrono>
#include <format>

namespace girax1 {

  namespace {

    // Convert an index counting Monday as zero to the day chrono numbers from
    // Sunday, which is what its formatter expects.
    std::chrono::weekday as_chrono(unsigned day)
    {
      return std::chrono::weekday{(day + 1) % 7};
    }

    // Fold a day name for comparison: lower case, and without the trailing full
    // stop that locales such as the French one put after an abbreviation.
    std::string folded(std::string_view text)
    {
      std::string out;
      for (const auto byte : text)
        out += static_cast<char>(std::tolower(static_cast<unsigned char>(byte)));
      while (out.ends_with('.'))
        out.pop_back();
      return out;
    }

    // Return a field the device sends as a string, as a number.
    unsigned number_of(const Json::Value& value)
    {
      if (value.isIntegral())
        return static_cast<unsigned>(value.asInt64());
      if (!value.isString())
        return 0;
      const auto text = value.asString();
      unsigned parsed = 0;
      const auto* end = text.data() + text.size();
      if (auto [ptr, ec] = std::from_chars(text.data(), end, parsed);
          ec != std::errc{} || ptr != end)
        return 0;
      return parsed;
    }

    // Return a field as a string whichever way the device spelled it.
    std::string text_of(const Json::Value& value)
    {
      if (value.isString())
        return value.asString();
      if (value.isIntegral())
        return std::format("{}", value.asInt64());
      if (value.isBool())
        return value.asBool() ? "true" : "false";
      return {};
    }

    // Return whether a field the device sends as a string means true.
    bool flag_of(const Json::Value& value)
    {
      return value.isBool() ? value.asBool() : text_of(value) == "true";
    }

  } // namespace

  std::string weekday_label(unsigned day, const std::locale& with)
  {
    if (day >= weekday_names.size()) [[unlikely]]
      return {};
    return std::format(with, "{:L%a}", as_chrono(day));
  }

  std::optional<unsigned> weekday_number(std::string_view name, const std::locale& with)
  {
    const auto wanted = folded(name);
    if (wanted.empty()) [[unlikely]]
      return std::nullopt;

    for (unsigned day = 0; day < weekday_names.size(); ++day) {
      // English first, so that it is understood whatever the locale is.
      if (folded(weekday_names[day]) == wanted || folded(weekday_full_names[day]) == wanted)
        return day;
      if (folded(std::format(with, "{:L%a}", as_chrono(day))) == wanted
          || folded(std::format(with, "{:L%A}", as_chrono(day))) == wanted)
        return day;
    }
    return std::nullopt;
  }

  std::set<unsigned> read_days(std::string_view mask)
  {
    std::set<unsigned> days;
    for (unsigned i = 0; i < weekday_names.size() && i < mask.size(); ++i)
      if (mask[i] != day_off)
        days.insert(i);
    return days;
  }

  std::string write_days(const std::set<unsigned>& days)
  {
    std::string mask;
    for (unsigned i = 0; i < weekday_names.size(); ++i)
      mask += days.contains(i) ? static_cast<char>('0' + i) : day_off;
    return mask;
  }

  std::string switch_time::day_text(const std::locale& with) const
  {
    if (weekdays.size() != weekday_names.size())
      return weekdays;
    const auto active = days();
    if (active.empty())
      return "never";
    if (active.size() == weekday_names.size())
      return "daily";

    std::string out;
    for (auto it = active.begin(); it != active.end();) {
      auto last = it;
      for (auto next = std::next(it); next != active.end() && *next == *last + 1; ++next)
        last = next;
      if (!out.empty())
        out += ',';
      out += weekday_label(*it, with);
      if (last != it)
        out += std::format("-{}", weekday_label(*last, with));
      it = std::next(last);
    }
    return out;
  }

  std::string switch_time::when() const
  {
    return time_type == point_in_time ? time : std::format("{} ({})", time, time_type);
  }

  const scene* timer::scene_of(const switch_time& entry) const
  {
    const auto found = scenes.find(entry.trigger_value);
    return found == scenes.end() ? nullptr : &found->second;
  }

  const switch_time* timer::entry(unsigned index) const
  {
    const auto found = std::ranges::find(switch_times, index, &switch_time::index);
    return found == switch_times.end() ? nullptr : &*found;
  }

  unsigned timer::free_index() const
  {
    unsigned highest = 0;
    for (const auto& entry : switch_times)
      highest = std::max(highest, entry.index);
    return highest + 1;
  }

  timer timer::parse(const Json::Value& object)
  {
    timer result;
    result.channel_id = number_of(object["id"]);
    result.name = object.isMember("displayName") ? text_of(object["displayName"])
                                                 : text_of(object["name"]);

    if (const auto& part = object["timer"]; part.isObject()) {
      result.next_trigger = text_of(part["nextTriggerTime"]);
      for (const auto& entry : part["switchTimes"])
        result.switch_times.push_back({
          .index = number_of(entry["index"]),
          .active = flag_of(entry["active"]),
          .time = text_of(entry["time"]),
          .weekdays = text_of(entry["weekdays"]),
          .trigger_value = number_of(entry["triggerValue"]),
          .time_type = entry.isMember("timeType") ? text_of(entry["timeType"])
                                                  : std::string{point_in_time},
          .recurrence = text_of(entry["recurrence"]),
          .validity = text_of(entry["validity"]),
          .begin_date = text_of(entry["beginDate"]),
        });
    }

    if (const auto& set = object["sceneSet"]; set.isObject())
      for (const auto& one : set["scenes"]) {
        girax1::scene made{.number = number_of(one["number"]), .name = text_of(one["name"])};
        for (const auto& point : one["datapoints"])
          made.actions.push_back({.point_id = number_of(point["id"]),
                                  .value = text_of(point["value"]),
                                  .urn = text_of(point["urn"]),
                                  .delay = number_of(point["delay"])});
        result.scenes.emplace(made.number, std::move(made));
      }

    return result;
  }

} // namespace girax1
