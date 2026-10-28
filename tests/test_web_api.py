"""Web 接口的规模参数边界：越界直接 400，而不是静默降级成空报告。"""
import config
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_research_stream_rejects_over_limit_subquestions():
    resp = client.get("/api/research/stream", params={"topic": "测试主题", "max_sub": 99})
    assert resp.status_code == 400
    assert "子问题上限" in resp.json()["detail"]


def test_research_stream_rejects_zero_rounds():
    """rounds=0 会让 researcher 一次都不检索，必须在入口拦住。"""
    resp = client.get("/api/research/stream", params={"topic": "测试主题", "rounds": 0})
    assert resp.status_code == 400
    assert "检索轮数" in resp.json()["detail"]


def test_index_renders_customisable_scale_inputs():
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'id="max-sub"' in resp.text and 'type="number"' in resp.text
    assert f'max="{config.MAX_SUBQUESTIONS_LIMIT}"' in resp.text
