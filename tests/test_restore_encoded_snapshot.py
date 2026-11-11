"""Recovery tests use synthetic bytes inside TemporaryDirectory only."""
import base64
import copy
import gzip
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools/restore_encoded_snapshot.py"
spec = importlib.util.spec_from_file_location("restore_encoded_snapshot", SCRIPT)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "snapshot"
        self.root.mkdir()

    def put(self, name, body, encoded=True):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64encode(body) if encoded else body)
        return path

    def plan(self):
        return recovery.build_plan(self.root)

    def assert_refused(self, plan):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with self.assertRaises((ValueError, OSError)):
            recovery.apply_plan(plan)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_plan_is_read_only_and_restores_supported_formats(self):
        bodies = {"run.py": b"value = 7\n", "meta.json": b'{"ok": true}',
                  "table.csv": b"code,name\n000001,fixture\n",
                  "table.csv.gz": gzip.compress(b"code|name\n000001|fixture\n"),
                  "note.txt": b"# code|date\n"}
        paths = {name: self.put(name, body) for name, body in bodies.items()}
        before = {name: path.read_bytes() for name, path in paths.items()}
        plan = self.plan()
        self.assertEqual(len(plan["items"]), 5)
        self.assertEqual(plan["unknown"], [])
        self.assertEqual(before, {name: path.read_bytes() for name, path in paths.items()})
        self.assertEqual(plan["root"], str(self.root))
        for item in plan["items"]:
            self.assertEqual(set(item), {"relative_path", "source_sha256", "decoded_sha256",
                                         "original_size", "decoded_size", "format"})
        recovery.apply_plan(plan)
        self.assertEqual(bodies, {name: path.read_bytes() for name, path in paths.items()})
        self.assertEqual(self.plan()["items"], [])

    def test_normal_files_and_base64_looking_python_remain_untouched(self):
        bodies = {"normal.py": b"YWJj\n", "normal.json": b'{"ok": 1}',
                  "normal.csv": b"code,name\n1,fixture\n", "normal.txt": b"# code\n",
                  "normal.csv.gz": gzip.compress(b"code,name\n1,fixture\n")}
        for name, body in bodies.items():
            self.put(name, body, encoded=False)
        plan = self.plan()
        self.assertEqual(plan["items"], [])
        recovery.apply_plan(plan)
        self.assertEqual(bodies, {name: (self.root / name).read_bytes() for name in bodies})

    def test_non_target_base64_invalid_json_and_gzip_are_unknown(self):
        cases = {"plain.txt": b"hello world", "bad.json": b'{"x": NaN}',
                 "infinity.json": b'{"x": Infinity}', "overflow.json": b'{"x": 1e999}',
                 "broken.json": b"{", "bad.csv": b'a,b\n"unterminated',
                 "bad.csv.gz": gzip.compress(b"code,name\n1,x\n")[:-1],
                 "crc.csv.gz": gzip.compress(b"code,name\n1,x\n")[:-8] + b"\x00" * 8,
                 "empty.csv.gz": gzip.compress(b""), "unrecognized.csv": b"english"}
        for name, body in cases.items():
            self.put(name, body)
        self.put("unsupported.bin", b"hello")
        plan = self.plan()
        self.assertEqual(plan["items"], [])
        self.assertEqual({item["relative_path"] for item in plan["unknown"]}, set(cases))

    def test_strict_base64_and_whitespace(self):
        self.put("bad.json", b"e30=!!", encoded=False)
        self.put("noncanonical.json", b"e31=", encoded=False)
        path = self.put("valid.json", b" \n e30=\t\r\n", encoded=False)
        plan = self.plan()
        self.assertEqual([item["relative_path"] for item in plan["items"]], ["valid.json"])
        self.assertEqual(len(plan["unknown"]), 2)
        recovery.apply_plan(plan)
        self.assertEqual(path.read_bytes(), b"{}")

    def test_drift_of_last_file_prevents_all_replacements(self):
        first = self.put("a.json", b'{"a": 1}')
        last = self.put("z.json", b'{"z": 1}')
        plan = self.plan()
        last.write_bytes(b"changed")
        self.assert_refused(plan)
        self.assertEqual(first.read_bytes(), base64.b64encode(b'{"a": 1}'))

    def test_tampered_metadata_rejected(self):
        self.put("file.json", b'{"x": 1}')
        plan = self.plan()
        for field, value in (("source_sha256", "0" * 64), ("decoded_sha256", "0" * 64),
                             ("original_size", 0), ("decoded_size", 0), ("format", "py")):
            with self.subTest(field=field):
                tampered = copy.deepcopy(plan)
                tampered["items"][0][field] = value
                self.assert_refused(tampered)

    def test_unsafe_paths_and_duplicates_rejected(self):
        self.put("file.json", b'{"x": 1}')
        plan = self.plan()
        for name in ("../file.json", "/file.json", "a/../file.json", "./file.json", "a//file.json"):
            with self.subTest(name=name):
                tampered = copy.deepcopy(plan)
                tampered["items"][0]["relative_path"] = name
                self.assert_refused(tampered)
        plan["items"].append(copy.deepcopy(plan["items"][0]))
        self.assert_refused(plan)

    def test_symlink_file_and_parent_rejected(self):
        source = self.put("real/file.json", b'{"x": 1}')
        plan = self.plan()
        (self.root / "link.json").symlink_to(source)
        (self.root / "linked").symlink_to(source.parent, target_is_directory=True)
        self.assertIn("link.json", {item["relative_path"] for item in self.plan()["unknown"]})
        for name in ("link.json", "linked/file.json"):
            tampered = copy.deepcopy(plan)
            tampered["items"][0]["relative_path"] = name
            self.assert_refused(tampered)

    def test_version_root_and_schema_rejected(self):
        self.put("file.json", b'{"x": 1}')
        plan = self.plan()
        for key, value in (("version", 99), ("root", "relative"), ("items", None)):
            tampered = copy.deepcopy(plan)
            tampered[key] = value
            self.assert_refused(tampered)
        with self.assertRaises(ValueError):
            recovery.apply_plan(plan, self.root.parent)

    def test_cli_plan_then_explicit_apply_and_unknown_exit(self):
        path = self.put("file.json", b'{"ok": 1}')
        plan_path = Path(self.temp.name) / "plan.json"
        command = [sys.executable, str(SCRIPT)]
        result = subprocess.run(command + ["--root", str(self.root), "--plan", str(plan_path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), base64.b64encode(b'{"ok": 1}'))
        result = subprocess.run(command + ["--apply", str(plan_path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), b'{"ok": 1}')
        self.put("unknown.txt", b"ambiguous words")
        unknown_plan = Path(self.temp.name) / "unknown.json"
        result = subprocess.run(command + ["--root", str(self.root), "--plan", str(unknown_plan)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(json.loads(unknown_plan.read_text())["unknown"]), 1)


if __name__ == "__main__":
    unittest.main()
