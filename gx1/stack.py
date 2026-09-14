"""Client for the Gira Device Stack API.

This is the WebSocket protocol the Gira Smart Home app uses.  Unlike the IoT
REST API in :mod:`gx1.x1` it is available on firmware versions older than 2.4,
which makes it the only way to control such a device.

The protocol was derived from observed app traffic.  A client connects to::

    wss://<device>:4432/gds/api?ui<base64(username + ":" + password)>

and then exchanges JSON messages.  A request looks like::

    {"request": {"command": "GetValue", "id": 150009, "_gdsqueryId": 7}}

and the matching answer like::

    {"response": {"request": {...}, "error": {"code": "0", "text": "OK"},
                  "id": "150009", "value": "1", "valueState": "Value"}}

Unsolicited value changes arrive as ``{"event": {"type": "Value", "value":
{"id": ..., "new": ..., "old": ...}}}`` on the same connection.
"""

import base64
import itertools
import json
import uuid
from pathlib import Path
from typing import Any, Iterator, Self

from .config import Config, load
from .websocket import OP_CLOSE, OP_TEXT, WebSocket, WebSocketError
from .x1 import X1Error

STACK_PORT = 4432
STACK_PATH = '/gds/api'
APPLICATION_ID = 'Gira.UniversalApp'

# Brightness data points are scaled as a percentage, confirmed by observing the
# app send values from 1 up to 98 across the full travel of its slider.
BRIGHTNESS_MAX = 100

# Error codes seen in answers of the device.
ERROR_OK = '0'
ERROR_UNKNOWN_COMMAND = '102'
ERROR_BAD_OBJECT_ID = '111'


class StackError(X1Error):
    """The device answered a command with an error."""

    def __init__(self, command: str, code: str, text: str, hint: str) -> None:
        """Keep the parts of the error so callers can distinguish the cases."""
        super().__init__(f'{command}: {text} ({code})' + (f': {hint}' if hint else ''))
        self.command = command
        self.code = code
        self.text = text
        self.hint = hint


