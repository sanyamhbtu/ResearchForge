"""Configuration loading for ResearchForge.

Values come from (in order of precedence):
  1. real environment variables
  2. a ``.env`` file in the project root
  3. the defaults below

No third-party dependency is used so that the app boots on a bare Python
installation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env parser. Existing environment variables win."""
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    project_root: Path = PROJECT_ROOT

    host: str = "127.0.0.1"
    port: int = 8000
    open_browser: bool = True

    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "researchforge.db")
    cache_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "http_cache")
    download_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "downloads")

    http_timeout: float = 25.0
    http_max_retries: int = 3
    http_backoff_base: float = 0.8
    cache_ttl_seconds: int = 3600
    max_concurrent_requests: int = 6

    contact_email: str = ""
    user_agent: str = "ResearchForge/1.0 (local research client)"

    semantic_scholar_api_key: str = ""
    huggingface_token: str = ""
    datagov_api_key: str = ""

    default_search_depth: int = 2
    default_page_size: int = 25
    max_results_per_source: int = 120

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: float = 60.0

    def __post_init__(self) -> None:
        if not self.user_agent:
            self.user_agent = "ResearchForge/1.0 (local research client)"
        if self.contact_email and "mailto" not in self.user_agent:
            self.user_agent = f"{self.user_agent} mailto:{self.contact_email}"

    # -- derived helpers ---------------------------------------------------
    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.cache_dir, self.download_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict:
        """Non-secret view of the active configuration, surfaced by /api/health."""
        return {
            "host": self.host,
            "port": self.port,
            "db_path": str(self.db_path),
            "download_dir": str(self.download_dir),
            "search_depth_default": self.default_search_depth,
            "page_size_default": self.default_page_size,
            "max_results_per_source": self.max_results_per_source,
            "contact_email_configured": bool(self.contact_email),
            "semantic_scholar_key_configured": bool(self.semantic_scholar_api_key),
            "huggingface_token_configured": bool(self.huggingface_token),
            "datagov_key_configured": bool(self.datagov_api_key),
            "llm_enabled": self.llm_enabled,
            "llm_model": self.llm_model if self.llm_enabled else "",
        }


def _resolve(value: str, default: Path) -> Path:
    if not value:
        return default
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate


def _default_data_dir() -> Path:
    """Return a writable runtime directory for local and serverless execution."""
    if os.environ.get("VERCEL") == "1":
        return Path("/tmp/researchforge")
    return PROJECT_ROOT / "data"


def load_settings(dotenv: bool = True) -> Settings:
    if dotenv:
        _load_dotenv(PROJECT_ROOT / ".env")

    default_data_dir = _default_data_dir()
    configured_data_dir = _env("RF_DATA_DIR")
    # Vercel's deployed bundle is read-only. Ignore project-relative or
    # /var/task paths there, including values copied from local env files.
    if os.environ.get("VERCEL") == "1" and (
        not configured_data_dir
        or configured_data_dir.startswith("/var/task")
        or not Path(configured_data_dir).is_absolute()
    ):
        configured_data_dir = str(default_data_dir)

    data_dir = _resolve(configured_data_dir, default_data_dir)
    settings = Settings(
        host=_env("RF_HOST", "127.0.0.1"),
        port=_env_int("RF_PORT", 8000),
        open_browser=_env_bool("RF_OPEN_BROWSER", True),
        data_dir=data_dir,
        db_path=_resolve(_env("RF_DB_PATH"), data_dir / "researchforge.db"),
        cache_dir=_resolve(_env("RF_CACHE_DIR"), data_dir / "http_cache"),
        download_dir=_resolve(_env("RF_DOWNLOAD_DIR"), data_dir / "downloads"),
        http_timeout=_env_float("RF_HTTP_TIMEOUT", 25.0),
        http_max_retries=_env_int("RF_HTTP_MAX_RETRIES", 3),
        http_backoff_base=_env_float("RF_HTTP_BACKOFF_BASE", 0.8),
        cache_ttl_seconds=_env_int("RF_CACHE_TTL_SECONDS", 3600),
        max_concurrent_requests=_env_int("RF_MAX_CONCURRENT_REQUESTS", 6),
        contact_email=_env("RF_CONTACT_EMAIL"),
        user_agent=_env("RF_USER_AGENT", "ResearchForge/1.0 (local research client)"),
        semantic_scholar_api_key=_env("SEMANTIC_SCHOLAR_API_KEY"),
        huggingface_token=_env("HUGGINGFACE_TOKEN"),
        datagov_api_key=_env("DATAGOV_API_KEY"),
        default_search_depth=_env_int("RF_DEFAULT_SEARCH_DEPTH", 2),
        default_page_size=_env_int("RF_DEFAULT_PAGE_SIZE", 25),
        max_results_per_source=_env_int("RF_MAX_RESULTS_PER_SOURCE", 120),
        llm_base_url=_env("RF_LLM_BASE_URL"),
        llm_api_key=_env("RF_LLM_API_KEY"),
        llm_model=_env("RF_LLM_MODEL"),
        llm_timeout=_env_float("RF_LLM_TIMEOUT", 60.0),
    )
    settings.ensure_dirs()
    return settings


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def override_settings(settings: Settings) -> None:
    """Used by tests to point the app at a throwaway database."""
    global _settings
    _settings = settings
    _settings.ensure_dirs()
