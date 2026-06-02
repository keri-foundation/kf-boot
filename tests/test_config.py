from __future__ import annotations

import pytest

from kfboot.config import AccountProfile, Config, WitnessBackend

from .support import make_config, make_witness_backends


def test_config_filters_profiles_to_available_witness_backends(tmp_path):
    config = make_config(tmp_path, witness_backends=make_witness_backends(1))

    assert config.bootstrap_account_options == ("1-of-1",)
    assert config.account_option("1-of-1") == {"code": "1-of-1", "witness_count": 1, "toad": 1}
    assert config.account_option("3-of-4") is None


def test_config_filters_out_profiles_outside_the_frozen_contract(tmp_path):
    config = make_config(
        tmp_path,
        witness_backends=make_witness_backends(4),
        bootstrap_account_options=("2-of-2", "1-of-1", "3-of-4", "1-of-4"),
    )

    assert config.bootstrap_account_options == ("1-of-1", "3-of-4")
    assert config.account_option("2-of-2") is None
    assert config.account_option("1-of-4") is None


def test_config_rejects_when_no_profile_matches_configured_backends(tmp_path):
    with pytest.raises(ValueError, match="No bootstrap account options"):
        make_config(
            tmp_path,
            witness_backends=make_witness_backends(1),
            bootstrap_account_options=("3-of-4",),
        )


def test_config_from_env_parses_witness_backend_pool(monkeypatch):
    monkeypatch.setenv(
        "KF_BOOT_WITNESS_BACKENDS",
        (
            "wit-1|http://127.0.0.1:5631|https://boot.example.com:5632,"
            "wit-2|http://127.0.0.1:5641|https://boot.example.com:5642,"
            "wit-3|http://127.0.0.1:5651|https://boot.example.com:5652,"
            "wit-4|http://127.0.0.1:5661|https://boot.example.com:5662"
        ),
    )
    monkeypatch.delenv("KF_BOOT_WIT_BOOT_URL", raising=False)
    monkeypatch.delenv("KF_BOOT_WIT_PUBLIC_URL", raising=False)
    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example")

    config = Config.from_env()

    assert [backend.id for backend in config.witness_backends] == ["wit-1", "wit-2", "wit-3", "wit-4"]
    assert config.keri_dir is None
    assert config.wit_boot_url == "http://127.0.0.1:5631"
    assert config.wit_public_url == "https://boot.example.com:5632"
    assert config.bootstrap_account_options == ("1-of-1", "3-of-4")


def test_config_from_env_uses_legacy_single_backend_fallback(monkeypatch):
    """Tests the legacy single-backend environment variables still build a valid config."""
    monkeypatch.delenv("KF_BOOT_WITNESS_BACKENDS", raising=False)
    monkeypatch.delenv("KF_BOOT_ACCOUNT_PROFILES", raising=False)
    monkeypatch.setenv("KF_BOOT_WIT_BOOT_URL", "http://boot.local/witness/")
    monkeypatch.setenv("KF_BOOT_WIT_PUBLIC_URL", "https://witness.example/")
    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers/")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example/")
    monkeypatch.setenv("KF_BOOT_BOOTSTRAP_WATCHER_REQUIRED", "false")

    config = Config.from_env()

    assert [backend.id for backend in config.witness_backends] == ["wit-1"]
    assert config.wit_boot_url == "http://boot.local/witness"
    assert config.wit_public_url == "https://witness.example"
    assert config.bootstrap_account_options == ("1-of-1",)
    assert config.bootstrap_watcher_required is False


def test_config_from_env_parses_account_profiles(monkeypatch):
    """Test that account profiles are correctly parsed from env variables and can be retrieved"""

    # Set up environment variables for witness backends and account profiles
    # Witness backend contains only 1 witness
    monkeypatch.setenv(
        "KF_BOOT_WITNESS_BACKENDS",
        "wit-1|http://127.0.0.1:5631|https://boot.example.com:5632",
    )

    # Set up a single account profile with code "1-of-1" 
    monkeypatch.setenv(
        "KF_BOOT_ACCOUNT_PROFILES",
        "trial|1-of-1|10|5|4",
    )

    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example")

    config = Config.from_env()

    # Assert that the account profile is correctly parsed and available in the config
    assert [profile.code for profile in config.account_profiles] == ["1-of-1"]
    assert config.account_profile("1-of-1").tier == "trial"
    assert config.account_profile("1-of-1").max_accounts == 10
    assert config.account_profile("1-of-1").max_requests_per_minute == 5
    assert config.account_profile("1-of-1").api_budget == 4
    assert config.account_profile("missing") is None


