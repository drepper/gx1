#include "websocket.hh"

#include <array>
#include <cerrno>
#include <cstring>
#include <format>
#include <random>
#include <utility>

#include <netdb.h>
#include <poll.h>
#include <unistd.h>
#include <sys/socket.h>

namespace girax1 {

  namespace {

    constexpr std::string_view base64_alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

    // Return random bytes for a mask or a handshake key.
    template<std::size_t N>
    std::array<std::uint8_t, N> random_bytes()
    {
      static std::random_device device;
      std::array<std::uint8_t, N> bytes{};
      std::uniform_int_distribution<unsigned> spread(0, 255);
      for (auto& byte : bytes)
        byte = static_cast<std::uint8_t>(spread(device));
      return bytes;
    }

    // Wait until the descriptor can be read, returning false on a timeout.
    bool readable(int fd, std::chrono::milliseconds timeout)
    {
      ::pollfd waiting{.fd = fd, .events = POLLIN, .revents = 0};
      return ::poll(&waiting, 1, static_cast<int>(timeout.count())) > 0;
    }

  } // namespace

  std::string base64(std::span<const std::uint8_t> data)
  {
    std::string out;
    out.reserve((data.size() + 2) / 3 * 4);
    for (std::size_t i = 0; i < data.size(); i += 3) {
      const unsigned remaining = static_cast<unsigned>(std::min<std::size_t>(3, data.size() - i));
      unsigned group = 0;
      for (unsigned j = 0; j < 3; ++j)
        group = (group << 8) | (j < remaining ? data[i + j] : 0u);
      for (unsigned j = 0; j < 4; ++j)
        out += j <= remaining ? base64_alphabet[(group >> (18 - 6 * j)) & 0x3f] : '=';
    }
    return out;
  }

  websocket::~websocket()
  {
    close();
  }

  websocket::websocket(websocket&& other) noexcept : fd_(std::exchange(other.fd_, -1)), session_(std::exchange(other.session_, nullptr)), credentials_(std::exchange(other.credentials_, nullptr)), buffer_(std::move(other.buffer_)), closed_(std::exchange(other.closed_, true))
  {
  }

  websocket& websocket::operator=(websocket&& other) noexcept
  {
    if (this != &other) {
      discard();
      fd_ = std::exchange(other.fd_, -1);
      session_ = std::exchange(other.session_, nullptr);
      credentials_ = std::exchange(other.credentials_, nullptr);
      buffer_ = std::move(other.buffer_);
      closed_ = std::exchange(other.closed_, true);
    }
    return *this;
  }

  void websocket::discard() noexcept
  {
    if (session_ != nullptr) {
      ::gnutls_deinit(session_);
      session_ = nullptr;
    }
    if (credentials_ != nullptr) {
      ::gnutls_certificate_free_credentials(credentials_);
      credentials_ = nullptr;
    }
    if (fd_ >= 0) {
      ::close(fd_);
      fd_ = -1;
    }
    closed_ = true;
  }

  void websocket::close()
  {
    if (fd_ >= 0 && ! closed_) {
      // 1000 is the normal closure status of RFC 6455.
      const std::array<char, 2> status{'\x03', '\xe8'};
      auto _ = send(std::string_view(status.data(), status.size()), opcode::close);
    }
    discard();
  }

