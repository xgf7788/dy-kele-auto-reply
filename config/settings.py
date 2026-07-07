"""Configuration management using Pydantic Settings."""
import os
from pathlib import Path
from typing import List, Optional
from pydantic import Field, BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml


class StoreConfig(BaseModel):
    """Individual store configuration.

    Sensitive fields (cookies, phone) can be provided directly in the YAML
    or via environment variables for better security:

        cookies: "${COOKIES_STORE_001}"   # reads from env var
        phone: "${PHONE_STORE_002}"       # reads from env var
    """
    store_id: str
    name: str
    login_type: str = "cookie"  # cookie / qrcode / phone
    cookies: Optional[str] = None
    cookies_from_env: Optional[str] = None  # Env var name containing cookies
    username: Optional[str] = None
    password: Optional[str] = None
    phone: Optional[str] = None  # Phone number for SMS login
    phone_from_env: Optional[str] = None  # Env var name containing phone
    api_endpoint: str = "http://localhost:8000/api/chat/reply"
    api_timeout: int = 10
    api_key: Optional[str] = None
    api_key_from_env: Optional[str] = None  # Env var name containing API key
    reply_delay: float = 1.0
    enabled: bool = True
    max_concurrent: int = 5
    headless: bool = True
    im_url: Optional[str] = None  # Direct IM page URL
    page_refresh_interval: int = 0  # 页面自动刷新间隔（秒），0表示不自动刷新

    def model_post_init(self, __context) -> None:
        """Resolve env var references after model initialization."""
        import os

        # Resolve cookies from env var
        if self.cookies_from_env:
            env_val = os.getenv(self.cookies_from_env)
            if env_val:
                self.cookies = env_val
            else:
                import warnings
                warnings.warn(
                    f"Store '{self.store_id}': cookies_from_env='{self.cookies_from_env}' "
                    f"but env var is not set. Cookies will be empty."
                )

        # Resolve phone from env var
        if self.phone_from_env:
            env_val = os.getenv(self.phone_from_env)
            if env_val:
                self.phone = env_val

        # Resolve api_key from env var
        if self.api_key_from_env:
            env_val = os.getenv(self.api_key_from_env)
            if env_val:
                self.api_key = env_val


class Settings(BaseSettings):
    """Application settings."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Application
    app_name: str = "DY Kele Auto Reply"
    debug: bool = Field(default=False, alias="DEBUG")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Paths
    base_dir: Path = Path(__file__).parent.parent
    storage_dir: Path = base_dir / "storage"
    config_dir: Path = base_dir / "config"

    # Browser Pool - optimized for 24/7 operation
    max_browsers: int = Field(default=10, alias="MAX_BROWSERS")
    browser_timeout: int = 0  # 0 means no timeout (24/7 operation)
    navigation_timeout: int = 60000  # 60 seconds for navigation

    # Message Queue
    queue_maxsize: int = 1000
    handler_workers: int = 5

    # API Client - optimized for 24/7 operation
    default_api_timeout: int = 60  # 60 seconds API timeout
    max_retries: int = 0  # 0 means unlimited retries (24/7 operation)
    retry_delay: float = 5.0  # 5 seconds between retries

    # Database
    db_url: str = Field(default="sqlite:///storage/messages.db", alias="DB_URL")
    redis_url: Optional[str] = Field(default=None, alias="REDIS_URL")

    # Douyin Kele URLs
    kele_login_url: str = "https://life.douyin.com/"
    kele_im_url: str = "https://life.douyin.com/pc/im"

    # Polling intervals (seconds) - optimized for fast response
    message_poll_interval: float = 0.5  # 0.5 seconds between polls (faster detection)
    status_check_interval: float = 300.0  # 5 minutes between status checks

    # Timing constants (seconds) — centralized from scattered magic numbers
    page_load_wait: float = 5.0           # Wait for page content to fully render
    message_render_wait: float = 0.5      # Wait after scroll for lazy-loaded messages
    conversation_switch_wait: float = 2.0 # Base wait after clicking a conversation
    login_check_wait: float = 3.0         # Wait before checking login status
    popup_close_wait: float = 1.0         # Wait after closing a popup dialog
    store_init_stagger: float = 2.0       # Delay between initializing each store
    conversation_verify_interval: float = 0.3  # Check interval for dynamic verification
    conversation_verify_max_wait: float = 5.0  # Max wait for conversation verification
    unread_detect_fast_poll: float = 0.3       # Fast poll interval when processing
    dedup_cleanup_interval: int = 300          # Seconds between LRU cache cleanup
    processing_unlock_timeout: int = 300       # Auto-unlock timeout for processing user

    # Loaded stores
    stores: List[StoreConfig] = []

    def load_stores_from_yaml(self, filepath: Optional[str] = None) -> None:
        """Load store configurations from YAML file."""
        if filepath is None:
            filepath = self.config_dir / "accounts.yaml"

        if not os.path.exists(filepath):
            print(f"Warning: Accounts file not found: {filepath}")
            return

        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if data and "stores" in data:
            self.stores = [StoreConfig(**store) for store in data["stores"]]
            print(f"Loaded {len(self.stores)} store configurations")

    def filter_stores(self, store_filter: Optional[str] = None) -> None:
        """Filter stores to only those matching the given name or store_id.

        Called after load_stores_from_yaml to optionally run a single store.

        Args:
            store_filter: Store name or store_id to match. Case-insensitive.
                          If None, empty, or "all", keeps all stores unchanged.

        Raises:
            ValueError: If no store matches the filter — lists available stores.
        """
        if not store_filter or store_filter.lower() == "all":
            return  # Run all stores

        store_filter_lower = store_filter.lower().strip()

        # Try exact match on store_id first, then name, then partial match
        matched = []
        for store in self.stores:
            sid = (store.store_id or "").lower()
            name = (store.name or "").lower()
            if store_filter_lower == sid or store_filter_lower == name:
                matched = [store]
                break
            if store_filter_lower in sid or store_filter_lower in name:
                matched.append(store)

        if not matched:
            available = [f"  {s.store_id} ({s.name})" for s in self.stores]
            msg = (
                f"No store matching '{store_filter}'.\n"
                f"Available stores:\n" + "\n".join(available)
            )
            raise ValueError(msg)

        if len(matched) > 1:
            names = [f"  {s.store_id} ({s.name})" for s in matched]
            print(
                f"Warning: '{store_filter}' matched {len(matched)} stores, "
                f"using first match: {matched[0].name}\n"
                f"Be more specific. Matches:\n" + "\n".join(names)
            )

        self.stores = [matched[0]]
        print(f"Running single store: {self.stores[0].name} ({self.stores[0].store_id})")


# Global settings instance
settings = Settings()
settings.load_stores_from_yaml()