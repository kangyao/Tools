from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from function_spacing import collapse_guard_returns, separate_functions


class FunctionSpacingTests(unittest.TestCase):
    def test_named_local_methods_and_assigned_functions(self):
        source = ("function first()\nend\nlocal function second()\nend\n"
                  "function Panel:third()\nend\nlocal fourth = function()\nend\nPanel.fifth = function()\nend\n")
        output, missing = separate_functions(source)
        self.assertEqual(len(missing), 4)
        self.assertEqual(output.count("end\n\n"), 4)
        self.assertEqual(separate_functions(output), (output, []))

    def test_comments_and_annotations_stay_with_next_function(self):
        source = "function first()\nend\n-- second function\n---@param value integer\nfunction second(value)\nend\n"
        output, missing = separate_functions(source)
        self.assertIn("end\n\n-- second function\n---@param value integer\nfunction", output)
        self.assertEqual(missing, [5])
        source = "function first()\nend --[=[trailing\ncomment]=]\nfunction second()\nend\n"
        output, missing = separate_functions(source)
        self.assertIn("end --[=[trailing\ncomment]=]\n\nfunction", output)
        self.assertEqual(len(missing), 1)

    def test_nested_blocks_functions_and_single_line_definitions(self):
        source = ("function outer()\n    local function first()\n        if true then\n"
                  "            for i = 1, 2 do\n                repeat\n                until true\n"
                  "            end\n        end\n    end\n    local function second() return 2 end\nend\n"
                  "function last() return 3 end\n")
        output, missing = separate_functions(source)
        self.assertEqual(len(missing), 2)
        self.assertIn("    end\n\n    local function second", output)
        self.assertIn("end\n\nfunction last", output)

    def test_literals_and_non_function_blocks_are_unchanged(self):
        for source in (
            'local s = [=[function first()\nend\nfunction second()\nend]=]\n',
            '--[=[function first()\nend\nfunction second()\nend]=]\n',
            'local s = "function first()\\nend\\nfunction second()"\n',
            'local s = `function first()\nend\nfunction second()\nend`\n',
            "if true then\nend\nfunction next()\nend\n",
            "local t = {\n    a = function()\n    end,\n    b = function()\n    end,\n}\n",
            "function first()\nend\nprint(1)\nfunction second()\nend\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(separate_functions(source), (source, []))

    def test_ignore_ranges_and_ignored_compound_statements(self):
        cases = (
            "-- stylua: ignore start\nfunction first()\nend\nfunction second()\nend\n-- stylua: ignore end\n",
            "function first()\nend\n-- stylua: ignore\nfunction second()\nend\n",
            "-- stylua: ignore\nfunction outer()\n    function first()\n    end\n    function second()\n    end\nend\n",
            "-- stylua: ignore\nif true then\n    function first()\n    end\n    function second()\n    end\nend\n",
            "-- stylua: ignore\nlocal t = {\n    value = function()\n        function first()\n        end\n        function second()\n        end\n    end,\n}\n",
        )
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(separate_functions(source), (source, []))
        source = cases[0] + "function third()\nend\nfunction fourth()\nend\n"
        output, missing = separate_functions(source)
        self.assertEqual(len(missing), 1)
        self.assertIn("end\n\nfunction fourth", output)

    def test_existing_spacing_and_mixed_line_endings(self):
        source = "function first()\r\nend\r\nfunction second()\nend"
        output, missing = separate_functions(source)
        self.assertEqual(output, "function first()\r\nend\r\n\r\nfunction second()\nend")
        self.assertEqual(len(missing), 1)
        source = "function first()\nend\n\n\nfunction second()\nend\n"
        self.assertEqual(separate_functions(source), (source, []))

    def test_luau_if_expression_does_not_consume_function_end(self):
        source = "function first(): number\n    return if true then 1 else 2\nend\nfunction second()\nend\n"
        output, missing = separate_functions(source, "Luau")
        self.assertEqual(len(missing), 1)
        self.assertIn("end\n\nfunction second", output)


class GuardReturnTests(unittest.TestCase):
    def test_only_short_bare_returns_are_collapsed(self):
        source = "if not value then\n    return\nend\n"
        self.assertEqual(collapse_guard_returns(source, 80, 4), "if not value then return end\n")
        source = 'if name == "then end" then\n    return\nend\n'
        self.assertEqual(collapse_guard_returns(source, 80, 4), 'if name == "then end" then return end\n')
        for source in (
            'if value then\n    return "important"\nend\n',
            'if value then\n    return [=[important]=]\nend\n',
            'if value then\n    return `important`\nend\n',
            'if value then\n    return false\nend\n',
            'if value then\n    return compute()\nend\n',
            'if value then\n    print("test")\nend\n',
            'if value then\n    return\nelse\n    work()\nend\n',
            'local function f()\n    return\nend\n',
        ):
            with self.subTest(source=source):
                self.assertEqual(collapse_guard_returns(source, 80, 4), source)

    def test_long_conditions_and_indentation_count_towards_width(self):
        source = "\tif condition then\n\t\treturn\n\tend\n"
        expected = "\tif condition then return end\n"
        width = len(expected.rstrip("\n").expandtabs(4))
        self.assertEqual(collapse_guard_returns(source, width, 4), expected)
        self.assertEqual(collapse_guard_returns(source, width - 1, 4), source)
        source = 'if name == "中文中文中文" then\n    return\nend\n'
        self.assertEqual(collapse_guard_returns(source, 36, 4), source)

    def test_comments_literals_and_ignore_regions_are_untouched(self):
        sources = (
            'local text = [=[if value then\n    return\nend]=]\n',
            '--[=[if value then\n    return\nend]=]\n',
            'if value then -- explanation\n    return\nend\n',
            'if value then\n    -- explanation\n    return\nend\n',
            'if value then\n    return\nend -- explanation\n',
            '-- stylua: ignore\nif value then\n    return\nend\n',
            '-- stylua: ignore\nfunction f()\n    if value then\n        return\n    end\nend\n',
            '-- stylua: ignore start\nif value then\n    return\nend\n-- stylua: ignore end\n',
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(collapse_guard_returns(source, 80, 4), source)


if __name__ == "__main__":
    unittest.main()
