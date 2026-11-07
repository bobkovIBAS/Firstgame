import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/init_project.py"
SPEC = importlib.util.spec_from_file_location("roblox_project_init", SCRIPT)
INIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INIT)


class ProjectInitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="roblox-init-test-")
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_initialize_and_repeat_preserve_bytes_and_modification_times(self):
        report = INIT.initialize(self.root)
        self.assertEqual(len(report["changed"]), 5)
        first = self.snapshot()
        second = INIT.initialize(self.root)
        self.assertEqual(second["changed"], [])
        self.assertEqual(first, self.snapshot())
        self.assertTrue(INIT.initialize(self.root, check=True)["ready"])

    def test_existing_user_content_is_preserved_and_profile_reused(self):
        profile = self.root / "docs/style/README.md"
        profile.parent.mkdir(parents=True)
        profile.write_text("# Existing art direction\nCustom palette\n")
        (self.root / "AGENTS.md").write_bytes(b"# Team rules\r\nKeep this exactly.")
        (self.root / "README.md").write_bytes(b"# Existing project\r\nCustom readme")
        before = self.snapshot()
        report = INIT.initialize(self.root)
        self.assertEqual(report["design_profile"], "docs/style/README.md")
        for path in ["AGENTS.md", "README.md"]:
            self.assertTrue((self.root / path).read_bytes().startswith(before[path][0]))
        self.assertEqual(profile.read_bytes(), before["docs/style/README.md"][0])
        self.assertFalse((self.root / "docs/design").exists())
        second = self.snapshot()
        INIT.initialize(self.root)
        self.assertEqual(second, self.snapshot())

    def test_check_only_does_not_create_even_directories(self):
        report = INIT.initialize(self.root, check=True)
        self.assertFalse(report["ready"])
        self.assertEqual(len(report["pending"]), 5)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_custom_profile_path_is_remembered(self):
        INIT.initialize(self.root, design_profile="visual guide/profile.md")
        report = INIT.initialize(self.root)
        self.assertEqual(report["design_profile"], "visual guide/profile.md")
        self.assertEqual(report["changed"], [])
        self.assertIn("visual%20guide/profile.md", (self.root / "README.md").read_text())

    def test_active_agents_override_is_used(self):
        (self.root / "AGENTS.md").write_text("Original instructions")
        (self.root / "AGENTS.override.md").write_text("Override instructions")
        INIT.initialize(self.root)
        self.assertEqual((self.root / "AGENTS.md").read_text(), "Original instructions")
        self.assertIn(INIT.AGENTS_MARKER, (self.root / "AGENTS.override.md").read_text())

    def test_known_directory_conflict_is_detected_before_any_writes(self):
        (self.root / "docs").write_text("This is a file")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            INIT.initialize(self.root)
        self.assertEqual(before, self.snapshot())

    def test_invalid_config_is_not_replaced(self):
        (self.root / INIT.CONFIG).write_text("not valid json")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            INIT.initialize(self.root)
        self.assertEqual(before, self.snapshot())

    def test_profile_cannot_escape_project(self):
        with self.assertRaises(ValueError):
            INIT.initialize(self.root, design_profile="../outside.md")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_docs_symlink_cannot_write_into_another_project(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "docs").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                INIT.initialize(self.root)
            self.assertEqual(list(Path(outside).iterdir()), [])
            self.assertFalse((self.root / INIT.CONFIG).exists())

    def test_hook_skips_unrelated_directory(self):
        (self.root / ".git").mkdir()
        output = INIT.session_start({"hook_event_name": "SessionStart", "cwd": str(self.root)})
        self.assertIsNone(output)
        self.assertFalse((self.root / INIT.CONFIG).exists())

    def test_dangling_profile_symlink_is_not_reported_ready(self):
        (self.root / "docs/design").mkdir(parents=True)
        (self.root / "docs/design/README.md").symlink_to(self.root / "missing-profile.md")
        with self.assertRaises(ValueError):
            INIT.initialize(self.root)
        self.assertFalse((self.root / INIT.CONFIG).exists())

    def test_hook_initializes_roblox_root_from_nested_cwd(self):
        (self.root / ".git").mkdir()
        (self.root / "default.project.json").write_text("{}")
        nested = self.root / "src/client"
        nested.mkdir(parents=True)
        output = INIT.session_start({"hook_event_name": "SessionStart", "cwd": str(nested)})
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertTrue((self.root / INIT.CONFIG).exists())
        self.assertFalse((nested / INIT.CONFIG).exists())
        before = self.snapshot()
        INIT.session_start({"hook_event_name": "SessionStart", "cwd": str(nested)})
        self.assertEqual(before, self.snapshot())

    def test_hook_plan_and_project_opt_out_do_not_write(self):
        (self.root / "Game.rbxl").write_bytes(b"place marker")
        before = self.snapshot()
        INIT.session_start({"hook_event_name": "SessionStart", "cwd": str(self.root), "permission_mode": "plan"})
        self.assertEqual(before, self.snapshot())
        (self.root / INIT.CONFIG).write_text(json.dumps({"schema_version": 1, "design_profile": "docs/design/README.md", "auto_init": False}))
        before = self.snapshot()
        self.assertIsNone(INIT.session_start({"hook_event_name": "SessionStart", "cwd": str(self.root)}))
        self.assertEqual(before, self.snapshot())

    def test_cli_check_exit_codes_and_hook_wire_format(self):
        command = [sys.executable, "-B", str(SCRIPT), "--project", str(self.root), "--json"]
        result = subprocess.run(command + ["--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["ready"])
        (self.root / "Game.rbxl").write_bytes(b"place marker")
        event = {"hook_event_name": "SessionStart", "cwd": str(self.root), "source": "startup"}
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--hook"], input=json.dumps(event), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")
        result = subprocess.run(command + ["--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ready"])


if __name__ == "__main__":
    unittest.main()
