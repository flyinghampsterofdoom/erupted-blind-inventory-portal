from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    performance_metrics_enabled: bool = False
    performance_sample_rate: float = 0.1

    database_url: str = 'postgresql+psycopg://postgres:postgres@localhost:5432/blind_inventory'
    environment: str = 'production'
    demo_seed_enabled: bool = False
    schema_revision_check_enabled: bool = True
    v2_enabled_features: str = ''
    v2_principal_features: str = ''
    v2_consignment_cogs_actions_enabled: bool = False
    v2_credit_card_cogs_actions_enabled: bool = False
    integration_encryption_key: str | None = None
    app_secret_key: str = 'change-me'
    session_cookie_name: str = 'blind_inventory_session'
    session_ttl_minutes: int = 60
    session_cookie_secure: bool = False
    session_cookie_samesite: str = 'lax'

    r2_endpoint_url: str | None = None
    r2_bucket_name: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_region: str = 'auto'
    # Shared web/cron authority. Filesystem mode is restricted to local/test.
    purchase_order_pdf_storage: str = 'r2'
    purchase_order_pdf_local_directory: str | None = None
    purchase_order_pdf_cache_directory: str | None = None
    digital_signage_max_upload_bytes: int = 15 * 1024 * 1024
    digital_signage_max_active_displays: int = 9
    digital_signage_display_session_ttl_days: int = 30
    digital_signage_business_timezone: str = 'America/Los_Angeles'
    touchscreen_max_upload_bytes: int = 15 * 1024 * 1024
    touchscreen_cache_max_age_minutes: int = 30
    touchscreen_default_inventory_threshold: int = 0
    touchscreen_inactivity_minutes: int = 5

    snapshot_provider: str = 'mock'

    square_access_token: str | None = None
    square_application_id: str | None = None
    square_api_base_url: str = 'https://connect.squareup.com'
    square_api_version: str | None = None
    square_timeout_seconds: int = 30
    square_read_only: bool = True

    # Local Phase 1 authority is opt-in; deployment/production activation is separate.
    purchasing_v3_phase1_enabled: bool = False
    purchasing_v3_phase2_enabled: bool = False
    purchasing_monday_payments_enabled: bool = False
    purchasing_card1_account_id: int | None = None
    purchasing_card2_account_id: int | None = None
    purchasing_owner_principal_id: int | None = None
    purchasing_big_square_vendor_id: str | None = None
    purchasing_streamline_square_vendor_id: str | None = None

    ordering_reorder_weeks_default: int = 5
    ordering_stock_up_weeks_default: int = 10
    ordering_history_lookback_days_default: int = 120

    @property
    def database_url_normalized(self) -> str:
        url = self.database_url.strip()
        if url.startswith('postgres://'):
            return 'postgresql+psycopg://' + url[len('postgres://') :]
        if url.startswith('postgresql://'):
            return 'postgresql+psycopg://' + url[len('postgresql://') :]
        return url

    @property
    def environment_normalized(self) -> str:
        return self.environment.strip().lower()


settings = Settings()
