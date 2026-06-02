from __future__ import annotations

import argparse
import logging
import os
import sys

from hio.base import doing
from keri import help

from kfboot.basing import (
    CLEANUP_TASK_ACCOUNT_CLEANUP,
    CLEANUP_TASK_ACCOUNT_DELETE,
    CLEANUP_TASK_ACCOUNT_EXPIRE,
    CLEANUP_TASK_SESSION_CLEANUP,
    CLEANUP_TASK_SESSION_DELETE,
    CLEANUP_TASK_SESSION_EXPIRE,
    CleanupTaskRecord,
)
from kfboot.store import (
    BlockedTaskDismissAssessment,
    CleanupTaskNotBlockedError,
    CleanupTaskNotFoundError,
    ForcedDismissReasonRequiredError,
    RequeueReasonRequiredError,
    Store,
    UnsafeBlockedTaskDismissError,
    nowIso,
)
from kfboot.runtime import setup

logger = help.ogler.getLogger(__name__)

CLI_CLEANUP_TASK_KINDS = (
    CLEANUP_TASK_SESSION_EXPIRE,
    CLEANUP_TASK_SESSION_CLEANUP,
    CLEANUP_TASK_SESSION_DELETE,
    CLEANUP_TASK_ACCOUNT_EXPIRE,
    CLEANUP_TASK_ACCOUNT_CLEANUP,
    CLEANUP_TASK_ACCOUNT_DELETE,
)


def configure_logging(level: int = logging.INFO) -> None:
    """Set up console logging for the CLI and KERI logger.

    This configures the root logger and also updates KERI's console formatter
    so that log output uses the same application format.
    """

    # Root formatter for all console output.
    formatter = logging.Formatter(
        "%(asctime)s: %(levelname)s from %(name)s \n%(message)s\n"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if not root_logger.handlers:
        # No handlers yet: create one and attach the formatter.
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        handler.setLevel(level)
        root_logger.addHandler(handler)
    else:
        # Existing handlers may have no formatter or too-high levels.
        for handler in root_logger.handlers:
            handler.setLevel(level)
            if handler.formatter is None:
                handler.setFormatter(formatter)

    # Update the KERI logger helper so its console output uses the same formatter.
    if hasattr(help.ogler, "baseConsoleHandler"):
        help.ogler.baseConsoleHandler.setFormatter(formatter)
    if hasattr(help.ogler, "baseFormatter"):
        help.ogler.baseFormatter = formatter

    # Ensure any previously-created logger objects are not stuck at CRITICAL.
    for existing_logger in logging.Logger.manager.loggerDict.values():
        if isinstance(existing_logger, logging.Logger):
            if existing_logger.level > level:
                existing_logger.setLevel(level)

    # If the KERI logger helper exposes a level setter, apply it too.
    try:
        if hasattr(help.ogler, "basicConfig"):
            help.ogler.basicConfig(level=level)
        elif hasattr(help.ogler, "setLevel"):
            help.ogler.setLevel(level)
        elif hasattr(help.ogler, "setLogLevel"):
            help.ogler.setLogLevel(level)
    except Exception:
        pass


def _defaultDbPath() -> str:
    return os.environ.get("KF_BOOT_DB_PATH", "./var/kf-boot")


def _positiveInt(value: str) -> int:
    """Parse a positive integer for CLI list limits."""

    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _buildParser() -> argparse.ArgumentParser:
    
    # Build the top-level parser and subparsers for each CLI command 
    parser = argparse.ArgumentParser(
        prog="kf-boot",
        description="KERI Foundation boot service and local operator tools.",
    )
    subparsers = parser.add_subparsers(dest="command")
    
    # Add the "serve" command
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the boot service HTTP server.",
    )
    # Runs the server by default
    serve_parser.set_defaults(func=_runServe)

    # Add db path argument
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument(
        "--db-path",
        default=_defaultDbPath(),
        help="Path to the local kf-boot LMDB store.",
    )

    # Set up the cleanup parser
    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Inspect and repair local cleanup-task state.",
    )
    cleanup_subparsers = cleanup_parser.add_subparsers(dest="cleanup_command", required=True)

    # Add the "blocked" command
    blocked_parser = cleanup_subparsers.add_parser(
        "blocked",
        help="Manage blocked cleanup tasks.",
    )
    blocked_subparsers = blocked_parser.add_subparsers(dest="blocked_command", required=True)

    # Add the "list" subcommand for blocked tasks
    blocked_list = blocked_subparsers.add_parser(
        "list",
        parents=[db_parent],
        help="List blocked cleanup tasks.",
    )
    blocked_list.add_argument(
        "--kind",
        choices=CLI_CLEANUP_TASK_KINDS,
        help="Filter blocked tasks to one cleanup task kind.",
    )
    blocked_list.add_argument(
        "--limit",
        type=_positiveInt,
        default=None,
        help="Maximum number of blocked tasks to show.",
    )
    blocked_list.set_defaults(func=_runBlockedList)

    # Add the "show" subcommand for a particular blocked task
    blocked_show = blocked_subparsers.add_parser(
        "show",
        parents=[db_parent],
        help="Show one blocked cleanup task.",
    )
    blocked_show.add_argument("kind", choices=CLI_CLEANUP_TASK_KINDS, help="Cleanup task kind.")
    blocked_show.add_argument("subject", help="Cleanup task subject.")
    blocked_show.set_defaults(func=_runBlockedShow)

    # Add the "requeue" subcommand for a particular blocked task
    blocked_requeue = blocked_subparsers.add_parser(
        "requeue",
        parents=[db_parent],
        help="Requeue one blocked cleanup task after fixing its root cause.",
    )
    blocked_requeue.add_argument("kind", choices=CLI_CLEANUP_TASK_KINDS, help="Cleanup task kind.")
    blocked_requeue.add_argument("subject", help="Cleanup task subject.")
    blocked_requeue.add_argument(
        "--reason",
        required=True,
        help="Operator note recorded in the cleanup action audit trail.",
    )
    blocked_requeue.add_argument(
        "--actor",
        default="",
        help="Override the local operator name stored in the cleanup action audit trail.",
    )
    blocked_requeue.set_defaults(func=_runBlockedRequeue)

    blocked_dismiss = blocked_subparsers.add_parser(
        "dismiss",
        parents=[db_parent],
        help="Dismiss one blocked cleanup task queue record after operator review.",
    )
    blocked_dismiss.add_argument("kind", choices=CLI_CLEANUP_TASK_KINDS, help="Cleanup task kind.")
    blocked_dismiss.add_argument("subject", help="Cleanup task subject.")
    blocked_dismiss.add_argument(
        "--force",
        action="store_true",
        help=(
            "Dismiss the blocked queue record even when local state still shows "
            "cleanup debt. Use only after verifying cleanup out of band."
        ),
    )
    blocked_dismiss.add_argument(
        "--reason",
        default="",
        help="Operator note recorded in the cleanup action audit trail.",
    )
    blocked_dismiss.add_argument(
        "--actor",
        default="",
        help="Override the local operator name stored in the cleanup action audit trail.",
    )
    blocked_dismiss.set_defaults(func=_runBlockedDismiss)

    return parser


