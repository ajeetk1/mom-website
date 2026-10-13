
import hmac
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from cloudinary.exceptions import Error as CloudinaryError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.categories import CATEGORY_NAMES
from app.config import Settings, get_settings
from app.database import get_db
from app.models.category import Category
from app.models.post import Post, PostStatus, PostType
from app.models.telegram_update import TelegramUpdate
from app.schemas.telegram import (
    TelegramDocument,
    TelegramMessage,
    TelegramUpdate as TelegramUpdatePayload,
)
from app.services.article import create_draft
from app.services.telegram import TelegramAPIError, TelegramService
from app.services.uploads import save_telegram_image


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/telegram",
    tags=["telegram"],
)


# =========================================================
# Messages
# =========================================================

WELCOME_MESSAGE = (
    "Welcome! 👋\n\n"
    "What would you like to publish today?"
)

HELP_MESSAGE = (
    "Welcome! 👋\n\n"
    "Choose a post type to publish a new article.\n\n"
    "Available commands:\n"
    "/start — create a new post\n"
    "/latest — show recent articles\n"
    "/edit — edit an article\n"
    "/delete — delete an article"
)

EMPTY_MESSAGE = (
    "Please send the text for your article."
)

PHOTO_CAPTION_REQUIRED = (
    "Please send the photo again with a short caption. "
    "The caption will become the Featured post title."
)

SAVE_FAILED_MESSAGE = (
    "Sorry, I couldn't save your article. Please try again."
)

IMAGE_SAVE_FAILED_MESSAGE = (
    "Sorry, I couldn't save your photo. Please try again."
)

NO_PENDING_MESSAGE = (
    "I couldn't find an article waiting for a category. "
    "Please choose a post type using /start."
)

CATEGORY_MESSAGE = (
    "Which category should this article belong to?"
)

CATEGORY_INVALID_MESSAGE = (
    "Please select one of the category buttons below."
)

LATEST_EMPTY_MESSAGE = (
    "There are no published articles yet."
)

DELETE_USAGE_MESSAGE = (
    "To delete an article:\n\n"
    "/delete POST_ID\n\n"
    "Example: /delete 19\n\n"
    "I will ask you to confirm before archiving it."
)

EDIT_USAGE_MESSAGE = (
    "To edit an article:\n\n"
    "/edit POST_ID category CATEGORY\n"
    "/edit POST_ID title NEW TITLE\n"
    "/edit POST_ID text NEW ARTICLE TEXT\n\n"
    "Examples:\n"
    "/edit 19 category Mind & Mood\n"
    "/edit 19 title My new title\n"
    "/edit 19 text My new article text"
)

FEATURED_PLACEHOLDER = "New Featured Article"
LATEST_PLACEHOLDER = "New Latest Article"
ARCHIVE_PLACEHOLDER = "New Image with Text Post"


# =========================================================
# Telegram service
# =========================================================

def get_telegram_service(
    settings: Settings = Depends(get_settings),
) -> TelegramService:
    return TelegramService(settings)


# =========================================================
# Keyboards
# =========================================================

def _post_type_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "⭐ Featured",
                    "callback_data": "post_type:featured",
                }
            ],
            [
                {
                    "text": "📝 Latest — Text Only",
                    "callback_data": "post_type:latest",
                }
            ],
            [
                {
                    "text": "🖼️ Image with Text",
                    "callback_data": "post_type:archive",
                }
            ],
        ]
    }


def _category_keyboard(
    db: Session,
) -> dict[str, Any]:
    categories = db.scalars(
        select(Category).order_by(Category.name)
    ).all()

    buttons = [
        {
            "text": category.name,
            "callback_data": f"category:{category.id}",
        }
        for category in categories
    ]

    rows = [
        buttons[index:index + 2]
        for index in range(0, len(buttons), 2)
    ]

    return {"inline_keyboard": rows}


# =========================================================
# Main webhook
# =========================================================

