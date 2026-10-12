import logging
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.auth import require_admin
from app.models.category import Category
from app.models.post import Post, PostStatus
from app.schemas.post import PostCreate, PostListResponse, PostResponse, PostUpdate
from app.services.article import create_article, update_article

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/posts", tags=["posts"])


@router.get("", response_model=PostListResponse)
def list_posts(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=12, ge=1, le=100),
    category: str | None = Query(default=None, min_length=1),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    statement = select(Post).options(joinedload(Post.category)).where(
        Post.status == PostStatus.PUBLISHED
    )
    count_statement = select(func.count(Post.id)).where(Post.status == PostStatus.PUBLISHED)

    if category is not None:
        category_record = db.scalar(select(Category).where(Category.slug == category))
        if category_record is None:
            raise HTTPException(status_code=404, detail="Category not found")
        statement = statement.where(Post.category_id == category_record.id)
        count_statement = count_statement.where(Post.category_id == category_record.id)

    total = db.scalar(count_statement) or 0
    posts = db.scalars(
        statement.order_by(Post.published_at.desc(), Post.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    return {
        "items": posts,
        "page": page,
        "limit": limit,
        "total": total,
        "pages": ceil(total / limit),
    }


@router.get("/{slug}", response_model=PostResponse)
def get_post(slug: str, db: Session = Depends(get_db)) -> Post:
    post = db.scalar(
        select(Post)
        .options(joinedload(Post.category))
        .where(Post.slug == slug, Post.status == PostStatus.PUBLISHED)
    )
    if post is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return post


@router.post("", response_model=PostResponse, status_code=status.HTTP_201_CREATED)
def create_post(
    payload: PostCreate,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Post:
    try:
        post = create_article(db, payload)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _slug_exists(db, payload.slug):
            raise HTTPException(status_code=409, detail="An article with this slug already exists") from exc
        logger.exception("Database constraint failed while creating an article")
        raise HTTPException(status_code=400, detail="Article could not be created") from exc
    db.refresh(post)
    return _load_post(db, post.id)


@router.put("/{post_id}", response_model=PostResponse)
def update_post(
    post_id: int,
    payload: PostUpdate,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Post:
    existing_post = db.get(Post, post_id)
    if existing_post is None:
        raise HTTPException(status_code=404, detail="Article not found")
    slug_to_check = payload.slug or existing_post.slug
    try:
        update_article(db, post_id, payload)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _slug_exists(db, slug_to_check):
            raise HTTPException(status_code=409, detail="An article with this slug already exists") from exc
        logger.exception("Database constraint failed while updating article %s", post_id)
        raise HTTPException(status_code=400, detail="Article could not be updated") from exc
    return _load_post(db, post_id)


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_admin),
) -> Response:
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Article not found")
    db.delete(post)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _slug_exists(db: Session, slug: str) -> bool:
    return db.scalar(select(Post.id).where(Post.slug == slug)) is not None


def _load_post(db: Session, post_id: int) -> Post:
    post = db.scalar(select(Post).options(joinedload(Post.category)).where(Post.id == post_id))
    if post is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return post
