"""Commands for the graphical interface, received over MQTT.

The graphical interface can be controlled remotely, by home automation for
example, through an MQTT broker.  It subscribes to the topic of the
``[mqtt]`` table of the configuration file, ``girax1`` unless another is
given, and takes every message there as a command in JSON:

    {"command": "on", "device": "Flur/Deckenlampen Flur"}
    {"command": "dim", "device": "Wohnzimmer/", "brightness": 40}
    {"command": "addtime", "device": "Küche/Rollo links", "time": "sunset", "action": "down"}

The answer to every command is published as JSON on the topic with
``/reply`` appended.  It repeats the ``id`` of the command, if it had one, and
says whether the command succeeded:

    {"id": 7, "command": "on", "ok": true,
     "results": [{"device": "Flur/Deckenlampen Flur", "ok": true, "state": "on"}]}

The commands themselves are carried out by the interface; this module only
connects to the broker and decodes the messages.  The connection is kept in
a thread of its own, since paho-mqtt needs one, and hands what arrives over to
the interface through a queue, so that the X1 is only ever talked to from the
thread of the interface.  paho-mqtt is only needed when a broker is configured.
"""

from __future__ import annotations

import json
import queue
from dataclasses import dataclass
from typing import Any

from .config import MqttConfig

# The topic the answers go to, below the one the commands come in on.
REPLY_SUFFIX = '/reply'

# How long the broker may go without hearing from the client, in seconds, and
# how long to wait between attempts to reconnect.
KEEPALIVE = 60
RECONNECT_MIN = 1
RECONNECT_MAX = 60

# Quality of service for commands and answers: at least once.
QOS = 1

# How long closing the connection waits for the last answer to go out, in seconds.
FLUSH_TIMEOUT = 1.0


class CommandError(Exception):
    """A message is no command that can be carried out."""


@dataclass(frozen=True, slots=True)
class Event:
    """Something that happened to the connection, or a command that arrived."""

    # One of 'connected', 'disconnected' or 'command'.
    kind: str
    # The command, for kind 'command'.
    command: dict[str, Any] | None = None
    # Why the connection failed, or why a message is no command.
    error: str = ''


def decode(payload: bytes) -> dict[str, Any]:
    """Return the command a message carries, raising :class:`CommandError` if it is none."""
    try:
        command = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CommandError(f'not JSON: {e}') from None
    if not isinstance(command, dict) or not isinstance(command.get('command'), str):
        raise CommandError('a command must be a JSON object with a "command" string')
    return command


class Listener:
    """The connection to the broker, taking commands from the topic.

    Connecting, and reconnecting after losing the connection, happens in the
    background; :meth:`events` returns what happened since it was last called.
    """

    def __init__(self, settings: MqttConfig) -> None:
        """Prepare the connection; :meth:`start` makes it."""
        self.settings = settings
        self.queue: queue.Queue[Event] = queue.Queue()
        self.client: Any = None
        # The last answer published, which closing waits for.
        self.last: Any = None

    @property
    def reply_topic(self) -> str:
        """Return the topic the answers are published on."""
        return self.settings.topic + REPLY_SUFFIX

    def start(self) -> None:
        """Connect to the broker in the background.

        Raises :class:`ImportError` if paho-mqtt is not installed.
        """
        from paho.mqtt.client import Client
        from paho.mqtt.enums import CallbackAPIVersion

        settings = self.settings
        client = Client(CallbackAPIVersion.VERSION2,
                             client_id=settings.client_id or '',
                             reconnect_on_failure=True)
        if settings.username:
            client.username_pw_set(settings.username, settings.password or None)
        if settings.tls:
            client.tls_set()
        client.reconnect_delay_set(RECONNECT_MIN, RECONNECT_MAX)
        client.on_connect = self._connected
        client.on_disconnect = self._disconnected
        client.on_message = self._message
        self.client = client
        client.connect_async(settings.server, settings.port, keepalive=KEEPALIVE)
        client.loop_start()

    def stop(self) -> None:
        """Close the connection once the last answer went out, the one to quit for example."""
        if self.client is not None:
            if self.last is not None and self.client.is_connected():
                try:
                    self.last.wait_for_publish(FLUSH_TIMEOUT)
                except (RuntimeError, ValueError):
                    pass  # it cannot go out any more
            self.client.disconnect()
            self.client.loop_stop()
            self.client = None

    def events(self) -> list[Event]:
        """Return what happened since the last call, without waiting."""
        found = []
        while True:
            try:
                found.append(self.queue.get_nowait())
            except queue.Empty:
                return found

    def reply(self, answer: dict[str, Any]) -> None:
        """Publish the answer to a command."""
        if self.client is not None:
            self.last = self.client.publish(self.reply_topic,
                                            json.dumps(answer, ensure_ascii=False), qos=QOS)

    # Called by paho-mqtt in its own thread.

    def _connected(self, client: Any, _userdata: Any, _flags: Any, reason: Any,
                   _properties: Any) -> None:
        """Subscribe once connected, which after reconnecting is needed again."""
        if reason.is_failure:
            self.queue.put(Event('disconnected', error=str(reason)))
            return
        client.subscribe(self.settings.topic, qos=QOS)
        self.queue.put(Event('connected'))

    def _disconnected(self, _client: Any, _userdata: Any, _flags: Any, reason: Any,
                      _properties: Any) -> None:
        """Report a lost connection; paho-mqtt reconnects by itself."""
        self.queue.put(Event('disconnected', error=str(reason)))

    def _message(self, _client: Any, _userdata: Any, message: Any) -> None:
        """Hand a command over to the interface."""
        try:
            self.queue.put(Event('command', command=decode(message.payload)))
        except CommandError as e:
            self.queue.put(Event('command', error=str(e)))
