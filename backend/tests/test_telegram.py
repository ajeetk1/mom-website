import json
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.categories import CATEGORY_NAME_SLUGS, CATEGORY_NAMES
from app.database import get_db
from app.main import app
from app.models.category import Category
from app.models.post import Post, PostStatus
from app.models.telegram_update import TelegramUpdate
from app.services.telegram import TelegramAPIError, TelegramService

pytestmark = pytest.mark.asyncio

WEBHOOK = "/api/telegram/webhook"
SECRET_HEADER = {
    "X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"
}


def update(
    update_id: int,
    *,
    user_id: int = 42,
    message: dict | None = None,
) -> dict:
    if message is None:
        message = {
            "text": "Education is very important for every child."
        }

    return {
        "update_id": update_id,
        "message": {
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Mom",
            },
            "chat": {
                "id": user_id,
                "type": "private",
            },
            **message,
        },
    }


def post_type_selection(
    update_id: int,
    post_type: str,
    user_id: int = 42,
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Mom",
            },
            "message": {
                "message_id": update_id,
                "chat": {
                    "id": user_id,
                    "type": "private",
                },
            },
            "data": f"post_type:{post_type}",
        },
    }


async def select_post_type(
    client: httpx.AsyncClient,
    update_id: int,
    post_type: str,
) -> httpx.Response:
    return await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=post_type_selection(update_id, post_type),
    )


def seed_categories(factory) -> None:
    with factory() as db:
        db.add_all(
            [
                Category(name=name, slug=slug)
                for name, slug in CATEGORY_NAME_SLUGS
            ]
        )
        db.commit()


async def test_valid_webhook_saves_exact_text_as_uncategorized_draft(
    client: httpx.AsyncClient,
    test_app: tuple,
    api_stub,
) -> None:
    source = (
        "  Education is very important for every child.\n"
        "All children deserve to learn.  "
    )

    _, factory = test_app
    seed_categories(factory)

    selected = await select_post_type(client, 2001, "latest")
    assert selected.status_code == 200

    response = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(2002, message={"text": source}),
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    _, factory = test_app

    with factory() as db:
        post = db.scalar(select(Post))

        assert post is not None
        assert post.content == source.strip()
        assert post.title == source.strip().splitlines()[0][:255]
        assert post.summary is None
        assert post.category_id is None
        assert post.status == PostStatus.DRAFT

        update_record = db.scalar(
            select(TelegramUpdate).where(
                TelegramUpdate.update_id == 2002
            )
        )

        assert update_record is not None
        assert update_record.post_id == post.id

        
    message_payloads = [
        json.loads(request.content)
        for request in api_stub.calls
        if request.url.path.endswith("/sendMessage")
    ]

    category_payload = next(
        payload
        for payload in reversed(message_payloads)
        if payload.get("text", "").startswith(
            "Which category should this article belong to?"
        )
    )

    keyboard = category_payload.get("reply_markup", {}).get(
        "inline_keyboard", []
    )

    assert keyboard, "Expected category buttons in the Telegram reply"

    button_texts = [
        button["text"]
        for row in keyboard
        for button in row
    ]

    for category in CATEGORY_NAMES:
        assert any(
            category in button_text
            for button_text in button_texts
        )




async def test_invalid_secret_is_rejected_without_writes(
    client: httpx.AsyncClient,
    test_app: tuple,
) -> None:
    response = await client.post(
        WEBHOOK,
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        json={
            "update_id": "not-an-integer",
            "message": {},
        },
    )

    assert response.status_code == 403

    _, factory = test_app

    with factory() as db:
        assert db.scalar(select(func.count(Post.id))) == 0
        assert db.scalar(select(func.count(TelegramUpdate.id))) == 0



async def test_unauthorized_user_is_rejected_without_writes(
    client: httpx.AsyncClient,
    test_app: tuple,
) -> None:
    response = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(2004, user_id=999),
    )

    assert response.status_code == 403

    _, factory = test_app

    with factory() as db:
        assert db.scalar(select(func.count(Post.id))) == 0
        assert db.scalar(select(func.count(TelegramUpdate.id))) == 0