class Stack:
    """Connection to the Device Stack API of a Gira X1."""

    def __init__(self, filename: str | Path | Config, port: int = STACK_PORT) -> None:
        """Read the configuration file without contacting the device yet."""
        self.config: Config = filename if isinstance(filename, Config) else load(filename)
        self.port = port
        self.instance_id = str(uuid.uuid4())
        self._ws: WebSocket | None = None
        self._ids = itertools.count(1)
        self._events: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        """Connect and register on entry of a ``with`` statement."""
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        """Close the connection."""
        self.close()

    @property
    def connected(self) -> bool:
        """Return whether the WebSocket is open."""
        return self._ws is not None

    def connect(self) -> dict[str, Any]:
        """Open the WebSocket and register this program as an application."""
        credentials = base64.b64encode(
            f'{self.config.username}:{self.config.password}'.encode()).decode()
        try:
            self._ws = WebSocket.connect(self.config.host, self.port,
                                         f'{STACK_PATH}?ui{credentials}',
                                         tls=True, timeout=self.config.timeout,
                                         verify=self.config.verify_tls)
        except (OSError, WebSocketError) as e:
            raise X1Error(f'{self.config.host}: cannot open the device stack: {e}') from e
        return self.request('RegisterApplication', applicationId=APPLICATION_ID,
                            applicationType='ui', pretty='true',
                            instanceId=self.instance_id)

    def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    # ---------------------------------------------------------------- commands

    def request(self, command: str, **params: Any) -> dict[str, Any]:
        """Send one command and return its answer, raising on an error code."""
        if self._ws is None:
            raise X1Error('not connected to the device stack')
        query_id = next(self._ids)
        self._ws.send(json.dumps({'request': {'command': command, **params,
                                              '_gdsqueryId': query_id}}), OP_TEXT)

        while True:
            message = self._receive()
            if (response := message.get('response')) is None:
                continue
            echoed = response.get('request', {})
            if echoed.get('_gdsqueryId') is not None:
                if str(echoed['_gdsqueryId']) != str(query_id):
                    continue            # An answer to an earlier, abandoned request.
            elif echoed.get('command') != command:
                continue                # Some commands answer without echoing the query id.
            error = response.get('error', {})
            if str(error.get('code', ERROR_OK)) != ERROR_OK:
                raise StackError(command, str(error.get('code')), str(error.get('text', '')),
                                 str(error.get('hint', '')))
            return response

    def ui_configuration(self) -> list[dict[str, Any]]:
        """Return the configured functions with their data points."""
        return self.request('GetUIConfiguration', guid='', urns=True).get('config', [])

    def process_view(self) -> list[dict[str, Any]]:
        """Return the current value of every data point known to the user interface."""
        view = self.request('GetProcessView', ui='true', cached='true').get('processView', {})
        return view.get('datapoints', [])

    def device_config(self) -> dict[str, Any]:
        """Return the device configuration the app asks for at start-up."""
        return self.request('GetDeviceConfig', ipc='true')

    def configuration(self, object_id: int) -> dict[str, Any]:
        """Return the configuration of one object, for example a channel."""
        return self.request('GetConfiguration', object={'id': object_id}).get('object', {})

    def in_group(self, group: str) -> bool:
        """Return whether the configured user belongs to the named group."""
        return str(self.request('CheckGroup', group=group).get('isInGroup')) == 'true'

    def get_value(self, *, point_id: int | None = None,
                  urn: str | None = None) -> dict[str, Any]:
        """Return the value of a data point addressed by numeric id or by URN."""
        if (point_id is None) == (urn is None):
            raise ValueError('exactly one of point_id and urn has to be given')
        params: dict[str, Any] = {'internal': 'true'}
        params['id' if point_id is not None else 'urn'] = point_id if point_id is not None else urn
        return self.request('GetValue', **params)

    def set_value(self, value: Any, *, point_id: int | None = None,
                  urn: str | None = None) -> dict[str, Any]:
        """Write a data point addressed by numeric id or by URN.

        The value is sent as a bare JSON number and comes back as a string.
        Switching data points take 0 and 1, brightness takes a percentage from
        0 to 100.  The device confirms the write with a response and
        additionally pushes a ``Value`` event to every connected client.
        """
        if (point_id is None) == (urn is None):
            raise ValueError('exactly one of point_id and urn has to be given')
        params: dict[str, Any] = {'value': value}
        params['id' if point_id is not None else 'urn'] = point_id if point_id is not None else urn
        return self.request('SetValue', **params)

    def switch(self, point_id: int, on: bool) -> dict[str, Any]:
        """Turn a switching data point on or off."""
        return self.set_value(1 if on else 0, point_id=point_id)

    def move(self, point_id: int, down: bool) -> dict[str, Any]:
        """Start a covering moving via its ``Up-Down`` data point.

        The device takes the same 0 and 1 as a switch here.  Which direction
        each value means was not observed directly; 0 for up and 1 for down
        follows the KNX convention for this kind of data point.
        """
        return self.set_value(1 if down else 0, point_id=point_id)

    def stop(self, point_id: int) -> dict[str, Any]:
        """Halt a moving covering via its ``Step-Up-Down`` data point.

        The app sends 1 here, which stops a covering that is under way.  On a
        standing covering the same telegram steps the slats instead.
        """
        return self.set_value(1, point_id=point_id)

    def dim(self, point_id: int, percent: int) -> dict[str, Any]:
        """Set a brightness data point, which is scaled from 0 to 100."""
        if not 0 <= percent <= BRIGHTNESS_MAX:
            raise ValueError(f'brightness has to be between 0 and {BRIGHTNESS_MAX}')
        return self.set_value(percent, point_id=point_id)

    # ------------------------------------------------------------------ events

    def poll(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Return the next pushed event, or ``None`` when none arrived in time."""
        if self._events:
            return self._events.pop(0)
        try:
            self._receive(timeout)      # Anything pushed lands in the event queue.
        except TimeoutError:
            return None
        return self._events.pop(0) if self._events else None

    def events(self, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        """Yield value change events the device pushes on its own, forever."""
        while True:
            if (event := self.poll(timeout)) is not None:
                yield event

    # ---------------------------------------------------------------- internals

    def _receive(self, timeout: float | None = None) -> dict[str, Any]:
        """Read the next JSON message, remembering events that arrive in between."""
        assert self._ws is not None
        while True:
            frame = self._ws.recv(timeout)
            if frame is None:
                raise TimeoutError('no answer from the device stack')
            opcode, payload = frame
            if opcode == OP_CLOSE:
                self.close()
                raise X1Error('the device closed the stack connection')
            try:
                message = json.loads(payload)
            except ValueError:
                continue                # Not something this protocol produces; ignore it.
            if 'event' in message:
                self._events.append(message['event'])
            return message
