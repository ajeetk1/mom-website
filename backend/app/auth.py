from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_admin(x_admin_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    configured_key = settings.admin_api_key

    if configured_key is None or not configured_key.get_secret_value().strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured",
        )

    if x_admin_api_key is None or x_admin_api_key != configured_key.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin API key",
        )