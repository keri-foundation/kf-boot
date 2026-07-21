# kf-boot

`kf-boot` is the KERI Foundation boot service for hosted witness and watcher onboarding.

This repository implements the boot service contract shared with `locksmith`.
Keep the implementation, README contract, and client integration aligned.

# To run

Install the package in editable mode first so the `src/` layout and console
script both resolve correctly:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
python -m pip install -e '.[dev]'
```

```
export KF_BOOT_HOST=127.0.0.1
export KF_BOOT_PORT=9723
export KF_BOOT_WIT_BOOT_URL=http://127.0.0.1:5631
export KF_BOOT_WIT_PUBLIC_URL=http://127.0.0.1:5632
export KF_BOOT_WAT_BOOT_URL=http://127.0.0.1:7631
export KF_BOOT_WAT_PUBLIC_URL=http://127.0.0.1:7632
kf-boot
```
This will work if witness and watcher services are running on the same machine.

## Scope

`kf-boot` owns:

- public bootstrap discovery
- authenticated onboarding
- authenticated approved-account management
- signed boot-server replies
- onboarding session metadata
- account metadata
- hosted witness and watcher allocation through local boot APIs

`kf-boot` does not own:

- local AID creation
- local key custody
- signing on behalf of users
- witness registration or witness-auth UX
- watcher OOBI resolution or watcher protocol flows
- local watcher or sidecar behavior

## Public Surfaces

The contract has two public trust domains:

- onboarding surface: ephemeral first contact and onboarding
- approved-account surface: management for already onboarded accounts

This implementation keeps one codebase, but the public interface still models two surfaces:

- onboarding CESR ingress: `POST /onboarding`
- approved-account CESR ingress: `POST /account`

Discovery remains plain JSON:

- `GET /health`
- `GET /bootstrap/config`

`GET /bootstrap/config` also advertises the separate onboarding and account surface URLs for local testing.

## Transport And Auth

Public discovery is plain HTTPS JSON.

All authenticated business routes use:

- CESR-over-HTTP
- KRAM
- signed KERI `exn` request/reply exchanges

This service does not use ESSR.

Auth principals:

- onboarding routes are authenticated by the hidden ephemeral onboarding AID
- approved-account routes are authenticated by the permanent account AID

First-contact rule:

- the first authenticated onboarding business message must include or be preceded by the ephemeral AID inception or keystate material so the server can resolve sender state for KRAM

Boot-server authentication:

- `kf-boot` has a durable boot-server `Habery` and durable boot-server `Hab`
- signed replies prepend the boot-server KEL
- clients verify the service by percolated discovery
- clients do not need preinstalled boot-server inception material

## Message Contract

Authenticated business routes are signed KERI `exn` exchanges.

Onboarding routes:

- `exn /onboarding/session/start`
- `exn /onboarding/session/status`
- `exn /onboarding/account/create`
- `exn /onboarding/complete`
- `exn /onboarding/cancel`
- `exn /operations/status`

Approved-account routes:

- `exn /account/witnesses`
- `exn /account/watchers`
- `exn /account/watchers/status`
- `exn /account/delete`
- `exn /account/witnesses/delete`
- `exn /account/watchers/delete`
- `exn /operations/status`

There is no JSON bearer-session flow for these routes.
There are no authenticated `qry` routes in this slice.

## Runtime Shape

`kf-boot` uses the real `keripy` CESR/KRAM path:

- Falcon CESR intake
- `Parser`
- `Kevery`
- `Kramer`
- `Exchanger`

The service runs under one root HIO `Doist`:

- Falcon is served by an HIO HTTP server doer.
- downstream witness/watcher boot calls run as HIO client work through the boot operation worker.
- request handlers persist boot operation intent and return signed status payloads without blocking on downstream boot APIs.
- periodic lifecycle cleanup is a normal HIO doer on the same runtime.

The server does not manually trust `serder.pre` as an auth substitute.
Business handlers run only after the request has been parsed through the KRAM-enabled `Kevery`.

## Onboarding Flow

1. `locksmith` fetches `GET /bootstrap/config`.
2. `locksmith` creates or selects the permanent local account AID.
3. `locksmith` creates a hidden ephemeral onboarding AID locally.
4. `locksmith` sends the ephemeral inception or keystate material to the onboarding surface.
5. `locksmith` sends authenticated `exn /onboarding/session/start` with the permanent account AID.
6. `kf-boot` creates or reuses a durable session provisioning operation and replies with a signed boot-server `exn`.
7. The root HIO boot operation worker allocates the witness pool and required hosted watcher.
8. `locksmith` polls `exn /operations/status` or `exn /onboarding/session/status` until provisioning succeeds.
9. `locksmith` registers the permanent account AID with the allocated witnesses, rotates it onto that witness set, and resolves the witness and watcher OOBIs.
10. `locksmith` sends authenticated `exn /onboarding/account/create`.
11. `locksmith` sends authenticated `exn /onboarding/complete`.
12. Future operations move to the approved-account surface and use the permanent account AID.

## Product Rules

- one vault maps to one onboarded KF account
- the permanent account AID is always a local wallet AID
- witness profile is `1-of-1` or `3-of-4`
- `1-of-1` means one distinct configured witness backend
- `3-of-4` means four distinct configured witness backends
- the permanent account AID is supplied when the onboarding session starts
- the service allocates the witness pool before the account AID rotates onto it
- one hosted watcher is required before onboarding completes

## State And Retry Rules

Session states:

- `started`
- `witness_pool_allocated`
- `account_created`
- `completed`
- `expired`
- `failed`
- `cancelled`

Account states:

- `pending_onboarding`
- `onboarded`
- `failed`

Required behavior:

- onboarding sessions expire and can be marked expired on access
- `/onboarding/session/start` reuses the same active session resources for retry instead of allocating duplicates
- `/onboarding/session/start` returns a `session_provision_operation` while downstream resource allocation is pending
- `/onboarding/account/create` rejects the request while session provisioning is pending or failed
- `/onboarding/account/create` is idempotent within a session
- `/onboarding/complete` is idempotent within a session
- watcher status, hosted-resource delete, and account delete routes return durable boot operations
- retryable downstream failures leave the operation pending with `last_error`
- non-retryable downstream failures, or retry exhaustion, fail the operation
- blind retry after a partial allocation does not create a second witness or watcher set

## Development

`kf-boot` runs on Python 3.14 and depends on the local `keripy` / `hio`
workspace repos for active development in this workspace.

Create or refresh the local venv:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
python -m pip install -e '.[dev]'
```

