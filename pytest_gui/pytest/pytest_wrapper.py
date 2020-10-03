import json
import logging
import subprocess
from multiprocessing.connection import Listener
from queue import Queue
from threading import Thread

from decouple import config

from gevent import sleep


logger = logging.getLogger('pytest_gui.backend.main')


TEST_DIR = config("PYTEST_GUI_TEST_DIR", default=".")
PLUGIN_PORT = config("PYTEST_GUI_PLUGIN_PORT", cast=int, default=6000)
PLUGIN_PATH = "pytest_gui.pytest.pytest_gui_plugin"
ADDRESS = ('localhost', PLUGIN_PORT)


_builtin_markers = [
    "no_cover",
    "filterwarnings",
    "skip",
    "skipif",
    "xfail",
    "parametrize",
    "usefixtures",
    "tryfirst",
    "trylast",
]


def _filter_only_custom_markers(out):
    """Generator for parse output and return custom markers
    Args:
        out (str): output of pytest --markers
    Yields:
        tuple(str, str): contains name and description of the marker
    """
    for marker in out:
        if marker.startswith("@"):
            name = marker.split(":")[0].split(".")[2]
            desc = "".join(marker.split(":")[1:]).strip().rstrip(".")
            if any(name.startswith(marker) for marker in _builtin_markers):
                continue
            yield name, desc


class _TestRunner(Thread):
    def run(self, worker):
        try:
            while worker._cur_tests.poll() is None:
                output = worker._cur_tests.stdout.readline()
                if output != b'':
                    worker.log_queue.put(output.strip())
            worker._cur_tests.wait()

            worker._cur_tests = None
            worker.tests_running = False
            worker.test_stream_connection = None
        except Exception:
            if worker._cur_tests is not None:  # Exception raised not via kill
                raise


class PytestWorker:
    def __init__(self, test_dir):
        self.test_dir = test_dir
        self.tests = None
        self.markers = None
        self.tests_running = False
        self.test_stream_connection = None
        self._cur_tests = None
        self._listener = Listener(ADDRESS)
        self.log_queue = Queue()

    def __del__(self):
        self._listener.close()

    def discover(self):
        p, conn = self._run_pytest(self.test_dir, "--collect-only")
        logger.debug(f'Connection accepted from {self._listener.last_accepted}')
        # TODO: handle if failed 5 times
        try:
            for _ in range(5):  # We can't be blocking so try multiple times
                sleep(1)
                try:
                    self.tests = json.loads(conn.recv())  # Only one message
                except BlockingIOError:
                    continue
                else:
                    break
        finally:
            conn.close()
            p.wait()

    def get_markers(self):
        p, _ = self._run_pytest(self.test_dir, "--markers")
        self.markers = [{"name": name, "description": desc} for name, desc in _filter_only_custom_markers(p.stdout)]

    def run_tests(self):
        pytest_arg = [test["nodeid"] for test in self.tests if test["selected"]]
        p, conn = self._run_pytest(*pytest_arg)
        self._cur_tests = p
        self.tests_running = True
        self.test_stream_connection = conn
        _TestRunner().run(self)

    def stop_tests(self):
        # TODO: Race condition?
        if self._cur_tests is not None:
            self._cur_tests.kill()
            self._cur_tests = None
            self.tests_running = False
            self.test_stream_connection = None

    def _run_pytest(self, *args):
        command = ['pytest', "--capture=tee-sys", "-p", PLUGIN_PATH] + list(args)
        logger.info(f"Runing command: {' '.join(command)}")
        p = subprocess.Popen(command, stdout=subprocess.PIPE, universal_newlines=True)
        logger.debug("Waiting for plugin connection")
        conn = self._listener.accept()
        return p, conn


worker = PytestWorker(TEST_DIR)
