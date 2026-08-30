"""네이티브 프롬프트 입력 헬퍼의 Blender 비의존 회귀 테스트."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
    path = os.path.join(_ROOT, "uvmapping", "native_input.py")
    spec = importlib.util.spec_from_file_location("uvmapping_native_input_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


native_input = _load_module()


class NativeInputTest(unittest.TestCase):
    def setUp(self):
        native_input.shutdown()
        self.work_dir = tempfile.mkdtemp(prefix="uvmapping_native_input_test_")
        self.init_path = os.path.join(self.work_dir, "initial.txt")
        self.result_path = os.path.join(self.work_dir, "result.txt")

    def tearDown(self):
        native_input.shutdown()
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_support_and_open_state(self):
        self.assertTrue(native_input.is_supported("darwin"))
        self.assertTrue(native_input.is_supported("win32"))
        self.assertFalse(native_input.is_supported("linux"))
        self.assertFalse(native_input.is_open())

    def test_to_single_line_normalizes_korean_prompt(self):
        text = "  낡은 나무 배럴\r\n  금속 밴드 2개  "
        self.assertEqual(native_input.to_single_line(text), "낡은 나무 배럴 금속 밴드 2개")
        self.assertEqual(native_input.to_single_line(None), "")

    def test_darwin_command_passes_values_as_argv(self):
        command, flags = native_input.build_command(
            "darwin", self.work_dir, "프롬프트 입력", self.init_path, self.result_path
        )
        self.assertEqual(command[0], "osascript")
        self.assertEqual(command[-3:], ["프롬프트 입력", self.init_path, self.result_path])
        self.assertEqual(flags, 0)

    def test_windows_command_is_sta_bom_and_unique(self):
        first, flags = native_input.build_command(
            "win32", self.work_dir, "프롬프트 입력", self.init_path, self.result_path
        )
        second, _ = native_input.build_command(
            "win32", self.work_dir, "프롬프트 입력", self.init_path, self.result_path
        )
        first_script = first[first.index("-File") + 1]
        second_script = second[second.index("-File") + 1]
        self.assertNotEqual(first_script, second_script)
        self.assertIn("-STA", first)
        self.assertEqual(first[-3:], ["프롬프트 입력", self.init_path, self.result_path])
        self.assertEqual(flags, getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(first_script, "rb") as handle:
            self.assertEqual(handle.read(3), b"\xef\xbb\xbf")

    def test_unsupported_command(self):
        command, flags = native_input.build_command(
            "linux", self.work_dir, "제목", self.init_path, self.result_path
        )
        self.assertIsNone(command)
        self.assertEqual(flags, 0)

    def test_scripts_do_not_embed_backslash_paths(self):
        self.assertNotIn(chr(92), native_input._APPLESCRIPT)
        self.assertNotIn(chr(92), native_input._POWERSHELL)
        self.assertIn("System Events", native_input._APPLESCRIPT)
        self.assertIn("System.Windows.Forms", native_input._POWERSHELL)

    def test_poll_delivers_result_and_removes_temp_dir(self):
        class FinishedProcess:
            def poll(self):
                return 0

        callback_values = []
        work_dir = tempfile.mkdtemp(prefix="uvmapping_native_poll_")
        result_path = os.path.join(work_dir, "result.txt")
        with open(result_path, "w", encoding="utf-8") as handle:
            handle.write("한글 프롬프트")
        native_input._state.update(
            proc=FinishedProcess(),
            on_done=callback_values.append,
            dir=work_dir,
            result=result_path,
            started=0.0,
            timer_registered=True,
        )
        with mock.patch.object(native_input.time, "monotonic", return_value=1.0):
            self.assertIsNone(native_input._poll())
        self.assertEqual(callback_values, ["한글 프롬프트"])
        self.assertFalse(os.path.exists(work_dir))
        self.assertFalse(native_input.is_open())

    def test_shutdown_terminates_process_and_removes_temp_dir(self):
        class RunningProcess:
            def __init__(self):
                self.terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout):
                return 0

        process = RunningProcess()
        work_dir = tempfile.mkdtemp(prefix="uvmapping_native_shutdown_")
        native_input._state.update(
            proc=process,
            on_done=lambda _text: None,
            dir=work_dir,
            result=os.path.join(work_dir, "result.txt"),
            started=0.0,
            timer_registered=False,
        )
        native_input.shutdown()
        self.assertTrue(process.terminated)
        self.assertFalse(os.path.exists(work_dir))
        self.assertFalse(native_input.is_open())

    def test_shutdown_unregisters_blender_timer(self):
        class FakeTimers:
            def __init__(self):
                self.unregistered = []

            def is_registered(self, callback):
                return callback is native_input._poll

            def unregister(self, callback):
                self.unregistered.append(callback)

        timers = FakeTimers()
        fake_bpy = types.SimpleNamespace(app=types.SimpleNamespace(timers=timers))
        native_input._state["timer_registered"] = True
        with mock.patch.dict(sys.modules, {"bpy": fake_bpy}):
            native_input.shutdown()
        self.assertEqual(timers.unregistered, [native_input._poll])
        self.assertFalse(native_input._state["timer_registered"])

    def test_open_dialog_registers_persistent_timer(self):
        class FakeTimers:
            def __init__(self):
                self.registered = []

            def register(self, callback, **kwargs):
                self.registered.append((callback, kwargs))

            def is_registered(self, callback):
                return bool(self.registered) and callback is native_input._poll

            def unregister(self, _callback):
                self.registered.clear()

        class FakeProcess:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout):
                return 0

        timers = FakeTimers()
        fake_bpy = types.SimpleNamespace(app=types.SimpleNamespace(timers=timers))
        with (
            mock.patch.dict(sys.modules, {"bpy": fake_bpy}),
            mock.patch.object(native_input, "is_supported", return_value=True),
            mock.patch.object(native_input.sys, "platform", "darwin"),
            mock.patch.object(native_input.subprocess, "Popen", return_value=FakeProcess()),
        ):
            error = native_input.open_dialog("프롬프트 입력", "기존값", lambda _text: None)
            self.assertIsNone(error)
            self.assertEqual(len(timers.registered), 1)
            callback, kwargs = timers.registered[0]
            self.assertIs(callback, native_input._poll)
            self.assertTrue(kwargs["persistent"])
            native_input.shutdown()


if __name__ == "__main__":
    unittest.main()
