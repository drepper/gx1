#include "stack.hh"

#include <charconv>
#include <format>
#include <memory>
#include <random>

#include <unistd.h>

namespace girax1 {

  namespace {

    // Render a JSON value without any spacing, the way the app sends it.
    std::string compact(const Json::Value& value)
    {
      Json::StreamWriterBuilder builder;
      builder["indentation"] = "";
      builder["commentStyle"] = "None";
      return Json::writeString(builder, value);
    }

    // Parse a JSON document, returning nullopt when it is not one.
    std::optional<Json::Value> decode(const std::string& text)
    {
      Json::CharReaderBuilder builder;
      const std::unique_ptr<Json::CharReader> reader(builder.newCharReader());
      Json::Value parsed;
      std::string errors;
      if (!reader->parse(text.data(), text.data() + text.size(), &parsed, &errors))
        return std::nullopt;
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

    // Encode credentials the way the query string of the handshake wants them.
    std::string credentials(const config& settings)
    {
      const auto joined = std::format("{}:{}", settings.username, settings.password);
      return base64({reinterpret_cast<const std::uint8_t*>(joined.data()), joined.size()});
    }

  } // namespace

  result<void> stack::connect()
  {
    const auto path = std::format("/gds/api?ui{}", credentials(config_));
    if (auto opened = socket_.connect(config_.host, config_.port, path, config_.timeout,
                                      config_.verify_tls, config_.ca_cert.string());
        !opened) [[unlikely]]
      return opened;

    Json::Value parameters;
    parameters["applicationId"] = std::string(application_id);
    parameters["applicationType"] = "ui";
    parameters["pretty"] = "false";
    parameters["instanceId"] = std::format("{:08x}-0000-4000-8000-{:012x}",
                                           std::random_device{}(), ::getpid());
    if (auto registered = request("RegisterApplication", parameters); !registered) [[unlikely]]
      return std::unexpected(registered.error());
    return {};
  }

  result<Json::Value> stack::next_message(std::chrono::milliseconds timeout)
  {
    for (;;) {
      auto got = socket_.receive(timeout);
      if (!got) [[unlikely]]
        return std::unexpected(got.error());
      if (!*got) [[unlikely]]
        return fail("the device did not answer in time");
      if ((*got)->kind == opcode::close) [[unlikely]]
        return fail("the device closed the connection");

      auto parsed = decode((*got)->payload);
      if (!parsed)
        continue;                       // Not something this protocol produces.
      if (const auto& event = (*parsed)["event"]; event.isObject()) {
        if (text_of(event["type"]) == "Value") {
          const auto& value = event["value"];
          events_.push_back({.point_id = static_cast<unsigned>(
                               std::atoll(text_of(value["id"]).c_str())),
                             .urn = text_of(value["urn"]),
                             .old_value = text_of(value["old"]),
                             .new_value = text_of(value["new"]),
                             .source = text_of(value["source"])});
        }
      }
      return *parsed;
    }
  }

  result<Json::Value> stack::request(std::string_view command, Json::Value parameters)
  {
    const auto wanted = ++query_id_;
    Json::Value body = parameters.isObject() ? std::move(parameters) : Json::Value(Json::objectValue);
    body["command"] = std::string(command);
    body["_gdsqueryId"] = wanted;
    Json::Value envelope;
    envelope["request"] = std::move(body);

    if (auto sent = socket_.send(compact(envelope)); !sent) [[unlikely]]
      return std::unexpected(sent.error());

    for (;;) {
      auto message = next_message(config_.timeout);
      if (!message) [[unlikely]]
        return std::unexpected(message.error());
      const auto& response = (*message)["response"];
      if (!response.isObject())
        continue;

      // Some commands answer without echoing the query id, so the command is
      // the only thing tying the answer to the request.
      const auto& echoed = response["request"];
      if (echoed.isMember("_gdsqueryId")) {
        if (text_of(echoed["_gdsqueryId"]) != std::format("{}", wanted))
          continue;
      } else if (text_of(echoed["command"]) != command)
        continue;

      const auto code = text_of(response["error"]["code"]);
      if (!code.empty() && code != error_ok) [[unlikely]]
        return std::unexpected(error{std::format("{}: {} {}", command,
                                                 text_of(response["error"]["text"]),
                                                 text_of(response["error"]["hint"])),
                                     code});
      return response;
    }
  }

