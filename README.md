# DMSS HACS integration

DMSS is a Home Assistant custom integration for HACS. Version 0.3.0 adds on-demand HD cameras with received audio. Email/password setup, automatic session recovery, all three unlock buttons and doorbell events remain available.

Version 0.3.2 improves HomeKit startup and reopening with a faster local container,
decodable keyframe caching, and a fix for a cancelled shutdown timer interrupting
a reopened stream. HACS layout metadata and official repository validation from
0.3.1 remain included.

## Install

Home Assistant 2026.9.4 or newer is required.

For HACS, add `https://github.com/Loic760/DMSS-HACS` under **HACS → Custom repositories**, category **Integration**, install DMSS from the default branch (`main`), then restart Home Assistant.

For manual installation, download the repository and copy the complete `custom_components/dmss` folder to `/config/custom_components/dmss`, then restart Home Assistant. No terminal login or macOS Keychain dependency is needed on the Home Assistant host.

Open **Settings → Devices & services → Add integration → DMSS** and enter your DMSS email and password.

### HACS download error: `custom_components/None/manifest.json`

The integration manifest is at `custom_components/dmss/manifest.json`. `None` in
the error means HACS has not resolved the integration directory; it is not the
integration's domain. The published repository tree and manifest at commit
`36811f7` were independently verified using HACS 2.0.5's directory discovery.

1. Update HACS to its latest stable version and restart Home Assistant. A restart
   clears an unresolved repository path held by the running HACS instance.
2. Open **HACS → DMSS → ⋮ → Update information**, then download/redownload the
   latest default branch (`main`), rather than selecting the old `36811f7` commit.
3. Restart Home Assistant after the download.

If HACS still shows the same `None` path, remove the DMSS repository entry from
HACS, restart Home Assistant, and add the custom repository again as an
**Integration** before downloading it. Keep your configured DMSS entry under
**Settings → Devices & services**. Do not rename the integration folder to `None`
or delete Home Assistant's `.storage` files.

If the error persists after re-registration, include the HACS version and its
download log in an issue. A repository metadata update alone cannot repair an
incomplete GitHub tree response or stale path inside a running HACS instance.

## Door controls

The interphone channels and relays are discovered from your account. Channels named front and back produce:

- `button.unlock_building_front_1`
- `button.unlock_building_back_1`
- `button.unlock_building_back_2`

Each press sends an unlock pulse. The device controls the release duration. The integration does not infer physical locked/unlocked state from a successful cloud response. Existing user-renamed entity IDs are preserved, and Home Assistant adds a suffix if an ID is already occupied.

Discovery includes the primary relay of a configured door channel when DMSS omits its relay list. This is enabled by default and can be changed in the integration's options. Listed relays are always exposed.

## Doorbell events and notifications

Version 0.2.2 adds a live native event subscription to the indoor VTH over local TCP port 5000. Home Assistant must reach the indoor interphone on that port; outdoor stations can remain on another network. Credentials are obtained from the existing DMSS login, with no extra device password setup. The integration subscribes to `VideoTalkPhone.attachCallState` and reads `VideoTalkPhone.getCallState` every second on the same local connection. This read-only fallback detects the current incoming-call state when the firmware only pushes the final call log. Unsupported state queries leave the existing push feed enabled. It never answers, rejects or hangs up a call.

New event entities:

- `event.building_doorbell_front`
- `event.building_doorbell_back`
- `event.building_doorbell_unknown`

Event types and phases:

| Event type | Meaning | Phase |
|---|---|---|
| `ring` | Incoming/ringing call | `start` |
| `call` | Call connecting or connected | `connecting` or `connected` |
| `call_ended` | Current call ended, or completed-call record | `end` |
| `missed_call` | Explicit missed-call state or record | `end` |

Ringing transitions trigger once, even when both push and state polling report the same call. Connected and ended states are not converted into ring alerts. Polling an idle interphone after setup does not create an event. A ring-to-idle transition alone is not guessed to be a missed call; only an explicit missed signal creates `missed_call`. Door attribution uses the event's channel or a known outdoor address from inventory. If the device omits door identity, the event appears on the unknown entity instead of guessing. Local connection failures make event entities unavailable; reconnects refresh device credentials. Cloud unlock buttons remain independent.

In the automation editor, choose **Device → your DMSS interphone**. Its trigger list now offers **Front doorbell rings**, **Back doorbell rings**, and **Doorbell rings (entrance unknown)**. These triggers only match ring starts, exclude missed-call records, and are scoped to the selected device. Add an iPhone notification action and save.

For a YAML notification automation, use the native event bus example below. Replace the mobile notification service with your own service under Home Assistant's Actions. The `phase: start` filter ensures a missed-call record does not become a delayed ring notification.

```yaml
alias: Building doorbell notification
triggers:
  - trigger: event
    event_type: dmss_doorbell
    event_data:
      type: ring
      phase: start
actions:
  - action: notify.mobile_app_your_iphone
    data:
      title: Building doorbell
      message: >-
        {{ trigger.event.data.door | title }} entrance is ringing.
mode: queued
```

