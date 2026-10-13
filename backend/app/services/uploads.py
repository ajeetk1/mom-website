import re
from pathlib import Path
from uuid import uuid4

import cloudinary
import cloudinary.uploader

from app.config import get_settings


UPLOAD_DIRECTORY = Path(__file__).resolve().parents[2] / "uploads"

settings = get_settings()

cloudinary.config(
    cloud_name=settings.cloudinary_cloud_name,
    api_key=settings.cloudinary_api_key,
    api_secret=(
        settings.cloudinary_api_secret.get_secret_value()
        if settings.cloudinary_api_secret is not None
        else None
    ),
    secure=True,
)


def save_telegram_image(
    content: bytes,
    file_path: str,
) -> tuple[str, None]:
    suffix = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "jpg"

    if not re.fullmatch(r"[a-z0-9]{1,10}", suffix):
        suffix = "jpg"

    result = cloudinary.uploader.upload(
        content,
        public_id=f"notes-from-mom/{uuid4().hex}",
        resource_type="image",
        format=suffix,
    )

    secure_url = result.get("secure_url")

    if not isinstance(secure_url, str) or not secure_url.startswith("https://"):
        raise RuntimeError("Cloudinary did not return a valid image URL")

    return secure_url, None
