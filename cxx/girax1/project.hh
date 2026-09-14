#pragma once

#include <generator>
#include <map>
#include <string>
#include <string_view>
#include <vector>

#include <json/json.h>

namespace girax1 {

  // Characters that turn a device path into a pattern.
  inline constexpr std::string_view wildcards = "*?[";

  // The grouping that names the room a device sits in.
  inline constexpr std::string_view room_kind = "Location";

  // One readable or writable value of a function.
  struct data_point {
    std::string name{};
    unsigned point_id = 0;
    std::string urn{};

    // Return whether the point is bound to a group address.  Unbound points are
    // reported with an id of zero and can be neither read nor written.
    bool usable() const noexcept { return point_id != 0; }
  };

  // One configured function, for example a lamp or a blind.
  struct device {
    std::string name{};
    unsigned view_id = 0;
    std::string view_urn{};
    std::string function_type{};
    std::string channel_type{};
    std::vector<data_point> data_points{};
    unsigned timer_channel = 0;
    unsigned programmed_slots = 0;

    // Return the short function type, for example "Switch" or "Covering".
    std::string kind() const;

    // Return the named data point, compared without regard to case.
    const data_point* point(std::string_view wanted) const;
  };

  // A set of devices the project groups together.
  struct group {
    std::string kind{};
    std::string subtype{};
    std::string name{};
    std::vector<unsigned> members{};

    // Return a name that stays unambiguous when two groups share one.
    std::string label() const;
  };

  // Everything the device knows about its own configuration.
  class project {
  public:
    // Build a project from the raw answer to GetUIConfiguration.
    static project parse(const Json::Value& configuration);

    // Return every device, in the order the device reports them.
    const std::vector<device>& devices() const noexcept { return devices_; }

    // Return the groups that have members, by kind.
    std::map<std::string, std::vector<group>> groupings() const;

    // Return the devices of a group, skipping references to unknown ones.
    std::vector<const device*> members(const group& of) const;

    // Return the "room/name" path identifying a device.
    std::string path(const device& which) const;

    // Return the device a path names exactly, or nullptr when there is none.
    const device* find(std::string_view path) const;

    // Yield the path of every device a string selects.
    //
    // This is what turns a pattern into work for an interface that only ever
    // acts on one device: the caller walks the paths and asks for each in turn.
    // Recognised, in this order: the exact path, a pattern containing a
    // wildcard, a room written with a trailing slash, and a bare device name.
    std::generator<std::string> matching(std::string_view pattern) const;

    // Return the name of a data point given its id, for readable output.
    std::string point_kind(unsigned point_id) const;

    // Return the project identifier the device reports.
    const std::string& guid() const noexcept { return guid_; }

  private:
    // Work out the room of every device and give each one a unique path.
    void index();

    std::vector<device> devices_{};
    std::vector<group> groups_{};
    std::map<unsigned, std::string> paths_{};
    std::map<unsigned, std::string> rooms_{};
    std::string guid_{};
  };

} // namespace girax1