@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    telegram: TelegramService = Depends(get_telegram_service),
) -> dict[str, bool]:

    # Verify webhook secret.
    provided_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        "",
    )

    expected_secret = (
        settings.telegram_webhook_secret.get_secret_value()
        if settings.telegram_webhook_secret is not None
        else ""
    )

    print("Webhook secret configured:", bool(expected_secret))
    print("Webhook secret matches:", bool(provided_secret) and hmac.compare_digest(provided_secret.encode("utf-8"), expected_secret.encode("utf-8")))

    if not expected_secret or not hmac.compare_digest(
        provided_secret.encode("utf-8"),
        expected_secret.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=403,
            detail="Invalid webhook credentials",
        )

    if not settings.telegram_is_configured:
        raise HTTPException(
            status_code=503,
            detail="Telegram webhook is not configured",
        )

    # Validate update.
    try:
        raw_payload = await request.json()
        payload = TelegramUpdatePayload.model_validate(raw_payload)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid Telegram update",
        ) from exc

    # -----------------------------------------------------
    # Inline-button callback
    # -----------------------------------------------------

    callback = payload.callback_query

    if callback is not None:
        sender_id = callback.from_user.id

        if sender_id != settings.telegram_allowed_user_id:
            raise HTTPException(
                status_code=403,
                detail="Unauthorized Telegram user",
            )

        callback_message = callback.message
        chat_id = (
            callback_message.chat.id
            if callback_message is not None
            and callback_message.chat is not None
            else sender_id
        )

        # Stop the Telegram button's loading indicator.
        try:
            await telegram.answer_callback_query(callback.id)
        except TelegramAPIError:
            logger.warning(
                "Could not acknowledge callback for update %s",
                payload.update_id,
            )

        # Do not process the same button click twice.
        if _update_exists(db, payload.update_id):
            return {"ok": True}

        data = callback.data or ""

        if data.startswith("post_type:"):
            value = data.partition(":")[2]

            post_types = {
                "featured": PostType.FEATURED,
                "latest": PostType.LATEST,
                "archive": PostType.ARCHIVE,
            }

            selected_type = post_types.get(value)

            if selected_type is None:
                return await _record_and_reply(
                    db,
                    payload.update_id,
                    telegram,
                    chat_id,
                    "Please use /start and choose a valid post type.",
                )

            return await _handle_post_type_selection(
                db=db,
                update_id=payload.update_id,
                telegram=telegram,
                chat_id=chat_id,
                post_type=selected_type,
            )

        if data.startswith("category:"):
            category_id = data.partition(":")[2]

            if not category_id.isdigit():
                return await _record_and_reply(
                    db,
                    payload.update_id,
                    telegram,
                    chat_id,
                    CATEGORY_INVALID_MESSAGE,
                    reply_markup=_category_keyboard(db),
                )

            category = db.get(Category, int(category_id))

            if category is None:
                return await _record_and_reply(
                    db,
                    payload.update_id,
                    telegram,
                    chat_id,
                    "That category is no longer available. Please choose again.",
                    reply_markup=_category_keyboard(db),
                )

            return await _handle_category(
                db=db,
                update_id=payload.update_id,
                telegram=telegram,
                chat_id=chat_id,
                category=category,
            )

        return await _record_and_reply(
            db,
            payload.update_id,
            telegram,
            chat_id,
            "I didn't recognize that button. Please use /start.",
        )

    # -----------------------------------------------------
    # Ordinary message
    # -----------------------------------------------------

    message = payload.message

    if message is None:
        return {"ok": True}

    sender_id = (
        message.from_user.id
        if message.from_user is not None
        else None
    )

    if sender_id != settings.telegram_allowed_user_id:
        raise HTTPException(
            status_code=403,
            detail="Unauthorized Telegram user",
        )

    chat_id = _chat_id(message, sender_id)

    if chat_id is None:
        return {"ok": True}

    if _update_exists(db, payload.update_id):
        return await _retry_confirmation(
            db,
            payload.update_id,
            telegram,
            chat_id,
        )

    raw_message = (
        raw_payload.get("message")
        if isinstance(raw_payload, dict)
        else None
    )

    media_fields = (
        [
            field
            for field in (
                "photo",
                "document",
                "video",
                "animation",
                "sticker",
            )
            if field in raw_message
        ]
        if isinstance(raw_message, dict)
        else []
    )

    logger.info(
        "Authorized Telegram update %s media fields: %s; "
        "caption_present=%s",
        payload.update_id,
        ",".join(media_fields) or "none",
        message.caption is not None,
    )

    # Commands take priority over draft handling.
    command = _command(message.text)

    if command == "/start":
        return await _record_and_reply(
            db,
            payload.update_id,
            telegram,
            chat_id,
            WELCOME_MESSAGE,
            reply_markup=_post_type_keyboard(),
        )

    if command == "/help":
        return await _record_and_reply(
            db,
            payload.update_id,
            telegram,
            chat_id,
            HELP_MESSAGE,
            reply_markup=_post_type_keyboard(),
        )

    if command == "/latest":
        return await _handle_latest(
            db=db,
            update_id=payload.update_id,
            telegram=telegram,
            chat_id=chat_id,
        )

    if command == "/delete":
        return await _handle_delete_command(
            db=db,
            update_id=payload.update_id,
            telegram=telegram,
            chat_id=chat_id,
            text=message.text or "",
        )

    if command == "/edit":
        return await _handle_edit_command(
            db=db,
            update_id=payload.update_id,
            telegram=telegram,
            chat_id=chat_id,
            text=message.text or "",
        )

    if command is not None:
        return await _record_and_reply(
            db,
            payload.update_id,
            telegram,
            chat_id,
            HELP_MESSAGE,
            reply_markup=_post_type_keyboard(),
        )

    # Find the current draft, if any.
    pending_post = _get_pending_post(db)

    if pending_post is None:
        return await _record_and_reply(
            db,
            payload.update_id,
            telegram,
            chat_id,
            "Let's create a post first. Tap /start to choose a post type.",
            reply_markup=_post_type_keyboard(),
        )

    # The draft already has text, so it is waiting for a category.
    if pending_post.content.strip():
        category = _find_category(db, message.text)

        if category is not None:
            return await _handle_category(
                db=db,
                update_id=payload.update_id,
                telegram=telegram,
                chat_id=chat_id,
                category=category,
            )

        return await _record_and_reply(
            db,
            payload.update_id,
            telegram,
            chat_id,
            CATEGORY_INVALID_MESSAGE + "\nChoose a category:",
            reply_markup=_category_keyboard(db),
        )

    # -----------------------------------------------------
    # Latest: text-only post
    # -----------------------------------------------------

    if pending_post.post_type == PostType.LATEST:
        if message.text is None or not message.text.strip():
            return await _record_and_reply(
                db,
                payload.update_id,
                telegram,
                chat_id,
                "For a Latest post, please send text only. No image is needed.",
            )

        article_text = message.text.strip()
        pending_post.title = _title_from_text(article_text)
        pending_post.content = article_text

        return await _save_post_step(
            db=db,
            update_id=payload.update_id,
            post=pending_post,
            telegram=telegram,
            chat_id=chat_id,
            reply=CATEGORY_MESSAGE,
            reply_markup=_category_keyboard(db),
        )

    # -----------------------------------------------------
    # Featured and Image with Text: image first
    # -----------------------------------------------------

    file_id = _message_image_file_id(message)

    if pending_post.featured_image_url is None:
        if file_id is None:
            expected = (
                "For Featured, send a photo with a short caption/title."
                if pending_post.post_type == PostType.FEATURED
                else "For Image with Text, send a photo first. A caption is optional."
            )

            return await _record_and_reply(
                db,
                payload.update_id,
                telegram,
                chat_id,
                expected,
            )

        caption = (
            message.caption.strip()
            if message.caption and message.caption.strip()
            else None
        )

        if (
            pending_post.post_type == PostType.FEATURED
            and caption is None
        ):
            return await _record_and_reply(
                db,
                payload.update_id,
                telegram,
                chat_id,
                PHOTO_CAPTION_REQUIRED,
            )

        try:
            file_info = await telegram.get_file(file_id)
            telegram_file_path = file_info.get("file_path")

            if not isinstance(telegram_file_path, str):
                raise TelegramAPIError(
                    "Telegram returned an invalid file response"
                )

            image_content = await telegram.download_file(
                telegram_file_path
            )

            image_url, saved_image_path = save_telegram_image(
                image_content,
                telegram_file_path,
            )

        except (TelegramAPIError, OSError, CloudinaryError):
            logger.exception(
                "Could not save Telegram image for update %s",
                payload.update_id,
            )

            await _send_image_save_failure(
                telegram,
                chat_id,
                payload.update_id,
            )

            raise HTTPException(
                status_code=503,
                detail="The photo could not be saved",
            ) from None

        
        pending_post.featured_image_url = image_url
        pending_post.telegram_file_id = file_id

        if pending_post.post_type == PostType.ARCHIVE:
            pending_post.title = ARCHIVE_PLACEHOLDER
            pending_post.content = ""
        elif caption:
            pending_post.title = caption[:255]

        try:
            _record_update(db, payload.update_id, pending_post.id)
            db.commit()

        except SQLAlchemyError:
            db.rollback()

            if saved_image_path is not None:
                saved_image_path.unlink(missing_ok=True)

            await _send_save_failure(
                telegram,
                chat_id,
                payload.update_id,
            )

            raise HTTPException(
                status_code=503,
                detail="The photo could not be saved to the draft",
            ) from None

        
        if pending_post.post_type in (
            PostType.ARCHIVE,
            PostType.FEATURED,
        ):
            return await _save_post_step(
                db=db,
                update_id=payload.update_id,
                post=pending_post,
                telegram=telegram,
                chat_id=chat_id,
                reply=CATEGORY_MESSAGE,
                reply_markup=_category_keyboard(db),
            )

        prompt = (
            "Great! 👍\n\nNow send me the article text."
        )

        try:
            await telegram.send_message(chat_id, prompt)
        except TelegramAPIError:
            logger.warning(
                "Image saved but prompt failed for update %s",
                payload.update_id,
            )

        return {"ok": True}

    # Image is already attached; now expect the article body.
    if message.text is None or not message.text.strip():
        return await _record_and_reply(
            db,
            payload.update_id,
            telegram,
            chat_id,
            EMPTY_MESSAGE,
        )

    article_text = message.text.strip()
    pending_post.content = article_text

    # Image with Text may not have had a caption.
    if (
        pending_post.post_type == PostType.ARCHIVE
        and pending_post.title == ARCHIVE_PLACEHOLDER
    ):
        pending_post.title = _title_from_text(article_text)

    return await _save_post_step(
        db=db,
        update_id=payload.update_id,
        post=pending_post,
        telegram=telegram,
        chat_id=chat_id,
        reply=CATEGORY_MESSAGE,
        reply_markup=_category_keyboard(db),
    )