  result<void> websocket::connect(const std::string& host, unsigned port, const std::string& path, std::chrono::milliseconds timeout, bool verify, const std::string& ca_cert)
  {
    const auto service = std::format("{}", port);
    ::addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;

    ::addrinfo* found = nullptr;
    if (const int rc = ::getaddrinfo(host.c_str(), service.c_str(), &hints, &found); rc != 0) [[unlikely]]
      return fail(std::format("cannot resolve {}: {}", host, ::gai_strerror(rc)));

    for (::addrinfo* candidate = found; candidate != nullptr; candidate = candidate->ai_next) {
      fd_ = ::socket(candidate->ai_family, candidate->ai_socktype, candidate->ai_protocol);
      if (fd_ < 0)
        continue;
      if (::connect(fd_, candidate->ai_addr, candidate->ai_addrlen) == 0)
        break;
      ::close(fd_);
      fd_ = -1;
    }
    ::freeaddrinfo(found);
    if (fd_ < 0) [[unlikely]]
      return fail(std::format("cannot reach {}:{}: {}", host, port, std::strerror(errno)));

    if (::gnutls_certificate_allocate_credentials(&credentials_) < 0) [[unlikely]] {
      discard();
      return fail("cannot allocate TLS credentials");
    }
    if (! ca_cert.empty())
      ::gnutls_certificate_set_x509_trust_file(credentials_, ca_cert.c_str(), GNUTLS_X509_FMT_PEM);
    else if (verify)
      ::gnutls_certificate_set_x509_system_trust(credentials_);

    if (::gnutls_init(&session_, GNUTLS_CLIENT) < 0) [[unlikely]] {
      discard();
      return fail("cannot create a TLS session");
    }
    ::gnutls_set_default_priority(session_);
    ::gnutls_credentials_set(session_, GNUTLS_CRD_CERTIFICATE, credentials_);
    ::gnutls_server_name_set(session_, GNUTLS_NAME_DNS, host.data(), host.size());
    ::gnutls_transport_set_int(session_, fd_);
    ::gnutls_handshake_set_timeout(session_, static_cast<unsigned>(timeout.count()));
    if (verify)
      ::gnutls_session_set_verify_cert(session_, host.c_str(), 0);

    int rc = 0;
    do
      rc = ::gnutls_handshake(session_);
    while (rc < 0 && ::gnutls_error_is_fatal(rc) == 0);
    if (rc < 0) [[unlikely]] {
      const std::string reason = ::gnutls_strerror(rc);
      discard();
      return fail(std::format("TLS handshake with {}:{} failed: {}", host, port, reason));
    }

    closed_ = false;
    if (auto upgraded = upgrade(host, port, path); ! upgraded) [[unlikely]] {
      discard();
      return upgraded;
    }
    return {};
  }

  result<void> websocket::upgrade(const std::string& host, unsigned port, const std::string& path)
  {
    const auto key = base64(random_bytes<16>());
    const auto request = std::format(
        "GET {} HTTP/1.1\r\nHost: {}:{}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: {}\r\nSec-WebSocket-Version: 13\r\n\r\n",
        path, host, port, key
    );
    if (::gnutls_record_send(session_, request.data(), request.size()) < 0) [[unlikely]]
      return fail("cannot send the upgrade request");

    std::string head;
    while (head.find("\r\n\r\n") == std::string::npos) {
      std::array<char, 4096> chunk{};
      const auto got = ::gnutls_record_recv(session_, chunk.data(), chunk.size());
      if (got <= 0) [[unlikely]]
        return fail("the connection closed during the upgrade");
      head.append(chunk.data(), static_cast<std::size_t>(got));
    }
    const auto split = head.find("\r\n\r\n") + 4;
    buffer_.assign(head, split, std::string::npos);

    const auto first_line = head.substr(0, head.find("\r\n"));
    if (first_line.find(" 101") == std::string::npos) [[unlikely]]
      return fail(std::format("the upgrade was refused: {}", first_line));
    return {};
  }

  result<void> websocket::send(std::string_view payload, opcode kind)
  {
    if (fd_ < 0) [[unlikely]]
      return fail("not connected");

    std::string frame;
    frame += static_cast<char>(0x80 | std::to_underlying(kind));
    const auto size = payload.size();
    if (size < 126)
      frame += static_cast<char>(0x80 | size);
    else if (size < 65536) {
      frame += static_cast<char>(0x80 | 126);
      frame += static_cast<char>((size >> 8) & 0xff);
      frame += static_cast<char>(size & 0xff);
    } else {
      frame += static_cast<char>(0x80 | 127);
      for (int shift = 56; shift >= 0; shift -= 8)
        frame += static_cast<char>((size >> shift) & 0xff);
    }

    const auto mask = random_bytes<4>();
    frame.append(reinterpret_cast<const char*>(mask.data()), mask.size());
    for (std::size_t i = 0; i < size; ++i)
      frame += static_cast<char>(payload[i] ^ mask[i % 4]);

    for (std::size_t sent = 0; sent < frame.size();) {
      const auto wrote = ::gnutls_record_send(session_, frame.data() + sent, frame.size() - sent);
      if (wrote < 0) [[unlikely]]
        return fail(std::format("cannot send: {}", ::gnutls_strerror(static_cast<int>(wrote))));
      sent += static_cast<std::size_t>(wrote);
    }
    return {};
  }