async def test_duplicate_update_does_not_create_duplicate_draft(
    client: httpx.AsyncClient,
    test_app: tuple,
) -> None:
    selected = await select_post_type(client, 2005, "latest")
    assert selected.status_code == 200

    payload = update(
        2006,
        message={"text": "Education is important."},
    )

    first = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=payload,
    )

    second = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=payload,
    )

    assert first.status_code == second.status_code == 200

    _, factory = test_app

    with factory() as db:
        assert db.scalar(select(func.count(Post.id))) == 1

        assert db.scalar(
            select(func.count(TelegramUpdate.id)).where(
                TelegramUpdate.update_id == 2006
            )
        ) == 1


async def test_message_without_post_type_requests_selection(
    client: httpx.AsyncClient,
    test_app: tuple,
    api_stub,
) -> None:
    response = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(2007, message={"text": "  "}),
    )

    assert response.status_code == 200

    reply = json.loads(api_stub.calls[-1].content)
    assert reply["text"] == (
        "Let's create a post first. "
        "Tap /start to choose a post type."
    )
    assert "reply_markup" in reply

    _, factory = test_app

    with factory() as db:
        assert db.scalar(select(func.count(Post.id))) == 0


async def test_start_command_shows_post_type_keyboard(
    client: httpx.AsyncClient,
    api_stub,
) -> None:
    response = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(2008, message={"text": "/start"}),
    )

    assert response.status_code == 200

    reply = json.loads(api_stub.calls[-1].content)

    assert reply["text"] == (
        "Welcome! 👋\n\nWhat would you like to publish today?"
    )

    keyboard = reply["reply_markup"]["inline_keyboard"]

    button_data = [
        button["callback_data"]
        for row in keyboard
        for button in row
        if "callback_data" in button
    ]

    assert "post_type:featured" in button_data
    assert "post_type:latest" in button_data
    assert "post_type:archive" in button_data


async def test_help_command_shows_post_type_keyboard(
    client: httpx.AsyncClient,
    api_stub,
) -> None:
    response = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(2009, message={"text": "/help"}),
    )

    assert response.status_code == 200

    reply = json.loads(api_stub.calls[-1].content)

    assert reply["text"]
    assert reply.get("reply_markup", {}).get("inline_keyboard")


async def test_latest_post_requests_category_after_text(
    client: httpx.AsyncClient,
    test_app: tuple,
    api_stub,
) -> None:
    _, factory = test_app
    seed_categories(factory)

    selected = await select_post_type(client, 2010, "latest")
    assert selected.status_code == 200

    response = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(
            2011,
            message={
                "text": "A memory from our family."
            },
        ),
    )

    assert response.status_code == 200

    reply = json.loads(api_stub.calls[-1].content)["text"]

    assert reply.startswith(
    "Which category should this article belong to?"

)

    with factory() as db:
        post = db.scalar(select(Post))

        assert post is not None
        assert post.title == "A memory from our family."
        assert post.content == "A memory from our family."
        assert post.category_id is None
        assert post.status == PostStatus.DRAFT


async def test_category_name_publishes_pending_latest_post(
    client: httpx.AsyncClient,
    test_app: tuple,
    api_stub,
) -> None:
    _, factory = test_app
    seed_categories(factory)

    selected = await select_post_type(client, 2012, "latest")
    assert selected.status_code == 200

    submitted = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(
            2013,
            message={"text": "A memory from our family."},
        ),
    )

    published = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(
            2014,
            message={"text": "mInD & mOoD"},
        ),
    )

    assert submitted.status_code == 200
    assert published.status_code == 200

    confirmation = json.loads(api_stub.calls[-1].content)["text"]

    assert "Published successfully!" in confirmation
    assert "Category: Mind & Mood" in confirmation
    assert "Status: published" in confirmation

    with factory() as db:
        post = db.scalar(select(Post))

        assert post is not None
        assert post.content == "A memory from our family."
        assert post.category is not None
        assert post.category.name == "Mind & Mood"
        assert post.status == PostStatus.PUBLISHED
        assert post.published_at is not None
        assert db.scalar(select(func.count(Post.id))) == 1

        category_update = db.scalar(
            select(TelegramUpdate).where(
                TelegramUpdate.update_id == 2014
            )
        )

        assert category_update is not None
        assert category_update.post_id == post.id


