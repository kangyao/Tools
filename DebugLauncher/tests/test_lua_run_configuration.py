from __future__ import annotations

import json
from pathlib import Path
import unittest
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUN_CONFIGURATION = PROJECT_ROOT / ".run" / "Lua.run.xml"


class LuaRunConfigurationTests(unittest.TestCase):
    def test_pure_lua_attach_uses_lua_root(self) -> None:
        root = ElementTree.parse(RUN_CONFIGURATION).getroot()
        configuration = root.find("configuration")
        self.assertIsNotNone(configuration)
        assert configuration is not None

        options = {
            option.attrib["name"]: option.attrib.get("value", "")
            for option in configuration.findall("option")
        }
        attach = json.loads(options["attachConfiguration"])

        self.assertEqual(configuration.attrib["name"], "Lua")
        self.assertEqual(options["debugMode"], "ATTACH")
        self.assertEqual(options["attachPort"], "4711")
        self.assertEqual(attach["type"], "minigameplay-lua")
        self.assertEqual(attach["languageMode"], "lua")
        self.assertEqual(attach["runtimeProtocol"], "cpp")
        self.assertEqual(attach["runtimeId"], "minigameplay-main-lua")
        self.assertEqual(Path(attach["luaRoot"]), PROJECT_ROOT / "Scripts")

        mappings = configuration.findall(
            "./option[@name='serverMappings']/list/ServerMappingSettings"
        )
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0].attrib.get("language"), "Lua")
        self.assertEqual(mappings[0].attrib.get("languageId"), "lua")


if __name__ == "__main__":
    unittest.main()