Run tests:

```bash
source .venv/bin/activate
pytest tests
ruff check src tests
```

Required environment:

- `KF_BOOT_WITNESS_BACKENDS`
- `KF_BOOT_WAT_BOOT_URL`
- `KF_BOOT_WAT_PUBLIC_URL`

Optional environment:

- `KF_BOOT_WIT_BOOT_URL` legacy single-backend fallback
- `KF_BOOT_WIT_PUBLIC_URL` legacy single-backend fallback
- `KF_BOOT_DB_PATH`
- `KF_BOOT_KERI_DIR` optional KERI head directory root override. When unset,
  KERI uses its normal default path resolution (`/usr/local/var` with
  `~/.keri` fallback).
- `KF_BOOT_KERI_NAME`
- `KF_BOOT_BOOT_HAB_NAME`
- `KF_BOOT_ONBOARDING_PUBLIC_URL`
- `KF_BOOT_ACCOUNT_PUBLIC_URL`
- `KF_BOOT_ONBOARDING_PATH`
- `KF_BOOT_ACCOUNT_PATH`
- `KF_BOOT_SESSION_TTL_SECONDS`


## CLI Commands

`kf-boot` has two roles:

- running the boot service
- local operator inspection and repair of blocked cleanup tasks

Start the server:

```bash
kf-boot
```

or explicitly:

```bash
kf-boot serve
```

Blocked cleanup task commands operate directly on the local LMDB store and are
intended for local operator use on the same machine:

```bash
kf-boot cleanup blocked list --db-path ./var/kf-boot
kf-boot cleanup blocked show --db-path ./var/kf-boot KIND SUBJECT
kf-boot cleanup blocked requeue --db-path ./var/kf-boot --reason "operator note" KIND SUBJECT
kf-boot cleanup blocked dismiss --db-path ./var/kf-boot KIND SUBJECT
```

Common options:

- `--db-path` points at the local `kf-boot` LMDB store. When omitted, the CLI
  uses `KF_BOOT_DB_PATH` or `./var/kf-boot`.
- `--actor NAME` optionally overrides the local operator name stored in the
  cleanup action audit trail for `requeue` and `dismiss`.
- `list --kind KIND` filters blocked tasks to one cleanup kind. `KIND` must be
  one of the known cleanup task kinds.
- `list --limit N` limits how many blocked tasks are shown. `N` must be greater
  than `0`.

Blocked task workflow:

1. Run `list` to see which cleanup tasks are blocked.
2. Run `show` for one task to inspect `blocked_reason`, `last_error`,
   `attempt_count`, and the dismiss safety assessment.
3. If the root cause is fixed and cleanup should run again, use `requeue`.
4. If the task is redundant or cleanup was already handled, use `dismiss`.

Important behavior:

- `requeue` clears the blocked state and makes the task due immediately so the
  cleanup runner can try it again on the next sweep.
- `requeue` requires `--reason` so the audit trail records why the operator put
  the blocked task back on the runnable queue.
- `dismiss` removes the blocked task queue record only. It does not perform
  cleanup by itself.
- `show` prints a local safety assessment for dismissal:
  - `dismiss_safe: yes` means local state suggests the task is redundant or the
    cleanup phase is already complete, meaning the queue record is safe to dismiss.
  - `dismiss_safe: no` means dismissing the task would likely abandon cleanup debt.
- `dismiss` refuses unsafe removals by default. Use `--force --reason ...` only
  after an operator has verified that cleanup really happened and only the
  blocked queue record should be removed.