# =========================================================
# Post type selection
# =========================================================

async def _handle_post_type_selection(
    *,
    db: Session,
    update_id: int,
    telegram: TelegramService,
    chat_id: int,
    post_type: PostType,
) -> dict[str, bool]:

    existing = _get_pending_post(db)

    if existing is not None:
        return await _record_and_reply(
            db,
            update_id,
            telegram,
            chat_id,
            (
                "You already have a draft in progress. "
                "Please finish it before starting another post."
            ),
        )

    placeholders = {
        PostType.FEATURED: FEATURED_PLACEHOLDER,
        PostType.LATEST: LATEST_PLACEHOLDER,
        PostType.ARCHIVE: ARCHIVE_PLACEHOLDER,
    }

    try:
        post = create_draft(
            db,
            title=placeholders[post_type],
            content="",
            slug_seed=f"telegram-{post_type.value}-{update_id}",
            post_type=post_type,
        )

        _record_update(db, update_id, post.id)
        db.commit()

    except SQLAlchemyError:
        db.rollback()
        logger.exception(
            "Could not create Telegram draft for update %s",
            update_id,
        )

        await _send_save_failure(telegram, chat_id, update_id)

        raise HTTPException(
            status_code=503,
            detail="The draft could not be created",
        ) from None

    prompts = {
        PostType.FEATURED: (
            "⭐ Featured selected!\n\n"
            "Send a photo with a short caption. "
            "The caption becomes the post title. "
            "Then send the article text."
        ),
        PostType.LATEST: (
            "📝 Latest selected!\n\n"
            "Send the complete text in one message. "
            "The first line will be used as the title."
        ),
        PostType.ARCHIVE: (
            "🖼️ From the Archive selected!\n\n"
            "Send an image only. No text or caption is needed."
        ),
    }

    try:
        await telegram.send_message(chat_id, prompts[post_type])
    except TelegramAPIError:
        logger.warning(
            "Draft created but prompt could not be sent for update %s",
            update_id,
        )

    return {"ok": True}


