"""测试工具：在临时目录中构造服务。"""
import tempfile

from service.engine import ExchangeService
from service.store import EventStore


def make_service(dispatcher=None):
    tmp = tempfile.mkdtemp(prefix="exchange-test-")
    return ExchangeService(EventStore(tmp), dispatcher=dispatcher), tmp


def iso(y, m=1, d=1, h=0, minute=0):
    return f"{y:04d}-{m:02d}-{d:02d}T{h:02d}:{minute:02d}:00Z"
