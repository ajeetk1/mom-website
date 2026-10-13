from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.database import Base, get_db
from app.main import app
from app.routes.telegram import get_telegram_service
from app.services.telegram import TelegramService


@dataclass
class TelegramAPIStub:
    status_code: int = 200
    calls: list[httpx.Request] = field(default_factory=list)
    fail_get_file: bool = False

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            if request.method == "GET":
               return httpx.Response(200, content=b"telegram image bytes")

            if request.url.path.endswith("/getFile"):
               if self.fail_get_file:
                return httpx.Response(
            500,
            json={
                "ok": False,
                "description": "simulated file failure",
            },
        )

               result = {"file_path": "photos/example.jpg"}
            else:
               result = {}
            return httpx.Response(
                self.status_code,
                json={"ok": self.status_code < 400, "result": result},
            )

        return httpx.MockTransport(handle)


@pytest.fixture
def api_stub() -> TelegramAPIStub:
    return TelegramAPIStub()


@pytest.fixture
def settings() -> Settings:
    return Settings(
    database_url="sqlite+pysqlite:///:memory:",
    telegram_bot_token="test-token-not-real",
    telegram_webhook_secret="test-webhook-secret",
    telegram_allowed_user_id=42,
    admin_api_key="test-admin-key",
)


@pytest.fixture
def test_app(
    settings: Settings,
    api_stub: TelegramAPIStub,
) -> Iterator[tuple[FastAPI, sessionmaker[Session]]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db() -> Iterator[Session]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_telegram_service] = lambda: TelegramService(
        settings,
        transport=api_stub.transport(),
    )
    try:
        yield app, factory
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest_asyncio.fixture
async def client(
    test_app: tuple[FastAPI, sessionmaker[Session]],
) -> AsyncIterator[httpx.AsyncClient]:
    application, _ = test_app
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