def test_config_from_env_parses_onboarding_request_quota(monkeypatch):
    """Assert config is reading the onboarding request quotas properly"""
    monkeypatch.setenv(
        "KF_BOOT_WITNESS_BACKENDS",
        "wit-1|http://127.0.0.1:5631|https://boot.example.com:5632",
    )
    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example")
    monkeypatch.setenv("KF_BOOT_BOOTSTRAP_API_REQUESTS_PER_MINUTE", "17")

    config = Config.from_env()

    assert config.bootstrap_api_requests_per_minute == 17


def test_config_from_env_parses_account_delete_quota(monkeypatch):
    monkeypatch.setenv(
        "KF_BOOT_WITNESS_BACKENDS",
        "wit-1|http://127.0.0.1:5631|https://boot.example.com:5632",
    )
    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example")
    monkeypatch.setenv("KF_BOOT_BOOTSTRAP_API_REQUESTS_PER_MINUTE", "3")

    config = Config.from_env()

    assert config.bootstrap_api_requests_per_minute == 3


def test_config_from_env_parses_cleanup_settings(monkeypatch):
    monkeypatch.setenv(
        "KF_BOOT_WITNESS_BACKENDS",
        "wit-1|http://127.0.0.1:5631|https://boot.example.com:5632",
    )
    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example")
    monkeypatch.setenv("KF_BOOT_ACCOUNT_TTL_SECONDS", "1800")
    monkeypatch.setenv("KF_BOOT_BOOT_API_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("KF_BOOT_CLOSED_SESSION_RETENTION_SECONDS", "90")
    monkeypatch.setenv("KF_BOOT_CLEANUP_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("KF_BOOT_CLEANUP_BATCH_SIZE", "7")
    monkeypatch.setenv("KF_BOOT_CLEANUP_TIME_BUDGET_SECONDS", "3")
    monkeypatch.setenv("KF_BOOT_CLEANUP_FAILURE_BACKOFF_SECONDS", "45")
    monkeypatch.setenv("KF_BOOT_CLEANUP_FAILURE_BACKOFF_MAX_SECONDS", "300")
    monkeypatch.setenv("KF_BOOT_CLEANUP_FAILURE_JITTER_SECONDS", "2")
    monkeypatch.setenv("KF_BOOT_CLEANUP_BLOCK_AFTER_ATTEMPTS", "6")
    monkeypatch.setenv("KF_BOOT_CLEANUP_BLOCK_AFTER_FAILURE_AGE_SECONDS", "1800")
    monkeypatch.setenv("KF_BOOT_EXPIRED_ACCOUNT_RETENTION_SECONDS", "120")

    config = Config.from_env()

    assert config.account_ttl_seconds == 1800
    assert config.boot_api_timeout_seconds == 12
    assert config.closed_session_retention_seconds == 90
    assert config.cleanup_interval_seconds == 15
    assert config.cleanup_batch_size == 7
    assert config.cleanup_time_budget_seconds == 3
    assert config.cleanup_failure_backoff_seconds == 45
    assert config.cleanup_failure_backoff_max_seconds == 300
    assert config.cleanup_failure_jitter_seconds == 2
    assert config.cleanup_block_after_attempts == 6
    assert config.cleanup_block_after_failure_age_seconds == 1800
    assert config.expired_account_retention_seconds == 120


def test_config_from_env_rejects_malformed_account_profiles(monkeypatch):
    """Tests that malformed account profile entries are rejected with a clear error message."""

    # Witness backend only has 1 witness, so only "1-of-1" code is supported for account profiles
    monkeypatch.setenv(
        "KF_BOOT_WITNESS_BACKENDS",
        "wit-1|http://127.0.0.1:5631|https://boot.example.com:5632",
    )

    # Set up a malformed account profile that is missing the api_budget field
    monkeypatch.setenv("KF_BOOT_ACCOUNT_PROFILES", "trial|1-of-1|10|5")
    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example")

    # The entry is missing the api_budget field
    with pytest.raises(ValueError, match="KF_BOOT_ACCOUNT_PROFILES entries must be formatted"):
        Config.from_env()


def test_config_from_env_rejects_duplicate_account_profile_codes(monkeypatch):
    """Tests that duplicate account profile codes are rejected with a clear error message."""
    monkeypatch.setenv(
        "KF_BOOT_WITNESS_BACKENDS",
        "wit-1|http://127.0.0.1:5631|https://boot.example.com:5632",
    )
    monkeypatch.setenv(
        "KF_BOOT_ACCOUNT_PROFILES",
        "trial|1-of-1|10|5|4,org|1-of-1|20|50|100",
    )
    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example")
    
    # The profile code "1-of-1" is duplicated in both entries
    with pytest.raises(ValueError, match="Duplicate account profile code"):
        Config.from_env()


def test_config_from_env_generates_default_account_profiles_when_not_configured(monkeypatch):
    """Tests that default account profiles are generated based on supported bootstrap options when no explicit profiles are provided"""
    monkeypatch.setenv(
        "KF_BOOT_WITNESS_BACKENDS",
        (
            "wit-1|http://127.0.0.1:5631|https://boot.example.com:5632,"
            "wit-2|http://127.0.0.1:5641|https://boot.example.com:5642,"
            "wit-3|http://127.0.0.1:5651|https://boot.example.com:5652,"
            "wit-4|http://127.0.0.1:5661|https://boot.example.com:5662"
        ),
    )
    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example")

    config = Config.from_env()

    # When no account profiles are explicitly configure, default profiles should be generated
    assert [profile.code for profile in config.account_profiles] == ["1-of-1", "3-of-4"]
    assert config.account_profile("1-of-1").tier == "trial"
    assert config.account_profile("1-of-1").max_accounts == 1
    assert config.account_profile("1-of-1").max_requests_per_minute == 30
    assert config.account_profile("1-of-1").api_budget == 100
    assert config.account_profile("3-of-4").tier == "org"
    assert config.account_profile("3-of-4").max_accounts == 3
    assert config.account_profile("3-of-4").api_budget == 200


def test_config_from_env_rejects_account_profile_code_not_supported_by_witness_backends(monkeypatch):
    """Tests that account profile codes that are not supported by witness backends are rejected"""
    monkeypatch.setenv(
        "KF_BOOT_WITNESS_BACKENDS",
        "wit-1|http://127.0.0.1:5631|https://boot.example.com:5632",
    )
    monkeypatch.setenv(
        "KF_BOOT_ACCOUNT_PROFILES",
        "org|3-of-4|2|10|100",
    )
    monkeypatch.setenv("KF_BOOT_WAT_BOOT_URL", "http://boot.local/watchers")
    monkeypatch.setenv("KF_BOOT_WAT_PUBLIC_URL", "https://watcher.example")

    # The code "3-of-4" is not supported because there is only 1 witness backend configured
    with pytest.raises(ValueError, match="Account profile code '3-of-4' is not supported"):
        Config.from_env()


def test_config_rejects_explicit_account_profiles_missing_supported_options(tmp_path):
    """ Tests that all supported bootstrap options must have a corresponding account profile """
    with pytest.raises(ValueError, match="Missing account profile code"):
        make_config(
            tmp_path,
            account_profiles=(
                # Only "1-of-1" profile is provided, "3-of-4" is missing 
                AccountProfile(
                    tier="trial",
                    code="1-of-1",
                    max_accounts=1,
                    max_requests_per_minute=30,
                    api_budget=100,
                ),
            ),
        )


def test_config_from_env_rejects_malformed_witness_backend_entry(monkeypatch):
    monkeypatch.setenv("KF_BOOT_WITNESS_BACKENDS", "wit-1|http://127.0.0.1:5631")

    with pytest.raises(ValueError, match="formatted as"):
        Config.from_env()


@pytest.mark.parametrize(
    ("witness_backends", "message"),
    [
        (
            (
                WitnessBackend(
                    id="wit-1",
                    boot_url="http://127.0.0.1:5631",
                    public_url="https://boot.example.com:5632",
                ),
                WitnessBackend(
                    id="wit-1",
                    boot_url="http://127.0.0.1:5641",
                    public_url="https://boot.example.com:5642",
                ),
            ),
            "Duplicate witness backend id",
        ),
        (
            (
                WitnessBackend(
                    id="wit-1",
                    boot_url="http://127.0.0.1:5631",
                    public_url="https://boot.example.com:5632",
                ),
                WitnessBackend(
                    id="wit-2",
                    boot_url="http://127.0.0.1:5631",
                    public_url="https://boot.example.com:5642",
                ),
            ),
            "Duplicate witness backend boot_url",
        ),
    ],
)
def test_config_rejects_duplicate_witness_backend_identity(tmp_path, witness_backends, message):
    with pytest.raises(ValueError, match=message):
        make_config(tmp_path, witness_backends=witness_backends)


def test_config_rejects_nonpositive_session_ttl(tmp_path):
    with pytest.raises(ValueError, match="session_ttl_seconds must be greater than 0"):
        make_config(tmp_path, session_ttl_seconds=0)