# =========================================================
# Save a draft step and ask for the next step
# =========================================================

async def _save_post_step(
    *,
    db: Session,
    update_id: int,
    post: Post,
    telegram: TelegramService,
    chat_id: int,
    reply: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, bool]:

    try:
        _record_update(db, update_id, post.id)
        db.commit()

    except SQLAlchemyError:
        db.rollback()
        logger.exception(
            "Could not save draft step for update %s",
            update_id,
        )

        await _send_save_failure(telegram, chat_id, update_id)

        raise HTTPException(
            status_code=503,
            detail="The article could not be saved",
        ) from None

    
    try:
        await telegram.send_message(
            chat_id,
            reply,
            reply_markup=reply_markup,
        )
    except TelegramAPIError:
        logger.exception(
            "Could not send Telegram response for update %s",
            update_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Could not send Telegram response",
        ) from None

    return {"ok": True}



def _record_update(
    db: Session,
    update_id: int,
    post_id: int | None = None,
) -> None:
    db.add(
        TelegramUpdate(
            update_id=update_id,
            post_id=post_id,
        )
    )
    db.flush()


# =========================================================
# /latest
# =========================================================

async def _handle_latest(
    *,
    db: Session,
    update_id: int,
    telegram: TelegramService,
    chat_id: int,
) -> dict[str, bool]:

    posts = db.scalars(
        select(Post)
        .where(Post.status == PostStatus.PUBLISHED)
        .order_by(Post.published_at.desc(), Post.id.desc())
        .limit(10)
    ).all()

    if not posts:
        return await _record_and_reply(
            db, update_id, telegram, chat_id, LATEST_EMPTY_MESSAGE
        )

    lines = ["Recent published articles:\n"]

    for post in posts:
        category = (
            db.get(Category, post.category_id)
            if post.category_id is not None
            else None
        )
        category_name = (
            category.name if category is not None else "Uncategorized"
        )
        lines.append(
            f"#{post.id} — {post.title}\n"
            f"Category: {category_name}\n"
        )

    return await _record_and_reply(
        db, update_id, telegram, chat_id, "\n".join(lines)
    )


