// Text based program to control a Gira X1.
//
// The first argument is the command to carry out.  The project configuration,
// which holds the names of all devices, is downloaded from the X1 at start-up,
// so every command can address devices by name.
//
// Devices are addressed by a path made of the room they sit in, a slash and the
// device name, for example "Flur/Deckenlampen Flur".
//
// The library only ever acts on a single device.  Where a command accepts a
// pattern, this program expands it through girax1::project::matching and then asks
// the library about one device at a time.

#include <algorithm>
#include <charconv>
#include <cstdlib>
#include <format>
#include <locale>
#include <map>
#include <print>
#include <ranges>
#include <set>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "girax1/config.hh"
#include "girax1/project.hh"
#include "girax1/stack.hh"
#include "girax1/timer.hh"

namespace {

  // Data points this program knows how to present and drive.
  constexpr std::string_view switch_point = "OnOff";
  constexpr std::string_view cover_point = "Up-Down";
  constexpr std::string_view stop_point = "Step-Up-Down";
  constexpr std::string_view brightness_point = "Brightness";

  // Where the configuration lives unless told otherwise.
  constexpr std::string_view default_config = "~/.config/girax1.toml";

  // Print a message on the error stream.
  template<typename... Args>
  void complain(std::format_string<Args...> pattern, Args&&... args)
  {
    std::println(stderr, "{}", std::format(pattern, std::forward<Args>(args)...));
  }

  // Render a data point value the way a person would read it.
  std::string readable(std::string_view name, std::string_view value)
  {
    if (value.empty() || value == "None")
      return "?";
    if (name == switch_point)
      return value == "0" ? "off" : value == "1" ? "on" : std::string(value);
    if (name == brightness_point)
      return std::format("{}%", value);
    if (name == cover_point)
      return value == "0" ? "up" : value == "1" ? "down" : std::string(value);
    return std::string(value);
  }

  // Return how many characters a string shows, which is not how many bytes it
  // has: the names of the rooms here contain umlauts.
  std::size_t display_width(std::string_view text)
  {
    std::size_t width = 0;
    for (const auto byte : text)
      if ((static_cast<unsigned char>(byte) & 0xc0) != 0x80)
        ++width;
    return width;
  }

  // Return a string padded to a width counted in characters.
  std::string padded(std::string_view text, std::size_t width)
  {
    std::string out(text);
    for (auto shown = display_width(text); shown < width; ++shown)
      out += ' ';
    return out;
  }

  // Parse a whole number, returning nullopt when the text is not one.
  std::optional<unsigned> as_number(std::string_view text)
  {
    unsigned value = 0;
    const auto* end = text.data() + text.size();
    if (auto [ptr, ec] = std::from_chars(text.data(), end, value); ec != std::errc{} || ptr != end)
      return std::nullopt;
    return value;
  }

  // Normalise a time of day to the HH:MM:SS the device stores.
  std::optional<std::string> time_of_day(std::string_view text)
  {
    std::vector<std::string_view> parts;
    for (const auto part : std::views::split(text, ':'))
      parts.emplace_back(part.begin(), part.end());
    if (parts.size() < 2 || parts.size() > 3)
      return std::nullopt;

    const auto hour = as_number(parts[0]);
    const auto minute = as_number(parts[1]);
    const auto second = parts.size() == 3 ? as_number(parts[2]) : std::optional<unsigned>{0};
    if (! hour || ! minute || ! second) [[unlikely]]
      return std::nullopt;
    if (*hour > 23 || *minute > 59 || *second > 59) [[unlikely]]
      return std::nullopt;
    return std::format("{:02}:{:02}:{:02}", *hour, *minute, *second);
  }

