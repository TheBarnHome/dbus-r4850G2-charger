#!/usr/bin/env python3
"""Static check that every D-Bus callback exists.

`python3 -m py_compile` cannot catch this: a callback registered as
``onchangecallback=self.foo`` and never defined compiles fine and crashes at
service start-up, on the target only. A blind edit removed two of them once.
"""

import ast
import sys
import unittest
from pathlib import Path

SERVICE_FILE = Path(__file__).resolve().parent.parent / "dbus-r4850-ac-charger.py"


class CallbackWiringTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(SERVICE_FILE.read_text())

    def _methods(self, class_name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
        self.fail(f"class {class_name} not found")

    def test_every_onchangecallback_exists(self):
        methods = self._methods("R4850Service")
        referenced = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "onchangecallback" and isinstance(kw.value, ast.Attribute):
                    referenced.add(kw.value.attr)
        self.assertTrue(referenced, "no onchangecallback found, has the API changed?")
        missing = referenced - methods
        self.assertEqual(missing, set(), f"callbacks referenced but not defined: {sorted(missing)}")

    def test_writable_paths_are_callbacks(self):
        """A writeable path without a callback silently swallows writes."""
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            if "writeable" in kwargs and isinstance(kwargs["writeable"], ast.Constant) and kwargs["writeable"].value:
                self.assertIn(
                    "onchangecallback", kwargs,
                    "a writeable path has no onchangecallback",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
