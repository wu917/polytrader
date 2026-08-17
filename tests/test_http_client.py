

# ---------- 代理熔断（circuit breaker，2026-08-17） ----------

class _Boom:
    """session.request 永远抛连接异常。"""

    def request(self, *a, **kw):
        import requests as _rq
        raise _rq.ConnectionError("proxy down")


class _Ok:
    """session.request 永远 200。"""

    class _R:
        status_code = 200
        text = '{"ok": 1}'

    def request(self, *a, **kw):
        return _Ok._R()


def _fast_client(monkeypatch):
    """重试 1 次、无 sleep 的 client（测试提速）。"""
    import polytrader.data.http_client as hc
    c = hc.HttpClient(proxy=None, timeout=1, max_retries=1)
    return c


def test_circuit_opens_after_consecutive_failures(monkeypatch):
    """连续失败达阈值 → 熔断打开 → 后续请求快速失败（不走网络）。"""
    c = _fast_client(monkeypatch)
    monkeypatch.setattr(c, "session", _Boom())
    from polytrader.data.http_client import HttpError
    # 阈值 5 次整体失败：前 5 次走网络失败
    for i in range(c.CB_THRESHOLD):
        try:
            c.get("https://x.test/a")
            assert False
        except HttpError as e:
            assert "fast-fail" not in str(e)  # 前 5 次是真实失败
    assert c.circuit_open
    # 第 6 次起快速失败（错误信息含 fast-fail，且不经过 session）
    calls = {"n": 0}

    class _Count(_Boom):
        def request(self, *a, **kw):
            calls["n"] += 1
            return super().request(*a, **kw)
    monkeypatch.setattr(c, "session", _Count())
    try:
        c.get("https://x.test/a")
        assert False
    except HttpError as e:
        assert "fast-fail" in str(e)
    assert calls["n"] == 0  # 快速失败未触网


def test_circuit_closes_on_probe_success(monkeypatch):
    """熔断打开后探测请求成功 → 熔断解除恢复正常。"""
    c = _fast_client(monkeypatch)
    monkeypatch.setattr(c, "session", _Boom())
    from polytrader.data.http_client import HttpError
    for _ in range(c.CB_THRESHOLD):
        try:
            c.get("https://x.test/a")
        except HttpError:
            pass
    assert c.circuit_open
    # 探测窗口到期（打开瞬间重置了探测时钟，回拨使其到期）
    c._cb_last_probe = 0.0
    monkeypatch.setattr(c, "session", _Ok())
    r = c.get("https://x.test/a")  # 探测请求成功
    assert r.status_code == 200
    assert not c.circuit_open  # 熔断解除


def test_circuit_success_resets_counter(monkeypatch):
    """成功请求重置失败计数（间歇失败不误开熔断）。"""
    c = _fast_client(monkeypatch)
    from polytrader.data.http_client import HttpError
    for _ in range(c.CB_THRESHOLD - 1):
        monkeypatch.setattr(c, "session", _Boom())
        try:
            c.get("https://x.test/a")
        except HttpError:
            pass
    monkeypatch.setattr(c, "session", _Ok())
    assert c.get("https://x.test/a").status_code == 200
    assert not c.circuit_open  # 计数已重置，未达阈值不开
    monkeypatch.setattr(c, "session", _Boom())
    try:
        c.get("https://x.test/a")
    except HttpError:
        pass
    assert not c.circuit_open  # 又只失败 1 次（<5），仍不开
