#pragma once

#include <chrono>
#include <filesystem>
#include <string>

#include "error.hh"

namespace girax1 {

  // Port on which a Gira X1 serves the Device Stack API.
  inline constexpr unsigned stack_port = 4432;

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
  };

  // Read and validate a TOML configuration file.
  result<config> load_config(const std::filesystem::path& file);

  // Expand a leading ~ to the home directory of the user.
  std::filesystem::path expand_user(std::filesystem::path path);

} // namespace girax1
