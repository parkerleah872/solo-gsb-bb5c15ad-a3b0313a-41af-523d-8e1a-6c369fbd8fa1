import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import app


@pytest.fixture()
def settings(tmp_path):
    db = tmp_path / "test.db"
    get_settings.cache_clear()
    s = get_settings()
    # 直接覆盖运行配置（比环境变量方式更方便）
    s.database_url = f"sqlite:///{db}"
    s.env = "test"
    s.master_key = "test-master-key"
    s.token_ttl = 600
    s.default_ttl = 86400
    s.rule_timeout = 1.0
    return s


@pytest.fixture()
async def client(settings):
    # 每个用例独立进入 startup：重新读取当前 settings 并建立新连接 / 清理任务
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    get_settings.cache_clear()
