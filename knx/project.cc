#include "project.hh"

#include <algorithm>
#include <cctype>
#include <format>
#include <fnmatch.h>

namespace knx {

  namespace {

    // Return a copy folded to lower case, for comparisons that ignore case.
    std::string folded(std::string_view text)
    {
      std::string out(text);
      std::ranges::transform(out, out.begin(),
                             [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
      return out;
    }

    // Return a field as a string whichever way the device spelled it.
    std::string text_of(const Json::Value& value)
    {
      if (value.isString())
        return value.asString();
      if (value.isIntegral())
        return std::format("{}", value.asInt64());
      return {};
    }

    unsigned number_of(const Json::Value& value)
    {
      return value.isIntegral() ? static_cast<unsigned>(value.asInt64())
                                : static_cast<unsigned>(std::atoll(text_of(value).c_str()));
    }

    // Walk the Root tree, collecting every node that gathers devices.
    void collect(const Json::Value& node, std::vector<group>& into)
    {
      const auto& children = node["content"];
      group made{.kind = text_of(node["mainType"]), .subtype = text_of(node["subType"]),
                 .name = text_of(node["name"])};
      for (const auto& child : children)
        if (child.isMember("channelViewID") && !child.isMember("mainType"))
          made.members.push_back(number_of(child["channelViewID"]));
      if (!made.members.empty())
        into.push_back(std::move(made));
      for (const auto& child : children)
        if (child.isMember("mainType"))
          collect(child, into);
    }

  } // namespace

  std::string device::kind() const
  {
    const auto dot = function_type.rfind('.');
    if (dot == std::string::npos)
      return function_type.empty() ? "Unknown" : function_type;
    return function_type.substr(dot + 1);
  }

  const data_point* device::point(std::string_view wanted) const
  {
    const auto key = folded(wanted);
    const auto found = std::ranges::find_if(data_points, [&key](const data_point& p) {
      return folded(p.name) == key;
    });
    return found == data_points.end() ? nullptr : &*found;
  }

  std::string group::label() const
  {
    return subtype == name ? name : std::format("{} ({})", name, subtype);
  }

  project project::parse(const Json::Value& configuration)
  {
    project made;
    for (const auto& entry : configuration) {
      if (entry.isMember("guid") && entry.size() == 1)
        made.guid_ = text_of(entry["guid"]);
      else if (text_of(entry["mainType"]) == "Root")
        collect(entry, made.groups_);
      else if (entry.isMember("channelViewID") && entry.isMember("name")) {
        device one{.name = text_of(entry["name"]),
                   .view_id = number_of(entry["channelViewID"]),
                   .view_urn = text_of(entry["channelViewUrn"]),
                   .function_type = text_of(entry["functionType"]),
                   .channel_type = text_of(entry["channelType"]),
                   .timer_channel = number_of(entry["switchTimeChannelID"]),
                   .programmed_slots = entry["switchTimes"].size()};
        for (const auto& point : entry["dataPoints"])
          one.data_points.push_back({.name = text_of(point["dataPoint"]),
                                     .point_id = number_of(point["id"]),
                                     .urn = text_of(point["urn"])});
        made.devices_.push_back(std::move(one));
      }
    }
    made.index();
    return made;
  }

  void project::index()
  {
    for (const auto& one : groups_)
      if (one.kind == room_kind)
        for (const auto member : one.members)
          rooms_.try_emplace(member, one.name);

    // A path only needs the id appended when two devices would share it.
    std::map<std::string, std::vector<unsigned>> taken;
    for (const auto& one : devices_) {
      const auto room = rooms_.find(one.view_id);
      taken[room == rooms_.end() ? one.name : std::format("{}/{}", room->second, one.name)]
        .push_back(one.view_id);
    }
    for (const auto& [text, owners] : taken)
      for (const auto owner : owners)
        paths_[owner] = owners.size() == 1 ? text : std::format("{}#{}", text, owner);
  }

  std::map<std::string, std::vector<group>> project::groupings() const
  {
    std::map<std::string, std::vector<group>> found;
    for (const auto& one : groups_)
      if (!one.members.empty())
        found[one.kind].push_back(one);
    return found;
  }

  std::vector<const device*> project::members(const group& of) const
  {
    std::vector<const device*> found;
    for (const auto id : of.members)
      if (const auto it = std::ranges::find(devices_, id, &device::view_id); it != devices_.end())
        found.push_back(&*it);
    return found;
  }

  std::string project::path(const device& which) const
  {
    const auto found = paths_.find(which.view_id);
    return found == paths_.end() ? which.name : found->second;
  }

  const device* project::find(std::string_view wanted) const
  {
    const auto key = folded(wanted);
    for (const auto& one : devices_)
      if (folded(path(one)) == key)
        return &one;
    return nullptr;
  }

  std::string project::point_kind(unsigned point_id) const
  {
    for (const auto& one : devices_)
      for (const auto& point : one.data_points)
        if (point.point_id == point_id && point.usable())
          return point.name;
    return {};
  }

  std::generator<std::string> project::matching(std::string_view pattern) const
  {
    const auto key = folded(pattern);

    for (const auto& one : devices_)
      if (folded(path(one)) == key) {
        co_yield path(one);
        co_return;
      }

    if (pattern.find_first_of(wildcards) != std::string_view::npos) {
      for (const auto& one : devices_)
        if (::fnmatch(key.c_str(), folded(path(one)).c_str(), 0) == 0)
          co_yield path(one);
      co_return;
    }

    if (pattern.ends_with('/')) {
      for (const auto& one : devices_)
        if (folded(path(one)).starts_with(key))
          co_yield path(one);
      co_return;
    }

    for (const auto& one : devices_)
      if (folded(one.name) == key)
        co_yield path(one);
  }

} // namespace knx
