"""클립보드 이미지 헬퍼의 Blender 비의존 회귀 테스트."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
    path = os.path.join(_ROOT, "uvmapping", "clipboard_image.py")
    spec = importlib.util.spec_from_file_location("uvmapping_clipboard_image_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


clipboard_image = _load_module()


class ClipboardImageTest(unittest.TestCase):
    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix="uvmapping_clipboard_test_")

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_supported_platforms(self):
        self.assertTrue(clipboard_image.is_supported("darwin"))
        self.assertTrue(clipboard_image.is_supported("win32"))
        self.assertFalse(clipboard_image.is_supported("linux"))

    def test_target_paths_are_unique_png_names(self):
        first = clipboard_image.target_path(self.work_dir)
        second = clipboard_image.target_path(self.work_dir)
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith(".png"))
        self.assertEqual(os.path.dirname(first), self.work_dir)

    def test_run_uses_argument_list_and_timeout(self):
        completed = subprocess.CompletedProcess(["tool"], 0, stdout="OK\n", stderr="")
        with mock.patch.object(clipboard_image.subprocess, "run", return_value=completed) as run:
            self.assertEqual(clipboard_image._run(["tool", "a b"]), "OK")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["tool", "a b"])
        self.assertEqual(kwargs["timeout"], clipboard_image._COMMAND_TIMEOUT)
        self.assertNotIn("shell", kwargs)

    def test_darwin_png_uses_osascript_argv(self):
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            output_path = command[-2]
            with open(output_path, "wb") as handle:
                handle.write(clipboard_image._PNG_SIGNATURE + b"image")
            return "PNG"

        with mock.patch.object(clipboard_image, "_run", side_effect=fake_run):
            path, error = clipboard_image.paste_to(self.work_dir, "darwin")
        self.assertIsNone(error)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(commands[0][0], "osascript")
        self.assertNotIn("shell", commands[0])

    def test_darwin_tiff_is_converted_and_cleaned(self):
        def fake_run(command, **_kwargs):
            if command[0] == "osascript":
                with open(command[-1], "wb") as handle:
                    handle.write(b"TIFF")
                return "TIFF"
            with open(command[-1], "wb") as handle:
                handle.write(clipboard_image._PNG_SIGNATURE + b"image")
            return ""

        with mock.patch.object(clipboard_image, "_run", side_effect=fake_run):
            path, error = clipboard_image.paste_to(self.work_dir, "darwin")
        self.assertIsNone(error)
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(any(name.endswith(".tiff") for name in os.listdir(self.work_dir)))

    def test_windows_uses_sta_bom_script_and_cleans_it(self):
        inspected = {}

        def fake_run(command, **kwargs):
            inspected["command"] = command
            inspected["flags"] = kwargs["creationflags"]
            script_path = command[command.index("-File") + 1]
            inspected["script_path"] = script_path
            with open(script_path, "rb") as handle:
                inspected["bom"] = handle.read(3)
            with open(command[-1], "wb") as handle:
                handle.write(clipboard_image._PNG_SIGNATURE + b"image")
            return "PNG"

        with mock.patch.object(clipboard_image, "_run", side_effect=fake_run):
            path, error = clipboard_image.paste_to(self.work_dir, "win32")
        self.assertIsNone(error)
        self.assertTrue(os.path.isfile(path))
        self.assertIn("-STA", inspected["command"])
        self.assertEqual(inspected["bom"], b"\xef\xbb\xbf")
        self.assertNotEqual(os.path.dirname(inspected["script_path"]), self.work_dir)
        self.assertFalse(os.path.exists(os.path.dirname(inspected["script_path"])))
        self.assertFalse(any(name.endswith(".ps1") for name in os.listdir(self.work_dir)))

    def test_failure_removes_partial_files(self):
        def fake_run(command, **_kwargs):
            with open(command[-2], "wb") as handle:
                handle.write(b"not a png")
            return "NOIMAGE"

        with mock.patch.object(clipboard_image, "_run", side_effect=fake_run):
            path, error = clipboard_image.paste_to(self.work_dir, "darwin")
        self.assertIsNone(path)
        self.assertIn("이미지가 없습니다", error)
        self.assertEqual(os.listdir(self.work_dir), [])

    def test_scripts_do_not_embed_backslash_paths(self):
        self.assertNotIn(chr(92), clipboard_image._APPLESCRIPT)
        self.assertNotIn(chr(92), clipboard_image._POWERSHELL)
        self.assertIn("GetImage", clipboard_image._POWERSHELL)


if __name__ == "__main__":
    unittest.main()
