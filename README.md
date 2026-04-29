# kf-boot

`kf-boot` is the KERI Foundation boot service for hosted witness and watcher onboarding.

This repository implements the frozen conference v1 contract shared with `locksmith-kf`.
Do not reopen the contract casually. If implementation and contract drift, fix one deliberately.

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

Conference v1 does not use ESSR.

Auth principals:

- onboarding routes are authenticated by the hidden ephemeral onboarding AID
- approved-account routes are authenticated by the permanent account AID

First-contact rule:

- the first authenticated onboarding business message must include or be preceded by the ephemeral AID inception or keystate material so the server can resolve sender state for KRAM

Boot-server authentication:

- `kf-boot` has a durable boot-server `Habery` and durable boot-server `Hab`
- signed replies prepend the boot-server KEL
- clients verify the service by percolated discovery
- conference v1 does not require preinstalled boot-server inception material

## Frozen Message Contract

Authenticated business routes are all `exn` in conference v1.

Onboarding routes:

- `exn /onboarding/session/start`
- `exn /onboarding/session/status`
- `exn /onboarding/account/create`
- `exn /onboarding/complete`
- `exn /onboarding/cancel`

Approved-account routes:

- `exn /account/witnesses`
- `exn /account/watchers`
- `exn /account/watchers/status`
- `exn /account/witnesses/delete`
- `exn /account/watchers/delete`

There is no JSON bearer-session flow for these routes.
There are no authenticated `qry` routes in this slice.

## Runtime Shape

`kf-boot` uses the real `keripy` CESR/KRAM path:

- Falcon CESR intake
- `Parser`
- `Kevery`
- `Kramer`
- `Exchanger`

The server does not manually trust `serder.pre` as an auth substitute.
Business handlers run only after the request has been parsed through the KRAM-enabled `Kevery`.

## Onboarding Flow

1. `locksmith-kf` fetches `GET /bootstrap/config`.
2. `locksmith-kf` creates a hidden ephemeral onboarding AID locally.
3. `locksmith-kf` sends the ephemeral inception or keystate material to the onboarding surface.
4. `locksmith-kf` sends authenticated `exn /onboarding/session/start`.
5. `kf-boot` allocates the witness pool before permanent account inception.
6. `kf-boot` creates the required hosted watcher and records the allocated resources before replying.
7. `kf-boot` replies with a signed boot-server `exn` and prepended boot-server KEL.
8. `locksmith-kf` creates the permanent local account AID using the returned witness list.
9. `locksmith-kf` finishes local witness registration and resolves witness and watcher OOBIs.
10. `locksmith-kf` sends authenticated `exn /onboarding/account/create`.
11. `locksmith-kf` sends authenticated `exn /onboarding/complete`.
12. Future operations move to the approved-account surface and use the permanent account AID.

## Frozen Product Rules

- one vault maps to one onboarded KF account in v1
- the permanent account AID is always a local wallet AID
- witness profile is `1-of-1` or `3-of-4`
- `1-of-1` means one distinct configured witness backend
- `3-of-4` means four distinct configured witness backends
- the service allocates the witness pool before permanent account inception
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
- `/onboarding/account/create` is idempotent within a session
- `/onboarding/complete` is idempotent within a session
- created witness and watcher ids are persisted before replies are sent
- blind retry after a failed allocation does not create a second witness or watcher set
- partial downstream failure moves the session to `failed`

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

## Docker Compose Distributed Dev Topology

`kf-boot` includes a compose topology that models a distributed deployment:

- four witness containers (`wit-1` to `wit-4`)
- one watcher container
- one `kf-boot` container

Use the compose file from the repository root:

```bash
docker compose -f docker-compose.distributed-dev.yml up --build
```

Exposed host ports:

- `5632`, `5642`, `5652`, `5662` (witness public HTTP)
- `7632` (watcher public HTTP)
- `9723` (`kf-boot` API)

The compose stack defaults public URLs to `localhost`. To advertise a different
host (for remote wallet clients), set:

```bash
export KF_BOOT_PUBLIC_HOST=192.0.2.10
docker compose -f docker-compose.distributed-dev.yml up --build
```

See `setup-guide.md` section "Docker Compose distributed development deployment"
for more details.

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
