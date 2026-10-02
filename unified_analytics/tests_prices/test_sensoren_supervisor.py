import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from price_monitor.cloud import CancellationProbe
from price_monitor.runtime import RuntimeProgress, activate, completed, context, phase
from price_monitor.sensoren_supervisor import (
    ProgressPublisher, instance_lock, restart_reason, stop_tree, supervise,
)

ROOT = Path(__file__).resolve().parents[1]


class WatchdogTests(unittest.TestCase):
    def tearDown(self):
        activate(None)

    def test_heartbeat_does_not_hide_stalled_work(self):
        with patch('price_monitor.runtime.time.monotonic', return_value=10):
            progress = RuntimeProgress()
            activate(progress)
            context(11, 'https://sensoren.ru/product/example/')
        with patch('price_monitor.runtime.time.monotonic', return_value=191):
            state = {**progress.watchdog_snapshot(), 'pid': 42}
        self.assertEqual(state['emitted_at'], 191)
        self.assertEqual(restart_reason(state, 42, 10, 191), 'Основной поток не продвигается')

    def test_nested_activity_does_not_reset_page_deadline(self):
        with patch('price_monitor.runtime.time.monotonic', return_value=10):
            progress = RuntimeProgress()
            activate(progress)
            context(11, 'https://sensoren.ru/product/example/')
        with patch('price_monitor.runtime.time.monotonic', return_value=611):
            with phase('database'):
                pass
            state = {**progress.watchdog_snapshot(), 'pid': 42}
            self.assertEqual(state['inactive_seconds'], 0)
            self.assertEqual(restart_reason(state, 42, 10, 611), 'Превышено время обработки одной страницы')
            completed()
            state = {**progress.watchdog_snapshot(), 'pid': 42}
            self.assertEqual(restart_reason(state, 42, 10, 611), '')

    def test_idle_polling_and_reconnection_do_not_timeout(self):
        progress = RuntimeProgress()
        activate(progress)
        context(11, 'old-page')
        context(None)
        progress.recover(ConnectionError())
        state = {**progress.watchdog_snapshot(), 'pid': 42}
        self.assertEqual(state['task_seconds'], 0)
        self.assertEqual(restart_reason(state, 42, 0, state['emitted_at']), '')

    def test_dead_publisher_and_previous_process_are_detected(self):
        state = dict(pid=42, emitted_at=10, inactive_seconds=0, task_seconds=0)
        self.assertEqual(restart_reason(state, 42, 10, 191), 'Процесс перестал отвечать')
        self.assertEqual(restart_reason(state, 43, 100, 110), '')
        self.assertEqual(restart_reason(state, 43, 100, 281), 'Нет локального сигнала сборщика')
        self.assertTrue(restart_reason(None, 42, 10, 191))

    def test_publisher_is_local_and_preserves_queue_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as directory:
            path = Path(directory)/'progress.json'
            outbox = Path(directory)/'saved-product.json'
            outbox.write_text('preserved', encoding='utf-8')
            progress = RuntimeProgress()
            publisher = ProgressPublisher(progress, path)
            publisher.start()
            publisher.close()
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['pid'], os.getpid())
            self.assertEqual(outbox.read_text(encoding='utf-8'), 'preserved')

    def test_instance_lock_prevents_duplicates_and_is_released(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as directory:
            path = Path(directory)/'supervisor.lock'
            with instance_lock(path):
                with self.assertRaises(OSError):
                    with instance_lock(path):
                        pass
            with instance_lock(path):
                pass

    def test_supervisor_restarts_crashed_child_with_same_outbox(self):
        stopping = threading.Event()
        first = Mock(pid=41, returncode=1)
        first.poll.return_value = 1
        second = Mock(pid=42, returncode=0)
        def launch(*args, **kwargs):
            if launch.count:
                stopping.set()
                return second
            launch.count += 1
            return first
        launch.count = 0
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as directory, \
                patch('price_monitor.sensoren_supervisor.subprocess.Popen', side_effect=launch) as popen, \
                patch('price_monitor.sensoren_supervisor.stop_tree') as stop, \
                patch.object(stopping, 'wait', side_effect=lambda seconds: stopping.is_set()):
            self.assertEqual(supervise(['--fresh-connections'], Path(directory), stopping), 0)
            self.assertEqual(popen.call_count, 2)
            self.assertEqual(popen.call_args_list[0].args[0], popen.call_args_list[1].args[0])
            self.assertEqual(stop.call_count, 2)

    def test_hung_child_can_be_terminated_without_cooperation(self):
        options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} \
            if os.name == 'nt' else {'start_new_session': True}
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], **options)
        try:
            stop_tree(child)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_browser_callback_cancellation_never_contacts_database(self):
        from price_monitor.sensoren_browser import SensorenBrowserClient
        from price_monitor.transport import FetchError
        check = Mock(return_value=False)
        stopping = threading.Event()
        probe = CancellationProbe(check, stopping, ttl=0)
        client = SensorenBrowserClient(cancelled=probe)
        try:
            client._check_cancelled(cached=True)
            check.assert_not_called()
            client._check_cancelled()
            check.assert_called_once()
            stopping.set()
            with self.assertRaises(FetchError):
                client._check_cancelled(cached=True)
            check.assert_called_once()
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
