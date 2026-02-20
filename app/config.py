from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    database_url: str = 'postgresql+psycopg://postgres:postgres@localhost:5432/blind_inventory'
    app_secret_key: str = 'change-me'
    session_cookie_name: str = 'blind_inventory_session'
    session_ttl_minutes: int = 30
    session_cookie_secure: bool = False
    session_cookie_samesite: str = 'lax'

    snapshot_provider: str = 'mock'

    square_access_token: str | None = None
    square_application_id: str | None = None
    square_api_base_url: str = 'https://connect.squareup.com'
    square_api_version: str | None = None
    square_timeout_seconds: int = 30
    square_read_only: bool = True

    @property
    def database_url_normalized(self) -> str:
        url = self.database_url.strip()
        if url.startswith('postgres://'):
            return 'postgresql+psycopg://' + url[len('postgres://') :]
        if url.startswith('postgresql://'):
            return 'postgresql+psycopg://' + url[len('postgresql://') :]
        return url


settings = Settings()
