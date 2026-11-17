"""Tests for Home Assistant app option handling and persistent device synchronization."""

import json
import os
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "homeassistant" / "govee_lan_bridge"))
import run_app


class AppOptionsTests(unittest.TestCase):
    def test_options_are_loaded_from_json(self):
        with tempfile.TemporaryDirectory() as data_dir:
            path = Path(data_dir, "options.json")
            path.write_text(json.dumps({"MQTT_HOST": "broker"}), encoding="utf-8")
            self.assertEqual(run_app.load_options(path), {"MQTT_HOST": "broker"})

    def test_defaults_are_forwarded_and_optional_credentials_are_unset(self):
        env = run_app.options_to_env({}, {"MQTT_PASS": "stale", "MQTT_PASS_FILE": "stale-file"})
        self.assertEqual(
            {key: env[key] for key in run_app.DEFAULTS if key in env},
            {
                "MQTT_HOST": "127.0.0.1",
                "MQTT_PORT": "1883",
                "GOVEE2MQTT_URL": "http://127.0.0.1:8056",
                "CONFIG_PATH": "/config/devices.toml",
                "DISCOVERY_INTERVAL": "600",
                "DISCOVERY_PREFIX": "homeassistant",
                "BATCH_WINDOW": "0.15",
                "MAX_FRAMES_PER_PACKET": "12",
            },
        )
        self.assertNotIn("MQTT_PASS", env)
        self.assertNotIn("MQTT_PASS_FILE", env)

    def test_all_options_and_credentials_are_forwarded_without_logging(self):
        options = {
            "MQTT_HOST": "broker",
            "MQTT_PORT": 1884,
            "MQTT_USER": "bridge-user",
            "MQTT_PASS": "not-for-logs",
            "GOVEE2MQTT_URL": "http://devices:8056",
            "CONFIG_PATH": "/config/custom/devices.toml",
            "DISCOVERY_INTERVAL": 120,
            "DISCOVERY_PREFIX": "ha",
            "BATCH_WINDOW": 0.25,
            "MAX_FRAMES_PER_PACKET": 8,
        }
        with tempfile.TemporaryDirectory() as config_root:
            options["CONFIG_PATH"] = os.path.join(config_root, "devices.toml")
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                env = run_app.options_to_env(options, {}, (config_root,))
        for key, value in options.items():
            self.assertEqual(env[key], str(value))
        self.assertNotIn("not-for-logs", output.getvalue())

        env = run_app.options_to_env({"MQTT_PASS_FILE": "/config/mqtt_pass"}, {})
        self.assertEqual(env["MQTT_PASS_FILE"], "/config/mqtt_pass")
        self.assertNotIn("MQTT_PASS", env)

    def test_password_sources_are_mutually_exclusive(self):
        with self.assertRaises(run_app.AppConfigError) as error:
            run_app.options_to_env({"MQTT_PASS": "secret-value", "MQTT_PASS_FILE": "/config/pass"}, {})
        self.assertIn("Set only one of MQTT_PASS and MQTT_PASS_FILE", str(error.exception))
        self.assertNotIn("secret-value", str(error.exception))

    def test_invalid_values_and_nonpersistent_config_path_are_rejected(self):
        with self.assertRaisesRegex(run_app.AppConfigError, "MQTT_PORT"):
            run_app.options_to_env({"MQTT_PORT": 0}, {})
        with self.assertRaisesRegex(run_app.AppConfigError, "persistent /config directory"):
            run_app.options_to_env({"CONFIG_PATH": "/tmp/devices.toml"}, {})


class DeviceSyncTests(unittest.TestCase):
    def test_manual_entries_merge_additively_and_preserve_existing_content(self):
        with tempfile.TemporaryDirectory() as config_root:
            path = Path(config_root, "devices.toml")
            path.write_text(
                '# keep this comment\n[[device]]\nid = "AUTO-1"\nsku = "H6088"\nip = "192.0.2.1"\n'
                'key = "auto"\nname = "Old name" # keep this inline comment\nenabled = true\n# keep this too\n',
                encoding="utf-8",
            )
            edited = [{"id": "AUTO-1", "sku": "H6088", "name": "Renamed", "enabled": False}]
            manual = [{"id": "MANUAL-1", "sku": "H60B0", "ip": "192.0.2.2", "name": "Study"}]

            self.assertTrue(run_app.sync_devices(path, edited + manual, (config_root,)))
            self.assertFalse(run_app.sync_devices(path, edited + manual, (config_root,)))
            contents = path.read_text(encoding="utf-8")
            parsed = tomllib.loads(contents)["device"]
            self.assertEqual(len(parsed), 2)
            self.assertEqual(parsed[0]["ip"], "192.0.2.1")
            self.assertEqual(parsed[0]["key"], "auto")
            self.assertEqual(parsed[0]["name"], "Renamed")
            self.assertFalse(parsed[0]["enabled"])
            self.assertTrue(parsed[1]["enabled"])
            self.assertEqual(parsed[1]["name"], "Study")
            self.assertIn("# keep this comment", contents)
            self.assertIn("# keep this inline comment", contents)
            self.assertIn("# keep this too", contents)

    def test_invalid_toml_is_reported_without_changing_the_previous_file(self):
        with tempfile.TemporaryDirectory() as config_root:
            path = Path(config_root, "devices.toml")
            original = '[[device]\nid = "broken"\n'
            path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(run_app.AppConfigError, "invalid TOML"):
                run_app.sync_devices(path, [{"id": "MANUAL-1", "sku": "H60B0"}], (config_root,))
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["devices.toml"])

    def test_empty_configuration_does_not_create_or_clear_a_file(self):
        with tempfile.TemporaryDirectory() as config_root:
            path = Path(config_root, "devices.toml")
            self.assertFalse(run_app.sync_devices(path, [], (config_root,)))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
