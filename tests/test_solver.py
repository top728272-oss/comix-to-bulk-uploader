"""Tests for solver.py - the automatic Cloudflare clearance helper.

No browser and no patchright needed: the pure helpers and the parent-side hook
are exercised directly, and the browser path is only reached behind
``mint_clearance`` (which lazily imports patchright).

Run from the project root:  .venv\\Scripts\\python.exe -m unittest discover tests
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

# Allow running this file directly (`python tests/test_solver.py`) as well as
# through unittest discovery: the engine lives in the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core
import solver


def _imported_modules(src: str) -> set:
    """Top-level module names imported anywhere in the source."""
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _top_level_imports(src: str) -> set:
    """Module names imported at the top level only (not inside functions)."""
    names = set()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class FakeContext:
    """Minimal Playwright-context stand-in for the solver hook."""

    def __init__(self) -> None:
        self.calls: list = []
        self.pages: list = []

    def add_cookies(self, cookies) -> None:
        self.calls.append(cookies)


class SolverHelperTests(unittest.TestCase):
    def test_extract_clearance_keeps_only_clearance_cookies(self):
        cookies = [
            {"name": "cf_clearance", "value": "a"},
            {"name": "session", "value": "b"},
            {"name": "__cf_bm", "value": "c"},
            {"name": "cf_chl_2", "value": "d"},
        ]
        names = [c["name"] for c in solver._extract_clearance(cookies)]
        self.assertEqual(names, ["cf_clearance", "__cf_bm", "cf_chl_2"])

    def test_extract_clearance_strips_fields_playwright_rejects(self):
        # patchright's cookies() returns extra keys; Playwright's add_cookies
        # must not receive them.
        cookies = [
            {
                "name": "cf_clearance",
                "value": "a",
                "domain": ".comix.to",
                "path": "/",
                "sameSite": "None",
                "partitionKey": "https://comix.to",
                "_crHasCrossSiteAncestor": False,
            }
        ]
        [record] = solver._extract_clearance(cookies)
        self.assertNotIn("partitionKey", record)
        self.assertNotIn("_crHasCrossSiteAncestor", record)
        self.assertEqual(record["domain"], ".comix.to")
        self.assertEqual(record["sameSite"], "None")

    def test_clearance_value_returns_cf_clearance(self):
        self.assertEqual(
            solver._clearance_value([{"name": "cf_clearance", "value": "v"}]), "v"
        )
        self.assertIsNone(solver._clearance_value([{"name": "x", "value": "v"}]))

    def test_parse_result_valid_json(self):
        out = '{"ok": true, "cookies": [], "user_agent": "UA", "error": ""}'
        self.assertEqual(solver.parse_result(out)["ok"], True)

    def test_parse_result_ignores_log_noise(self):
        out = "[i] launching\n[i] waiting\n" + '{"ok": true, "cookies": []}'
        result = solver.parse_result(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["cookies"], [])
        self.assertEqual(result["user_agent"], "")

    def test_parse_result_garbage_is_failure(self):
        result = solver.parse_result("totally not json")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_should_escalate_threshold(self):
        self.assertFalse(solver._should_escalate(3, 15))
        self.assertTrue(solver._should_escalate(15, 15))
        self.assertTrue(solver._should_escalate(30, 15))

    def test_solver_source_imports_patchright_lazily(self):
        src = Path(solver.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "patchright",
            _top_level_imports(src),
            "patchright must not be a top-level import in solver.py",
        )
        # ...but it must be imported somewhere (inside mint_clearance).
        self.assertIn("patchright", _imported_modules(src))


class SolverHookTests(unittest.TestCase):
    def setUp(self) -> None:
        solver._last_result.update(ts=0.0, ok=False, cookies=[])
        self._saved = solver.solve_via_subprocess

    def tearDown(self) -> None:
        solver.solve_via_subprocess = self._saved
        solver._last_result.update(ts=0.0, ok=False, cookies=[])

    def _hook(self, **cf):
        cfg = {"cloudflare": {"auto_solve": True, **cf}}
        return solver.make_cf_solver_hook(cfg, log=lambda m: None)

    def test_hook_injects_cookies_into_context(self):
        solver.solve_via_subprocess = lambda *a, **kw: {
            "ok": True,
            "cookies": [{"name": "cf_clearance", "value": "v"}],
            "user_agent": "",
            "error": "",
        }
        ctx = FakeContext()

        self.assertTrue(self._hook()(ctx, lambda m: None))
        self.assertEqual(len(ctx.calls), 1)
        self.assertEqual(ctx.calls[0][0]["name"], "cf_clearance")

    def test_hook_debounces_repeated_solves(self):
        spawns: list = []

        def fake(*a, **kw):
            spawns.append(1)
            return {
                "ok": True,
                "cookies": [{"name": "cf_clearance", "value": "v"}],
                "user_agent": "",
                "error": "",
            }

        solver.solve_via_subprocess = fake
        hook = self._hook()
        ctx = FakeContext()

        self.assertTrue(hook(ctx, lambda m: None))
        self.assertTrue(hook(ctx, lambda m: None))
        self.assertEqual(len(spawns), 1, "second call must reuse the fresh solve")

    def test_hook_returns_false_when_solver_fails(self):
        solver.solve_via_subprocess = lambda *a, **kw: {
            "ok": False,
            "cookies": [],
            "user_agent": "",
            "error": "nope",
        }
        self.assertFalse(self._hook()(FakeContext(), lambda m: None))

    def test_hook_disabled_never_spawns(self):
        def boom(*a, **kw):
            raise AssertionError("solver must not run when auto_solve is off")

        solver.solve_via_subprocess = boom
        hook = solver.make_cf_solver_hook(
            {"cloudflare": {"auto_solve": False}}, log=lambda m: None
        )
        self.assertFalse(hook(FakeContext(), lambda m: None))


class CoreImportGuardTests(unittest.TestCase):
    def test_core_never_imports_patchright_or_playwright(self):
        mods = _imported_modules(Path(core.__file__).read_text(encoding="utf-8"))
        self.assertNotIn("patchright", mods)
        self.assertNotIn("playwright", mods)


if __name__ == "__main__":
    unittest.main()
