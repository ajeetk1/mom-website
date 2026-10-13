
import httpx
import pytest
from sqlalchemy import select

from app.models.category import Category
from app.models.post import Post

pytestmark = pytest.mark.asyncio


async def test_rest_create_and_update_still_use_article_service(
    client: httpx.AsyncClient,
    test_app: tuple,
) -> None:
    _, factory = test_app

    with factory() as db:
        category = Category(
            name="Nutrition Notes",
            slug="nutrition-notes",
        )
        db.add(category)
        db.commit()
        category_id = category.id

    created = await client.post(
        "/api/posts",
        headers={"X-Admin-Api-Key": "test-admin-key"},
        json={
            "title": "Nutrition for every family",
            "slug": "nutrition-for-every-family",
            "content": "Article text.",
            "category_id": category_id,
            "status": "draft",
        },
    )

    assert created.status_code == 201, created.text

    created_json = created.json()
    assert created_json["category"]["slug"] == "nutrition-notes"

    post_id = created_json["id"]

    updated = await client.put(
        f"/api/posts/{post_id}",
        headers={"X-Admin-Api-Key": "test-admin-key"},
        json={"title": "A better title"},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "A better title"

    with factory() as db:
        post = db.scalar(
            select(Post).where(Post.id == post_id)
        )

        assert post is not None
        assert post.title == "A better title"


async def test_public_post_list_still_only_returns_published_articles(
    client: httpx.AsyncClient,
    test_app: tuple,
) -> None:
    _, factory = test_app

    with factory() as db:
        category = Category(
            name="Stories & Reflections",
            slug="stories-reflections",
        )
        db.add(category)
        db.flush()

        db.add_all(
            [
                Post(
                    title="Published",
                    slug="published-article",
                    content="Public",
                    category_id=category.id,
                    status="published",
                ),
                Post(
                    title="Draft",
                    slug="draft-article",
                    content="Private draft",
                    category_id=None,
                    status="draft",
                ),
            ]
        )
        db.commit()

    response = await client.get("/api/posts")

    assert response.status_code == 200
    assert [
        item["slug"] for item in response.json()["items"]
    ] == ["published-article"]
