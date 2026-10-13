import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.categories import CATEGORY_NAME_SLUGS, LEGACY_POST_CATEGORY_SLUG
from app.database import Base, SessionLocal, engine
from app.models import Category
from app.models.post import Post
from app.routes.categories import router as categories_router
from app.routes.posts import router as posts_router
from app.routes.telegram import router as telegram_router
from app.services.uploads import UPLOAD_DIRECTORY

logger = logging.getLogger(__name__)

DEFAULT_CATEGORIES = CATEGORY_NAME_SLUGS


def initialize_database() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        for name, slug in DEFAULT_CATEGORIES:
            category = db.scalar(select(Category).where(Category.slug == slug))
            if category is None:
                db.add(Category(name=name, slug=slug))
            else:
                category.name = name
        db.flush()

        obsolete_categories = list(
            db.scalars(
                select(Category).where(
                    Category.slug.not_in(
                        [slug for _, slug in DEFAULT_CATEGORIES]
                    )
                )
            )
        )
        if obsolete_categories:
            fallback_category_id = db.scalar(
                select(Category.id).where(
                    Category.slug == LEGACY_POST_CATEGORY_SLUG
                )
            )
            if fallback_category_id is None:
                raise RuntimeError("Legacy-post fallback category was not initialized")

            obsolete_category_ids = [category.id for category in obsolete_categories]
            db.execute(
                update(Post)
                .where(Post.category_id.in_(obsolete_category_ids))
                .values(category_id=fallback_category_id)
            )
            for category in obsolete_categories:
                db.delete(category)
        db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="Notes From Mom API",
    description="Article and category API for the Notes From Mom website.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.exception_handler(SQLAlchemyError)
async def handle_database_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.error(
        "Database operation failed",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "The database operation could not be completed."},
    )


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(posts_router)
app.include_router(categories_router)
app.include_router(telegram_router)
UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)

app.mount(
    "/uploads",
    StaticFiles(directory=str(UPLOAD_DIRECTORY)),
    name="uploads",
)
