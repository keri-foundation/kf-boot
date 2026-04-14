from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(f"KF_BOOT_{name}")
    if value is not None:
        return value

    if default is None:
        raise KeyError(f"Missing environment variable KF_BOOT_{name}")

    return default


def _split_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _split_str_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_bool(value: str, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: str
    region_id: str
    region_name: str
    witness_limit: int
    watcher_limit: int
    wit_boot_url: str
    wit_public_url: str
    wat_boot_url: str
    wat_public_url: str
    bootstrap_account_options: tuple[str, ...]
    bootstrap_watcher_required: bool
    bootstrap_accounts_per_ip: int
    bootstrap_aids_per_ip: int
    admin_principals: set[str]

    @classmethod
    def from_env(cls) -> "Config":
        wit_boot_url = _env("WIT_BOOT_URL", "http://127.0.0.1:5631") 
        wit_public_url = _env("WIT_PUBLIC_URL", "http://127.0.0.1:5632")
        wat_boot_url = _env("WAT_BOOT_URL", "http://127.0.0.1:7631")
        wat_public_url = _env("WAT_PUBLIC_URL", "http://127.0.0.1:7632")

        return cls(
            host=_env("HOST", "127.0.0.1"),
            port=int(_env("PORT", "9723")),
            db_path=_env("DB_PATH", "./var/kf-boot"),
            region_id=_env("REGION_ID", "nyc"),
            region_name=_env("REGION_NAME", "New York"),
            witness_limit=int(_env("WITNESS_LIMIT", "20")),
            watcher_limit=int(_env("WATCHER_LIMIT", "20")),
            wit_boot_url=wit_boot_url.rstrip("/"),
            wit_public_url=wit_public_url.rstrip("/"),
            wat_boot_url=wat_boot_url.rstrip("/"),
            wat_public_url=wat_public_url.rstrip("/"),
            bootstrap_account_options=_split_str_csv(
                _env("BOOTSTRAP_ACCOUNT_OPTIONS", "1-of-1,3-of-4")
            ),
            bootstrap_watcher_required=_parse_bool(
                _env("BOOTSTRAP_WATCHER_REQUIRED", "true"),
                True,
            ),
            bootstrap_accounts_per_ip=int(_env("BOOTSTRAP_ACCOUNTS_PER_IP", "1")),
            bootstrap_aids_per_ip=int(_env("BOOTSTRAP_AIDS_PER_IP", "10")),
            admin_principals=_split_csv(_env("ADMIN_PRINCIPALS", "")),
        )
