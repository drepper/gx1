#include "config.hh"

#include <cstdlib>
#include <format>
#include <optional>

#include <toml++/toml.hpp>

namespace knx {

  std::filesystem::path expand_user(std::filesystem::path path)
  {
    auto text = path.string();
    if (!text.starts_with("~/") && text != "~")
      return path;
    const char* home = ::getenv("HOME");
    if (home == nullptr) [[unlikely]]
      return path;
    return std::filesystem::path(home) / text.substr(text.size() > 1 ? 2 : 1);
  }

  namespace {

    // Return a string from a table, or nullopt when the key is absent.
    std::optional<std::string> string_of(const toml::table& table, std::string_view key)
    {
      if (auto node = table[key]; node)
        if (auto value = node.value<std::string>(); value)
          return value;
      return std::nullopt;
    }

  } // namespace

  result<config> load_config(const std::filesystem::path& file)
  {
    const auto path = expand_user(file);

    toml::table parsed;
    try {
      parsed = toml::parse_file(path.string());
    } catch (const toml::parse_error& e) {
      return fail(std::format("{}: {}", path.string(), e.description()));
    }

    const auto device = parsed["device"].as_table();
    const auto auth = parsed["auth"].as_table();
    const auto tls = parsed["tls"].as_table();
    if (device == nullptr) [[unlikely]]
      return fail(std::format("{}: the [device] table is missing", path.string()));

    config result{.source = path};
    if (auto host = string_of(*device, "host"); host)
      result.host = *host;
    else [[unlikely]]
      return fail(std::format("{}: device.host is required", path.string()));

    // The port of the Device Stack API is its own setting, because device.port
    // in this file names the port of the documented REST API instead.
    if (const auto stack = parsed["stack"].as_table(); stack != nullptr)
      result.port = (*stack)["port"].value_or(stack_port);

    if (auth != nullptr) {
      result.username = string_of(*auth, "username").value_or(std::string{});
      result.password = string_of(*auth, "password").value_or(std::string{});
    }
    if (result.username.empty() || result.password.empty()) [[unlikely]]
      return fail(std::format("{}: auth.username and auth.password are required",
                              path.string()));

    if (tls != nullptr) {
      result.verify_tls = (*tls)["verify"].value_or(false);
      if (auto cert = string_of(*tls, "ca_cert"); cert)
        result.ca_cert = expand_user(*cert);
    }
    if (const auto seconds = (*device)["timeout"].value<double>(); seconds)
      result.timeout = std::chrono::milliseconds(static_cast<long>(*seconds * 1000));

    return result;
  }

} // namespace knx
