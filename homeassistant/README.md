# Home Assistant integration

This integration exposes the privacy profiles from Frigate Déjà Vu's example
configuration as `select.frigate_privacy`. Home Assistant sends profile changes
to the Dejavu REST API and polls its status every 20 seconds, so changes made
through the API or CLI are also reflected in the selector.

## Example configuration

These files are designed to accompany the repository's
[`config.example.yaml`](../config.example.yaml) and
[`compose.example.yaml`](../compose.example.yaml):

- `config.example.yaml` defines the `perimeter`, `indoor`, and `all` profiles.
- `compose.example.yaml` publishes the Dejavu API on port `8898` and leaves API
  authentication disabled by default.
- The Home Assistant examples connect to
  `http://frigate-dejavu:8898`. The hostname must resolve to the Docker
  host from Home Assistant; change it in `rest.yaml` and `rest_command.yaml` if
  the host uses a different name or address.

The example profiles map directly to the selector:

| Selector option | API profile | Dejavu configuration |
| --- | --- | --- |
| `Perimeter` | `perimeter` | `profiles.perimeter` |
| `Indoor` | `indoor` | `profiles.indoor` |
| `All Cameras` | `all` | `profiles.all` |
| `Off` | — | Global off action |

## Installation

The files in this directory are configuration fragments. Append their contents
to the matching files in the Home Assistant configuration directory:

- `rest_command.yaml` defines the three profile actions and the off action.
- `rest.yaml` creates `sensor.frigate_privacy_state` from
  `/api/dejavu/status`.
- `template.yaml` creates `select.frigate_privacy`.

Ensure `configuration.yaml` includes those files:

```yaml
rest: !include rest.yaml
rest_command: !include rest_command.yaml
template: !include template.yaml
```

Check the Home Assistant configuration after adding the fragments, then reload
the affected YAML integrations or restart Home Assistant.

## Custom profiles

When renaming or replacing the example profiles, update all three matching
parts together:

1. The profile under `profiles:` in the Dejavu `config.yaml`.
2. The JSON `profile` value and command name in `rest_command.yaml`.
3. The option, state mapping, and action branch in `template.yaml`.

## Behavior

Only one privacy profile can be active at a time. Select `Off` before changing
from one active profile to another. The selector updates immediately and then
reconciles with the appliance state on the next status poll.
