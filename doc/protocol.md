# The Gira Device Stack API

This describes the protocol a Gira X1 speaks on port 4432, from the first TCP
connection through to controlling a lamp or a blind.

It is not a published specification.  Everything here was derived by observing
the traffic of the Gira Smart Home app through a logging proxy and confirmed
against a real device (a `GIGSRVKX02` running firmware 2.2.415.0).  Where
something is an inference rather than an observation it says so.

The device also implements the documented **Gira IoT REST API**, but only from
firmware 2.4 onwards.  On older firmware that API is absent — the paths answer
502 through the web server, because the service behind them does not exist —
and the protocol described here is the only way in.  It is what the Gira app
itself uses, on every firmware version.

## Contents

1. [Finding the API](#1-finding-the-api)
2. [Transport](#2-transport)
3. [Opening a session](#3-opening-a-session)
4. [Frames](#4-frames)
5. [Messages](#5-messages)
6. [Commands](#6-commands)
7. [The project](#7-the-project)
8. [Reading and writing values](#8-reading-and-writing-values)
9. [Devices by type](#9-devices-by-type)
10. [Timers](#10-timers)
11. [Data points underneath](#11-data-points-underneath)
12. [Error codes](#12-error-codes)
13. [What the protocol does not offer](#13-what-the-protocol-does-not-offer)

## 1. Finding the API

A Gira X1 listens on several ports.  Only 4432 carries this protocol.

| Port | TLS | `Server:` | Purpose |
|------|-----|-----------|---------|
| 22 | – | – | ssh |
| 80 | no | `nginx` | device portal (configuration web interface) |
| 443 | yes | `nginx` | redirects to port 80 |
| **4432** | **yes** | **`Gira Device Stack API`** | **this protocol** |
| 4433 | yes | `Gira Device OS/1.0` | device OS, no API paths |
| 8080 | no | `Gira Device OS/1.0` | device OS, no API paths |
| 8153 | no | `WebSocket++/0.8.1` | a WebSocket endpoint that stays silent |

The device will confirm the port itself once a session is open:

```jsonc
// → {"request":{"command":"GetDeviceConfig","ipc":"true","_gdsqueryId":1}}
{"deviceConfig": {"ipc": {"ApiPort": "4432", "ApiSecure": "true",
                          "ApiPortExtraCert": "4434", "AppDeviceName": "Gira X1"}}}
```

Port 4434 (`ApiPortExtraCert`) was not listening on the device examined.

## 2. Transport

TLS, and nothing else — the port speaks no cleartext.

```
TLSv1.2   ECDHE-RSA-AES256-GCM-SHA384   no ALPN
```

The certificate is issued by a private CA and cannot be verified against the
public trust store:

```
subject = CN=GI GSRVKX02 00:0A:B3:29:2C:0A GIGSRVKX02292C0A
issuer  = C=DE, L=Radevormwald, O=Gira Giersiepen GmbH & Co. KG, CN=Gira CA
```

`GIGSRVKX02` is the X1 device type identifier, so the certificate identifies
the model.  A client must either skip verification or be given the device
certificate as its trust anchor.  Gira's own documentation for the REST API
says the same thing about that API: the connection "is not fully trusted,
because it is not technically possible that the server provides a trusted TLS
certificate".

## 3. Opening a session

The port serves exactly one thing: a WebSocket upgrade at `/gds/api`.  Any
ordinary HTTP request is refused.

```http
GET /gds/api HTTP/1.1

HTTP/1.1 426 Upgrade Required
Server: Gira Device Stack API
```

The 426 carries **no `Upgrade:` header and no body**, so it does not say what
to upgrade to.  (Port 8153 answers the same way and identifies itself as
`WebSocket++`, which is the hint that the server side is that library.)

### Authorization

Credentials travel in the **query string**, not in a header:

```
/gds/api?ui<base64(username + ":" + password)>
```

The `ui` prefix is a literal two characters in front of the base64.  For a user
`User` with password `secret` the path is `/gds/api?uiVXNlcjpzZWNyZXQ=`.  The
credentials are the ones of a user configured in the device, the same used by
the Gira Smart Home app.

> Because the credentials are in the URL rather than in an `Authorization`
> header, anything that logs request lines — a proxy, a capture file — records
> them in full.  They are protected by TLS on the wire and by nothing at all
> once written down.

### The handshake

An ordinary RFC 6455 client handshake, with no extensions requested:

```http
GET /gds/api?ui<base64(user:password)> HTTP/1.1
Host: gira-x1.fritz.box:4432
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: <16 random bytes, base64>
Sec-WebSocket-Version: 13

HTTP/1.1 101 Switching Protocols
Connection: Upgrade
Sec-WebSocket-Accept: <the usual digest>
Server: Gira Device Stack API
Upgrade: websocket
```

Two details worth knowing:

- **The path matters.** A handshake to `/` is answered with `400 Bad Request`,
  not 101.  The upgrade only succeeds at `/gds/api`.
- **Wrong credentials were not tested**, so it is unknown whether the handshake
  itself validates them or whether a later command fails.  Do not rely on 101
  meaning the credentials are good.

The Gira app offers `permessage-deflate`; the device does not accept it, and
payloads are therefore plain text.

## 4. Frames

Standard RFC 6455.  What the device actually does:

- **It pings**, roughly every five seconds, with an empty payload.  A client
  that does not answer with a pong is disconnected.  This matters for any
  client that waits: a plain `sleep` of thirty seconds across a blind's travel
  will lose the connection.
- Client frames must be masked, as the standard requires.
- Messages observed are always single, unfragmented text frames.  Reassembly of
  continuation frames has not been needed in practice.
- Closing is the usual close frame; the device answers with its own.

## 5. Messages

Every message is a JSON object with exactly one of three keys.

### Request

```jsonc
{"request": {"command": "GetValue", "id": 150009, "internal": "true",
             "_gdsqueryId": 2}}
```

`_gdsqueryId` is chosen by the client and echoed back, which is how answers are
matched to questions on a connection where events arrive in between.  It is
**optional**: the Gira app omits it on `SetValue`, and the device then echoes
the request without one, leaving the command name as the only thing tying the
answer to the request.  A client should cope with both.

### Response

```jsonc
{"response": {
   "request": {"command":"GetValue","internal":"true","id":"150009","_gdsqueryId":"2"},
   "version": {"major":"1","minor":"0","text":"1.0"},
   "error":   {"code":"0","text":"OK","hint":""},
   "id":      "150009",
   "urn":     "urn:gds:dp:GiraX1.GIGSRVKX02:KNX-GA-Channel:Lampeeingangsflurschalten",
   "value":   "1",
   "valueState": "Value"}}
```

`error.code` is `"0"` on success.  Anything else is a failure and the payload
fields are absent.

> **Numbers go out as numbers and come back as strings.**  A request may send
> `"id": 150009` and `"value": 1`; the echo and the payload will read `"150009"`
> and `"1"`.  A parser that insists on the type it sent will break.

### Event

Pushed by the device without being asked, on the same connection:

```jsonc
{"event": {
   "version": {"major":"1","minor":"0","text":"1.0"},
   "error":   {"code":"0","text":"OK","hint":""},
   "type":    "Value",
   "value":   {"id":"150009",
               "urn":"urn:gds:dp:GiraX1.GIGSRVKX02:KNX-GA-Channel:Lampeeingangsflurschalten",
               "new":"1", "old":"0",
               "newInternal":"2.3:true", "oldInternal":"2.3:false",
               "state":"Value",
               "source":"c:Gira.UniversalApp"}}}
```

Events arrive for every change, whoever caused it — this client, the Gira app,
a wall switch, or a timer in the device.  `source` names the application that
caused it and is empty for a change the device made itself.  `newInternal`
carries the KNX datapoint type with the value, as `2.3:true` or
`13.8:2026-09-14 13:30:00`.

An event for a `SetValue` **arrives before the response to it**.  A client that
reads until it sees its response must not discard what comes first.

### Registration

One command must come first.  Any other command before it is refused:

```jsonc
{"error": {"code": "100", "text": "Registration missing.", "hint": ""}}
```

```jsonc
{"request": {"command": "RegisterApplication",
             "applicationId": "Gira.UniversalApp",
             "applicationType": "ui",
             "pretty": "false",
             "instanceId": "9bcbd3aa-7a26-4028-bc05-ff84261f4205",
             "_gdsqueryId": 1}}
```

`instanceId` is a UUID the client invents per session.  `applicationId` is the
name the Gira app uses; whether the device treats other names differently was
not tested.  With `pretty` set to `"true"` the reply to this command is
pretty-printed; later replies are compact regardless.

## 6. Commands

Confirmed present on firmware 2.2.415.0:

| Command | Purpose |
|---------|---------|
| `RegisterApplication` | open the session; must be first |
| `GetUIConfiguration` | the project: functions, data points, grouping |
| `GetProcessView` | current value of every data point |
| `GetValue` | one value, by `id` or by `urn` |
| `SetValue` | write one value |
| `GetConfiguration` | any object by `id` or `urn`: channels, data points, timers |
| `SetConfiguration` | write an object; how timers are edited |
| `GetDeviceConfig` | device settings, including its own API port |
| `CheckGroup` | whether the logged-in user is in a group |
| `GetUsers` | the configured users |

Answered with `102 Unknown command`, i.e. **not** present on this firmware:
`GetLicenses`, `GetDeviceInfo`, `GetProjectInfo`, `GetTrades`, `GetLocations`,
`GetFunctions`, `GetDevices`, `GetChannels`, `GetNextTriggerTimes`,
`GetTimeProfiles`, `GetScenes`.  Some of these are things the Gira app asks for
and does without; newer firmware may answer them.

`SetUIConfiguration` exists and was not explored.

## 7. The project

`GetUIConfiguration` returns everything the user interface needs.

```jsonc
// → {"request":{"command":"GetUIConfiguration","guid":"","urns":true,"_gdsqueryId":3}}
{"config": [
  {"guid": "58162990-615a-47f4-a2c2-d236705fcd1f"},
  {"channelViewID": 280037,
   "channelViewUrn": "urn:gds:chv:Switch-Switch-1",
   "functionType": "de.gira.schema.functions.Switch",
   "channelType": "de.gira.schema.channels.Switch",
   "name": "Deckenlampen Flur",
   "iconID": 1,
   "dataPoints": [{"dataPoint": "OnOff", "id": 150009,
                   "urn": "urn:gds:dp:GiraX1.GIGSRVKX02:KNX-GA-Channel:Lampeeingangsflurschalten"}],
   "parameters": [{"set":"Visu","key":"OffText","value":"Aus"}, …],
   "switchTimeChannelID": 30000,
   "switchTimeChannelUrn": "urn:gds:chn:GiraX1.GIGSRVKX02:Function-Timer-1",
   "switchTimes": []},
  … one entry per function …,
  {"mainType": "Root", "subType": "Root", "name": "Root", "content": [ … ]}
]}
```

**`guid` is a cache check, not a filter.**  Passing the project guid returns
only `[{"guid": …}]` — an empty answer meaning "you already have it".  Pass
`""` to get the whole configuration.

### Grouping

The single `Root` entry holds a tree that groups the functions.  Nodes carrying
a `mainType` are structure; children carrying only a `channelViewID` are
references to functions.

```
Root
├── Profile/Collection  "Profile collection"
├── Trade/Collection    "Gewerke"
│   ├── Trade/Lighting  "Licht"        → 280037, 280084, 280027, …
│   ├── Trade/Switching "Schalten"     → 280114
│   ├── Trade/Covering  "Beschattung"  → 280013, 280055, …
│   └── Trade/HVAC, Scenes, Timer, Security, …   (empty here)
├── Zone/Collection     "Zonenerfassung"
│   └── Zone/SecurityArea, Zone/DcsLine → Location nodes, no functions
└── Location/Collection "Standortbestimmung"
    ├── Location/Corridor "Flur"       → 280037, 280013, 280084
    ├── Location/Room     "Küche"      → …
    └── …
```

So a function can be reached by trade (`Gewerk`) and by location (`Standort`).
Trades with no members exist and are best hidden.  The `Zone` hierarchy listed
locations but no functions on the device examined.

## 8. Reading and writing values

### All values at once

```jsonc
// → {"request":{"command":"GetProcessView","ui":"true","cached":"true","_gdsqueryId":4}}
{"processView": {"datapoints": [
   {"id":"150002","state":"-0","valueState":"Value"},
   {"id":"150009","state":"0","valueState":"Value"}, … ]}}
```

Note `state`, not `value`, and note that `-0` occurs.

### One value

```jsonc
// → {"request":{"command":"GetValue","id":150009,"internal":"true","_gdsqueryId":5}}
{"id":"150009","urn":"…","value":"1","valueState":"Value"}
```

`urn` may be given instead of `id`.  `internal` is what the Gira app sends;
its effect was not isolated.

### Writing

```jsonc
// → {"request":{"command":"SetValue","id":150009,"value":1}}
{"id":"150009","urn":"…","value":"1"}
```

The value is a bare JSON number.  There is no separate command per device type:
a lamp, a dimmer and a blind are all written with `SetValue`, and only the data
point and the value differ.

## 9. Devices by type

A function's `functionType` says what it is; its `dataPoints` say what can be
done to it.  A data point with `"id": 0` is declared by the function type but
not bound to a KNX group address in this project, and can be neither read nor
written.

### Switch — `de.gira.schema.functions.Switch`

One data point.

| Data point | Values |
|------------|--------|
| `OnOff` | `1` on, `0` off |

```jsonc
// on
{"request":{"command":"SetValue","id":150009,"value":1}}
// off
{"request":{"command":"SetValue","id":150009,"value":0}}
```

### Light — `de.gira.schema.functions.KNX.Light`

A dimmable light. `Shift` is a relative dim step and was never observed being
written by the app.

| Data point | Values |
|------------|--------|
| `OnOff` | `1` on, `0` off |
| `Brightness` | **percentage, 0–100** |
| `Shift` | relative dimming (KNX DPT 3.007), not observed |

```jsonc
// full brightness
{"request":{"command":"SetValue","id":150004,"value":100}}
```

The scale is a percentage and not a 0–255 byte: dragging the app's slider from
bottom to top produced 1, 13, 33, 53, 96, 98 and never more than 100.

### Covering — `de.gira.schema.functions.Covering`

Blinds, roller shutters and awnings.

| Data point | Values |
|------------|--------|
| `Up-Down` | **`0` up, `1` down** |
| `Step-Up-Down` | `1` halts a covering that is moving |
| `Movement`, `Position`, `Slat-Position` | position feedback — see below |

```jsonc
// lower
{"request":{"command":"SetValue","id":150042,"value":1}}
// halt
{"request":{"command":"SetValue","id":150021,"value":1}}
```

The direction mapping was confirmed twice by watching a blind, and the device's
own timer scenes agree: the scene named `move-0` writes `Up-Down = 0` and is
used for the morning opening.

By KNX convention `Step-Up-Down` on a covering that is standing still steps the
slats rather than halting anything; only its use as a stop was observed here.

**`Movement`, `Position` and `Slat-Position` were unbound on every covering of
the installation examined.**  Where an actuator does report position and the
project binds these, they become ordinary data points: readable, and pushed as
events like any other.

## 10. Timers

Every function owns a timer channel of type
`de.gira.schema.channels.FunctionTimer`, named in `switchTimeChannelID`.  The
`switchTimes` array in the UI configuration is only a summary — `[{"index":0,
"active":true}, …]` — and the detail lives in the channel.

### Reading

```jsonc
// → {"request":{"command":"GetConfiguration","object":{"id":30060},"_gdsqueryId":6}}
{"object": {
  "name": "Function-Timer-11",
  "extendedType": "de.gira.schema.channels.FunctionTimer",
  "timer": {
    "name": "Trigger", "id": "30061", "type": "CALENDARDP",
    "nextTriggerTime": "2026-09-14 22:21:00",
    "switchTimes": [
      {"index":"1","active":"true","timeType":"pointInTime","time":"06:00:00",
       "recurrence":"daily","weekdays":"11111--","validity":"begin",
       "beginDate":"2000-03-25","triggerValue":"1"},
      {"index":"2","active":"true","timeType":"pointInTime","time":"22:21:00",
       "recurrence":"daily","weekdays":"1111111","validity":"begin",
       "beginDate":"2000-03-26","triggerValue":"2"}]},
  "sceneSet": {
    "name": "Scene-Set",
    "datapoints": [{"id":"150029","urn":"…:Jalousienallefahren"}],
    "scenes": [
      {"number":"1","name":"Scene1",
       "datapoints":[{"id":"150029","value":"0","delay":"0"}]},
      {"number":"2","name":"Scene2",
       "datapoints":[{"id":"150029","value":"1","delay":"0"}]}]},
  "timeIndex":   {"name":"Time-Index",   "id":"30062","type":"VARDP"},
  "triggerValue":{"name":"Trigger-Value","id":"30063","type":"VARDP"}}}
```

The two halves only make sense together:

- **`timer.switchTimes` says when.**  A time of day, a mask of weekdays, and a
  `triggerValue`.
- **`sceneSet.scenes` says what.**  Numbered scenes, each writing data points to
  fixed values.

`triggerValue` is a **scene number**.  Above, the blind opens at 06:00 on
weekdays by applying scene 1, which writes `Up-Down = 0`.

Scene naming follows the value, as the Gira app creates them: `switch-1` and
`switch-0` for a switch, `move-0` and `move-1` for a covering, and `dim-0`
through `dim-100` in steps of ten for a brightness.

### The weekday mask is written differently than it is read

This is the one trap in the whole protocol.

| Direction | Monday to Friday |
|-----------|------------------|
| read | `11111--` — a flag per day |
| write | `01234--` — **the number of the day itself** |

Both mean Mon–Fri, and a write of `01234--` reads back as `11111--`.  Writing
the read encoding programs the wrong days silently.

### Writing

All changes go through `SetConfiguration` on the timer channel.  Only the
fields being changed need to be sent; the device keeps the rest.  The entry is
identified by `index`.

Change the time and days of an existing entry:

```jsonc
{"request":{"command":"SetConfiguration","object":{"id":30072,
  "timer":{"switchTimes":[{"index":"1","time":"07:15:00",
                           "timeType":"pointInTime",
                           "weekdays":"01234--","recurrence":"daily"}]}}}}
```

Switch an entry on or off, which is all the app sends for that:

```jsonc
{"request":{"command":"SetConfiguration","object":{"id":30000,
  "timer":{"switchTimes":[{"active":"true","index":"1"}]}}}}
```

Add an entry under a free index:

```jsonc
{"request":{"command":"SetConfiguration","object":{"id":30000,
  "timer":{"switchTimes":[{"index":"1","active":"false","recurrence":"daily",
                           "weekdays":"01234--","timeType":"pointInTime",
                           "time":"06:30:00","randomTimeFunction":"false",
                           "triggerValue":"1"}]}}}}
```

Delete an entry:

```jsonc
{"request":{"command":"SetConfiguration","object":{"id":30000,
  "timer":{"switchTimes":[{"index":"1","remove":"true"}]}}}}
```

A channel that has never had a timer has **no scenes**, and a switch time
referring to a scene that does not exist is useless.  The scene set has to be
written first — which is what the app does, in three messages: define the
scenes, add the switch time, then activate it.

```jsonc
{"request":{"command":"SetConfiguration","object":{"id":30000,"enabled":true,
  "timer":{"switchTimes":[]},
  "sceneSet":{"datapoints":[{"id":150009}],
    "scenes":[{"number":"1","name":"switch-1",
               "datapoints":[{"id":150009,"value":1}]},
              {"number":"2","name":"switch-0",
               "datapoints":[{"id":150009,"value":0}]}]}}}}
```

Removing the last switch time, the app also clears the scenes:

```jsonc
{"request":{"command":"SetConfiguration","object":{"id":30000,
  "sceneSet":{"clear":"true","clearParameters":"scenes"}}}}
```

> Writing a scene set **replaces** whatever scenes the channel had, which
> repoints every switch time that referred to them.  It is only safe on a
> channel with none.

## 11. Data points underneath

`GetConfiguration` on a data point id shows the KNX object behind it, which is
where the datapoint type and the group addresses live.

```jsonc
// → {"request":{"command":"GetConfiguration","object":{"id":150042}}}
{"object": {
  "name": "KFCcheJalousie1fahren", "id": "150042", "type": "KNXDP",
  "valueType": "Binary", "value": "1", "internalValue": "2.3:true",
  "readGroupAddress": "1/1/0", "writeGroupAddress": "1/1/0",
  "listenerGroupAddresses": "",
  "permitReadFromBus": "false", "eventAlways": "true",
  "dptMainNumber": "1", "dptSubNumber": "8"}}
```

Data points are numbered from their channel: the KNX group address channel is
`150000` and its data points run upwards from `150001`.  Walking that range
reveals **every** group address in the project, including ones the user
interface does not show — on the installation examined that turned up a status
object for a lamp (`1.011`) and an unused brightness object, neither of which
appears in `GetUIConfiguration`.

Unused slots exist too, named `KNX-Data-Point-NN` with `dptMainNumber` 0 and no
group address.

## 12. Error codes

| Code | Text | Meaning |
|------|------|---------|
| `0` | OK | success |
| `2` | Failed to set object configuration | a `SetConfiguration` the device would not take |
| `100` | Registration missing | a command sent before `RegisterApplication` |
| `102` | Unknown command | not implemented on this firmware |
| `111` | Get object from ID failed | no such object; `hint` gives the id |

`error.hint` carries the detail — the offending id, or the command name.

```jsonc
{"response":{"request":{"command":"SetValue","value":"1","id":"999999","_gdsqueryId":"3"},
  "error":{"code":"111","text":"Get object from ID failed.","hint":"OID = 999999"}}}
```

Codes are strings in responses.  Other codes exist — the documented REST API
lists a `423 Device is currently locked` — but were not seen here.

## 13. What the protocol does not offer

- **No position or movement feedback for blinds**, unless the actuator provides
  it and the project binds `Movement` or `Position`.  Reading `Up-Down` gives
  the last direction *commanded*, not where the blind is or whether it is
  moving.  A blind that finished moving hours ago still reads `down`.
- **No acknowledgement that a physical device acted.**  The response to
  `SetValue` confirms the device accepted the value, not that the lamp lit.
- **No transactions.**  Several devices are written one command at a time.
- **No discovery of what a command expects.**  An unknown command is answered
  with 102 and a wrong parameter with a terse message; the shapes here were
  learned by watching a client that already knew them.

---

## Appendix: a session end to end

```
TCP   → gira-x1.fritz.box:4432
TLS   → TLSv1.2, certificate from "Gira CA", verification skipped

→ GET /gds/api?ui<base64(user:password)> HTTP/1.1
  Upgrade: websocket … Sec-WebSocket-Version: 13
← HTTP/1.1 101 Switching Protocols

→ {"request":{"command":"RegisterApplication","applicationId":"Gira.UniversalApp",
              "applicationType":"ui","pretty":"false",
              "instanceId":"…","_gdsqueryId":1}}
← {"response":{…,"error":{"code":"0","text":"OK","hint":""}}}

→ {"request":{"command":"GetUIConfiguration","guid":"","urns":true,"_gdsqueryId":2}}
← {"response":{…,"config":[…23 functions and the Root tree…]}}

→ {"request":{"command":"GetProcessView","ui":"true","cached":"true","_gdsqueryId":3}}
← {"response":{…,"processView":{"datapoints":[…]}}}

→ {"request":{"command":"SetValue","id":150009,"value":1}}
← {"event":{"type":"Value","value":{"id":"150009","old":"0","new":"1",
            "source":"c:Gira.UniversalApp"}}}          ← the event comes first
← {"response":{…,"id":"150009","value":"1"}}

  … device pings every ~5s, client must pong …

← close frame
```
