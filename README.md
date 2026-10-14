# DMSS HACS integration

DMSS is a Home Assistant custom integration for HACS. Version 0.1.1 provides email/password setup through the official DMSS desktop login, automatic session recovery, and door-release controls compatible with HomeKit Bridge.

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

This version provides account and door controls. Camera, two-way audio, and ring-event work is being developed separately; those features are not advertised as operational in 0.1.1.

Development checks (run each suite separately):

```sh
python -m unittest discover -s tests -p test_client.py
python -m unittest discover -s tests -p test_web_auth.py
python -m unittest discover -s tests -p test_runtime.py
python -m unittest discover -s tests -p test_homeassistant.py
ruff check custom_components --select F
```
