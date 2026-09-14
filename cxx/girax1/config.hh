#pragma once

#include <chrono>
#include <filesystem>
#include <map>
#include <string>
#include <string_view>
#include <vector>

#include "error.hh"

namespace girax1 {

  // Port on which a Gira X1 serves the Device Stack API.
  inline constexpr unsigned stack_port = 4432;

  // How long a blind is assumed to need for the journey from open to closed
  // when the configuration says nothing about it.
  inline constexpr double default_travel_seconds = 30.0;

  // Where the travel times live in the configuration file.
  inline constexpr std::string_view blind_section = "blinds";
  inline constexpr std::string_view travel_table = "blinds.seconds";

  // Everything needed to reach and authenticate against a device.  The file is
  // the same one the Python implementation reads, so both can share it.
  struct config {
    std::string host{};
    unsigned port = stack_port;
    std::string username{};
    std::string password{};
    bool verify_tls = false;
    std::filesystem::path ca_cert{};
    std::chrono::milliseconds timeout{10000};
    std::filesystem::path source{};
    double travel_default = default_travel_seconds;
    std::map<std::string, double> travel{};

    // Return how long the named blind needs from fully open to fully closed.
    double travel_seconds(const std::string& path) const
    {
      const auto found = travel.find(path);
      return found == travel.end() ? travel_default : found->second;
    }
  };

  // Read and validate a TOML configuration file.
  result<config> load_config(const std::filesystem::path& file);

  // Add a travel time for every named blind the file does not mention yet.
  //
  // The file is edited as text rather than written back from the parsed data,
  // so that comments and layout survive.  Returns the names that were added.
  result<std::vector<std::string>> ensure_travel_entries(const std::filesystem::path& source,
                                                         const std::vector<std::string>& paths,
                                                         double fallback);

  // Expand a leading ~ to the home directory of the user.
  std::filesystem::path expand_user(std::filesystem::path path);

} // namespace girax1
