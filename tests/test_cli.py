from __future__ import annotations

import pytest

from kfboot import cli
from kfboot.basing import (
    CLEANUP_TASK_SESSION_CLEANUP,
    CLEANUP_TASK_SESSION_DELETE,
    CLEANUP_TASK_SESSION_EXPIRE,
    SESSION_STATE_EXPIRED,
)
from kfboot.store import Store
from .support import (
    makeBlockedOrphanTask,
    makeBlockedOrphanTaskWithResource,
    makeBlockedTask,
)

def test_cli_blocked_list_prints_summary(tmp_path, capsys):
    """Test for the cleanup block list command"""

    # Create a blocked cleanup task 
    db_path, session_id = makeBlockedTask(tmp_path)

    # Run the list command 
    cli.main(["cleanup", "blocked", "list", "--db-path", db_path])

    # Capture the output
    output = capsys.readouterr().out

    # Assert that the output is accurate
    assert CLEANUP_TASK_SESSION_EXPIRE in output
    assert session_id in output
    assert "blocked_at=2024-01-01T00:00:06+00:00" in output
    assert "reason=simulated operator task" in output


def test_cli_blocked_show_prints_details(tmp_path, capsys):
    """Test for the cleanup block show command"""

    # Create a blocked task
    db_path, session_id = makeBlockedTask(tmp_path)

    # Run the show command
    cli.main(["cleanup", "blocked", "show", "--db-path", db_path, CLEANUP_TASK_SESSION_EXPIRE, session_id])

    # Capture the output
    output = capsys.readouterr().out
    
    # Assert that the output is accurate
    assert f"kind: {CLEANUP_TASK_SESSION_EXPIRE}" in output
    assert f"subject: {session_id}" in output
    assert "first_failed_at: 2024-01-01T00:00:05+00:00" in output
    assert "blocked_reason: simulated operator task" in output
    assert "dismiss_safe: no" in output


def test_cli_blocked_requeue_restores_due_work(tmp_path, capsys, monkeypatch):
    """Test for requeue command and admin record for requeue action"""

    # Create a blocked task
    db_path, session_id = makeBlockedTask(tmp_path)
    monkeypatch.setattr(cli, "nowIso", lambda: "2024-01-01T00:00:10+00:00")

    # Run the requeue command
    cli.main([
        "cleanup",
        "blocked",
        "requeue",
        "--db-path",
        db_path,
        "--actor",
        "operator-a",
        "--reason",
        "operator retried after backend recovery",
        CLEANUP_TASK_SESSION_EXPIRE,
        session_id,
    ])

    # Capture the output
    output = capsys.readouterr().out
    assert (
        f"Requeued cleanup task {CLEANUP_TASK_SESSION_EXPIRE} {session_id} "
        "due_at=2024-01-01T00:00:10+00:00"
    ) in output

    # Retrieve the store 
    store = Store(db_path, session_ttl_seconds=60)
    try:

        # Assert that the task is requeue correctly
        task = store.getCleanupTask(CLEANUP_TASK_SESSION_EXPIRE, session_id)
        assert task is not None
        assert task.blocked_at == ""
        assert task.blocked_reason == ""
        assert task.due_at == "2024-01-01T00:00:10+00:00"
        assert task.attempt_count == 0

        # Retrieve the Admin actions and assert accurate record
        actions = store.listCleanupAdminActions(limit=1)
        assert actions[0].action == "requeue"
        assert actions[0].actor == "operator-a"
        assert actions[0].operator_reason == "operator retried after backend recovery"
        assert actions[0].task_attempt_count == 1
        assert actions[0].task_first_failed_at == "2024-01-01T00:00:05+00:00"
    finally:
        store.close()


def test_cli_blocked_list_reports_empty_state(tmp_path, capsys):
    """Test that an empty store shows no blocked tasks message"""
    # Create an empty store
    db_path = str(tmp_path / "cli-empty" / "kf-boot")
    store = Store(db_path, session_ttl_seconds=60)
    store.close()

    # Run the list command
    cli.main(["cleanup", "blocked", "list", "--db-path", db_path])

    # Assert that the output indicates no blocked task
    output = capsys.readouterr().out
    assert "No blocked cleanup tasks." in output


def test_cli_blocked_list_rejects_non_positive_limit(tmp_path, capsys):
    """Test that the CLI validates positive list limits before running commands."""

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["cleanup", "blocked", "list", "--db-path", str(tmp_path), "--limit", "0"])

    assert excinfo.value.code == 2
    assert "value must be greater than 0" in capsys.readouterr().err