  result<bool> websocket::fill(std::chrono::milliseconds timeout)
  {
    // Bytes already decrypted by the TLS layer will not show up in poll, so
    // they have to be taken first.
    if (::gnutls_record_check_pending(session_) == 0 && ! readable(fd_, timeout))
      return false;

    std::array<char, 16384> chunk{};
    const auto got = ::gnutls_record_recv(session_, chunk.data(), chunk.size());
    if (got == 0) [[unlikely]] {
      closed_ = true;
      return fail("the device closed the connection");
    }
    if (got < 0) {
      if (::gnutls_error_is_fatal(static_cast<int>(got)) == 0)
        return false;
      closed_ = true;
      return fail(std::format("cannot receive: {}", ::gnutls_strerror(static_cast<int>(got))));
    }
    buffer_.append(chunk.data(), static_cast<std::size_t>(got));
    return true;
  }

  std::optional<std::tuple<bool, opcode, std::string>> websocket::next_frame()
  {
    if (buffer_.size() < 2)
      return std::nullopt;
    const auto byte = [this](std::size_t i) { return static_cast<std::uint8_t>(buffer_[i]); };

    const bool fin = (byte(0) & 0x80) != 0;
    const auto kind = static_cast<opcode>(byte(0) & 0x0f);
    std::size_t length = byte(1) & 0x7f;
    std::size_t offset = 2;
    if (length == 126) {
      if (buffer_.size() < 4)
        return std::nullopt;
      length = (std::size_t{byte(2)} << 8) | byte(3);
      offset = 4;
    } else if (length == 127) {
      if (buffer_.size() < 10)
        return std::nullopt;
      length = 0;
      for (std::size_t i = 2; i < 10; ++i)
        length = (length << 8) | byte(i);
      offset = 10;
    }

    std::array<std::uint8_t, 4> mask{};
    const bool masked = (byte(1) & 0x80) != 0;
    if (masked) { // A server must not mask, but be lenient.
      if (buffer_.size() < offset + 4)
        return std::nullopt;
      for (std::size_t i = 0; i < 4; ++i)
        mask[i] = byte(offset + i);
      offset += 4;
    }
    if (buffer_.size() < offset + length)
      return std::nullopt;

    std::string payload = buffer_.substr(offset, length);
    if (masked)
      for (std::size_t i = 0; i < payload.size(); ++i)
        payload[i] = static_cast<char>(payload[i] ^ mask[i % 4]);
    buffer_.erase(0, offset + length);
    return std::tuple{fin, kind, std::move(payload)};
  }

  result<std::optional<message>> websocket::receive(std::chrono::milliseconds timeout)
  {
    std::string pending;
    auto pending_kind = opcode::text;
    bool assembling = false;

    for (;;) {
      auto frame = next_frame();
      if (! frame) {
        auto more = fill(timeout);
        if (! more) [[unlikely]]
          return std::unexpected(more.error());
        if (! *more)
          return std::optional<message>{};
        continue;
      }

      auto [fin, kind, payload] = std::move(*frame);
      if (kind == opcode::ping) {
        if (auto sent = send(payload, opcode::pong); ! sent) [[unlikely]]
          return std::unexpected(sent.error());
        continue;
      }
      if (kind == opcode::pong)
        continue;
      if (kind == opcode::close) {
        closed_ = true;
        return std::optional{message{.kind = opcode::close, .payload = std::move(payload)}};
      }

      if (kind == opcode::continuation && assembling)
        pending += payload;
      else {
        pending = std::move(payload);
        pending_kind = kind;
        assembling = true;
      }
      if (fin)
        return std::optional{message{.kind = pending_kind, .payload = std::move(pending)}};
    }
  }

} // namespace girax1