# =========================================================
# /delete
# =========================================================

async def _handle_delete_command(
    *,
    db: Session,
    update_id: int,
    telegram: TelegramService,
    chat_id: int,
    text: str,
) -> dict[str, bool]:

    parts = text.strip().split(maxsplit=2)

    if len(parts) < 2 or not parts[1].isdigit():
        return await _record_and_reply(
            db, update_id, telegram, chat_id, DELETE_USAGE_MESSAGE
        )

    post_id = int(parts[1])
    post = db.get(Post, post_id)

    if post is None or post.status != PostStatus.PUBLISHED:
        return await _record_and_reply(
            db,
            update_id,
            telegram,
            chat_id,
            f"I couldn't find published article #{post_id}.",
        )

    if len(parts) == 2:
        return await _record_and_reply(
            db,
            update_id,
            telegram,
            chat_id,
            (
                "⚠️ Please confirm archiving this article.\n\n"
                f"#{post.id} — {post.title}\n\n"
                f"To confirm, send:\n/delete {post.id} confirm"
            ),
        )

    if parts[2].casefold() != "confirm":
        return await _record_and_reply(
            db,
            update_id,
            telegram,
            chat_id,
            f"Deletion was not confirmed.\n\n/delete {post.id} confirm",
        )

    image_path: Path | None = None

    if post.featured_image_url:
        image_path = (
            Path(__file__).resolve().parents[2]
            / "uploads"
            / Path(post.featured_image_url).name
        )

    try:
        post.status = PostStatus.ARCHIVED
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Failed to archive post %s", post_id)
        await _send_save_failure(telegram, chat_id, update_id)
        raise HTTPException(
            status_code=503,
            detail="The article could not be archived",
        ) from None

    if image_path is not None:
        try:
            image_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove image for post %s", post_id)

    return await _record_and_reply(
        db,
        update_id,
        telegram,
        chat_id,
        f"Archived successfully. 🗑️\nArticle #{post.id} is no longer visible on the website.",
    )


# =========================================================
# /edit
# =========================================================