async def test_numeric_category_uses_listed_order(
    client: httpx.AsyncClient,
    test_app: tuple,
    api_stub,
) -> None:
    _, factory = test_app
    seed_categories(factory)

    selected = await select_post_type(client, 2015, "latest")
    assert selected.status_code == 200

    await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(
            2016,
            message={"text": "Another family memory."},
        ),
    )

    response = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(2017, message={"text": "7"}),
    )

    assert response.status_code == 200

    confirmation = json.loads(api_stub.calls[-1].content)["text"]
    assert "Category: Stories & Reflections" in confirmation

    with factory() as db:
        post = db.scalar(select(Post))

        assert post is not None
        assert post.category is not None
        assert post.category.name == "Stories & Reflections"
        assert post.status == PostStatus.PUBLISHED


async def test_invalid_category_does_not_publish_or_create_post(
    client: httpx.AsyncClient,
    test_app: tuple,
    api_stub,
) -> None:
    _, factory = test_app
    seed_categories(factory)

    selected = await select_post_type(client, 2018, "latest")
    assert selected.status_code == 200

    await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(
            2019,
            message={
                "text": "An article waiting for a category."
            },
        ),
    )

    invalid = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(2020, message={"text": "Not a category"}),
    )

    assert invalid.status_code == 200

    reply = json.loads(api_stub.calls[-1].content)["text"]
    assert reply.startswith(
    "Please select one of the category buttons below."
)
    second_article = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(2021, message={"text": "A second article"}),
    )

    assert second_article.status_code == 200

    with factory() as db:
        posts = list(db.scalars(select(Post)))

        assert len(posts) == 1
        assert posts[0].status == PostStatus.DRAFT
        assert posts[0].category_id is None


async def test_duplicate_category_update_does_not_publish_twice(
    client: httpx.AsyncClient,
    test_app: tuple,
) -> None:
    _, factory = test_app
    seed_categories(factory)

    selected = await select_post_type(client, 2022, "latest")
    assert selected.status_code == 200

    await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(
            2023,
            message={"text": "One published article."},
        ),
    )

    category_payload = update(
        2024,
        message={"text": "Food & Juices"},
    )

    first = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=category_payload,
    )

    duplicate = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=category_payload,
    )

    assert first.status_code == duplicate.status_code == 200

    with factory() as db:
        assert db.scalar(select(func.count(Post.id))) == 1

        post = db.scalar(select(Post))
        assert post is not None
        assert post.status == PostStatus.PUBLISHED

        assert db.scalar(
            select(func.count(TelegramUpdate.id))
        ) == 3


async def test_database_failure_while_publishing_rolls_back(
    client: httpx.AsyncClient,
    test_app: tuple,
    api_stub,
) -> None:
    _, factory = test_app
    seed_categories(factory)

    selected = await select_post_type(client, 2025, "latest")
    assert selected.status_code == 200

    await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=update(
            2026,
            message={"text": "Publish only after category."},
        ),
    )

    def failing_db() -> Iterator[Session]:
        with factory() as db:

            def fail_commit() -> None:
                raise OperationalError(
                    "COMMIT",
                    {},
                    RuntimeError("database unavailable"),
                )

            db.commit = fail_commit  # type: ignore[method-assign]
            yield db

    app.dependency_overrides[get_db] = failing_db

    try:
        response = await client.post(
            WEBHOOK,
            headers=SECRET_HEADER,
            json=update(
                2027,
                message={"text": "Nutrition Notes"},
            ),
        )

        assert response.status_code == 503

        sent_texts = [
            json.loads(call.content).get("text")
            for call in api_stub.calls
            if call.url.path.endswith("/sendMessage")
        ]

        assert (
            "Sorry, I couldn't save your article. Please try again."
            not in sent_texts
        )

        with factory() as db:
            post = db.scalar(select(Post))

            assert post is not None
            assert post.status == PostStatus.DRAFT
            assert post.category_id is None

            assert db.scalar(
                select(func.count(TelegramUpdate.id)).where(
                    TelegramUpdate.update_id == 2027
                )
            ) == 0
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_telegram_api_failure_retries_without_duplicate_draft(
    client: httpx.AsyncClient,
    test_app: tuple,
    api_stub,
) -> None:
    selected = await select_post_type(client, 2028, "latest")
    assert selected.status_code == 200

    api_stub.status_code = 500

    article_payload = update(
        2029,
        message={
            "text": "Education is very important for every child."
        },
    )

    response = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=article_payload,
    )

    assert response.status_code == 503

    api_stub.status_code = 200

    retry = await client.post(
        WEBHOOK,
        headers=SECRET_HEADER,
        json=article_payload,
    )

    assert retry.status_code == 200

    _, factory = test_app

    with factory() as db:
        assert db.scalar(select(func.count(Post.id))) == 1

        record = db.scalar(
            select(TelegramUpdate).where(
                TelegramUpdate.update_id == 2029
            )
        )

        assert record is not None
        assert record.post_id is not None


