#pragma once

#include <chrono>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include <gnutls/gnutls.h>

#include "error.hh"

namespace knx {

  // The frame types of RFC 6455 that this client has to deal with.
  enum class opcode : std::uint8_t {
    continuation = 0x0, text = 0x1, binary = 0x2,
    close = 0x8, ping = 0x9, pong = 0xa,
  };

  // One message received from the peer.
  struct message {
    knx::opcode kind = opcode::text;
    std::string payload{};
  };

  // A WebSocket client speaking over TLS.
  //
  // Only what a Gira X1 uses is implemented: a client side handshake, masked
  // client frames, reassembly of fragmented messages and automatic answering of
  // pings.  No compression extension is offered, which keeps payloads readable.
  class websocket {
  public:
    websocket() = default;
    ~websocket();

    // Neither the socket nor the TLS session may be owned twice.
    websocket(const websocket&) = delete("a TLS session cannot be copied");
    websocket& operator=(const websocket&) = delete("a TLS session cannot be copied");
    websocket(websocket&& other) noexcept;
    websocket& operator=(websocket&& other) noexcept;

    // Connect to the host and upgrade the connection to a WebSocket.
    result<void> connect(const std::string& host, unsigned port, const std::string& path,
                         std::chrono::milliseconds timeout, bool verify,
                         const std::string& ca_cert);

    // Send one unfragmented, masked frame.
    result<void> send(std::string_view payload, opcode kind = opcode::text);

    // Return the next message, answering pings on the way.
    //
    // An empty result without an error means nothing arrived before the timeout
    // expired, which lets a caller poll without blocking for long.
    result<std::optional<message>> receive(std::chrono::milliseconds timeout);

    // Send a close frame and release everything.
    void close();

    // Return whether the connection is still usable.
    bool connected() const noexcept { return fd_ >= 0 && !closed_; }

  private:
    // Read more bytes from the peer into the buffer.
    result<bool> fill(std::chrono::milliseconds timeout);
    // Take one complete frame out of the buffer, if there is one.
    std::optional<std::tuple<bool, opcode, std::string>> next_frame();
    // Perform the HTTP upgrade and check the answer.
    result<void> upgrade(const std::string& host, unsigned port, const std::string& path);
    // Release the socket and the TLS session without sending anything.
    void discard() noexcept;

    int fd_ = -1;
    ::gnutls_session_t session_ = nullptr;
    ::gnutls_certificate_credentials_t credentials_ = nullptr;
    std::string buffer_{};
    bool closed_ = false;
  };

  // Encode bytes as base64, which the handshake needs for its key.
  std::string base64(std::span<const std::uint8_t> data);

} // namespace knx
