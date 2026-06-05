from __future__ import annotations

import os
from dataclasses import dataclass

from keri import help

logger = help.ogler.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(f"KF_BOOT_{name}")
    if value is not None:
        return value

    if default is None:
        raise KeyError(f"Missing environment variable KF_BOOT_{name}")

    return default


def _split_str_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_bool(value: str, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_url(value: str) -> str:
    return value.rstrip("/")


ONBOARDING_ROUTES = {
    "/operations/status",
    "/onboarding/session/start",
    "/onboarding/session/status",
    "/onboarding/account/create",
    "/onboarding/complete",
    "/onboarding/cancel",
}

ACCOUNT_ROUTES = {
    "/operations/status",
    "/account/witnesses",
    "/account/watchers",
    "/account/watchers/status",
    "/account/delete",
    "/account/witnesses/delete",
    "/account/watchers/delete",
}


# Account profile definitions are used to enforce per-tier limits and quotas.
# Each profile maps a staging tier to a bootstrap code and runtime limits.
@dataclass(frozen=True)
class AccountProfile:
    """
    Defines the account profile for a given bootstrap code

    Attributes:
    - tier: The staging tier (e.g. 'trial', 'org')
    - code: The bootstrap code (e.g. '1-of-1', '3-of-4')
    - max_accounts: Maximum number of accounts allowed for this profile
    - max_requests_per_minute: Maximum number of requests per minute allowed for this profile
    - api_budget: Maximum number of API requests allowed for this profile
    """
    tier: str
    code: str
    max_accounts: int
    max_requests_per_minute: int
    api_budget: int


def _parse_account_profiles(value: str) -> tuple[AccountProfile, ...]:
    """Parse the environment-configured account profiles into AccountProfile objects."""
    profiles: list[AccountProfile] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        parts = [part.strip() for part in item.split("|")]
        if len(parts) != 5:
            raise ValueError(
                "KF_BOOT_ACCOUNT_PROFILES entries must be formatted as "
                "'<tier>|<profile>|<max_accounts>|<max_requests_per_minute>|<api_budget>'"
            )
        tier, code, max_accounts, max_requests, api_budget = parts[:5]
        profiles.append(
            AccountProfile(
                tier=tier,
                code=code,
                max_accounts=int(max_accounts),
                max_requests_per_minute=int(max_requests),
                api_budget=int(api_budget),
            )
        )
    return tuple(profiles)


def _default_account_profiles(codes: tuple[str, ...]) -> tuple[AccountProfile, ...]:
    """
    Generate default account profiles based on the supported bootstrap account options 
    if no explicit profiles are provided in the configuration.
    
    It maps '1-of-1' to a 'trial' tier and '3-of-4' to an 'org' tier with predefined limits.
    
    Trial tier has:
    - max_accounts=1 
    - max_requests_per_minute=30
    - api_budget=100
    
    Org tier has: 
    - max_accounts=3
    - max_requests_per_minute=60
    - api_budget=200
    """

    defaults: list[AccountProfile] = []
    for code in codes:
        option = _account_option(code)
        tier = "trial" if option["witness_count"] == 1 else "org"
        max_accounts = 1 if option["witness_count"] == 1 else 3
        max_requests = 30 if option["witness_count"] == 1 else 60
        api_budget = 100 if option["witness_count"] == 1 else 200
        defaults.append(
            AccountProfile(
                tier=tier,
                code=option["code"],
                max_accounts=max_accounts,
                max_requests_per_minute=max_requests,
                api_budget=api_budget,
            )
        )
    return tuple(defaults)


def _account_option(code: str) -> dict[str, int | str]:
    parts = code.lower().split("-of-")
    if len(parts) != 2:
        return {"code": code, "witness_count": 0, "toad": 0}

    try:
        toad = int(parts[0])
        witness_count = int(parts[1])
    except ValueError:
        return {"code": code, "witness_count": 0, "toad": 0}

    return {
        "code": code,
        "witness_count": witness_count,
        "toad": toad,
    }


FROZEN_ACCOUNT_OPTIONS = {
    "1-of-1": (1, 1),
    "3-of-4": (4, 3),
}


def _supported_account_options(codes: tuple[str, ...], *, witness_backend_count: int) -> tuple[str, ...]:
    supported: list[str] = []
    seen: set[str] = set()
    for code in codes:
        normalized = (code or "").strip().lower()
        if normalized not in FROZEN_ACCOUNT_OPTIONS:
            continue
        if normalized in seen:
            continue
        witness_count, _ = FROZEN_ACCOUNT_OPTIONS[normalized]
        if witness_count <= witness_backend_count:
            supported.append(normalized)
            seen.add(normalized)
    return tuple(supported)


@dataclass(frozen=True)
class WitnessBackend:
    id: str
    boot_url: str
    public_url: str


def _parse_witness_backends(value: str) -> tuple[WitnessBackend, ...]:
    backends: list[WitnessBackend] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        parts = [part.strip() for part in item.split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                "KF_BOOT_WITNESS_BACKENDS entries must be formatted as '<id>|<boot_url>|<public_url>'"
            )
        backends.append(
            WitnessBackend(
                id=parts[0],
                boot_url=_normalize_url(parts[1]),
                public_url=_normalize_url(parts[2]),
            )
        )
    return tuple(backends)


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: str
    keri_dir: str | None
    keri_name: str
    boot_hab_name: str
    onboarding_path: str
    account_path: str
    onboarding_public_url: str
    account_public_url: str
    region_id: str
    region_name: str
    witness_limit: int
    watcher_limit: int
    wit_boot_url: str
    wit_public_url: str
    wat_boot_url: str
    wat_public_url: str
    bootstrap_account_options: tuple[str, ...]
    # Whether newly bootstrapped accounts must allocate a watcher during onboarding
    bootstrap_watcher_required: bool
    # Max number of concurrent onboarding accounts allowed from one client IP
    bootstrap_accounts_per_ip: int
    # Max number of onboarding ephemeral AIDs allowed from one client IP
    bootstrap_aids_per_ip: int
    # How long an open onboarding session stays valid before it becomes expired
    session_ttl_seconds: int
    # Per-bootstrap-option account policy definitions such as tier and request budgets
    account_profiles: tuple[AccountProfile, ...] = ()
    # Configured witness boot backends that can allocate hosted witnesses
    witness_backends: tuple[WitnessBackend, ...] = ()
    # Request rate limit applied to bootstrap API traffic from a single IP
    bootstrap_api_requests_per_minute: int = 10
    # HTTP timeout used for downstream witness/watcher boot API calls
    boot_api_timeout_seconds: float = 10.0
    # Idle TTL for onboarded accounts before they are expired and cleaned up
    account_ttl_seconds: float = 172800.0  # 48 hours
    # How long closed session rows are retained before final session deletion
    closed_session_retention_seconds: float | None = None
    # Flag for enabling periodic cleanup work
    cleanup_runner_enabled: bool = True
    # Delay between periodic cleanup sweep attempts
    cleanup_interval_seconds: float = 60.0
    # Max number of cleanup tasks to process in one sweep pass
    cleanup_batch_size: int = 100
    # Soft budget for one sweep before the runner yields until next interval
    cleanup_time_budget_seconds: float = 5.0
    # Initial retry delay after a cleanup teardown/delete failure
    cleanup_failure_backoff_seconds: float = 60.0
    # Maximum retry delay cap for repeated cleanup failures
    cleanup_failure_backoff_max_seconds: float = 900.0
    # Random jitter added to cleanup retry delays to avoid synchronized retries
    cleanup_failure_jitter_seconds: float = 5.0
    # Block a poisoned cleanup task once it has failed this many times
    cleanup_block_after_attempts: int = 10
    # Block a poisoned cleanup task once it has been failing this long
    cleanup_block_after_failure_age_seconds: float = 86400.0
    # Retention delay after account cleanup before final account deletion is allowed
    expired_account_retention_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.bootstrap_api_requests_per_minute < 0:
            raise ValueError("bootstrap_api_requests_per_minute must be greater than or equal to 0.")
        if self.session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be greater than 0.")
        if self.boot_api_timeout_seconds <= 0:
            raise ValueError("boot_api_timeout_seconds must be greater than 0.")
        if self.account_ttl_seconds < 0:
            raise ValueError("account_ttl_seconds must be greater than or equal to 0.")
        if (
            self.closed_session_retention_seconds is not None
            and self.closed_session_retention_seconds < 0
        ):
            raise ValueError("closed_session_retention_seconds must be greater than or equal to 0.")
        if self.cleanup_batch_size <= 0:
            raise ValueError("cleanup_batch_size must be greater than 0.")
        if self.cleanup_time_budget_seconds <= 0:
            raise ValueError("cleanup_time_budget_seconds must be greater than 0.")
        if self.cleanup_failure_backoff_seconds < 0:
            raise ValueError("cleanup_failure_backoff_seconds must be greater than or equal to 0.")
        if self.cleanup_failure_backoff_max_seconds < self.cleanup_failure_backoff_seconds:
            raise ValueError(
                "cleanup_failure_backoff_max_seconds must be greater than or equal to cleanup_failure_backoff_seconds."
            )
        if self.cleanup_failure_jitter_seconds < 0:
            raise ValueError("cleanup_failure_jitter_seconds must be greater than or equal to 0.")
        if self.cleanup_block_after_attempts <= 0:
            raise ValueError("cleanup_block_after_attempts must be greater than 0.")
        if self.cleanup_block_after_failure_age_seconds <= 0:
            raise ValueError("cleanup_block_after_failure_age_seconds must be greater than 0.")
        if self.expired_account_retention_seconds < 0:
            raise ValueError("expired_account_retention_seconds must be greater than or equal to 0.")
        if self.cleanup_interval_seconds < 0:
            raise ValueError("cleanup_interval_seconds must be greater than or equal to 0.")
        if self.closed_session_retention_seconds is None:
            object.__setattr__(
                self,
                "closed_session_retention_seconds",
                float(max(self.session_ttl_seconds, 0)),
            )

        backends = tuple(self.witness_backends)
        if not backends:
            if not self.wit_boot_url or not self.wit_public_url:
                raise ValueError("At least one witness backend must be configured.")
            backends = (
                WitnessBackend(
                    id="wit-1",
                    boot_url=_normalize_url(self.wit_boot_url),
                    public_url=_normalize_url(self.wit_public_url),
                ),
            )

        normalized: list[WitnessBackend] = []
        seen_ids: set[str] = set()
        seen_boot_urls: set[str] = set()
        for backend in backends:
            backend_id = backend.id.strip()
            boot_url = _normalize_url(backend.boot_url)
            public_url = _normalize_url(backend.public_url)
            if not backend_id or not boot_url or not public_url:
                raise ValueError("Witness backend id, boot_url, and public_url are required.")
            if backend_id in seen_ids:
                raise ValueError(f"Duplicate witness backend id '{backend_id}'.")
            if boot_url in seen_boot_urls:
                raise ValueError(f"Duplicate witness backend boot_url '{boot_url}'.")
            seen_ids.add(backend_id)
            seen_boot_urls.add(boot_url)
            normalized.append(
                WitnessBackend(
                    id=backend_id,
                    boot_url=boot_url,
                    public_url=public_url,
                )
            )

        supported_options = _supported_account_options(
            tuple(self.bootstrap_account_options),
            witness_backend_count=len(normalized),
        )
        if not supported_options:
            raise ValueError("No bootstrap account options are supported by the configured witness backends.")

        # Check for account profiles and validate that their codes are supported 
        account_profiles = tuple(self.account_profiles)
        if account_profiles:
            normalized_profiles: list[AccountProfile] = []
            seen_codes: set[str] = set()
            for profile in account_profiles:
                if profile.code not in supported_options:
                    raise ValueError(
                        f"Account profile code '{profile.code}' is not supported by the configured witness backends."
                    )
                if profile.code in seen_codes:
                    raise ValueError(f"Duplicate account profile code '{profile.code}'.")
                normalized_profiles.append(profile)
                seen_codes.add(profile.code)
            # Check that all supported options have a corresponding account profile
            missing_codes = tuple(code for code in supported_options if code not in seen_codes)
            if missing_codes:
                raise ValueError(
                    "Missing account profile code(s) for supported bootstrap option(s): "
                    f"{', '.join(missing_codes)}."
                )
            account_profiles = tuple(normalized_profiles)
        else:
            # If no explicit account profiles were provided, generate default profiles based on the supported bootstrap options
            account_profiles = _default_account_profiles(supported_options)

        object.__setattr__(self, "witness_backends", tuple(normalized))
        object.__setattr__(self, "wit_boot_url", normalized[0].boot_url)
        object.__setattr__(self, "wit_public_url", normalized[0].public_url)
        object.__setattr__(self, "bootstrap_account_options", supported_options)
        object.__setattr__(self, "account_profiles", tuple(account_profiles))
        logger.info(
            f"Config initialized and validated:\n"
            f"Host: {self.host}\n"
            f"Port: {self.port}\n"
            f"Region Name: {self.region_name}\n"
            f"Witness backends: {', '.join(backend.id for backend in normalized)}\n"
            f"Bootstrap account options: {', '.join(supported_options)}\n"
            f"Account Profiles: {', '.join(f'{profile.code} (tier={profile.tier})' for profile in account_profiles)}\n"
            f"Bootstrap Account per IP: {self.bootstrap_accounts_per_ip}\n"
            f"Bootstrap API requests per minute: {self.bootstrap_api_requests_per_minute}\n"
            f"Boot API timeout seconds: {self.boot_api_timeout_seconds}\n"
            f"Account TTL seconds: {self.account_ttl_seconds}\n"
            f"Closed session retention seconds: {self.closed_session_retention_seconds}\n"
            f"Cleanup runner enabled: {self.cleanup_runner_enabled}\n"
            f"Cleanup interval seconds: {self.cleanup_interval_seconds}\n"
            f"Cleanup batch size: {self.cleanup_batch_size}\n"
            f"Cleanup time budget seconds: {self.cleanup_time_budget_seconds}\n"
            f"Cleanup failure backoff seconds: {self.cleanup_failure_backoff_seconds}\n"
            f"Cleanup failure backoff max seconds: {self.cleanup_failure_backoff_max_seconds}\n"
            f"Cleanup failure jitter seconds: {self.cleanup_failure_jitter_seconds}\n"
            f"Cleanup block after attempts: {self.cleanup_block_after_attempts}\n"
            f"Cleanup block after failure age seconds: {self.cleanup_block_after_failure_age_seconds}\n"
            f"Expired account retention seconds: {self.expired_account_retention_seconds}\n"
        )

    def account_option(self, code: str) -> dict[str, int | str] | None:
        target = (code or "").strip().lower()
        for item in self.bootstrap_account_options:
            option = _account_option(item)
            if option["code"].lower() == target:
                return option
        return None

    def account_profile(self, code: str) -> AccountProfile | None:
        """Return the AccountProfile for the given bootstrap code, or None if not found."""
        target = (code or "").strip().lower()
        for profile in self.account_profiles:
            if profile.code.lower() == target:
                return profile
        return None

    @property
    def onboarding_surface(self) -> dict[str, str]:
        return {"path": self.onboarding_path, "url": self.onboarding_public_url}

    @property
    def account_surface(self) -> dict[str, str]:
        return {"path": self.account_path, "url": self.account_public_url}

    @classmethod
    def from_env(cls) -> "Config":
        host = _env("HOST", "127.0.0.1")
        port = int(_env("PORT", "9723"))
        onboarding_path = _env("ONBOARDING_PATH", "/onboarding").rstrip("/") or "/onboarding"
        account_path = _env("ACCOUNT_PATH", "/account").rstrip("/") or "/account"

        onboarding_public_url = _env("ONBOARDING_PUBLIC_URL", f"http://{host}:{port}{onboarding_path}")
        account_public_url = _env("ACCOUNT_PUBLIC_URL", f"http://{host}:{port}{account_path}")

        witness_backends_env = os.environ.get("KF_BOOT_WITNESS_BACKENDS")
        witness_backends = _parse_witness_backends(witness_backends_env or "")
        if witness_backends:
            wit_boot_url = witness_backends[0].boot_url
            wit_public_url = witness_backends[0].public_url
        else:
            wit_boot_url = _env("WIT_BOOT_URL")
            wit_public_url = _env("WIT_PUBLIC_URL")
        wat_boot_url = _env("WAT_BOOT_URL")
        wat_public_url = _env("WAT_PUBLIC_URL")

        account_profiles_env = os.environ.get("KF_BOOT_ACCOUNT_PROFILES", "")
        account_profiles = _parse_account_profiles(account_profiles_env) if account_profiles_env else ()
        closed_session_retention_env = os.environ.get("KF_BOOT_CLOSED_SESSION_RETENTION_SECONDS")

        return cls(
            host=host,
            port=port,
            db_path=_env("DB_PATH", "./var/kf-boot"),
            keri_dir=os.environ.get("KF_BOOT_KERI_DIR"),
            keri_name=_env("KERI_NAME", "kf-boot"),
            boot_hab_name=_env("BOOT_HAB_NAME", "boot-server"),
            onboarding_path=onboarding_path,
            account_path=account_path,
            onboarding_public_url=onboarding_public_url.rstrip("/"),
            account_public_url=account_public_url.rstrip("/"),
            region_id=_env("REGION_ID", "nyc"),
            region_name=_env("REGION_NAME", "New York"),
            witness_limit=int(_env("WITNESS_LIMIT", "200")),
            watcher_limit=int(_env("WATCHER_LIMIT", "200")),
            wit_boot_url=_normalize_url(wit_boot_url),
            wit_public_url=_normalize_url(wit_public_url),
            wat_boot_url=_normalize_url(wat_boot_url),
            wat_public_url=_normalize_url(wat_public_url),
            bootstrap_account_options=_split_str_csv(
                _env("BOOTSTRAP_ACCOUNT_OPTIONS", "1-of-1,3-of-4")
            ),
            bootstrap_watcher_required=_parse_bool(
                _env("BOOTSTRAP_WATCHER_REQUIRED", "true"),
                True,
            ),
            bootstrap_accounts_per_ip=int(_env("BOOTSTRAP_ACCOUNTS_PER_IP", "1")),
            bootstrap_aids_per_ip=int(_env("BOOTSTRAP_AIDS_PER_IP", "10")),
            session_ttl_seconds=int(_env("SESSION_TTL_SECONDS", "300")),
            account_profiles=account_profiles,
            witness_backends=witness_backends,
            bootstrap_api_requests_per_minute=int(
                _env("BOOTSTRAP_API_REQUESTS_PER_MINUTE", "10")
            ),
            boot_api_timeout_seconds=float(
                _env("BOOT_API_TIMEOUT_SECONDS", "10")
            ),
            account_ttl_seconds=float(
                _env("ACCOUNT_TTL_SECONDS", "172800")  # 48 hours
            ),
            closed_session_retention_seconds=(
                float(closed_session_retention_env)
                if closed_session_retention_env is not None
                else None
            ),
            cleanup_runner_enabled=_parse_bool(
                _env("CLEANUP_RUNNER_ENABLED", "true"),
                True,
            ),
            cleanup_interval_seconds=float(
                _env("CLEANUP_INTERVAL_SECONDS", "60")
            ),
            cleanup_batch_size=int(
                _env("CLEANUP_BATCH_SIZE", "100")
            ),
            cleanup_time_budget_seconds=float(
                _env("CLEANUP_TIME_BUDGET_SECONDS", "5")
            ),
            cleanup_failure_backoff_seconds=float(
                _env("CLEANUP_FAILURE_BACKOFF_SECONDS", "60")
            ),
            cleanup_failure_backoff_max_seconds=float(
                _env("CLEANUP_FAILURE_BACKOFF_MAX_SECONDS", "900")
            ),
            cleanup_failure_jitter_seconds=float(
                _env("CLEANUP_FAILURE_JITTER_SECONDS", "5")
            ),
            cleanup_block_after_attempts=int(
                _env("CLEANUP_BLOCK_AFTER_ATTEMPTS", "10")
            ),
            cleanup_block_after_failure_age_seconds=float(
                _env("CLEANUP_BLOCK_AFTER_FAILURE_AGE_SECONDS", "86400")
            ),
            expired_account_retention_seconds=float(
                _env("EXPIRED_ACCOUNT_RETENTION_SECONDS", "0")
            ),
        )