  result<project> stack::ui_configuration()
  {
    Json::Value parameters;
    parameters["guid"] = "";
    parameters["urns"] = true;
    auto answer = request("GetUIConfiguration", parameters);
    if (!answer) [[unlikely]]
      return std::unexpected(answer.error());
    return project::parse((*answer)["config"]);
  }

  result<std::map<unsigned, std::string>> stack::process_view()
  {
    Json::Value parameters;
    parameters["ui"] = "true";
    parameters["cached"] = "true";
    auto answer = request("GetProcessView", parameters);
    if (!answer) [[unlikely]]
      return std::unexpected(answer.error());

    std::map<unsigned, std::string> values;
    for (const auto& point : (*answer)["processView"]["datapoints"])
      values.emplace(static_cast<unsigned>(std::atoll(text_of(point["id"]).c_str())),
                     text_of(point["state"]));
    return values;
  }

  result<std::string> stack::get_value(unsigned point_id)
  {
    Json::Value parameters;
    parameters["id"] = point_id;
    parameters["internal"] = "true";
    auto answer = request("GetValue", parameters);
    if (!answer) [[unlikely]]
      return std::unexpected(answer.error());
    return text_of((*answer)["value"]);
  }

  result<std::string> stack::write(unsigned point_id, const Json::Value& value)
  {
    Json::Value parameters;
    parameters["id"] = point_id;
    parameters["value"] = value;
    auto answer = request("SetValue", parameters);
    if (!answer) [[unlikely]]
      return std::unexpected(answer.error());
    return text_of((*answer)["value"]);
  }

  result<std::string> stack::set_value(unsigned point_id, std::string_view value)
  {
    unsigned number = 0;
    const auto* end = value.data() + value.size();
    if (auto [ptr, ec] = std::from_chars(value.data(), end, number);
        ec == std::errc{} && ptr == end)
      return write(point_id, Json::Value(number));
    return write(point_id, Json::Value(std::string(value)));
  }

  result<std::string> stack::switch_to(unsigned point_id, bool on)
  {
    return write(point_id, Json::Value(on ? 1 : 0));
  }

  result<std::string> stack::move(unsigned point_id, bool down)
  {
    return write(point_id, Json::Value(down ? 1 : 0));
  }

  result<std::string> stack::stop(unsigned point_id)
  {
    return write(point_id, Json::Value(1));
  }

  result<std::string> stack::dim(unsigned point_id, unsigned percent)
  {
    if (percent > brightness_max) [[unlikely]]
      return fail(std::format("brightness has to be between 0 and {}", brightness_max));
    return write(point_id, Json::Value(percent));
  }

  result<timer> stack::read_timer(unsigned channel_id)
  {
    Json::Value object;
    object["id"] = channel_id;
    Json::Value parameters;
    parameters["object"] = std::move(object);
    auto answer = request("GetConfiguration", parameters);
    if (!answer) [[unlikely]]
      return std::unexpected(answer.error());
    return timer::parse((*answer)["object"]);
  }

  result<void> stack::set_switch_time(unsigned channel_id, unsigned index,
                                      const Json::Value& fields)
  {
    Json::Value entry = fields;
    entry["index"] = std::format("{}", index);
    Json::Value times(Json::arrayValue);
    times.append(std::move(entry));
    Json::Value object;
    object["id"] = channel_id;
    object["timer"]["switchTimes"] = std::move(times);
    Json::Value parameters;
    parameters["object"] = std::move(object);
    if (auto answer = request("SetConfiguration", parameters); !answer) [[unlikely]]
      return std::unexpected(answer.error());
    return {};
  }

