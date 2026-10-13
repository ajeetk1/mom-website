from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: SecretStr
    admin_api_key: SecretStr | None = None
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_allowed_user_id: int | None = Field(default=None, gt=0)

    #Cloudinary image storage
    cloudinary_cloud_name: str | None = None
    cloudinary_api_key: str | None = None
    cloudinary_api_secret: SecretStr | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def telegram_is_configured(self) -> bool:
        token = (
            self.telegram_bot_token.get_secret_value().strip()
            if self.telegram_bot_token is not None
            else ""
        )
        secret = (
            self.telegram_webhook_secret.get_secret_value().strip()
            if self.telegram_webhook_secret is not None
            else ""
        )
        return bool(token and secret and self.telegram_allowed_user_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
