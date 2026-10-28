"""让 tests/ 能直接 import 项目根目录下的模块（config / budget / graph ...）。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import observability  # noqa: E402


@pytest.fixture(autouse=True)
def _disable_langfuse(monkeypatch):
    """测试不依赖本地 Langfuse；否则 keys 已配置但服务未启动时会在退出阶段重试导出。"""
    monkeypatch.setattr(observability, "_ENABLED", False)
