import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config_file


class ConfigTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = str(Path(directory) / "config.json")

    def write_config(self, cameras, **sections):
        Path(self.path).write_text(json.dumps({"cameras": cameras, **sections}), encoding="utf-8")

    def test_camera_is_enabled_and_keep_open_is_off_by_default(self):
        self.write_config([{"name": "front", "password": "test"}])
        self.assertEqual(config_file.list_cameras(self.path), ["front"])
        self.assertFalse(config_file.load_config(self.path, "front")["keep_stream_open"])

    def test_disabled_camera_is_excluded_from_workers_and_gateways(self):
        self.write_config([
            {"name": "front", "password": "test", "disabled": False},
            {"name": "garage", "disabled": True}])
        self.assertEqual(config_file.list_cameras(self.path), ["front"])
        self.assertEqual(config_file.rtsp_gateway_config(self.path)["camera_ports"], {"front": 8555})
        self.assertEqual(config_file.rest_gateway_config(self.path)["camera_ports"], {"front": 8081})
        with self.assertRaisesRegex(config_file.ConfigError, "unknown camera"):
            config_file.load_config(self.path, "garage")

    def test_disabling_camera_does_not_renumber_other_worker_ports(self):
        self.write_config([
            {"name": "front", "disabled": True},
            {"name": "garage", "password": "test"}])
        self.assertEqual(config_file.rtsp_gateway_config(self.path)["camera_ports"], {"garage": 8556})
        self.assertEqual(config_file.rest_gateway_config(self.path)["camera_ports"], {"garage": 8082})

    def test_per_camera_keep_open_overrides_shared_value_in_both_directions(self):
        for shared in (False, True):
            with self.subTest(shared=shared):
                self.write_config([
                    {"name": "front", "password": "test", "target_ip": "192.0.2.1"},
                    {"name": "garage", "password": "test", "target_ip": "192.0.2.2",
                     "keep_stream_open": not shared}], rtsp={"keep_stream_open": shared})
                self.assertEqual(config_file.load_config(self.path, "front")["keep_stream_open"], shared)
                self.assertEqual(config_file.load_config(self.path, "garage")["keep_stream_open"], not shared)

    def test_disabled_camera_does_not_reserve_identity_or_onvif_port(self):
        entry = {"password": "test", "did": "TEST-000001-ABCDE", "onvif_port": 8000}
        self.write_config([dict(entry, name="front"), dict(entry, name="garage", disabled=True)])
        self.assertEqual(config_file.list_cameras(self.path), ["front"])

    def test_all_disabled_cameras_are_rejected(self):
        self.write_config([{"name": "front", "disabled": True}])
        with self.assertRaisesRegex(config_file.ConfigError, "all cameras are disabled"):
            config_file.list_cameras(self.path)

    def test_disabled_camera_still_requires_unique_valid_name_and_known_keys(self):
        for disabled in ({"name": "front", "disabled": True},
                         {"name": "invalid name", "disabled": True},
                         {"name": "garage", "disabled": True, "typo": True}):
            with self.subTest(disabled=disabled):
                self.write_config([{"name": "front", "password": "test"}, disabled])
                with self.assertRaises(config_file.ConfigError):
                    config_file.list_cameras(self.path)

    def test_enabled_cameras_still_require_passwords_and_distinct_identities(self):
        for cameras in ([{"name": "front"}],
                        [{"name": "front", "password": "test"},
                         {"name": "garage", "password": "test"}],
                        [{"name": "front", "password": "test", "target_ip": "192.0.2.1"},
                         {"name": "garage", "password": "test", "target_ip": "192.0.2.1"}]):
            with self.subTest(cameras=cameras):
                self.write_config(cameras)
                with self.assertRaises(config_file.ConfigError):
                    config_file.list_cameras(self.path)


if __name__ == "__main__":
    unittest.main()