def _storeForCli(db_path: str) -> Store:
    """Open only the durable task store needed by local operator commands."""

    return Store(db_path)


def _printTaskSummary(task: CleanupTaskRecord) -> None:
    print(
        f"{task.kind} {task.subject} "
        f"blocked_at={task.blocked_at or '-'} "
        f"attempts={task.attempt_count} "
        f"reason={task.blocked_reason or '-'}"
    )


def _printTaskDetails(task: CleanupTaskRecord) -> None:
    print(f"kind: {task.kind}")
    print(f"subject: {task.subject}")
    print(f"due_at: {task.due_at or ''}")
    print(f"attempt_count: {task.attempt_count}")
    print(f"created_at: {task.created_at}")
    print(f"updated_at: {task.updated_at}")
    print(f"claimed_at: {task.claimed_at}")
    print(f"last_attempt_at: {task.last_attempt_at}")
    print(f"first_failed_at: {task.first_failed_at}")
    print(f"last_error: {task.last_error}")
    print(f"blocked_at: {task.blocked_at}")
    print(f"blocked_reason: {task.blocked_reason}")


def _printDismissAssessment(assessment: BlockedTaskDismissAssessment) -> None:
    print(f"dismiss_safe: {'yes' if assessment.safe_to_dismiss else 'no'}")
    print(f"cleanup_assured: {'yes' if assessment.cleanup_assured else 'no'}")
    print(f"local_resource_count: {assessment.local_resource_count}")
    print(f"local_related_record_count: {assessment.local_related_record_count}")
    print(f"subject_exists: {'yes' if assessment.subject_exists else 'no'}")
    print(f"subject_state: {assessment.subject_state}")
    print(f"resources_cleaned_at: {assessment.resources_cleaned_at}")
    print(f"dismiss_reason: {assessment.reason}")