def test_cli_blocked_dismiss_enforces_safety_checks_and_forced_workflow(tmp_path, capsys):
    """Exercise the full unsafe-dismiss workflow before allowing a forced dismissal."""
    db_path, session_id = makeBlockedTask(tmp_path)

    # First try to dismiss an unsafe task without force
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["cleanup", "blocked", "dismiss", "--db-path", db_path, CLEANUP_TASK_SESSION_EXPIRE, session_id])

    # Assert that the error message indicates refusal to dismiss
    assert excinfo.value.code == 1
    error_output = capsys.readouterr().err
    assert "Refusing to dismiss blocked cleanup task" in error_output
    assert "dismiss_reason:" in error_output

    store = Store(db_path, session_ttl_seconds=60)
    try:
        task = store.getCleanupTask(CLEANUP_TASK_SESSION_EXPIRE, session_id)
        assert task is not None
    finally:
        store.close()

    # Force is required for unsafe dismissal, but the operator must include a reason
    with pytest.raises(SystemExit) as excinfo:
        cli.main([
            "cleanup",
            "blocked",
            "dismiss",
            "--db-path",
            db_path,
            "--force",
            CLEANUP_TASK_SESSION_EXPIRE,
            session_id,
        ])

    assert excinfo.value.code == 1
    assert "Forced dismissal requires an operator reason." in capsys.readouterr().err

    # Once the operator explicitly forces the action with a reason, the queue record is removed
    cli.main([
        "cleanup",
        "blocked",
        "dismiss",
        "--db-path",
        db_path,
        "--force",
        "--actor",
        "operator-b",
        "--reason",
        "cleanup confirmed manually in downstream systems",
        CLEANUP_TASK_SESSION_EXPIRE,
        session_id,
    ])

    output = capsys.readouterr().out
    assert f"Dismissed blocked cleanup task queue record {CLEANUP_TASK_SESSION_EXPIRE} {session_id}" in output
    assert "warning: forced dismiss bypassed local cleanup assurance checks" in output

    store = Store(db_path, session_ttl_seconds=60)
    try:
        actions = store.listCleanupAdminActions(limit=1)
        assert store.getCleanupTask(CLEANUP_TASK_SESSION_EXPIRE, session_id) is None
        assert actions[0].action == "dismiss"
        assert actions[0].forced is True
        assert actions[0].actor == "operator-b"
        assert actions[0].operator_reason == "cleanup confirmed manually in downstream systems"
    finally:
        store.close()


def test_cli_blocked_dismiss_allows_safe_orphan_task(tmp_path, capsys):
    """Test that blocked orphan tasks with no resources are safe to dismiss"""
    # Create a blocked orphan task
    db_path = makeBlockedOrphanTask(tmp_path)

    # Attempt to dismiss the task
    cli.main([
        "cleanup",
        "blocked",
        "dismiss",
        "--db-path",
        db_path,
        CLEANUP_TASK_SESSION_CLEANUP,
        "missing-session",
    ])

    # Assert that the dismissal was successful
    output = capsys.readouterr().out
    assert "Dismissed blocked cleanup task queue record session_cleanup missing-session" in output

    store = Store(db_path, session_ttl_seconds=60)
    try:
        assert store.getCleanupTask(CLEANUP_TASK_SESSION_CLEANUP, "missing-session") is None
    finally:
        store.close()


def test_cli_blocked_dismiss_refuses_orphan_task_with_leftover_resources(tmp_path, capsys):
    """Test that blocked orphan task with resources are not safe to dismiss without force and reason"""

    # Create an orphan task with resources
    db_path = makeBlockedOrphanTaskWithResource(tmp_path)

    # Attempt to dismiss the task without force or reason
    with pytest.raises(SystemExit) as excinfo:
        cli.main([
            "cleanup",
            "blocked",
            "dismiss",
            "--db-path",
            db_path,
            CLEANUP_TASK_SESSION_CLEANUP,
            "missing-session",
        ])

    # Assert the dismissal is refused
    assert excinfo.value.code == 1
    error_output = capsys.readouterr().err
    assert "Refusing to dismiss blocked cleanup task" in error_output
    assert "orphaned resources" in error_output


def test_cli_blocked_dismiss_refuses_delete_phase_task_while_subject_row_exists(tmp_path, capsys):
    """Test that delete-phase tasks still require force while the row exists."""

    db_path = str(tmp_path / "cli-session-delete" / "kf-boot")
    store = Store(db_path, session_ttl_seconds=60)
    session = store.createSession(
        ephemeral_aid="E-cli-delete",
        account_aid="A-cli-delete",
        account_alias="alpha",
        chosen_profile_code="1-of-1",
        client_ip="127.0.0.1",
        region_id="test-region",
        region_name="Test Region",
        watcher_required=True,
        witness_count=1,
        toad=1,
        account_tier="trial",
    )
    session.state = SESSION_STATE_EXPIRED
    session.expired_at = "2024-01-01T00:00:00+00:00"
    session.resources_cleaned_at = "2024-01-01T00:00:01+00:00"
    store.saveSession(session)
    store.claimDueCleanupTask(now="2024-01-01T00:00:02+00:00")
    store.blockCleanupTask(
        CLEANUP_TASK_SESSION_DELETE,
        session.session_id,
        now="2024-01-01T00:00:03+00:00",
        blocked_reason="simulated operator task",
        last_error="simulated failure",
        first_failed_at="2024-01-01T00:00:02+00:00",
    )
    store.close()

    with pytest.raises(SystemExit) as excinfo:
        cli.main([
            "cleanup",
            "blocked",
            "dismiss",
            "--db-path",
            db_path,
            CLEANUP_TASK_SESSION_DELETE,
            session.session_id,
        ])

    assert excinfo.value.code == 1
    error_output = capsys.readouterr().err
    assert "Refusing to dismiss blocked cleanup task" in error_output
    assert "delete phase" in error_output
