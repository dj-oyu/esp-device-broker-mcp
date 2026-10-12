"""Dependency-free unit tests; also collected by pytest in Windows/CI."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.firmware_build import parse_profile, source_fingerprint, FirmwareBuild

def request(cursor=None, levels=('error', 'warning'), context=2, limit=40):
    return SimpleNamespace(cursor=cursor, levels=levels, context=context, limit=limit)

class BuildTests(unittest.TestCase):

    def test_profile_strict_filter(self):
        env = parse_profile('IDF_PATH=C:/idf\nIDF_PYTHON_ENV_PATH=C:/python\nPATH=C:/tools\nTOKEN=private\n')
        self.assertNotIn('TOKEN', env)
        for value in ('noise', 'IDF_PATH=x\nIDF_PATH=y', 'KEY=bad\x00value', 'PATH=x'):
            with self.assertRaises(ValueError):
                parse_profile(value)

    def test_version_registration_no_fallback(self):
        try:
            from cardputer_mcp.config import BuildProject
        except ModuleNotFoundError as error:
            self.skipTest(str(error))
        with self.assertRaises(ValueError):
            BuildProject(project_dir=Path('/project'), trusted_profile=Path('/profile'), expected_idf='6.1')
        with self.assertRaises(ValueError):
            BuildProject(project_dir=Path('relative'), trusted_profile=Path('/profile'))

    def test_logs_multireader_filters_context_redaction(self):
        logs = BuildLogs('alice')
        for line in ['source.cpp', 'warning: trouble token=private', 'note: here', 'unknown text', '[4/5] compiling']:
            logs.append(line, 'stderr')
        first = logs.read('alice', request())
        self.assertEqual(first, logs.read('alice', request()))
        self.assertEqual(first['entries'][0]['level'], 'unknown')
        self.assertEqual(first['entries'][1]['level'], 'warning')
        self.assertNotIn('private', str(first))
        self.assertIn('note: here', str(first))
        with self.assertRaises(ValueError):
            logs.read('bob', request())
        cursor = SimpleNamespace(**first['next_cursor'])
        self.assertEqual([], logs.read('alice', request(cursor))['entries'])

    def test_large_output_gap_expiry_restart(self):
        logs = BuildLogs('alice', max_lines=8, max_bytes=1000)
        epoch = logs.epoch
        for _ in range(10000):
            logs.append('error: ' + 'x' * 500)
        self.assertLessEqual(logs.bytes, 1000)
        self.assertLessEqual(len(logs.entries), 8)
        self.assertTrue(logs.read('alice', request(SimpleNamespace(epoch=epoch, sequence=0)))['gap'])
        self.assertTrue(logs.read('alice', request(SimpleNamespace(epoch='other', sequence=0)))['gap'])
        logs.deadline = 0
        self.assertTrue(logs.read('alice', request())['expired'])

    def test_fingerprint_changes_and_build_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'CMakeLists.txt').write_text('one')
            before = source_fingerprint(root)
            (root / 'build').mkdir()
            (root / 'build' / 'junk').write_text('ignored')
            self.assertEqual(before, source_fingerprint(root))
            (root / 'CMakeLists.txt').write_text('two')
            self.assertNotEqual(before, source_fingerprint(root))

    def test_registered_plan_no_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'CMakeLists.txt').write_text('source')
            profile = root / 'profile.ps1'
            profile.write_text('return')
            settings = SimpleNamespace(build_projects={'cardputer': SimpleNamespace(project_dir=root, trusted_profile=profile, expected_idf='6.0.1', target='esp32s3')})
            plan = FirmwareBuild(settings).prepare('cardputer', 'alice')
            self.assertNotIn(temp, str(plan))
            self.assertEqual('6.0.1', plan['expected_idf'])
            with self.assertRaises(ValueError):
                FirmwareBuild(settings).prepare('unknown', 'alice')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX runner; Windows uses owned job API')
    def test_process_bounded_streams_and_stderr_not_error(self):
        process = BuildProcess()
        lines = []
        code = process.run([sys.executable, '-c', 'import sys;print("x"*100000);print("note: text",file=sys.stderr)'], {}, Path('/tmp'), lambda line, stream: lines.append((line, stream)), timeout=10)
        self.assertEqual(code, 0)
        self.assertFalse(process.active)
        self.assertLessEqual(max((len(line) for line, stream in lines)), 4096)
        self.assertIn(('note: text', 'stderr'), lines)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX runner; Windows uses owned job API')
    def test_interruption_stops_owned_process(self):
        process = BuildProcess()
        failures = []

        def run():
            try:
                process.run([sys.executable, '-c', 'import time;time.sleep(20)'], {}, Path('/tmp'), lambda *_: None, timeout=30)
            except ValueError as error:
                failures.append(str(error))
        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.1)
        process.stop_requested.set()
        worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertFalse(process.active)
        self.assertEqual(['build_interrupted_needs_review'], failures)

class WindowsOwnedBuildTests(unittest.TestCase):

    def test_leader_exit_does_not_release_live_descendants(self):
        import os
        from unittest.mock import patch
        from cardputer_mcp import build_process as module

        class FakeAPI:
            last = None

            def __init__(self, out, err):
                self.out = out
                self.err = err
                self.children = 2
                self.configured = False
                self.terminated = False
                FakeAPI.last = self

            def create_job(self):
                return 101

            def configure_job(self, job):
                self.configured = True

            def create_suspended(self, job, args, env, cwd, handles):
                assert self.configured and job == 101
                assert env == {'PATH': 'trusted'}
                handles.process = 202
                handles.thread = 303
                os.write(self.out, b'[1/1] build\n')
                os.write(self.err, b'note: stderr\n')

            def resume_thread(self, thread):
                assert thread == 303

            def close_handle(self, handle):
                pass

            def poll_process(self, process):
                return 0

            def active_count(self, job):
                return self.children

            def terminate_job(self, job):
                self.terminated = True
                self.children = 0
        runner = BuildProcess()
        lines = []
        cwd = Path('/tmp')
        with patch.object(module.os, 'name', 'nt'), patch.object(module, 'PipeWindowsAPI', FakeAPI), patch.dict(sys.modules, {'msvcrt': SimpleNamespace(get_osfhandle=lambda fd: fd)}), patch.object(module.os, 'set_handle_inheritable', lambda *_: None, create=True):
            code = runner.run(['trusted-python', 'idf.py', 'build'], {'PATH': 'trusted'}, cwd, lambda line, stream: lines.append((line, stream)))
        self.assertEqual(code, 0)
        self.assertTrue(FakeAPI.last.terminated)
        self.assertFalse(runner.active)
        self.assertIsNone(runner.owner)
        self.assertIn(('note: stderr', 'stderr'), lines)

    def test_shutdown_failure_latches_ownership(self):
        import os
        from unittest.mock import patch
        from cardputer_mcp import build_process as module

        class FakeAPI:

            def __init__(self, *_):
                pass

            def create_job(self):
                return 101

            def configure_job(self, job):
                raise RuntimeError('synthetic')

            def active_count(self, job):
                raise RuntimeError('synthetic')
        runner = BuildProcess()
        cwd = Path('/tmp')
        with patch.object(module.os, 'name', 'nt'), patch.object(module, 'PipeWindowsAPI', FakeAPI), patch.dict(sys.modules, {'msvcrt': SimpleNamespace(get_osfhandle=lambda fd: fd)}), patch.object(module.os, 'set_handle_inheritable', lambda *_: None, create=True):
            with self.assertRaises(Exception):
                runner.run(['trusted'], {}, cwd, lambda *_: None)
        self.assertTrue(runner.active)
        self.assertTrue(runner.uncertain)
        self.assertIsNotNone(runner.owner)

class FirmwareVerticalTests(unittest.TestCase):

    def test_profile_diagnostics_build_manifest_and_reader_boundary(self):
        from unittest.mock import patch
        from cardputer_mcp import firmware_build as module
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'project'
            project.mkdir()
            (project / 'CMakeLists.txt').write_text('source')
            profile = root / 'profile.ps1'
            profile.write_text('return')
            system = root / 'system'
            powershell = system / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            powershell.parent.mkdir(parents=True)
            powershell.write_bytes(b'ps')
            idf = root / 'idf'
            idf_py = idf / 'tools/idf.py'
            idf_py.parent.mkdir(parents=True)
            idf_py.write_bytes(b'idf')
            python_env = root / 'python'
            python = python_env / 'Scripts/python.exe'
            python.parent.mkdir(parents=True)
            python.write_bytes(b'python')
            compiler = root / 'xtensa-esp32s3-elf-gcc.exe'
            compiler.write_bytes(b'gcc')
            settings = SimpleNamespace(state_dir=root / 'state', build_projects={'cardputer': SimpleNamespace(project_dir=project, trusted_profile=profile, expected_idf='6.0.1', target='esp32s3')})
            builder = FirmwareBuild(settings)
            plan = builder.prepare('cardputer', 'alice') | {'job_id': 'job'}
            calls = []

            class FakeProcess:
                stop_requested = threading.Event()

                def run(self, args, env, cwd, emit, timeout=1800):
                    calls.append(args)
                    self_test.assertNotIn('UNTRUSTED_TOKEN', env)
                    if '-e' in args:
                        for line in (f'IDF_PATH={idf}', f'IDF_PYTHON_ENV_PATH={python_env}', f'PATH={root}', 'UNTRUSTED_TOKEN=secret'):
                            emit(line, 'stdout')
                    elif args[-1] == '--version':
                        emit('ESP-IDF v6.0.1' if args[1] == str(idf_py) else 'version', 'stdout')
                    else:
                        directory = Path(args[args.index('-B') + 1])
                        directory.mkdir(parents=True, exist_ok=True)
                        (directory / 'project_description.json').write_text('{"target":"esp32s3"}')
                        (directory / 'firmware.bin').write_bytes(b'firmware')
                        emit('warning: synthetic token=hidden', 'stderr')
                    return 0
            self_test = self
            builder.process = FakeProcess()
            with patch.object(module, 'os', SimpleNamespace(name='nt', environ={'SYSTEMROOT': str(system), 'UNTRUSTED_TOKEN': 'secret'})), patch.object(module.shutil, 'which', return_value=str(compiler)):
                result = builder._execute(plan, lambda _: None)
            self.assertEqual(result, 'firmware_build_succeeded_no_flash')
            self.assertEqual(calls[0][1:3], ['-NoProfile', '-File'])
            self.assertEqual(calls[-1][-1], 'build')
            self.assertEqual(len(calls), 5)
            report = builder.read_logs(SimpleNamespace(job_id='job', **vars(request())), 'alice')
            self.assertFalse(report['manifest']['flash_authorized'])
            self.assertEqual(report['manifest']['idf_version'], '6.0.1')
            self.assertNotIn('hidden', str(report))
            with self.assertRaises(ValueError):
                builder.read_logs(SimpleNamespace(job_id='job', **vars(request())), 'bob')
if __name__ == '__main__':
    unittest.main()
