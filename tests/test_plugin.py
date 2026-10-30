"""Plugin registration, menu and toolbar wiring. Requires QGIS."""

import importlib.util
import unittest

if importlib.util.find_spec("qgis") is None:
    raise unittest.SkipTest("A QGIS Python environment is required.")

from unittest.mock import Mock, patch

from qgis.core import QgsApplication

from drainage_channel_catchment_tools import classFactory

from .utilities import start_qgis


class PluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        start_qgis()

    def setUp(self):
        self.iface = Mock()
        self.iface.mainWindow.return_value = None
        self.plugin = classFactory(self.iface)
        self.addCleanup(self.plugin.unload)

    def test_provider_contains_both_tools(self):
        self.plugin.initProcessing()
        provider = QgsApplication.processingRegistry().providerById("drainagecatchment")
        self.assertEqual(
            [a.id() for a in provider.algorithms()],
            ["drainagecatchment:delineate", "drainagecatchment:longestflowpath"],
        )
        self.assertTrue(all(a.group() == "" for a in provider.algorithms()))

    def test_icons_render(self):
        self.plugin.initGui()
        icons = [self.plugin.provider.icon()] + [
            a.icon() for a in self.plugin.provider.algorithms()
        ]
        icons += [action.icon() for action, _ in self.plugin.actions]
        self.assertEqual(len(icons), 7)
        for icon in icons:
            self.assertFalse(icon.pixmap(24, 24).isNull())

    def test_actions_are_added_once_and_removed_on_unload(self):
        self.plugin.initGui()
        self.plugin.initGui()
        self.assertEqual(len(self.plugin.actions), 4)
        names = [action.objectName() for action, _ in self.plugin.actions]
        self.assertEqual(len(set(names)), 4)
        self.assertTrue(all(name.startswith("DrainageChannelCatchmentTools") for name in names))
        self.assertEqual(self.iface.addPluginToRasterMenu.call_count, 4)
        self.assertEqual(self.iface.addToolBarIcon.call_count, 2)
        self.plugin.unload()
        self.assertEqual(self.iface.removePluginRasterMenu.call_count, 4)
        self.assertEqual(self.iface.removeToolBarIcon.call_count, 2)
        self.assertIsNone(QgsApplication.processingRegistry().providerById("drainagecatchment"))

    def test_toolbar_actions_open_the_tool_dialogs(self):
        self.plugin.initGui()
        with patch("processing.execAlgorithmDialog") as dialog:
            for action, toolbar in self.plugin.actions:
                if toolbar:
                    action.trigger()
        self.assertEqual(
            [c.args[0] for c in dialog.call_args_list],
            ["drainagecatchment:delineate", "drainagecatchment:longestflowpath"],
        )

    def test_style_actions_reject_other_layers(self):
        self.plugin.initGui()
        self.iface.activeLayer.return_value = None
        self.plugin.update_catchment_layer()
        self.plugin.update_flow_path_layer()
        self.assertEqual(self.iface.messageBar.return_value.pushWarning.call_count, 2)

    def test_duplicate_installation_is_rejected(self):
        self.plugin.initProcessing()
        other = classFactory(None)
        with self.assertRaises(RuntimeError):
            other.initProcessing()
        other.unload()
        self.assertIsNotNone(QgsApplication.processingRegistry().providerById("drainagecatchment"))
