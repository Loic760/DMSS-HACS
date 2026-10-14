# DMSS HACS integration

DMSS is a Home Assistant custom integration for HACS. Version 0.2.2 provides email/password setup through the official DMSS desktop login, automatic session recovery, and door-release controls compatible with HomeKit Bridge.

## Install

Home Assistant 2026.9.4 or newer is required.

For HACS, add `https://github.com/Loic760/DMSS-HACS` under **HACS → Custom repositories**, category **Integration**, install DMSS, then restart Home Assistant. HACS requires the repository to be public. While this repository is private, use the manual installation below.

For manual installation, download the repository and copy the complete `custom_components/dmss` folder to `/config/custom_components/dmss`, then restart Home Assistant. No terminal login or macOS Keychain dependency is needed on the Home Assistant host.

Open **Settings → Devices & services → Add integration → DMSS** and enter your DMSS email and password.

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

## Apple Home / HomeKit Bridge

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

The client was tested against the actual account and installation. The two primary door releases were physically confirmed by the installation owner. Home Assistant 2026.9.4 and 2026.10.1 tests verify setup, all three exact entity IDs, one-shot button dispatch, unavailable states, authentication error handling, and unloading. CI checks both Home Assistant versions. Protocol tests cover independent renewal, expired sessions, credential rejection, signatures, and non-replayed ambiguous commands.

This version provides account/door controls and doorbell event handling. Camera and two-way audio remain separate local work and are not included in this release.

Development checks (run each suite separately):

```sh
python -m unittest discover -s tests -p test_client.py
python -m unittest discover -s tests -p test_web_auth.py
python -m unittest discover -s tests -p test_runtime.py
python -m unittest discover -s tests -p test_bell.py
python -m unittest discover -s tests -p test_homeassistant.py
ruff check custom_components --select F
```
