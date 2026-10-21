"""
测试全局隔离：在导入任何 backend 模块之前把数据目录指向临时目录，
保证测试绝不读写仓库下真实的 data/（账号凭据、下载历史、已下载文章）。

另外屏蔽真实网络：历史回溯相关测试全部使用 FakeAdapter / FakeDownloader，
任何漏网的 requests 调用都会直接失败，而不会连到微信 / 微信读书。
"""

import os
import sys
import tempfile
import socket
import pytest
from pathlib import Path

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="wmt_test_data_"))
os.environ["WECHAT_MP_DATA_DIR"] = str(_TEST_DATA_DIR)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ("localhost", "127.0.0.1", "::1"):
            raise AssertionError("离线测试禁止外部网络请求")
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)


def pytest_sessionfinish(session, exitstatus):
    import shutil

    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
