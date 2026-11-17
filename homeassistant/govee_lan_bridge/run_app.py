#!/usr/bin/env python3
"""Translate Home Assistant app options to the existing bridge environment."""

import json
import math
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path


DEFAULTS = {
    "MQTT_HOST": "127.0.0.1",
    "MQTT_PORT": 1883,
    "MQTT_USER": "",
    "MQTT_PASS": "",
    "MQTT_PASS_FILE": "",
    "GOVEE2MQTT_URL": "http://127.0.0.1:8056",
    "CONFIG_PATH": "/config/devices.toml",
    "DISCOVERY_INTERVAL": 600,
    "DISCOVERY_PREFIX": "homeassistant",
    "BATCH_WINDOW": 0.15,
    "MAX_FRAMES_PER_PACKET": 12,
}
DEVICE_FIELDS = ("id", "sku", "ip", "key", "name", "room", "enabled", "family", "segments")
STRING_FIELDS = {"id", "sku", "ip", "key", "name", "room", "family"}
CONFIG_HEADER = "# govee-lan-bridge devices\n"
PERSISTENT_ROOTS = ("/config",)


class AppConfigError(ValueError):
    """A safe-to-display app configuration error."""


def load_options(path="/data/options.json"):
    try:
        with open(path, encoding="utf-8") as options_file:
            options = json.load(options_file)
    except (OSError, json.JSONDecodeError) as err:
        raise AppConfigError(f"Unable to read Home Assistant app options from {path}: {err}") from err
    if not isinstance(options, dict):
        raise AppConfigError("Home Assistant app options must be a JSON object.")
    return options


def options_to_env(options, base_env=None, persistent_roots=PERSISTENT_ROOTS):
    """Validate app options and return the complete bridge environment."""
    env = dict(os.environ if base_env is None else base_env)
    values = {key: options.get(key, default) for key, default in DEFAULTS.items()}
    password = values["MQTT_PASS"]
    password_file = values["MQTT_PASS_FILE"]
    if password and password_file:
        raise AppConfigError("Set only one of MQTT_PASS and MQTT_PASS_FILE, not both.")

    for key in ("MQTT_HOST", "GOVEE2MQTT_URL", "CONFIG_PATH", "DISCOVERY_PREFIX"):
        if not isinstance(values[key], str) or not values[key].strip():
            raise AppConfigError(f"{key} must be a non-empty string.")
    if isinstance(values["MQTT_PORT"], bool) or not isinstance(values["MQTT_PORT"], int):
        raise AppConfigError("MQTT_PORT must be an integer from 1 to 65535.")
    if not 1 <= values["MQTT_PORT"] <= 65535:
        raise AppConfigError("MQTT_PORT must be an integer from 1 to 65535.")
    if isinstance(values["DISCOVERY_INTERVAL"], bool) or not isinstance(values["DISCOVERY_INTERVAL"], int) or values["DISCOVERY_INTERVAL"] < 1:
        raise AppConfigError("DISCOVERY_INTERVAL must be a positive integer.")
    if isinstance(values["MAX_FRAMES_PER_PACKET"], bool) or not isinstance(values["MAX_FRAMES_PER_PACKET"], int) or values["MAX_FRAMES_PER_PACKET"] < 1:
        raise AppConfigError("MAX_FRAMES_PER_PACKET must be a positive integer.")
    if (
        isinstance(values["BATCH_WINDOW"], bool)
        or not isinstance(values["BATCH_WINDOW"], (int, float))
        or not math.isfinite(values["BATCH_WINDOW"])
        or values["BATCH_WINDOW"] < 0
    ):
        raise AppConfigError("BATCH_WINDOW must be a finite non-negative number.")

    validate_persistent_path(values["CONFIG_PATH"], persistent_roots)
    for key in ("MQTT_USER", "MQTT_PASS", "MQTT_PASS_FILE"):
        if not isinstance(values[key], str):
            raise AppConfigError(f"{key} must be text.")

    for key in DEFAULTS:
        env.pop(key, None)
        if values[key] != "":
            env[key] = str(values[key])
    return env


def validate_persistent_path(path, roots=PERSISTENT_ROOTS):
    if not os.path.isabs(path):
        raise AppConfigError("CONFIG_PATH must be an absolute path under /config so the file persists.")
    normalized = os.path.realpath(path)
    if not any(os.path.commonpath((normalized, root)) == root and normalized != root for root in roots):
        raise AppConfigError("CONFIG_PATH must be a file path under the persistent /config directory.")
    return normalized