  // Parse a days specification such as "Mon-Fri", "Mon,Wed" or "weekend".
  std::optional<std::set<unsigned>> parse_days(std::string_view text)
  {
    static const std::map<std::string_view, std::set<unsigned>> groups{{"daily", {0, 1, 2, 3, 4, 5, 6}}, {"everyday", {0, 1, 2, 3, 4, 5, 6}}, {"weekdays", {0, 1, 2, 3, 4}}, {"weekend", {5, 6}}};

    std::string lowered(text);
    std::ranges::transform(lowered, lowered.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    if (const auto found = groups.find(lowered); found != groups.end())
      return found->second;

    std::set<unsigned> days;
    for (const auto piece : std::views::split(text, ',')) {
      const std::string_view part(piece.begin(), piece.end());
      const auto dash = part.find('-');
      const auto first = girax1::weekday_number(dash == std::string_view::npos ? part : part.substr(0, dash));
      const auto last = dash == std::string_view::npos ? first : girax1::weekday_number(part.substr(dash + 1));
      if (! first || ! last) [[unlikely]]
        return std::nullopt;
      for (unsigned day = *first;; day = (day + 1) % girax1::weekday_names.size()) {
        days.insert(day);
        if (day == *last)
          break;
      }
    }
    return days;
  }

  // Everything a command needs: the open session and the downloaded project.
  struct session {
    girax1::stack& device;
    const girax1::project& plan;
  };

  // Collect the devices a list of patterns selects, without repeating any.
  //
  // This is where the single device interface of the library is turned into
  // work on many: the generator yields paths, and each is looked up on its own.
  std::pair<std::vector<const girax1::device*>, bool> select(const girax1::project& plan, std::span<const std::string> patterns)
  {
    std::vector<const girax1::device*> found;
    bool failed = false;
    for (const auto& pattern : patterns) {
      bool any = false;
      for (const auto& path : plan.matching(pattern)) {
        any = true;
        if (const auto* one = plan.find(path); one != nullptr && ! std::ranges::contains(found, one))
          found.push_back(one);
      }
      if (! any) [[unlikely]] {
        complain("no device matches '{}'", pattern);
        failed = true;
      }
    }
    return {found, failed};
  }

  // Return the devices that were named on their own rather than swept up.
  std::set<unsigned> named_alone(const girax1::project& plan, std::span<const std::string> patterns)
  {
    std::set<unsigned> named;
    for (const auto& pattern : patterns) {
      if (pattern.ends_with('/') || pattern.find_first_of(girax1::wildcards) != std::string::npos)
        continue;
      for (const auto& path : plan.matching(pattern))
        if (const auto* one = plan.find(path); one != nullptr)
          named.insert(one->view_id);
    }
    return named;
  }

  // Return the single device a path names, complaining when it is not one.
  const girax1::device* exactly_one(const girax1::project& plan, const std::string& path)
  {
    std::vector<const girax1::device*> found;
    for (const auto& match : plan.matching(path))
      if (const auto* one = plan.find(match); one != nullptr)
        found.push_back(one);
    if (found.size() != 1) [[unlikely]] {
      complain("'{}' matches {} devices, name exactly one", path, found.size());
      return nullptr;
    }
    return found.front();
  }

  // Describe what a switch time does, resolved through its scene.
  std::string scene_text(const girax1::project& plan, const girax1::timer& programming, const girax1::switch_time& entry)
  {
    const auto* found = programming.scene_of(entry);
    if (found == nullptr)
      return std::format("scene {}", entry.trigger_value);
    std::string text;
    for (const auto& action : found->actions) {
      if (! text.empty())
        text += ", ";
      const auto name = plan.point_kind(action.point_id);
      text += std::format("{} {}", name.empty() ? std::format("{}", action.point_id) : name, readable(name, action.value));
    }
    return text.empty() ? std::format("scene {} '{}'", found->number, found->name) : text;
  }

  // ------------------------------------------------------------------ commands

  int command_list(session use, std::span<const std::string> arguments)
  {
    std::string wanted;
    bool detailed = false;
    for (std::size_t i = 0; i < arguments.size(); ++i) {
      if (arguments[i] == "--by" && i + 1 < arguments.size())
        wanted = arguments[++i];
      else if (arguments[i] == "--long")
        detailed = true;
      else [[unlikely]] {
        complain("list: unexpected argument '{}'", arguments[i]);
        return 1;
      }
    }

    const auto show = [&](const girax1::device& one, std::string_view indent) {
      std::println("{}{:<28} {:<10} id={}", indent, one.name, one.kind(), one.view_id);
      if (! detailed)
        return;
      for (const auto& point : one.data_points)
        if (point.usable())
          std::println("{}   {:<16} {}", indent, point.name, point.point_id);
        else
          std::println("{}   {:<16} -  not bound to a group address", indent, point.name);
    };

    const auto groupings = use.plan.groupings();
    if (wanted.empty()) {
      std::println("{} device{}", use.plan.devices().size(), use.plan.devices().size() == 1 ? "" : "s");
      for (const auto& one : use.plan.devices())
        show(one, "   ");
      std::println("");
      if (groupings.empty()) {
        std::println("no groupings configured");
        return 0;
      }
      std::println("possible groupings");
      for (const auto& [kind, groups] : groupings) {
        std::println("   --by {}", kind);
        for (const auto& one : groups)
          std::println("      {:<28} {} device{}", one.label(), one.members.size(), one.members.size() == 1 ? "" : "s");
      }
      return 0;
    }

    for (const auto& [kind, groups] : groupings) {
      if (! std::ranges::equal(kind, wanted, {}, [](unsigned char c) { return std::tolower(c); }, [](unsigned char c) { return std::tolower(c); }))
        continue;
      for (const auto& one : groups) {
        const auto members = use.plan.members(one);
        std::println("{}  ({} device{})", one.label(), members.size(), members.size() == 1 ? "" : "s");
        for (const auto* member : members)
          show(*member, "   ");
        std::println("");
      }
      return 0;
    }
    complain("unknown grouping '{}'", wanted);
    return 1;
  }

  int command_status(session use, std::span<const std::string> arguments)
  {
    auto [devices, failed] = arguments.empty() ? std::pair{std::vector<const girax1::device*>{}, false} : select(use.plan, arguments);
    if (arguments.empty())
      for (const auto& one : use.plan.devices())
        devices.push_back(&one);

    auto values = use.device.process_view();
    if (! values) [[unlikely]] {
      complain("{}", values.error().message);
      return 1;
    }

    std::size_t width = 0;
    for (const auto* one : devices)
      width = std::max(width, display_width(use.plan.path(*one)));

    for (const auto* one : devices) {
      std::string parts;
      for (const auto& point : one->data_points) {
        if (! point.usable())
          continue;
        const auto found = values->find(point.point_id);
        if (! parts.empty())
          parts += ", ";
        parts += std::format("{} {}", point.name, readable(point.name, found == values->end() ? std::string{} : found->second));
      }
      std::println("{}  {:<9} {}", padded(use.plan.path(*one), width), one->kind(), parts.empty() ? "no data points" : parts);
    }
    return failed ? 1 : 0;
  }

  // Drive one data point of every selected device, reporting what happened.
  int drive(session use, std::span<const std::string> arguments, std::string_view point_name, auto&& action, std::string_view refusal, bool quiet_when_swept = false)
  {
    if (arguments.empty()) [[unlikely]] {
      complain("at least one device has to be named");
      return 1;
    }
    auto [devices, failed] = select(use.plan, arguments);
    const auto named = quiet_when_swept ? named_alone(use.plan, arguments) : std::set<unsigned>{};

    for (const auto* one : devices) {
      const auto* point = one->point(point_name);
      if (point == nullptr || ! point->usable()) {
        if (! quiet_when_swept || named.contains(one->view_id)) {
          complain("{}: no {} data point, a {} {}", use.plan.path(*one), point_name, one->kind(), refusal);
          failed = true;
        }
        continue;
      }
      auto done = action(point->point_id);
      if (! done) [[unlikely]] {
        complain("{}: {}", use.plan.path(*one), done.error().message);
        failed = true;
        continue;
      }
      std::println("{} {}", use.plan.path(*one), readable(point_name, *done));
    }
    return failed ? 1 : 0;
  }

  int command_switch(session use, std::span<const std::string> arguments, bool on)
  {
    return drive(use, arguments, switch_point, [&](unsigned id) { return use.device.switch_to(id, on); }, "cannot be switched this way");
  }

  int command_cover(session use, std::span<const std::string> arguments, std::string_view action)
  {
    if (action == "stop")
      return drive(use, arguments, stop_point, [&](unsigned id) { return use.device.stop(id); }, "cannot be moved");
    return drive(use, arguments, cover_point, [&](unsigned id) { return use.device.move(id, action == "down"); }, "cannot be moved");
  }

  int command_dim(session use, std::span<const std::string> arguments)
  {
    if (arguments.empty()) [[unlikely]] {
      complain("dim: a brightness and at least one device are required");
      return 1;
    }
    const auto percent = as_number(arguments[0]);
    if (! percent || *percent > girax1::brightness_max) [[unlikely]] {
      complain("dim: brightness has to be between 0 and {}", girax1::brightness_max);
      return 1;
    }
    // Devices without a brightness are passed over unless named on their own.
    return drive(use, arguments.subspan(1), brightness_point, [&](unsigned id) { return use.device.dim(id, *percent); }, "cannot be dimmed", true);
  }

  // Wait, while keeping the connection to the device alive.
  //
  // Sleeping outright would leave the pings the device sends every few seconds
  // unanswered, and it drops a connection that stops replying.  Polling answers
  // them as a side effect.
  void wait_alive(session use, std::chrono::duration<double> seconds)
  {
    const auto deadline = std::chrono::steady_clock::now() + seconds;
    for (auto left = seconds; left.count() > 0;
         left = deadline - std::chrono::steady_clock::now())
      static_cast<void>(use.device.poll(std::chrono::duration_cast<std::chrono::milliseconds>(
          std::min(left, std::chrono::duration<double>(1.0)))));
  }

  // Close the named blinds by a percentage of their travel.
  //
  // Nothing reports where a blind stands, so the position is reached by timing:
  // the blind is driven fully open first, which is the one position that can be
  // relied upon, and then closed for the matching fraction of its travel time.
  int command_close(session use, std::span<const std::string> arguments)
  {
    if (arguments.size() < 2) [[unlikely]] {
      complain("close: a percentage and at least one device are required");
      return 1;
    }
    const auto percent = as_number(arguments[0]);
    if (! percent || *percent > girax1::brightness_max) [[unlikely]] {
      complain("close: the percentage has to be between 0 and 100");
      return 1;
    }

    auto [devices, failed] = select(use.plan, arguments.subspan(1));
    const auto named = named_alone(use.plan, arguments.subspan(1));

    struct blind {
      const girax1::device* which;
      unsigned move;
      unsigned halt;
      double travel;
    };
    std::vector<blind> blinds;
    for (const auto* one : devices) {
      const auto* move = one->point(cover_point);
      const auto* halt = one->point(stop_point);
      if (move == nullptr || ! move->usable() || halt == nullptr || ! halt->usable()) {
        if (named.contains(one->view_id)) {
          complain("{}: a {} cannot be closed by a percentage", use.plan.path(*one),
                   one->kind());
          failed = true;
        }
        continue;
      }
      blinds.push_back({one, move->point_id, halt->point_id,
                        use.device.settings().travel_seconds(use.plan.path(*one))});
    }
    if (blinds.empty())
      return failed ? 1 : 0;

    // Drive fully open first: that is the only position known without feedback.
    for (const auto& one : blinds)
      if (auto sent = use.device.move(one.move, false); ! sent) [[unlikely]] {
        complain("{}: {}", use.plan.path(*one.which), sent.error().message);
        return 1;
      }
    const auto opening = std::ranges::max(blinds, {}, &blind::travel).travel;
    std::println("opening {} blind{}, waiting {:g}s", blinds.size(),
                 blinds.size() == 1 ? "" : "s", opening);
    wait_alive(use, std::chrono::duration<double>(opening));

    if (*percent == 0) {
      for (const auto& one : blinds)
        std::println("{} open", use.plan.path(*one.which));
      return failed ? 1 : 0;
    }

    // Close them together and halt each one after its own share of the travel.
    const auto started = std::chrono::steady_clock::now();
    for (const auto& one : blinds)
      if (auto sent = use.device.move(one.move, true); ! sent) [[unlikely]] {
        complain("{}: {}", use.plan.path(*one.which), sent.error().message);
        return 1;
      }
    auto schedule = blinds;
    std::ranges::stable_sort(schedule, {}, &blind::travel);
    for (const auto& one : schedule) {
      const auto due = one.travel * *percent / 100.0;
      wait_alive(use, std::chrono::duration<double>(due)
                 - (std::chrono::steady_clock::now() - started));
      if (*percent < girax1::brightness_max)
        if (auto sent = use.device.stop(one.halt); ! sent) [[unlikely]]
          complain("{}: {}", use.plan.path(*one.which), sent.error().message);
      std::println("{} closed {}% ({:.1f}s of {:g}s)", use.plan.path(*one.which), *percent,
                   due, one.travel);
    }
    if (*percent >= girax1::brightness_max)
      std::println("left running to the end of travel");
    return failed ? 1 : 0;
  }

  int command_timers(session use, std::span<const std::string> arguments)
  {
    std::vector<std::string> patterns;
    bool all = false;
    bool detailed = false;
    for (const auto& argument : arguments) {
      if (argument == "--all")
        all = true;
      else if (argument == "--long")
        detailed = true;
      else
        patterns.push_back(argument);
    }

    auto [devices, failed] = patterns.empty() ? std::pair{std::vector<const girax1::device*>{}, false} : select(use.plan, patterns);
    if (patterns.empty())
      for (const auto& one : use.plan.devices())
        devices.push_back(&one);

    std::size_t shown = 0;
    for (const auto* one : devices) {
      if (one->timer_channel == 0 || (one->programmed_slots == 0 && ! all))
        continue;
      auto programming = use.device.read_timer(one->timer_channel);
      if (! programming) [[unlikely]] {
        complain("{}: {}", use.plan.path(*one), programming.error().message);
        failed = true;
        continue;
      }
      ++shown;
      std::println("{}   [{}]   {}", use.plan.path(*one), one->kind(), programming->next_trigger.empty() ? "nothing due" : std::format("next {}", programming->next_trigger));
      if (programming->switch_times.empty())
        std::println("   no switch times programmed");
      for (const auto& entry : programming->switch_times) {
        std::println("   {}  {:<9} {:<16} {}  {}", entry.index, entry.when(), entry.day_text(), entry.active ? "on " : "off", scene_text(use.plan, *programming, entry));
        if (detailed)
          std::println("      scene {}, {}, valid from {} ({})", entry.trigger_value, entry.recurrence, entry.begin_date, entry.validity);
      }
      std::println("");
    }
    if (shown == 0)
      std::println("{}", all ? "no timer channels found" : "no timer programming found");
    return failed ? 1 : 0;
  }

  // Print one switch time the way the timer commands report a change.
  void report(session use, const girax1::device& one, const girax1::timer& programming, unsigned index)
  {
    if (const auto* entry = programming.entry(index); entry != nullptr)
      std::println("{}  {}  {}  {}  {}  {}", use.plan.path(one), entry->index, entry->when(), entry->day_text(), entry->active ? "on" : "off", scene_text(use.plan, programming, *entry));
  }

  int command_settime(session use, std::span<const std::string> arguments)
  {
    if (arguments.size() < 2) [[unlikely]] {
      complain("settime: a device and a switch time index are required");
      return 1;
    }
    const auto* one = exactly_one(use.plan, arguments[0]);
    if (one == nullptr)
      return 1;
    const auto index = as_number(arguments[1]);
    if (! index) [[unlikely]] {
      complain("settime: '{}' is not a switch time index", arguments[1]);
      return 1;
    }
    if (one->timer_channel == 0) [[unlikely]] {
      complain("{} has no timer channel", use.plan.path(*one));
      return 1;
    }

    Json::Value fields;
    for (std::size_t i = 2; i < arguments.size(); ++i) {
      const auto& argument = arguments[i];
      if (argument == "--days" && i + 1 < arguments.size()) {
        const auto days = parse_days(arguments[++i]);
        if (! days) [[unlikely]] {
          complain("settime: '{}' is not a days specification", arguments[i]);
          return 1;
        }
        fields["weekdays"] = girax1::write_days(*days);
        fields["recurrence"] = "daily";
      } else if (argument == "--active" || argument == "--no-active")
        fields["active"] = argument == "--active" ? "true" : "false";
      else if (argument == "--scene" && i + 1 < arguments.size()) {
        const auto scene = as_number(arguments[++i]);
        if (! scene) [[unlikely]] {
          complain("settime: '{}' is not a scene number", arguments[i]);
          return 1;
        }
        fields["triggerValue"] = std::format("{}", *scene);
      } else if (const auto when = time_of_day(argument); when) {
        fields["time"] = *when;
        fields["timeType"] = std::string(girax1::point_in_time);
      } else [[unlikely]] {
        complain("settime: unexpected argument '{}'", argument);
        return 1;
      }
    }
    if (fields.empty()) [[unlikely]] {
      complain("nothing to change; give a time, --days, --active or --scene");
      return 1;
    }

    auto programming = use.device.read_timer(one->timer_channel);
    if (! programming) [[unlikely]] {
      complain("{}: {}", use.plan.path(*one), programming.error().message);
      return 1;
    }
    if (programming->entry(*index) == nullptr) [[unlikely]] {
      std::string available;
      for (const auto& entry : programming->switch_times)
        available += std::format("{}{}", available.empty() ? "" : ", ", entry.index);
      complain("{} has no switch time {}; programmed: {}", use.plan.path(*one), *index, available.empty() ? "none" : available);
      return 1;
    }

    if (auto written = use.device.set_switch_time(one->timer_channel, *index, fields); ! written) [[unlikely]] {
      complain("{}: {}", use.plan.path(*one), written.error().message);
      return 1;
    }
    programming = use.device.read_timer(one->timer_channel);
    if (! programming) [[unlikely]] {
      complain("{}: {}", use.plan.path(*one), programming.error().message);
      return 1;
    }
    report(use, *one, *programming, *index);
    if (! programming->next_trigger.empty())
      std::println("next {}", programming->next_trigger);
    return 0;
  }

  int command_addtime(session use, std::span<const std::string> arguments)
  {
    if (arguments.size() < 3) [[unlikely]] {
      complain("addtime: a device, a time and an action are required");
      return 1;
    }
    const auto* one = exactly_one(use.plan, arguments[0]);
    if (one == nullptr)
      return 1;
    if (one->timer_channel == 0) [[unlikely]] {
      complain("{} has no timer channel", use.plan.path(*one));
      return 1;
    }
    const auto when = time_of_day(arguments[1]);
    if (! when) [[unlikely]] {
      complain("addtime: '{}' is not a time of day such as 06:30", arguments[1]);
      return 1;
    }

    // Work out which data point the action writes, and what the scenes for it
    // would be called if the channel has none yet.
    const auto& action = arguments[2];
    std::string_view point_name;
    std::string value;
    std::string family;
    if (action == "on" || action == "off") {
      point_name = switch_point;
      value = action == "on" ? "1" : "0";
      family = "switch";
    } else if (action == "up" || action == "down") {
      point_name = cover_point;
      value = action == "down" ? "1" : "0";
      family = "move";
    } else if (const auto percent = as_number(action); percent && *percent <= girax1::brightness_max) {
      point_name = brightness_point;
      value = std::format("{}", *percent);
      family = "dim";
    } else [[unlikely]] {
      complain("'{}' is not an action; use on, off, up, down or a brightness from 0 to {}", action, girax1::brightness_max);
      return 1;
    }

    const auto* point = one->point(point_name);
    if (point == nullptr || ! point->usable()) [[unlikely]] {
      complain("a {} has no {} data point, so it cannot do '{}'", one->kind(), point_name, action);
      return 1;
    }

    std::set<unsigned> days{0, 1, 2, 3, 4, 5, 6};
    bool active = false;
    for (std::size_t i = 3; i < arguments.size(); ++i) {
      if (arguments[i] == "--days" && i + 1 < arguments.size()) {
        const auto parsed = parse_days(arguments[++i]);
        if (! parsed) [[unlikely]] {
          complain("addtime: '{}' is not a days specification", arguments[i]);
          return 1;
        }
        days = *parsed;
      } else if (arguments[i] == "--active")
        active = true;
      else if (arguments[i] == "--no-active")
        active = false;
      else [[unlikely]] {
        complain("addtime: unexpected argument '{}'", arguments[i]);
        return 1;
      }
    }

    auto programming = use.device.read_timer(one->timer_channel);
    if (! programming) [[unlikely]] {
      complain("{}: {}", use.plan.path(*one), programming.error().message);
      return 1;
    }

    const auto scene_writing = [&](const girax1::timer& in) -> std::optional<unsigned> {
      for (const auto& [number, which] : in.scenes)
        for (const auto& made : which.actions)
          if (made.point_id == point->point_id && std::atoll(made.value.c_str()) == std::atoll(value.c_str()))
            return number;
      return std::nullopt;
    };

    auto scene = scene_writing(*programming);
    if (! scene) {
      if (! programming->scenes.empty()) [[unlikely]] {
        complain(
            "{}: no scene writes '{}'; the channel already has scenes, "
            "so none are created",
            use.plan.path(*one), action
        );
        return 1;
      }
      // A channel without scenes gets the set the Gira app would create for it.
      std::vector<std::tuple<unsigned, std::string, std::string>> wanted;
      if (family == "switch")
        wanted = {{1, "switch-1", "1"}, {2, "switch-0", "0"}};
      else if (family == "move")
        wanted = {{1, "move-0", "0"}, {2, "move-1", "1"}};
      else
        for (unsigned step = 0, number = 1; step <= girax1::brightness_max; step += 10, ++number)
          wanted.emplace_back(number, std::format("dim-{}", step), std::format("{}", step));

      if (auto made = use.device.set_scene_set(one->timer_channel, point->point_id, wanted); ! made) [[unlikely]] {
        complain("{}: {}", use.plan.path(*one), made.error().message);
        return 1;
      }
      programming = use.device.read_timer(one->timer_channel);
      if (! programming) [[unlikely]] {
        complain("{}: {}", use.plan.path(*one), programming.error().message);
        return 1;
      }
      scene = scene_writing(*programming);
      if (! scene) [[unlikely]] {
        complain("{}: the device did not take the scenes", use.plan.path(*one));
        return 1;
      }
    }

    const auto index = programming->free_index();
    if (auto added = use.device.add_switch_time(one->timer_channel, index, *when, girax1::write_days(days), *scene, active); ! added) [[unlikely]] {
      complain("{}: {}", use.plan.path(*one), added.error().message);
      return 1;
    }
    programming = use.device.read_timer(one->timer_channel);
    if (! programming) [[unlikely]] {
      complain("{}: {}", use.plan.path(*one), programming.error().message);
      return 1;
    }
    if (programming->entry(index) == nullptr) [[unlikely]] {
      complain("{}: the device did not keep the new switch time", use.plan.path(*one));
      return 1;
    }
    report(use, *one, *programming, index);
    return 0;
  }

  int command_deltime(session use, std::span<const std::string> arguments)
  {
    if (arguments.size() < 2) [[unlikely]] {
      complain("deltime: a device and a switch time index are required");
      return 1;
    }
    const auto* one = exactly_one(use.plan, arguments[0]);
    if (one == nullptr)
      return 1;
    const auto index = as_number(arguments[1]);
    if (! index) [[unlikely]] {
      complain("deltime: '{}' is not a switch time index", arguments[1]);
      return 1;
    }
    const bool keep_scenes = std::ranges::contains(arguments.subspan(2), std::string("--keep-scenes"));
    if (one->timer_channel == 0) [[unlikely]] {
      complain("{} has no timer channel", use.plan.path(*one));
      return 1;
    }

    auto programming = use.device.read_timer(one->timer_channel);
    if (! programming) [[unlikely]] {
      complain("{}: {}", use.plan.path(*one), programming.error().message);
      return 1;
    }
    if (programming->entry(*index) == nullptr) [[unlikely]] {
      std::string available;
      for (const auto& entry : programming->switch_times)
        available += std::format("{}{}", available.empty() ? "" : ", ", entry.index);
      complain("{} has no switch time {}; programmed: {}", use.plan.path(*one), *index, available.empty() ? "none" : available);
      return 1;
    }

    if (auto removed = use.device.remove_switch_time(one->timer_channel, *index); ! removed) [[unlikely]] {
      complain("{}: {}", use.plan.path(*one), removed.error().message);
      return 1;
    }
    programming = use.device.read_timer(one->timer_channel);
    if (! programming) [[unlikely]] {
      complain("{}: {}", use.plan.path(*one), programming.error().message);
      return 1;
    }
    // The app drops the scenes along with the last switch time that used them.
    if (programming->switch_times.empty() && ! keep_scenes)
      if (auto cleared = use.device.clear_scenes(one->timer_channel); ! cleared) [[unlikely]]
        complain("{}: the scenes were kept: {}", use.plan.path(*one), cleared.error().message);

    std::string remaining;
    for (const auto& entry : programming->switch_times)
      remaining += std::format("{}{}", remaining.empty() ? "" : ", ", entry.index);
    std::println("{}: switch time {} deleted, remaining: {}", use.plan.path(*one), *index, remaining.empty() ? "none" : remaining);
    return 0;
  }

  int command_watch(session use, std::span<const std::string> arguments)
  {
    std::map<unsigned, std::string> names;
    for (const auto& one : use.plan.devices())
      for (const auto& point : one.data_points)
        if (point.usable())
          names.emplace(point.point_id, std::format("{}/{}", one.name, point.name));

    const auto seconds = arguments.empty() ? std::optional<unsigned>{} : as_number(arguments[0]);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(seconds.value_or(0));
    std::println("watching for value changes, press Ctrl-C to stop");
    while (! seconds || std::chrono::steady_clock::now() < deadline) {
      auto event = use.device.poll(std::chrono::milliseconds(1000));
      if (! event) [[unlikely]] {
        complain("{}", event.error().message);
        return 1;
      }
      if (! *event)
        continue;
      const auto found = names.find((*event)->point_id);
      std::println("  {}: {} -> {}  (source {})", found == names.end() ? (*event)->urn : found->second, (*event)->old_value, (*event)->new_value, (*event)->source.empty() ? "device" : (*event)->source);
    }
    return 0;
  }

  // Give every blind an entry for its travel time, so the file can be corrected.
  //
  // The default is only a guess, and a guess nobody can see is a guess nobody
  // will fix; writing the entries out puts the number where it can be measured.
  void record_blinds(girax1::stack& device, const girax1::project& plan)
  {
    std::vector<std::string> blinds;
    for (const auto& one : plan.devices())
      if (const auto* point = one.point(cover_point); point != nullptr && point->usable())
        blinds.push_back(plan.path(one));
    if (blinds.empty() || device.settings().source.empty())
      return;

    auto added = girax1::ensure_travel_entries(device.settings().source, blinds,
                                               device.settings().travel_default);
    if (! added) [[unlikely]] {
      complain("cannot record the blinds: {}", added.error().message);
      return;
    }
    if (! added->empty())
      complain("added {} blind{} to {} at {:g}s each; correct the entries to match your blinds",
               added->size(), added->size() == 1 ? "" : "s",
               device.settings().source.string(), device.settings().travel_default);
  }

  void usage()
  {
    std::println("usage: x1 [-c CONFIG] COMMAND [ARGUMENT...]");
    std::println("");
    std::println("  list [--by KIND] [--long]     list the devices and the groupings");
    std::println("  status [PATH...]              show the status of devices");
    std::println("  on PATH...                    switch devices on");
    std::println("  off PATH...                   switch devices off");
    std::println("  up|down|stop PATH...          drive or halt coverings");
    std::println("  dim PERCENT PATH...           set the brightness of lights");
    std::println("  close PERCENT PATH...         close blinds by a percentage of travel");
    std::println("  timers [PATH...] [--all] [--long]        read the timer programming");
    std::println("  settime PATH INDEX [TIME] [--days D] [--active|--no-active] [--scene N]");
    std::println("  addtime PATH TIME ACTION [--days D] [--active]");
    std::println("  deltime PATH INDEX [--keep-scenes]");
    std::println("  watch [SECONDS]               print value changes as they happen");
    std::println("");
    std::println("Devices are named by a path such as \"Flur/Deckenlampen Flur\".  A bare");
    std::println("device name, a room with a trailing slash, and a pattern such as");
    std::println("\"*/Rollo*\" also work where several devices are allowed.");
  }

} // namespace

int main(int argc, char** argv)
{
  // Day names are printed and parsed in the language of the environment.  This
  // does not disturb the rest of the output: std::format stays independent of
  // the locale unless a format specification asks for it.
  try {
    std::locale::global(std::locale(""));
  }
  catch (const std::runtime_error&) {
    // An unusable setting in the environment is no reason to refuse to run.
  }

  const std::vector<std::string> arguments(argv + 1, argv + argc);
  std::filesystem::path settings_file(default_config);

  std::size_t at = 0;
  while (at < arguments.size() && arguments[at].starts_with('-')) {
    if ((arguments[at] == "-c" || arguments[at] == "--config") && at + 1 < arguments.size()) {
      settings_file = arguments[at + 1];
      at += 2;
    } else if (arguments[at] == "-h" || arguments[at] == "--help") {
      usage();
      return 0;
    } else [[unlikely]] {
      complain("unknown option '{}'", arguments[at]);
      return 1;
    }
  }
  if (at >= arguments.size()) [[unlikely]] {
    usage();
    return 1;
  }

  const auto command = arguments[at];
  const std::span rest(arguments.begin() + static_cast<std::ptrdiff_t>(at) + 1, arguments.end());

  auto settings = girax1::load_config(settings_file);
  if (! settings) [[unlikely]] {
    complain("configuration error: {}", settings.error().message);
    return 1;
  }

  girax1::stack device(*settings);
  if (auto opened = device.connect(); ! opened) [[unlikely]] {
    complain("error: {}", opened.error().message);
    return 1;
  }

  auto plan = device.ui_configuration();
  if (! plan) [[unlikely]] {
    complain("error: {}", plan.error().message);
    return 1;
  }
  record_blinds(device, *plan);
  const session use{.device = device, .plan = *plan};

  if (command == "list")
    return command_list(use, rest);
  if (command == "status")
    return command_status(use, rest);
  if (command == "on" || command == "off")
    return command_switch(use, rest, command == "on");
  if (command == "up" || command == "down" || command == "stop")
    return command_cover(use, rest, command);
  if (command == "dim")
    return command_dim(use, rest);
  if (command == "close")
    return command_close(use, rest);
  if (command == "timers")
    return command_timers(use, rest);
  if (command == "settime")
    return command_settime(use, rest);
  if (command == "addtime")
    return command_addtime(use, rest);
  if (command == "deltime")
    return command_deltime(use, rest);
  if (command == "watch")
    return command_watch(use, rest);

  complain("unknown command '{}'", command);
  usage();
  return 1;
}