async def _handle_edit_command(
    *,
    db: Session,
    update_id: int,
    telegram: TelegramService,
    chat_id: int,
    text: str,
) -> dict[str, bool]:

    parts = text.strip().split(maxsplit=3)

    if len(parts) < 4 or not parts[1].isdigit():
        return await _record_and_reply(
            db, update_id, telegram, chat_id, EDIT_USAGE_MESSAGE
        )

    post_id = int(parts[1])
    field = parts[2].casefold()
    value = parts[3].strip()
    post = db.get(Post, post_id)

    if post is None or post.status != PostStatus.PUBLISHED:
        return await _record_and_reply(
            db,
            update_id,
            telegram,
            chat_id,
            f"I couldn't find published article #{post_id}.",
        )

    if not value:
        return await _record_and_reply(
            db, update_id, telegram, chat_id, EDIT_USAGE_MESSAGE
        )

    category: Category | None = None

    if field == "category":
        category = _find_category(db, value)

        if category is None:
            return await _record_and_reply(
                db,
                update_id,
                telegram,
                chat_id,
                CATEGORY_INVALID_MESSAGE,
                reply_markup=_category_keyboard(db),
            )

        post.category_id = category.id

    elif field == "title":
        post.title = value[:255]

    elif field == "text":
        post.content = value

    else:
        return await _record_and_reply(
            db, update_id, telegram, chat_id, EDIT_USAGE_MESSAGE
        )

    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Failed to edit post %s", post_id)
        await _send_save_failure(telegram, chat_id, update_id)
        raise HTTPException(
            status_code=503,
            detail="The article could not be edited",
        ) from None

    if field == "category" and category is not None:
        confirmation = (
            "Updated successfully! ✅\n\n"
            f"Article: {post.title}\nCategory: {category.name}"
        )
    elif field == "title":
        confirmation = f"Updated successfully! ✅\n\nNew title: {post.title}"
    else:
        confirmation = f"Updated successfully! ✅\n\nArticle #{post.id} text has been updated."

    return await _record_and_reply(
        db, update_id, telegram, chat_id, confirmation
    )


# =========================================================
# Category handling and publishing
# =========================================================

async def _handle_category(
    *,
    db: Session,
    update_id: int,
    telegram: TelegramService,
    chat_id: int,
    category: Category,
) -> dict[str, bool]:

    post = _get_pending_post(db)

    if post is None or (
    post.post_type != PostType.ARCHIVE
    and not post.content.strip()
    ):
        return await _record_and_reply(
            db,
            update_id,
            telegram,
            chat_id,
            NO_PENDING_MESSAGE,
            reply_markup=_post_type_keyboard(),
        )

    try:
        post.category_id = category.id
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime.now(UTC).replace(tzinfo=None)

        _record_update(db, update_id, post.id)
        db.commit()

    except SQLAlchemyError:
        db.rollback()
        logger.exception("Failed to publish post %s", post.id)
        await _send_save_failure(telegram, chat_id, update_id)
        raise HTTPException(
            status_code=503,
            detail="The article could not be published",
        ) from None

    try:
        await _send_published_confirmation(
            telegram, chat_id, post, category
        )
    except TelegramAPIError:
        logger.warning(
            "Post %s published, but confirmation failed",
            post.id,
        )

    return {"ok": True}


# =========================================================
# Generic Telegram reply
# =========================================================

async def _record_and_reply(
    db: Session,
    update_id: int,
    telegram: TelegramService,
    chat_id: int,
    reply: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, bool]:

    try:
        _record_update(db, update_id)
        db.commit()

    
    except IntegrityError:
        db.rollback()

        if _update_exists(db, update_id):
            await telegram.send_message(
                chat_id,
                reply,
                reply_markup=reply_markup,
            )
            return {"ok": True}

        await _send_save_failure(telegram, chat_id, update_id)
        raise HTTPException(
            status_code=503,
            detail="The update could not be processed",
        ) from None


    except SQLAlchemyError:
        db.rollback()
        await _send_save_failure(telegram, chat_id, update_id)
        raise HTTPException(
            status_code=503,
            detail="The update could not be processed",
        ) from None

    try:
        await telegram.send_message(
            chat_id,
            reply,
            reply_markup=reply_markup,
        )
    
    except TelegramAPIError:
        logger.exception(
            "Could not send Telegram response for update %s",
            update_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Could not send Telegram response",
        ) from None

    return {"ok": True}


# =========================================================
# Error handling
# =========================================================