def validate_devices(devices):
    if not isinstance(devices, list):
        raise AppConfigError("devices must be a list of device entries.")
    validated = []
    for index, device in enumerate(devices, 1):
        if not isinstance(device, dict):
            raise AppConfigError(f"devices entry {index} must be an object.")
        if set(device) - set(DEVICE_FIELDS):
            raise AppConfigError(f"devices entry {index} contains an unsupported field.")
        for field in ("id", "sku"):
            if not isinstance(device.get(field), str) or not device[field].strip():
                raise AppConfigError(f"devices entry {index} requires a non-empty {field}.")
        for field in STRING_FIELDS:
            if field in device and not isinstance(device[field], str):
                raise AppConfigError(f"devices entry {index}: {field} must be text.")
        if device.get("family") not in (None, "", "segments", "sections"):
            raise AppConfigError(f"devices entry {index}: family must be segments or sections.")
        if "enabled" in device and not isinstance(device["enabled"], bool):
            raise AppConfigError(f"devices entry {index}: enabled must be true or false.")
        if "segments" in device and (
            isinstance(device["segments"], bool)
            or not isinstance(device["segments"], int)
            or not 1 <= device["segments"] <= 56
        ):
            raise AppConfigError(f"devices entry {index}: segments must be an integer from 1 to 56.")
        validated.append(dict(device))
    return validated


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _entry_block(device):
    lines = ["", "[[device]]"]
    for field in DEVICE_FIELDS:
        if field in device and device[field] not in (None, ""):
            lines.append(f"{field} = {_toml_value(device[field])}")
    if "enabled" not in device:
        lines.append("enabled = true")
    return "\n".join(lines) + "\n"


def _update_block(block, device):
    for field in DEVICE_FIELDS:
        value = device.get(field)
        if value in (None, ""):
            continue
        line = f"{field} = {_toml_value(value)}"
        pattern = re.compile(rf"(?m)^({re.escape(field)}\s*=).*?(\s+#.*)?$")
        if pattern.search(block):
            block = pattern.sub(lambda match: f"{match.group(1)} {_toml_value(value)}{match.group(2) or ''}", block, count=1)
        else:
            table = re.compile(r"(?m)^(\[\[device\]\][^\n]*(?:\n|$))")
            block = table.sub(lambda match: f"{match.group(1)}{line}\n", block, count=1)
    return block


def sync_devices(path, devices, persistent_roots=PERSISTENT_ROOTS):
    """Add or update GUI entries without dropping file entries, comments, or discoveries."""
    target = validate_persistent_path(path, persistent_roots)
    devices = validate_devices(devices)
    try:
        text = Path(target).read_text(encoding="utf-8")
    except FileNotFoundError:
        text = CONFIG_HEADER
    except (OSError, UnicodeDecodeError) as err:
        raise AppConfigError(f"Cannot read app device file {target}: {err}") from err

    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise AppConfigError(f"Cannot sync app devices: {target} contains invalid TOML; the file was left unchanged: {err}") from err
    entries = parsed.get("device", [])
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise AppConfigError(f"Cannot sync app devices: {target} must contain [[device]] entries; the file was left unchanged.")

    blocks = re.split(r"(?m)^(?=\[\[device\]\](?:[ \t]*(?:#.*)?)?$)", text)
    known_ids = {entry.get("id") for entry in entries if entry.get("id")}
    changed = False
    for device in devices:
        if device["id"] in known_ids:
            encoded_id = _toml_value(device["id"])
            for index, block in enumerate(blocks):
                if re.search(rf"(?m)^id\s*=\s*{re.escape(encoded_id)}\s*(?:#.*)?$", block):
                    updated = _update_block(block, device)
                    if updated != block:
                        blocks[index] = updated
                        changed = True
            continue
        blocks.append(_entry_block(device))
        known_ids.add(device["id"])
        changed = True

    if not changed:
        return False
    updated_text = "".join(blocks)
    try:
        tomllib.loads(updated_text)
    except tomllib.TOMLDecodeError as err:
        raise AppConfigError(f"Cannot sync app devices: generated TOML is invalid; {target} was left unchanged: {err}") from err

    tmp_path = None
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=os.path.dirname(target),
            prefix=f".{os.path.basename(target)}.", delete=False,
        ) as output:
            tmp_path = output.name
            output.write(updated_text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
    except OSError as err:
        raise AppConfigError(f"Cannot write app device file {target}: {err}") from err
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
    return True


def main():
    try:
        options = load_options()
        env = options_to_env(options)
        devices = options.get("devices", [])
        if sync_devices(env["CONFIG_PATH"], devices):
            print(f"Updated app-configured devices in {env['CONFIG_PATH']}.", flush=True)
    except AppConfigError as err:
        print(f"App configuration error: {err}", file=sys.stderr, flush=True)
        return 1
    os.execvpe("python", ["python", "-u", "/app/bridge.py"], env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