  result<void> stack::add_switch_time(unsigned channel_id, unsigned index,
                                      std::string_view time, std::string_view weekdays,
                                      unsigned trigger_value, bool active)
  {
    Json::Value entry;
    entry["active"] = active ? "true" : "false";
    entry["recurrence"] = "daily";
    entry["weekdays"] = std::string(weekdays);
    entry["timeType"] = std::string(point_in_time);
    entry["time"] = std::string(time);
    entry["randomTimeFunction"] = "false";
    entry["triggerValue"] = std::format("{}", trigger_value);
    return set_switch_time(channel_id, index, entry);
  }

  result<void> stack::remove_switch_time(unsigned channel_id, unsigned index)
  {
    Json::Value entry;
    entry["remove"] = "true";
    return set_switch_time(channel_id, index, entry);
  }

  result<void> stack::set_scene_set(unsigned channel_id, unsigned point_id,
                                    const std::vector<std::tuple<unsigned, std::string,
                                                                 std::string>>& scenes)
  {
    Json::Value points(Json::arrayValue);
    Json::Value target;
    target["id"] = point_id;
    points.append(std::move(target));

    Json::Value list(Json::arrayValue);
    for (const auto& [number, name, value] : scenes) {
      Json::Value written;
      written["id"] = point_id;
      written["value"] = value;
      Json::Value members(Json::arrayValue);
      members.append(std::move(written));
      Json::Value one;
      one["number"] = std::format("{}", number);
      one["name"] = name;
      one["datapoints"] = std::move(members);
      list.append(std::move(one));
    }

    Json::Value object;
    object["id"] = channel_id;
    object["sceneSet"]["datapoints"] = std::move(points);
    object["sceneSet"]["scenes"] = std::move(list);
    Json::Value parameters;
    parameters["object"] = std::move(object);
    if (auto answer = request("SetConfiguration", parameters); !answer) [[unlikely]]
      return std::unexpected(answer.error());
    return {};
  }

  result<void> stack::clear_scenes(unsigned channel_id)
  {
    Json::Value object;
    object["id"] = channel_id;
    object["sceneSet"]["clear"] = "true";
    object["sceneSet"]["clearParameters"] = "scenes";
    Json::Value parameters;
    parameters["object"] = std::move(object);
    if (auto answer = request("SetConfiguration", parameters); !answer) [[unlikely]]
      return std::unexpected(answer.error());
    return {};
  }

  result<bool> stack::in_group(std::string_view name)
  {
    Json::Value parameters;
    parameters["group"] = std::string(name);
    auto answer = request("CheckGroup", parameters);
    if (!answer) [[unlikely]]
      return std::unexpected(answer.error());
    return text_of((*answer)["isInGroup"]) == "true";
  }

  result<std::optional<value_event>> stack::poll(std::chrono::milliseconds timeout)
  {
    if (!events_.empty()) {
      auto event = events_.front();
      events_.pop_front();
      return std::optional{std::move(event)};
    }
    auto got = socket_.receive(timeout);
    if (!got) [[unlikely]]
      return std::unexpected(got.error());
    if (!*got)
      return std::optional<value_event>{};
    if (auto parsed = decode((*got)->payload); parsed)
      if (const auto& event = (*parsed)["event"];
          event.isObject() && text_of(event["type"]) == "Value") {
        const auto& value = event["value"];
        return std::optional{value_event{
          .point_id = static_cast<unsigned>(std::atoll(text_of(value["id"]).c_str())),
          .urn = text_of(value["urn"]),
          .old_value = text_of(value["old"]),
          .new_value = text_of(value["new"]),
          .source = text_of(value["source"])}};
      }
    return std::optional<value_event>{};
  }

} // namespace girax1