async def _send_save_failure(
    telegram: TelegramService,
    chat_id: int,
    update_id: int,
) -> None:
    logger.warning(
        "Save failure occurred for update %s; generic failure message suppressed",
        update_id,
    )


async def _send_image_save_failure(
    telegram: TelegramService,
    chat_id: int,
    update_id: int,
) -> None:
    try:
        await telegram.send_message(chat_id, IMAGE_SAVE_FAILED_MESSAGE)
    except TelegramAPIError:
        logger.warning("Could not send image failure for update %s", update_id)


# =========================================================
# Idempotency and pending post
# =========================================================

def _update_exists(db: Session, update_id: int) -> bool:
    return (
        db.scalar(
            select(TelegramUpdate.id).where(
                TelegramUpdate.update_id == update_id
            )
        )
        is not None
    )


async def _retry_confirmation(
    db: Session,
    update_id: int,
    telegram: TelegramService,
    chat_id: int,
) -> dict[str, bool]:
    post_id = db.scalar(
        select(TelegramUpdate.post_id).where(
            TelegramUpdate.update_id == update_id
        )
    )

    if post_id is None:
        return {"ok": True}

    post = db.get(Post, post_id)

    if post is None:
        return {"ok": True}

    try:
        if post.status == PostStatus.PUBLISHED and post.category_id is not None:
            category = db.get(Category, post.category_id)
            if category is not None:
                await _send_published_confirmation(
                    telegram, chat_id, post, category
                )
    except TelegramAPIError:
        logger.warning(
            "Could not retry confirmation for update %s",
            update_id,
        )

    return {"ok": True}


async def _send_published_confirmation(
    telegram: TelegramService,
    chat_id: int,
    post: Post,
    category: Category,
) -> None:
    await telegram.send_message(
        chat_id,
        (
            "Published successfully! ✅\n\n"
            f"Title: {post.title}\n"
            f"Category: {category.name}\n"
            f"Post ID: {post.id}\n"
            "Status: published"
        ),
    )


def _get_pending_post(db: Session) -> Post | None:
    return db.scalar(
        select(Post)
        .join(TelegramUpdate, TelegramUpdate.post_id == Post.id)
        .where(
            Post.status == PostStatus.DRAFT,
            TelegramUpdate.post_id.is_not(None),
        )
        .order_by(TelegramUpdate.id.desc())
        .limit(1)
    )


# =========================================================
# Category lookup
# =========================================================

def _find_category(
    db: Session,
    text: str | None,
) -> Category | None:
    if text is None or not text.strip():
        return None

    value = text.strip()

    categories = {
        category.name.casefold(): category
        for category in db.scalars(select(Category))
    }

    if value.isdigit():
        number = int(value)
        if 1 <= number <= len(CATEGORY_NAMES):
            return categories.get(
                CATEGORY_NAMES[number - 1].casefold()
            )
        return None

    return categories.get(value.casefold())


# =========================================================
# Message helpers
# =========================================================

def _chat_id(
    message: TelegramMessage,
    sender_id: int,
) -> int | None:
    if message.chat is None:
        return sender_id
    return message.chat.id


def _command(text: str | None) -> str | None:
    if not text or not text.startswith("/"):
        return None

    first_word = text.split(maxsplit=1)[0]
    command, _, _bot_name = first_word.partition("@")
    return command


def _title_from_text(text: str) -> str:
    first_line = next(
        (line.strip() for line in text.splitlines() if line.strip()),
        text.strip(),
    )
    return first_line[:255] or "Untitled"


def _message_image_file_id(
    message: TelegramMessage,
) -> str | None:
    if message.photo:
        return message.photo[-1].file_id

    if _is_image_document(message.document):
        return message.document.file_id

    return None


def _is_image_document(
    document: TelegramDocument | None,
) -> bool:
    if document is None:
        return False

    image_extensions = {
        ".avif",
        ".bmp",
        ".gif",
        ".jpeg",
        ".jpg",
        ".png",
        ".tif",
        ".tiff",
        ".webp",
    }

    mime_type = (document.mime_type or "").casefold()
    extension = Path(document.file_name or "").suffix.casefold()

    return (
        mime_type.startswith("image/")
        and mime_type != "image/svg+xml"
    ) or extension in image_extensions