The entities can also be used in state-change automations; filter `event_type: ring` and `phase: start`, and ignore initial restored/unavailable states. Event-bus triggers above only fire for received events and avoid restoration alerts. No notification is sent until the user configures an automation.

The native subscription, call-state attachment and repeated current-state queries succeeded on the tested indoor interphone. The owner previously observed a missed-call event. Home Assistant tests replay the complete incoming → connected → ended sequence and verify that notification triggers fire only on the incoming event. Actual incoming-start timing and front/back attribution still need a physical ring test; idle queries alone do not establish those results.

After updating, restart Home Assistant. The existing automation filtered to `type: ring` and `phase: start` remains valid. Download integration diagnostics to check `incoming_call_poll_connections` and `incoming_call_push_subscriptions`; supported, connected installations should report a nonzero polling count. No camera, microphone, call-answer or hang-up connection is opened by this listener.

Protocol references: [Dahua native/RPC implementation](https://github.com/rroller/dahua), [VTO/VTH event meanings](https://github.com/myhomeiot/DahuaVTO). The protocol implementation is independent and contains no copied credentials or device addresses.

## HD cameras with audio

After updating and restarting Home Assistant, the existing DMSS entry adds:

- `camera.building_front`
- `camera.building_back`

The integration requests **main stream0 (HD)**. Both entrances were live-verified
at **1280×720 H264**, with no resize or video re-encoding. That is the highest
configured main profile exposed by the tested installation; capability queries
did not expose a higher sensor resolution. Original video is copied into a local
FLV feed, while received16kHz mono PCM is converted to AAC so HA can play it.
Home Assistant's stream component supports AAC/MP3 rather than raw PCM.
[Stream documentation](https://www.home-assistant.io/integrations/stream/)

Both real cameras produced video plus AAC through the installable integration's
media manager. Two-second received-audio samples were non-silent but very quiet;
speech audibility and physical Apple TV/iPhone pairing need owner observation.
This is listening audio; it does not transmit a microphone or establish a call.
The indoor interphone has no camera.

### Starts when viewed, not24/7

Leave each camera's **Preload stream off**. Live viewing opens the cloud feed;
an idle integration opens none. Home Assistant can retain an HLS worker for
approximately30 seconds after the last viewer; the bridge releases its source
after the last consumer disconnects, with a short idle grace. A dashboard card
configured for continuous live viewing, recording, another consumer, or Preload
can keep the connection active. It is based on connected consumers, not whether
a person is physically looking at the screen.
[Camera/preload documentation](https://www.home-assistant.io/integrations/camera/)

Thumbnails use only the most recently cached full-resolution frame; thumbnail
refreshes never start another camera connection. Before the first live view
there may be no thumbnail, and an idle thumbnail can be stale. Open the camera
to refresh it. Capturing the cache does not open a second cloud session.

Each new or recovered connection refreshes the media lease and device
credentials through the integration's shared renewal lock. Read failures use a
bounded reconnect while retaining the same local player. Verification/locked
account failures request reauthentication. Expiry recovery is fault-injection
tested; a full live token-lifetime test would exceed the authorized15-second
camera session allowance.

### Concurrent viewers and device impact

HA and HomeKit viewers of the **same entrance** share one upstream connection
inside this integration. Separate front/back feeds need separate connections.
The sharing and cleanup behavior are tested; the physical installation's maximum
concurrent cloud streams and interaction with a separate DMSS phone are not.
Another phone uses its own session, so resource/account limits could make a view
busy, fail or affect an existing view. No guarantee of unlimited physical viewers
is made.

A preview is an active viewing session and consumes bandwidth/device resources.
It sends no unlock, call-answer, hang-up or microphone commands. It can still be
logged by the device/cloud, and firmware may show a viewing/busy indicator. Those
physical indicators were not observed during development; viewing is not promised
invisible or free of impact on other users.

### HomeKit camera accessories

Keep the existing unlock-button bridge. Add each camera in a separate HomeKit
**accessory-mode** instance. This is independent of the button bridge. Set
`support_audio: true`; `video_codec: copy` preserves compatible original H264.
HomeKit converts the received audio to its supported Opus profile. If managing
HomeKit in YAML, merge these entries into your existing `homekit:` list, using
unused ports; do not replace your current button bridge:

```yaml
homekit:
  - name: Building Front Camera
    port: 21071
    mode: accessory
    filter:
      include_entities:
        - camera.building_front
    entity_config:
      camera.building_front:
        support_audio: true
        video_codec: copy
        audio_codec: libopus
        max_width: 1280
        max_height: 720
        max_fps: 25
        stream_count: 3
  - name: Building Back Camera
    port: 21072
    mode: accessory
    filter:
      include_entities:
        - camera.building_back
    entity_config:
      camera.building_back:
        support_audio: true
        video_codec: copy
        audio_codec: libopus
        max_width: 1280
        max_height: 720
        max_fps: 25
        stream_count: 3
```

`stream_count` advertises a HomeKit viewer limit; it is not proof of physical
camera capacity. These local viewers use the shared upstream. Pair both accessory
codes in Apple Home. Actual Apple TV pairing remains untested.
[Official HomeKit camera settings](https://www.home-assistant.io/integrations/homekit/)

### HomeKit startup and reopening

Version 0.3.2 uses a local FLV container that announces H264/AAC codec headers
early. In an offline replay of the same saved 720p camera recording, an FFmpeg
consumer using HomeKit's default input probing sent its first video RTP packet
in approximately **1.5 seconds**, versus **5 seconds** with the previous MPEG-TS
container. These measurements exclude cloud lease acquisition and connection
setup; they are not an Apple Home or remote-viewing latency guarantee.

A viewer joining an active feed receives codec headers and the newest complete
keyframe group. Older history is discarded. A cancelled idle timer cannot stop
a reopened session. Offline regression tests verify cold starts, a second viewer
sharing the source, and reopening after shutdown, including decodable H264 video
and Opus audio output. HA's PyAV decoder is checked separately.

After updating from 0.3.1, restart Home Assistant. Keep the existing camera entity
IDs, HomeKit pairing and YAML settings (`video_codec: copy`, `support_audio: true`,
`audio_codec: libopus`). Leave Preload stream off. The update does not keep a
camera connection open while idle; the installed Apple Home setup still needs
the owner's startup/reopen confirmation after updating.

## Apple Home / HomeKit Bridge — existing buttons

Home Assistant's HomeKit Bridge supports `button` entities as momentary switches. Switching one on presses it once and the bridge resets it. This matches a door-release pulse. See the [official supported integrations](https://www.home-assistant.io/integrations/homekit/#supported-integrations).

Add these three entities to the bridge's include filter in its options, or use this example for a YAML-managed bridge:

```yaml
homekit:
  - name: Building Doors
    filter:
      include_entities:
        - button.unlock_building_front_1
        - button.unlock_building_back_1
        - button.unlock_building_back_2
```

Pair the bridge with Apple Home. An Apple TV configured as a Home hub can provide remote Home access. Actual Apple TV pairing has not been tested during development.

## Login and renewal

The inspected official desktop login has no CAPTCHA step in its ordinary email/password flow. Live desktop login, mobile user-info and device APIs, token-login, credential decoding, and door requests succeeded without CAPTCHA. This is evidence about the tested flow, not a promise that the server can never require verification. A verification or account-lock response stops authentication and requests user action; this integration never bypasses CAPTCHA or silently falls back to mobile authentication.

Independent renewal policies run while Home Assistant is running:

- Session/token-login check after 30 minutes.
- User/open-user credentials and regional host reload after 15 minutes.
- Full desktop password login after six hours, independently of token rotation, or after a known expired-session response.
- Fresh inventory and device credentials for each door operation, with one rebuild after an explicit credential rejection.

These intervals are maintenance policies; the server's exact token expiry has not been established. No separate refresh-token endpoint was found. Automatic renewal uses the official token-login and fresh password-login routes.

Renewal is checked every five minutes even when no controls are used. Inventory polling runs every ten minutes. Network operations are serialized and run outside Home Assistant's event loop. Ambiguous unlock timeouts are never replayed automatically.

## Errors and credentials

Setup failures appear on the integration in Home Assistant. Account authentication failures trigger its reauthentication flow. Inventory or renewal failures make the controls unavailable; a failed press raises a Home Assistant action error. Home Assistant decides how those errors are displayed.

Email and password are stored in the Home Assistant config entry. Session and device credentials remain in memory and are recreated after restart. Diagnostics expose counts and status only. Repository files contain no personal credentials, session exports, APKs, or device-specific addresses.

## Validation and scope

The client was tested against the actual account and installation. The two primary door releases were physically confirmed by the installation owner. Home Assistant 2026.9.4 and2026.10.4 (latest stable checked October31,2026) tests cover setup, all three exact button IDs, camera IDs, one-shot button dispatch, unavailable states, authentication handling, lazy media and unloading. CI checks both versions with their matching component dependencies. Protocol tests cover independent renewal, expired sessions, credential rejection, signatures, and non-replayed ambiguous commands.

Cloud HD video and received audio are included. Outdoor two-way microphone audio is not part of this camera release. Camera connections and FFmpeg processes close when idle/unloaded. No Mac, local Vite server, APK or separate bridge add-on is needed on the HA host; the integration starts its loopback media bridge itself. Home Assistant's FFmpeg and stream dependencies must be available.

Development checks (run each suite separately):

```sh
python -m unittest discover -s tests -p test_client.py
python -m unittest discover -s tests -p test_web_auth.py
python -m unittest discover -s tests -p test_runtime.py
python -m unittest discover -s tests -p test_bell.py
python -m unittest discover -s tests -p test_homeassistant.py
python -m unittest discover -s tests -p test_camera.py
python -m unittest discover -s tests -p test_media.py
ruff check custom_components --select F
```
