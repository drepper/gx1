#pragma once

#include <deque>
#include <string>
#include <string_view>

#include <json/json.h>

#include "config.hh"
#include "error.hh"
#include "project.hh"
#include "timer.hh"
#include "websocket.hh"

namespace knx {

  // Brightness data points are scaled as a percentage.
  inline constexpr unsigned brightness_max = 100;

  // Identifier this library registers itself with at the device.
  inline constexpr std::string_view application_id = "Gira.UniversalApp";

  // Error codes the device answers with.
  inline constexpr std::string_view error_ok = "0";
  inline constexpr std::string_view error_unknown_command = "102";

  // One value change the device pushed on its own.
  struct value_event {
    unsigned point_id = 0;
    std::string urn{};
    std::string old_value{};
    std::string new_value{};
    std::string source{};
  };

  // A connection to the Device Stack API of a Gira X1.
  //
  // Every operation acts on a single device.  Turning a pattern into several
  // devices is the job of knx::project::matching, which yields the paths a
  // caller then asks about one at a time.
  class stack {
  public:
    explicit stack(knx::config settings) : config_(std::move(settings)) {}

    // A live session cannot be shared.
    stack(const stack&) = delete("a device session cannot be copied");
    stack& operator=(const stack&) = delete("a device session cannot be copied");
    stack(stack&&) noexcept = default;
    stack& operator=(stack&&) noexcept = default;

    // Open the connection and register this program as an application.
    result<void> connect();

    // Close the connection.
    void close() { socket_.close(); }

    // Send one command and return its answer, failing on an error code.
    result<Json::Value> request(std::string_view command, Json::Value parameters = {});

    // Return the configured functions with their data points.
    result<knx::project> ui_configuration();

    // Return the current value of every data point, by data point id.
    result<std::map<unsigned, std::string>> process_view();

    // Return the value of one data point.
    result<std::string> get_value(unsigned point_id);

    // Write one data point.  Switches take 0 and 1, brightness a percentage.
    result<std::string> set_value(unsigned point_id, std::string_view value);

    // Turn one switching data point on or off.
    result<std::string> switch_to(unsigned point_id, bool on);

    // Start one covering moving; 1 lowers it and 0 raises it.
    result<std::string> move(unsigned point_id, bool down);

    // Halt one covering through its Step-Up-Down data point.
    result<std::string> stop(unsigned point_id);

    // Set the brightness of one light, as a percentage.
    result<std::string> dim(unsigned point_id, unsigned percent);

    // Return the timer programming of one function timer channel.
    result<knx::timer> read_timer(unsigned channel_id);

    // Change one switch time, sending only the fields that are present.
    result<void> set_switch_time(unsigned channel_id, unsigned index,
                                 const Json::Value& fields);

    // Add a switch time under a free index.  The weekday mask has to be in the
    // encoding knx::write_days produces, and the trigger value has to name a
    // scene that already exists.
    result<void> add_switch_time(unsigned channel_id, unsigned index,
                                 std::string_view time, std::string_view weekdays,
                                 unsigned trigger_value, bool active);

    // Delete one switch time.
    result<void> remove_switch_time(unsigned channel_id, unsigned index);

    // Define the scenes of a timer channel, each writing one data point.  This
    // replaces whatever scenes the channel had, so it is only safe where none
    // are configured.
    result<void> set_scene_set(unsigned channel_id, unsigned point_id,
                               const std::vector<std::tuple<unsigned, std::string,
                                                            std::string>>& scenes);

    // Remove every scene of a timer channel.
    result<void> clear_scenes(unsigned channel_id);

    // Return whether the configured user belongs to a group.
    result<bool> in_group(std::string_view name);

    // Return the next pushed value change, if one arrives in time.
    result<std::optional<value_event>> poll(std::chrono::milliseconds timeout);

    // Return the settings this session was made from.
    const knx::config& settings() const noexcept { return config_; }

  private:
    // Read the next message, remembering events that arrive in between.
    result<Json::Value> next_message(std::chrono::milliseconds timeout);
    // Write one data point and return the value the device confirmed.
    result<std::string> write(unsigned point_id, const Json::Value& value);

    knx::config config_;
    knx::websocket socket_{};
    std::deque<value_event> events_{};
    unsigned query_id_ = 0;
  };

} // namespace knx
