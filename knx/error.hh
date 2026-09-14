#pragma once

#include <expected>
#include <string>
#include <utility>

namespace knx {

  // Why an operation did not succeed.  A code is only set when the failure was
  // reported by the device itself, in which case it is the code of the Device
  // Stack API, for example "102" for an unknown command.
  struct error {
    std::string message{};
    std::string code{};

    // Return whether the device reported this error rather than the transport.
    bool from_device() const noexcept { return !code.empty(); }
  };

  // The result of an operation that the device may refuse.
  template<typename T>
  using result = std::expected<T, error>;

  // Build a failed result carrying the given message.
  inline std::unexpected<error> fail(std::string message, std::string code = {})
  {
    return std::unexpected(error{std::move(message), std::move(code)});
  }

} // namespace knx
