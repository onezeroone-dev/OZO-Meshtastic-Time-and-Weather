import configparser
from pathlib import Path
import unittest


class ServiceUnitTests(unittest.TestCase):
    def test_service_has_config_and_serial_access_groups(self):
        unit = configparser.ConfigParser(interpolation=None)
        path = Path(__file__).resolve().parent.parent / "systemd" / "ozo-meshtastic-tw.service"
        with path.open(encoding="utf-8") as file:
            unit.read_file(file)

        service = unit["Service"]
        self.assertEqual(service["User"], "ozo-meshtastic")
        self.assertEqual(service["Group"], "dialout")
        self.assertIn("ozo-meshtastic", service["SupplementaryGroups"].split())


if __name__ == "__main__":
    unittest.main()