def _blockedTaskOrExit(store: Store, *, kind: str, subject: str) -> CleanupTaskRecord:
    # Get the task
    task = store.getCleanupTask(kind, subject)
    
    # Check if it exists
    if task is None:
        print(
            f"Cleanup task not found for kind={kind!r} subject={subject!r}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    
    # Check if it is indeed blocked
    if not task.blocked_at:
        print(
            f"Cleanup task {kind!r} {subject!r} is not blocked.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # return the task
    return task


def _runServe(_args: argparse.Namespace) -> None:
    _app, ctx, doers = setup()
    try:
        logger.info(
            f"Server starting on http://{ctx.config.host}:{ctx.config.port}\n"
            f"Onboarding surface: {ctx.config.onboarding_public_url}\n"
            f"Account surface: {ctx.config.account_public_url}"
        )
        doist = doing.Doist(name="kf-boot", real=True, tock=0.00125)
        doist.do(doers=doers)
    finally:
        ctx.close()
        logger.info(
            "Server stopped"
        )


def _runBlockedList(args: argparse.Namespace) -> None:
    # Get the Store
    store = _storeForCli(args.db_path)
    try:
        # Retrieve list of blocked tasks
        rows = store.listBlockedCleanupTasks(kind=args.kind, limit=args.limit)
        if not rows:
            print("No blocked cleanup tasks.")
            return
        # Print the tasks in the summary format
        for task in rows:
            _printTaskSummary(task)
    finally:
        store.close()


def _runBlockedShow(args: argparse.Namespace) -> None:
    # Get the Store
    store = _storeForCli(args.db_path)
    try:
        # Validate the task
        task = _blockedTaskOrExit(store, kind=args.kind, subject=args.subject)
        
        # Get the task details
        _printTaskDetails(task)
        _printDismissAssessment(store.blockedCleanupTaskDismissAssessment(args.kind, args.subject))
    finally:
        store.close()


def _runBlockedRequeue(args: argparse.Namespace) -> None:
    # Get the Store
    store = _storeForCli(args.db_path)
    try:
        try:
            requeued = store.requeueBlockedCleanupTask(
                args.kind,
                args.subject,
                now=nowIso(),
                actor=args.actor,
                operator_reason=args.reason,
            )
        except CleanupTaskNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc
        except CleanupTaskNotBlockedError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc
        except RequeueReasonRequiredError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc

        # Requeueing clears the blocked state and makes the task due immediately
        # so the local cleanup runner can pick it up on the next sweep.
        print(
            f"Requeued cleanup task {requeued.kind} {requeued.subject} "
            f"due_at={requeued.due_at}"
        )
    finally:
        store.close()


def _runBlockedDismiss(args: argparse.Namespace) -> None:
    store = _storeForCli(args.db_path)
    try:
        assessment = store.blockedCleanupTaskDismissAssessment(args.kind, args.subject)
        try:
            dismissed = store.dismissBlockedCleanupTask(
                args.kind,
                args.subject,
                now=nowIso(),
                actor=args.actor,
                operator_reason=args.reason,
                force=args.force,
            )
        except CleanupTaskNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc
        except CleanupTaskNotBlockedError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc
        except UnsafeBlockedTaskDismissError as exc:
            print(
                "Refusing to dismiss blocked cleanup task because local state still "
                "shows cleanup debt. Requeue it after fixing the root cause, or "
                "rerun with --force after verifying cleanup out of band.",
                file=sys.stderr,
            )
            print(f"dismiss_reason: {exc.assessment.reason}", file=sys.stderr)
            raise SystemExit(1) from exc
        except ForcedDismissReasonRequiredError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from exc

        # Dismissing a blocked task only removes queue state. It does not perform
        # resource cleanup, which is why the store enforces safety checks before
        # allowing non-forced dismissal.
        print(f"Dismissed blocked cleanup task queue record {dismissed.kind} {dismissed.subject}")
        if args.force and not assessment.safe_to_dismiss:
            print("warning: forced dismiss bypassed local cleanup assurance checks")
    finally:
        store.close()


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    argv = list(argv) if argv is not None else sys.argv[1:]
    if not argv:
        _runServe(argparse.Namespace())
        return

    parser = _buildParser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        raise SystemExit(2)
    func(args)


if __name__ == "__main__":
    main()
