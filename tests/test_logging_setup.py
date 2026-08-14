"""logging_setup 懒初始化 + HttpClient 请求日志的单测。"""
from __future__ import annotations

import io
import logging
import sys

import pytest

from polytrader.data.http_client import HttpClient
from polytrader.logging_setup import setup_logging


def _fresh_root() -> logging.Logger:
    root = logging.getLogger("polytrader")
    # 记录并清空现有 handler（测试隔离）
    root.handlers = []
    return root


def test_get_logger_lazy_init_adds_stdout_handler():
    """未显式 setup_logging 时，get_logger 应自动补 stdout handler。"""
    root = _fresh_root()
    from polytrader.logging_setup import get_logger

    lg = get_logger("test.lazy")
    assert lg.name == "polytrader.test.lazy"
    assert any(isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
               for h in root.handlers)


def test_setup_logging_idempotent():
    """setup_logging 重复调用不重复加 handler。"""
    root = _fresh_root()
    setup_logging(log_file="")
    n1 = len(root.handlers)
    setup_logging(log_file="")
    assert len(root.handlers) == n1


def test_http_client_logs_success(capsys):
    """HttpClient 成功请求应输出 INFO 日志（method/url/status/ms）。"""
    import http.server
    import socketserver
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _fresh_root()
        from polytrader.logging_setup import get_logger
        get_logger("data.http")  # 触发懒初始化
        http = HttpClient(proxy=None, timeout=5)
        r = http.get(f"http://127.0.0.1:{port}/test")
        assert r.status_code == 200
        out = capsys.readouterr().out
        assert "→ GET" in out and f"http://127.0.0.1:{port}/test" in out
        assert "200" in out and "resp: {\"ok\": true}" in out
    finally:
        srv.shutdown()


def test_http_client_failure_logs_warning(caplog):
    """HttpClient 失败请求应输出 WARNING（含重试次数）。"""
    _fresh_root()
    with caplog.at_level(logging.WARNING):
        http = HttpClient(proxy=None, timeout=1, max_retries=2)
        with pytest.raises(Exception):
            http.get("http://127.0.0.1:1/")  # 必然失败（拒绝连接）
        assert any("request GET" in r.message and "failed" in r.message
                   for r in caplog.records)