async def test_database_failure_during_post_type_selection(
    client: httpx.AsyncClient,
    test_app: tuple,
    api_stub,
) -> None:
    _, factory = test_app

    def failing_db() -> Iterator[Session]:
        with factory() as db:

            def fail_commit() -> None:
                raise OperationalError(
                    "COMMIT",
                    {},
                    RuntimeError("database unavailable"),
                )

            db.commit = fail_commit  # type: ignore[method-assign]
            yield db

    app.dependency_overrides[get_db] = failing_db

    try:
        response = await client.post(
            WEBHOOK,
            headers=SECRET_HEADER,
            json=post_type_selection(2030, "latest"),
        )

        assert response.status_code == 503

        with factory() as db:
            assert db.scalar(select(func.count(Post.id))) == 0
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_telegram_file_failure_does_not_log_bot_token(
    client: httpx.AsyncClient,
    test_app: tuple,
    api_stub,
    settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    selected = await select_post_type(client, 2031, "featured")
    assert selected.status_code == 200

    
    # Keep sendMessage working; fail only getFile.
    api_stub.fail_get_file = True

    try:
        response = await client.post(
            WEBHOOK,
            headers=SECRET_HEADER,
            json=update(
                2032,
                message={
                    "caption": "A photo whose download will fail.",
                    "photo": [
                        {"file_id": "photo-file-id"}
                    ],
                },
            ),
        )

        assert response.status_code == 503
        assert (
            settings.telegram_bot_token.get_secret_value()
            not in caplog.text
        )

        _, factory = test_app

        with factory() as db:
            assert db.scalar(select(func.count(Post.id))) == 1
    finally:
        api_stub.fail_get_file = False


async def test_telegram_service_hides_bot_token_on_api_error(
    settings,
) -> None:
    async def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "ok": False,
                "description": "failure",
            },
        )

    service = TelegramService(
        settings,
        transport=httpx.MockTransport(fail),
    )

    with pytest.raises(TelegramAPIError) as error:
        await service.send_message(42, "test")

    assert (
        settings.telegram_bot_token.get_secret_value()
        not in str(error.value)
    )


async def test_telegram_service_wraps_get_file_and_set_webhook(
    settings,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)

        if request.method == "GET":
            return httpx.Response(200, content=b"image bytes")

        result = (
            {"file_path": "photos/example.jpg"}
            if request.url.path.endswith("/getFile")
            else True
        )

        return httpx.Response(
            200,
            json={"ok": True, "result": result},
        )

    file_service = TelegramService(
        settings,
        transport=httpx.MockTransport(respond),
    )

    file_result = await file_service.get_file("telegram-file-id")
    assert file_result == {"file_path": "photos/example.jpg"}

    assert (
        await file_service.download_file("photos/example.jpg")
        == b"image bytes"
    )

    webhook_service = TelegramService(
        settings,
        transport=httpx.MockTransport(respond),
    )

    await webhook_service.set_webhook(
        "https://example-tunnel.test/api/telegram/webhook",
        "a-random-webhook-secret",
    )

    assert requests[0].url.path.endswith("/getFile")
    assert json.loads(requests[0].content) == {
        "file_id": "telegram-file-id"
    }

    assert requests[1].url.path.endswith("/photos/example.jpg")
    assert requests[2].url.path.endswith("/setWebhook")

    webhook_payload = json.loads(requests[2].content)

    assert webhook_payload["secret_token"] == "a-random-webhook-secret"
    assert webhook_payload["url"] == (
        "https://example-tunnel.test/api/telegram/webhook"
    )
