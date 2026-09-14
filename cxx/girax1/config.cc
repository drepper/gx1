#include "config.hh"

#include <cstdlib>
#include <format>
#include <optional>
#include <sstream>
#include <set>
#include <fstream>

#include <toml++/toml.hpp>

namespace girax1 {

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

    if (const auto blinds = parsed[blind_section].as_table(); blinds != nullptr) {
      result.travel_default = (*blinds)["default_seconds"].value_or(default_travel_seconds);
      if (const auto seconds = (*blinds)["seconds"].as_table(); seconds != nullptr)
        for (const auto& [name, node] : *seconds) {
          const auto value = node.value<double>();
          if (! value || *value <= 0.0) [[unlikely]]
            return fail(std::format("{}: {}.\"{}\" has to be a positive number of seconds",
                                    path.string(), travel_table, std::string_view(name.str())));
          result.travel.emplace(std::string(name.str()), *value);
        }
    }

    return result;
  }

  result<std::vector<std::string>> ensure_travel_entries(const std::filesystem::path& source,
                                                         const std::vector<std::string>& paths,
                                                         double fallback)
  {
    std::string text;
    try {
      const std::ifstream in(source);
      if (! in) [[unlikely]]
        return fail(std::format("cannot read {}", source.string()));
      std::ostringstream held;
      held << in.rdbuf();
      text = held.str();
    } catch (const std::exception& e) {
      return fail(std::format("cannot read {}: {}", source.string(), e.what()));
    }

    std::set<std::string> known;
    try {
      const auto parsed = toml::parse(text);
      if (const auto blinds = parsed[blind_section].as_table(); blinds != nullptr)
        if (const auto seconds = (*blinds)["seconds"].as_table(); seconds != nullptr)
          for (const auto& [name, _] : *seconds)
            known.emplace(name.str());
    } catch (const toml::parse_error& e) {
      return fail(std::format("{}: {}", source.string(), e.description()));
    }

    std::vector<std::string> missing;
    for (const auto& one : paths)
      if (! known.contains(one))
        missing.push_back(one);
    if (missing.empty())
      return missing;

    std::string added;
    for (const auto& one : missing)
      added += std::format("\"{}\" = {:g}\n", one, fallback);

    const auto marker = std::format("[{}]", travel_table);
    if (const auto at = text.find(marker); at != std::string::npos) {
      // Keep the new entries inside the table that is already there.
      const auto line_end = text.find('\n', at) + 1;
      text.insert(line_end, added);
    } else {
      if (! text.ends_with('\n'))
        text += '\n';
      text += std::format("\n# How long each blind needs for the journey from fully open to"
                          " fully\n# closed, in seconds.  Measure a blind and correct its entry"
                          " to make\n# closing by a percentage accurate.\n{}\n{}",
                          marker, added);
    }

    std::ofstream out(source, std::ios::trunc);
    if (! out) [[unlikely]]
      return fail(std::format("cannot write {}", source.string()));
    out << text;
    if (! out) [[unlikely]]
      return fail(std::format("cannot write {}", source.string()));
    return missing;
  }

} // namespace girax1
