"""Package checks from the PyQGIS cookbook's plugin and release rules. No QGIS needed."""

import configparser
import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "drainage_channel_catchment_tools"
REQUIRED = (
    "name",
    "qgisMinimumVersion",
    "description",
    "about",
    "version",
    "author",
    "email",
    "repository",
)


class MetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(PLUGIN / "metadata.txt", encoding="utf-8")
        cls.metadata = parser["general"]

    def test_required_fields_are_present(self):
        for key in REQUIRED:
            self.assertTrue(self.metadata.get(key, "").strip(), key)

    def test_values_follow_the_rules(self):
        self.assertRegex(self.metadata["version"], r"^\d+\.\d+\.\d+$")
        self.assertIn(self.metadata["category"], ("Raster", "Vector", "Database", "Mesh", "Web"))
        self.assertEqual(self.metadata["hasProcessingProvider"], "yes")
        for key in ("description", "about", "changelog"):
            self.assertIsNone(re.search(r"<[a-zA-Z/]", self.metadata[key]), f"HTML in {key}")

    def test_icon_is_a_png_inside_the_plugin(self):
        icon = PLUGIN / self.metadata["icon"]
        self.assertEqual(icon.suffix, ".png")
        self.assertEqual(icon.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_package_layout(self):
        self.assertRegex(PLUGIN.name, r"^[A-Za-z_-][A-Za-z0-9_-]*$")
        self.assertNotIn("plugin", PLUGIN.name.lower())
        for name in (
            "__init__.py",
            "metadata.txt",
            "LICENSE",
            "mainPlugin.py",
            "processing_provider/__init__.py",
            "processing_provider/provider.py",
        ):
            self.assertTrue((PLUGIN / name).is_file(), name)
        self.assertEqual((PLUGIN / "processing_provider" / "__init__.py").read_text(), "")
