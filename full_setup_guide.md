# Set up a 3-of-4 KERI pool across four hosts

For a high-level overview of the services, how they fit together, and recommended counts, see **[stack_overview.md](./stack_overview.md)**.

This guide runs a **`3-of-4`** witness threshold on **four machines**. Each host runs **one** witness (`witopnet`), **one** watcher (`watopnet`), and **one** extra service so load is spread evenly: **kf-boot** (+ nginx) on Host 1, **kf-registrar** on Host 2, **kf-observer** on Host 3, and **kf-mailbox** on Host 4. **Circus** supervises the app processes on each host, and **systemd** starts Circus.

**kf-boot** offers the frozen **`3-of-4`** account option only when it has at least **four** witness backends. This layout is that set: one backend per host.

**What you are installing**

- **[kf-boot](https://github.com/keri-foundation/kf-boot)** — HTTP service that exposes health, bootstrap configuration, onboarding, and account APIs for KERI clients. It runs on **Host 1** only and listens on **loopback**; nginx on Host 1 serves the public HTTPS URLs.
- **[kf-mailbox](https://github.com/keri-foundation/kf-mailbox)** — store-and-forward mailbox provider. Runs on **Host 4**. **kf-boot** on Host 1 allocates mailboxes over the mailbox **boot** URL (`KF_BOOT_MBX_BOOT_URL`); wallets use the mailbox **public** URL returned at allocation.
- **`witopnet`** (package **[witness-hk](https://github.com/keri-foundation/witness-hk)**) — KERI **witness** process. Each host runs one instance, with its own config directory, database (**`--base`**), **boot** port (operator API that **kf-boot** calls), and **HTTP** port (the URL clients use).
- **`watopnet`** (package **[watcher-hk](https://github.com/keri-foundation/watcher-hk)**) — KERI **watcher** process. Each host runs one instance. **kf-boot** has a single watcher backend, so onboarding allocates watchers on **Host 1's** watcher. The watchers on Hosts 2–4 stay up as local watcher endpoints; point **`KF_BOOT_WAT_BOOT_URL`** and **`KF_BOOT_WAT_PUBLIC_URL`** at one of them only if you intend that host to be the allocation watcher.
- **[kf-registrar](https://github.com/keri-foundation/kf-registrar)** — ACDC TEL registrar (bulk TEL for observers). One instance on **Host 2**. Wallets do **not** talk to it; **kf-observer** does.
- **[kf-observer](https://github.com/keri-foundation/kf-observer)** — verified TEL query service for wallets/verifiers. One instance on **Host 3**. It polls the registrar and falls back to a witness for issuer KELs when needed.

**Why mailbox on Host 4:** There is no protocol requirement that mailbox share a host with **kf-boot**—only that Host 1 can reach the mailbox **boot** URL and clients can reach the mailbox **public** URL. Putting mailbox on Host 4 keeps one “extra” service per machine (boot / registrar / observer / mailbox) and spreads disk and CPU.

**Reference implementation:** this repo’s **`docker-compose.yml`** matches this guide’s DigitalOcean co-location: **kf-boot** on Host 1, **kf-registrar** on Host 2, **kf-observer** on Host 3, **kf-mailbox** on Host 4. Use it as the worked example for ports, env vars, and the **boot vs public URL** split. The steps below are the bare-metal equivalent (plus Circus, systemd, and nginx).

**Startup order:**

1. Witness and watcher on **every** host.
2. **kf-observer** on Host 3 (create habitat / export AID+KEL, then start).
3. Copy the observer AID+KEL to Host 2, start **kf-registrar**, introduce the observer KEL (`POST /introductions`).
4. **kf-mailbox** on Host 4.
5. **kf-boot** on Host 1 (needs witness boots + mailbox boot reachable).

**kf-boot** must reach every witness **boot** URL in **`KF_BOOT_WITNESS_BACKENDS`** (and the mailbox boot URL) before onboarding allocates. Bring up Hosts 2–4 before relying on onboarding from Host 1.

---

## Host plan

| Host | Address | Processes |
|------|---------|-----------|
| Host 1 | `192.0.2.11` | `witopnet` (wit-1), `watopnet` (wat-1), `kf-boot`, nginx |
| Host 2 | `192.0.2.12` | `witopnet` (wit-2), `watopnet` (wat-2), `kf-registrar` |
| Host 3 | `192.0.2.13` | `witopnet` (wit-3), `watopnet` (wat-3), `kf-observer` |
| Host 4 | `192.0.2.14` | `witopnet` (wit-4), `watopnet` (wat-4), `kf-mailbox` |

Addresses **`192.0.2.11`–`192.0.2.14`** are from **TEST-NET-1** (RFC 5737 documentation range). On a real network, substitute each host’s reachable address everywhere **`HOST_IP`** / boot URLs appear. Client-facing **`PUBLIC_*`** URLs must be whatever wallets can actually open (often the same address; see below).

`boot.example.com` is the nginx name for **kf-boot** on Host 1. Point that name at Host 1 when you terminate TLS.

Each host uses the **same ports** for wit/wat. The TEL and mailbox services add:

| Role | Boot / admin | HTTP / public (`0.0.0.0`) |
|------|--------------|---------------------------|
| Witness | 5631 (`--boothost` = this host) | 5632 |
| Watcher | 7631 (`--boothost` = this host) | 7632 |
| kf-mailbox (Host 4) | 6731 (`--boot-host` = Host 4) | 6732 |
| kf-registrar (Host 2) | 6631 | 6632 |
| kf-observer (Host 3) | — | 6633 |
| kf-boot (Host 1 only) | — | 9723 (loopback; nginx → 443) |

---

## Boot URL vs public URL

This is the most important consistency rule (and the main lesson from the Docker compose layout).

| Kind | Used by | Set where |
|------|---------|-----------|
| **Boot** | **kf-boot** → witness/watcher management APIs | `--boothost` / `--bootport`; `boot_url` in **`KF_BOOT_WITNESS_BACKENDS`**; **`KF_BOOT_WAT_BOOT_URL`** |
| **Public** | Wallets / Locksmith / allocation payloads | **`witopnet.curls`** / **`watopnet.curls`**; `public_url` in **`KF_BOOT_WITNESS_BACKENDS`**; **`KF_BOOT_WAT_PUBLIC_URL`** |

Rules:

1. Each witness **`public_url`** must equal **`witopnet.curls[0]`** in that host’s config file **`/opt/keri/witness/config/keri/cf/main/witopnet.json`** (same string; trailing slash optional but keep them consistent).
2. **`KF_BOOT_WAT_PUBLIC_URL`** must equal **`watopnet.curls[0]`** in Host 1’s **`/opt/keri/watcher/config/keri/cf/main/watopnet.json`**.
3. **Boot** and **public** need not share a hostname. Boot must be reachable **from Host 1**. Public must be reachable **from clients** (Locksmith, wallets).
4. Witness/watcher **HTTP** always binds **`0.0.0.0`**. **Boot** binds **`--boothost`** to that host’s operator address; firewall boot ports so only Host 1 can open them.

**Bare-metal example in this guide:** clients and kf-boot both use the host addresses, so boot and public share the same host:port base:

| Host | Witness `curls` / `public_url` | Witness `boot_url` | Watcher `curls` |
|------|--------------------------------|--------------------|-----------------|
| Host 1 | `http://192.0.2.11:5632/` | `http://192.0.2.11:5631` | `http://192.0.2.11:7632/` |
| Host 2 | `http://192.0.2.12:5632/` | `http://192.0.2.12:5631` | `http://192.0.2.12:7632/` |
| Host 3 | `http://192.0.2.13:5632/` | `http://192.0.2.13:5631` | `http://192.0.2.13:7632/` |
| Host 4 | `http://192.0.2.14:5632/` | `http://192.0.2.14:5631` | `http://192.0.2.14:7632/` |

**kf-boot** watcher backend (Host 1):

- **`KF_BOOT_WAT_BOOT_URL`** = `http://192.0.2.11:7631`
- **`KF_BOOT_WAT_PUBLIC_URL`** = `http://192.0.2.11:7632`

**Local Docker** (`docker-compose.yml`) keeps the same boot plane (`192.0.2.11`–`14`) for container-to-container calls, but advertises **published localhost ports** as public URLs so Locksmith on the Docker host can reach them (for example Host 4 witness public `http://127.0.0.1:45632`). See [Local Docker](#local-docker) at the end.

---

## 1. Baseline packages

Run this on **every** host:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y libsodium-dev build-essential curl git
```

On **Host 1** only, install nginx and optional certbot:

```bash
sudo apt install -y nginx
sudo apt install -y certbot python3-certbot-nginx
```

---

## 2. Service user and layout

Run this on **every** host. One witness tree and one watcher tree per machine (same layout the compose volumes mount under `/opt/keri/...`):

```bash
sudo useradd -r -m -d /opt/keri -s /usr/sbin/nologin keri 2>/dev/null || true
sudo mkdir -p /opt/keri/witness/venv \
  /opt/keri/witness/config/keri/cf/main \
  /opt/keri/watcher/{config/keri/cf/main,venv} \
  /opt/keri/bin /var/log/keri
sudo chown -R keri:keri /opt/keri /var/log/keri
```

On **Host 1** only, add the kf-boot directories:

```bash
sudo mkdir -p /opt/keri/kfboot/{venv,var,keri}
sudo chown -R keri:keri /opt/keri/kfboot
```

On **Host 2** only, add the registrar directories:

```bash
sudo mkdir -p /opt/keri/registrar/{venv,config/keri/cf,bootstrap}
sudo chown -R keri:keri /opt/keri/registrar
```

On **Host 3** only, add the observer directories:

```bash
sudo mkdir -p /opt/keri/observer/{venv,config/keri/cf,bootstrap}
sudo chown -R keri:keri /opt/keri/observer
```

On **Host 4** only, add the mailbox directories:

```bash
sudo mkdir -p /opt/keri/mailbox/venv
sudo chown -R keri:keri /opt/keri/mailbox
```

Paths used below:

```text
/opt/keri/witness/venv/                         # witopnet virtualenv
/opt/keri/witness/config/keri/cf/main/witopnet.json
/opt/keri/watcher/venv/
/opt/keri/watcher/config/keri/cf/main/watopnet.json
/opt/keri/kfboot/venv/                          # Host 1 only
/opt/keri/kfboot/var/                           # KF_BOOT_DB_PATH
/opt/keri/kfboot/keri/                          # KF_BOOT_KERI_DIR
/opt/keri/mailbox/venv/                         # Host 4 only
/opt/keri/registrar/venv/                       # Host 2 only
/opt/keri/registrar/config/keri/cf/kf-registrar.json
/opt/keri/registrar/bootstrap/                  # observer AID + KEL copies
/opt/keri/observer/venv/                        # Host 3 only
/opt/keri/observer/config/keri/cf/kf-observer.json
/opt/keri/observer/bootstrap/                   # exported observer AID + KEL
/opt/keri/bin/run-witopnet.sh
/opt/keri/bin/run-watopnet.sh
/opt/keri/bin/run-kf-boot.sh                    # Host 1 only
/opt/keri/bin/run-kf-mailbox.sh                 # Host 4 only
/opt/keri/bin/run-kf-registrar.sh               # Host 2 only
/opt/keri/bin/run-kf-observer.sh                # Host 3 only
/var/log/keri/
```

Convention: run commands as **`keri`** with **`sudo -Hu keri`**; **`uv`** on **`PATH`** via **`/opt/keri/.local/bin`**.

---

## 3. uv and Python 3.14.0

On **every** host:

```bash
sudo -Hu keri bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri && uv python install 3.14.0'
```

---

## 4. Install witness-hk, watcher-hk, kf-boot, mailbox, registrar, observer

Each line creates the venv, installs **`keri`** from **keripy** Git, then installs the package (same dependency set the Docker image installs).

On **every** host:

```bash
sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri/witness && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install "keri @ git+https://github.com/WebOfTrust/keripy.git" && uv pip install git+https://github.com/keri-foundation/witness-hk.git'

sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri/watcher && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install "keri @ git+https://github.com/WebOfTrust/keripy.git" && uv pip install git+https://github.com/keri-foundation/watcher-hk.git'
```

On **Host 1** only:

```bash
sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri/kfboot && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install "keri @ git+https://github.com/WebOfTrust/keripy.git" && uv pip install git+https://github.com/keri-foundation/kf-boot.git'
```

On **Host 2** only:

```bash
sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri/registrar && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install "keri @ git+https://github.com/WebOfTrust/keripy.git" && uv pip install git+https://github.com/keri-foundation/kf-registrar.git'
```

On **Host 3** only:

```bash
sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri/observer && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install "keri @ git+https://github.com/WebOfTrust/keripy.git" && uv pip install git+https://github.com/keri-foundation/kf-observer.git'
```

On **Host 4** only:

```bash
sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/keri/mailbox && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install "keri @ git+https://github.com/WebOfTrust/keripy.git" && uv pip install git+https://github.com/keri-foundation/kf-mailbox.git'
```

*To install from a local checkout, use **`uv pip install -e /path/to/<repo>`** in that service’s venv after **`keri`** is installed.*

---

## 5. Circus

On **every** host:

```bash
sudo mkdir -p /opt/circus && sudo chown keri:keri /opt/circus
sudo -Hu keri bash -c 'export PATH="/opt/keri/.local/bin:$PATH" && cd /opt/circus && uv venv --python 3.14.0 venv && . venv/bin/activate && uv pip install circus'
```

---

## 6. KERI config files

`--config-dir` is the directory **above** `keri/cf/`. Each host has a single witness, so **`--base witopnet`** is enough.

**`curls`** is the **public** URL for this host (what wallets open). It must match the `public_url` you put in **`KF_BOOT_WITNESS_BACKENDS`** (and for the watcher on Host 1, **`KF_BOOT_WAT_PUBLIC_URL`**).

`/opt/keri/witness/config/keri/cf/main/witopnet.json` on **Host 1**:

```json
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "witopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["http://192.0.2.11:5632/"]
  }
}
```

On Host 2 use **`http://192.0.2.12:5632/`**, on Host 3 **`http://192.0.2.13:5632/`**, on Host 4 **`http://192.0.2.14:5632/`**.

`/opt/keri/watcher/config/keri/cf/main/watopnet.json` on **Host 1**:

```json
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "watopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["http://192.0.2.11:7632/"]
  }
}
```

On Host 2 use **`http://192.0.2.12:7632/`**, on Host 3 **`http://192.0.2.13:7632/`**, on Host 4 **`http://192.0.2.14:7632/`**.

**Address binding (launch scripts below):**

- Witness **boot**: **`--boothost $HOST_IP`**, **`--bootport 5631`**.
- Witness **HTTP**: **`--host 0.0.0.0`**, **`--http 5632`** (listen); **`curls`** advertise **`$PUBLIC_WITNESS_URL`**.
- Watcher **boot**: **`--boothost $HOST_IP`**, **`--bootport 7631`**.
- Watcher **HTTP**: **`--host 0.0.0.0`**, **`--http 7632`**; **`curls`** advertise **`$PUBLIC_WATCHER_URL`**.

For **HTTPS** client URLs, put the `https://` URL in **`curls`** and the matching **`public_url`**, and terminate TLS in front of that HTTP port.

### Observer and registrar configs

These services also use a config file **above** `keri/cf/`. The launch scripts below rewrite them on start (same pattern as wit/wat).

**Observer → registrar bootstrap (no Docker shared volume):** current **kf-observer** builds expose **`/health`** and the query surface; they do **not** rely on a shared filesystem. On first bring-up:

1. Host 3 creates the observer habitat and exports **`observer.aid`** + **`observer.kel`** under **`/opt/keri/observer/bootstrap/`**.
2. Copy those two files to Host 2 at **`/opt/keri/registrar/bootstrap/`** (for example `scp`).
3. Host 2 starts **kf-registrar** with **`--observer <AID>`**, then **`POST`s** the KEL to **`/introductions`** on the registrar external port (**6632**).

After a rotation, re-export the KEL and re-introduce it the same way. The registrar allow-list is the observer **AID**; introduction supplies verification keys for that AID.

`/opt/keri/observer/config/keri/cf/kf-observer.json` on **Host 3** (URLs for Host 2 registrar and Host 1 witness):

```json
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "kf-observer": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["http://192.0.2.13:6633/"],
    "registrars": ["http://192.0.2.12:6632/"],
    "witnesses": ["http://192.0.2.11:5632/"]
  },
  "kram": {
    "enabled": true,
    "denials": [],
    "caches": {
      "~": [1000, 5000, 60000, 300000, 5000, 60000, 300000]
    }
  }
}
```

`/opt/keri/registrar/config/keri/cf/kf-registrar.json` on **Host 2** (after you know the observer AID):

```json
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "kf-registrar": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["http://192.0.2.12:6632/"],
    "observers": ["REPLACE_WITH_OBSERVER_AID"]
  },
  "kram": {
    "enabled": true,
    "denials": [],
    "caches": {
      "~": [1000, 5000, 60000, 300000, 5000, 60000, 300000]
    }
  }
}
```

Use one long-lived **`--passcode`** (keystore bran) for registrar and one for observer so **`--no-prompt`** can reopen the same habitats after restart. Store them outside the repo (for example 1Password). The examples below use a placeholder; replace it before production.

---

## 7. Launch scripts

These mirror **`docker/entrypoint-host*.sh`**: **`HOST_IP`** is the boot bind / operator address; **`PUBLIC_WITNESS_URL`** / **`PUBLIC_WATCHER_URL`** are what go into **`curls`**. On a normal LAN they are often the same host with different ports; keep them as separate variables so you can diverge when clients cannot reach the operator address (NAT, Docker published ports, CDN, etc.).

### `/opt/keri/bin/run-witopnet.sh` (every host)

Host 1 is shown. On Host 2 set **`HOST_IP=192.0.2.12`** and **`PUBLIC_WITNESS_URL=http://192.0.2.12:5632`**, and likewise for Hosts 3 and 4.

```bash
#!/usr/bin/env bash
set -euo pipefail
HOST_IP=192.0.2.11
PUBLIC_WITNESS_URL=http://192.0.2.11:5632
PUBLIC_WITNESS_URL="${PUBLIC_WITNESS_URL%/}"
export KERI_BASER_MAP_SIZE="${KERI_BASER_MAP_SIZE:-2147483648}"

cat > /opt/keri/witness/config/keri/cf/main/witopnet.json <<EOF
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "witopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["${PUBLIC_WITNESS_URL}/"]
  }
}
EOF

exec /opt/keri/witness/venv/bin/witopnet marshal start \
  --config-dir /opt/keri/witness/config \
  --base witopnet \
  --host 0.0.0.0 \
  --http 5632 \
  --boothost "$HOST_IP" \
  --bootport 5631 \
  --loglevel INFO \
  --logfile /var/log/keri/witopnet
```

### `/opt/keri/bin/run-watopnet.sh` (every host)

Same **`HOST_IP`** pattern; set **`PUBLIC_WATCHER_URL`** to that host’s watcher public URL.

```bash
#!/usr/bin/env bash
set -euo pipefail
HOST_IP=192.0.2.11
PUBLIC_WATCHER_URL=http://192.0.2.11:7632
PUBLIC_WATCHER_URL="${PUBLIC_WATCHER_URL%/}"
export KERI_BASER_MAP_SIZE="${KERI_BASER_MAP_SIZE:-2147483648}"

cat > /opt/keri/watcher/config/keri/cf/main/watopnet.json <<EOF
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "watopnet": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["${PUBLIC_WATCHER_URL}/"]
  }
}
EOF

exec /opt/keri/watcher/venv/bin/watopnet start \
  --config-dir /opt/keri/watcher/config \
  --host 0.0.0.0 \
  --http 7632 \
  --boothost "$HOST_IP" \
  --bootport 7631 \
  --loglevel INFO \
  --logfile /var/log/keri/watopnet
```

### `/opt/keri/bin/run-kf-boot.sh` (Host 1 only)

Use **`KF_BOOT_WITNESS_BACKENDS`**. Leave **`KF_BOOT_WIT_BOOT_URL`** and **`KF_BOOT_WIT_PUBLIC_URL`** unset. Each entry is **`id|boot_url|public_url`**:

- **`boot_url`** — how **kf-boot** on Host 1 reaches that witness boot API (operator plane).
- **`public_url`** — what wallets get; must match **`witopnet.curls[0]`** in that host’s **`/opt/keri/witness/config/keri/cf/main/witopnet.json`**.

**`KF_BOOT_WAT_BOOT_URL`** / **`KF_BOOT_WAT_PUBLIC_URL`** name Host 1’s watcher the same way (**`KF_BOOT_WAT_PUBLIC_URL`** ↔ **`watopnet.curls[0]`** in **`/opt/keri/watcher/config/keri/cf/main/watopnet.json`**).

Replace **`boot.example.com`** with the nginx vhost for **kf-boot**.

```bash
#!/usr/bin/env bash
set -euo pipefail
export KERI_BASER_MAP_SIZE="${KERI_BASER_MAP_SIZE:-2147483648}"
export KF_BOOT_HOST=127.0.0.1
export KF_BOOT_PORT=9723
export KF_BOOT_DB_PATH=/opt/keri/kfboot/var
export KF_BOOT_KERI_DIR=/opt/keri/kfboot/keri
export KF_BOOT_WITNESS_BACKENDS="wit-1|http://192.0.2.11:5631|http://192.0.2.11:5632,wit-2|http://192.0.2.12:5631|http://192.0.2.12:5632,wit-3|http://192.0.2.13:5631|http://192.0.2.13:5632,wit-4|http://192.0.2.14:5631|http://192.0.2.14:5632"
export KF_BOOT_WAT_BOOT_URL=http://192.0.2.11:7631
export KF_BOOT_WAT_PUBLIC_URL=http://192.0.2.11:7632
export KF_BOOT_MBX_BOOT_URL=http://192.0.2.14:6731
export KF_BOOT_ONBOARDING_PUBLIC_URL=https://boot.example.com/onboarding
export KF_BOOT_ACCOUNT_PUBLIC_URL=https://boot.example.com/account
exec /opt/keri/kfboot/venv/bin/kf-boot
```

**`KF_BOOT_MBX_BOOT_URL`** is how **kf-boot** on Host 1 reaches the mailbox boot API on Host 4. Setting it also makes **`bootstrap.mailbox_enabled`** true in **`/bootstrap/config`**. The mailbox **public** URL wallets use comes from the mailbox process (**`--public-url`**), not from a separate **kf-boot** env var.

### `/opt/keri/bin/run-kf-mailbox.sh` (Host 4 only)

```bash
#!/usr/bin/env bash
set -euo pipefail
export HOME=/opt/keri/mailbox
HOST_IP=192.0.2.14
PUBLIC_MAILBOX_URL=http://192.0.2.14:6732
PUBLIC_MAILBOX_URL="${PUBLIC_MAILBOX_URL%/}"
exec /opt/keri/mailbox/venv/bin/kf-mailbox serve \
  --boot-host "$HOST_IP" \
  --boot-port 6731 \
  --host 0.0.0.0 \
  --port 6732 \
  --base mailbox \
  --public-url "${PUBLIC_MAILBOX_URL}"
```

Bind **boot** to Host 4’s operator address and firewall **6731** so only Host 1 can open it (same pattern as witness boot). Expose **6732** to clients.

### `/opt/keri/bin/run-kf-observer.sh` (Host 3 only)

This script writes config, ensures the observer habitat exists, exports AID+KEL for the registrar host, then starts the process.

```bash
#!/usr/bin/env bash
set -euo pipefail
export HOME=/opt/keri/observer
HOST_IP=192.0.2.13
PUBLIC_OBSERVER_URL=http://192.0.2.13:6633
PUBLIC_OBSERVER_URL="${PUBLIC_OBSERVER_URL%/}"
REGISTRAR_URL=http://192.0.2.12:6632/
WITNESS_URL=http://192.0.2.11:5632/
CONFIG_DIR=/opt/keri/observer/config
BOOTSTRAP_DIR=/opt/keri/observer/bootstrap
NAME=observer
ALIAS=observer
BASE=observer
# Replace with your real passcode (22-char bran; hyphens optional — stripped below).
PASSCODE="${KERI_PASSCODE:-DoB2-e4Rr-gVOr-Nb1Y-7yBl-gI3n-i4cB-gf07}"
PASSCODE="${PASSCODE//-/}"
export KERI_BASER_MAP_SIZE="${KERI_BASER_MAP_SIZE:-2147483648}"

mkdir -p "${CONFIG_DIR}/keri/cf" "${BOOTSTRAP_DIR}"

cat > "${CONFIG_DIR}/keri/cf/kf-observer.json" <<EOF
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "kf-observer": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["${PUBLIC_OBSERVER_URL}/"],
    "registrars": ["${REGISTRAR_URL}"],
    "witnesses": ["${WITNESS_URL}"]
  },
  "kram": {
    "enabled": true,
    "denials": [],
    "caches": {
      "~": [1000, 5000, 60000, 300000, 5000, 60000, 300000]
    }
  }
}
EOF

/opt/keri/observer/venv/bin/python - <<PY
from keri.app import Configer, Habery
from keri.kering import Vrsn_2_0

cf = Configer(
    name="kf-observer",
    headDirPath="${CONFIG_DIR}",
    temp=False,
    reopen=True,
    clear=False,
)
hby = Habery(name="${NAME}", base="${BASE}", bran="${PASSCODE}", cf=cf, version=Vrsn_2_0)
hab = hby.habByName("${ALIAS}")
if hab is None:
    hab = hby.makeHab(name="${ALIAS}", transferable=True)
aid = hab.pre
kel = b"".join(hby.db.clonePreIter(pre=aid, gvrsn=Vrsn_2_0))
hby.close()
with open("${BOOTSTRAP_DIR}/observer.aid", "w", encoding="utf-8") as fh:
    fh.write(aid)
with open("${BOOTSTRAP_DIR}/observer.kel", "wb") as fh:
    fh.write(kel)
print(f"Observer AID {aid}")
print(f"Observer KEL bytes {len(kel)}")
PY

exec /opt/keri/observer/venv/bin/kf-observer start \
  --name "${NAME}" \
  --alias "${ALIAS}" \
  --base "${BASE}" \
  --config-dir "${CONFIG_DIR}" \
  --config-file kf-observer \
  --host 0.0.0.0 \
  --http 6633 \
  --registrar "${REGISTRAR_URL}" \
  --witness "${WITNESS_URL}" \
  --passcode "${PASSCODE}" \
  --no-prompt \
  --loglevel INFO
```

After the first successful start on Host 3, copy bootstrap files to Host 2:

```bash
# from an admin machine or Host 3
scp /opt/keri/observer/bootstrap/observer.aid \
    /opt/keri/observer/bootstrap/observer.kel \
    keri@192.0.2.12:/opt/keri/registrar/bootstrap/
```

### `/opt/keri/bin/run-kf-registrar.sh` (Host 2 only)

Requires **`/opt/keri/registrar/bootstrap/observer.aid`** and **`observer.kel`** already in place.

```bash
#!/usr/bin/env bash
set -euo pipefail
export HOME=/opt/keri/registrar
HOST_IP=192.0.2.12
PUBLIC_REGISTRAR_URL=http://192.0.2.12:6632
PUBLIC_REGISTRAR_URL="${PUBLIC_REGISTRAR_URL%/}"
CONFIG_DIR=/opt/keri/registrar/config
BOOTSTRAP_DIR=/opt/keri/registrar/bootstrap
AID_FILE="${BOOTSTRAP_DIR}/observer.aid"
KEL_FILE="${BOOTSTRAP_DIR}/observer.kel"
NAME=registrar
ALIAS=registrar
BASE=registrar
PASSCODE="${KERI_PASSCODE:-DoB2-e4Rr-gVOr-Nb1Y-7yBl-gI3n-i4cB-gf07}"
PASSCODE="${PASSCODE//-/}"
export KERI_BASER_MAP_SIZE="${KERI_BASER_MAP_SIZE:-2147483648}"

OBSERVER_AID="$(tr -d '[:space:]' < "${AID_FILE}")"
test -n "${OBSERVER_AID}"
test -s "${KEL_FILE}"

mkdir -p "${CONFIG_DIR}/keri/cf"

cat > "${CONFIG_DIR}/keri/cf/kf-registrar.json" <<EOF
{
  "dt": "2026-01-01T00:00:00.000000+00:00",
  "kf-registrar": {
    "dt": "2026-01-01T00:00:00.000000+00:00",
    "curls": ["${PUBLIC_REGISTRAR_URL}/"],
    "observers": ["${OBSERVER_AID}"]
  },
  "kram": {
    "enabled": true,
    "denials": [],
    "caches": {
      "~": [1000, 5000, 60000, 300000, 5000, 60000, 300000]
    }
  }
}
EOF

/opt/keri/registrar/venv/bin/kf-registrar start \
  --name "${NAME}" \
  --alias "${ALIAS}" \
  --base "${BASE}" \
  --config-dir "${CONFIG_DIR}" \
  --config-file kf-registrar \
  --host 0.0.0.0 \
  --http 6632 \
  --boothost 0.0.0.0 \
  --bootport 6631 \
  --observer "${OBSERVER_AID}" \
  --passcode "${PASSCODE}" \
  --no-prompt \
  --loglevel INFO &
reg_pid=$!
trap 'kill "${reg_pid}" 2>/dev/null || true' EXIT INT TERM

for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:6632/health"; then
    break
  fi
  sleep 1
done

intro_ok=0
for _ in $(seq 1 30); do
  code="$(curl -sS -m 10 -o /tmp/intro.out -w '%{http_code}' \
    -H 'Content-Type: application/cesr' \
    --data-binary @"${KEL_FILE}" \
    "http://127.0.0.1:6632/introductions" || true)"
  if [[ "${code}" == "200" || "${code}" == "204" ]]; then
    intro_ok=1
    break
  fi
  sleep 1
done
if [[ "${intro_ok}" -ne 1 ]]; then
  echo "Failed to introduce observer KEL (last HTTP ${code:-none})" >&2
  cat /tmp/intro.out 2>/dev/null || true
  exit 1
fi

wait "${reg_pid}"
```

On **every** host:

```bash
sudo chmod 750 /opt/keri/bin/run-witopnet.sh /opt/keri/bin/run-watopnet.sh
sudo chown keri:keri /opt/keri/bin/run-witopnet.sh /opt/keri/bin/run-watopnet.sh
```

On **Host 1** only:

```bash
sudo chmod 750 /opt/keri/bin/run-kf-boot.sh
sudo chown keri:keri /opt/keri/bin/run-kf-boot.sh
```

On **Host 2** only:

```bash
sudo chmod 750 /opt/keri/bin/run-kf-registrar.sh
sudo chown keri:keri /opt/keri/bin/run-kf-registrar.sh
```

On **Host 3** only:

```bash
sudo chmod 750 /opt/keri/bin/run-kf-observer.sh
sudo chown keri:keri /opt/keri/bin/run-kf-observer.sh
```

On **Host 4** only:

```bash
sudo chmod 750 /opt/keri/bin/run-kf-mailbox.sh
sudo chown keri:keri /opt/keri/bin/run-kf-mailbox.sh
```

Raise the process file-descriptor limit (Circus sets **`rlimit_nofile = 65536`** below). Default **`nofile` ~1024** will exhaust around ~100 hosted witness AIDs per **`witopnet`**.

---

## 8. Circus configuration

Witness starts before the watcher. Higher Circus **`priority`** values start first.

Per-host additions:

- **Host 1:** **kf-boot** (priority 10) after wit/wat.
- **Host 2:** **kf-registrar** (priority 15) after wit/wat.
- **Host 3:** **kf-observer** (priority 15) after wit/wat.
- **Host 4:** **kf-mailbox** (priority 15) after wit/wat.

Across hosts: start **Host 3** (observer) and export/copy the AID+KEL **before** starting **Host 2**’s registrar watcher for the first time. Bring up Host 4 (mailbox) before Host 1 so **kf-boot** can reach **`KF_BOOT_MBX_BOOT_URL`**.

### Every host

`/etc/circus/circus.ini`:

```ini
[circus]
check_delay = 5
endpoint = tcp://127.0.0.1:5555
pubsub_endpoint = tcp://127.0.0.1:5556
statsd = false

[watcher:witopnet]
cmd = /opt/keri/bin/run-witopnet.sh
numprocesses = 1
priority = 25
working_dir = /opt/keri/witness
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/witopnet-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/witopnet-circus.stderr.log

[watcher:watopnet]
cmd = /opt/keri/bin/run-watopnet.sh
numprocesses = 1
priority = 21
working_dir = /opt/keri/watcher
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/watopnet-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/watopnet-circus.stderr.log
```

### Host 1 only

Append **kf-boot**:

```ini
[watcher:kf-boot]
cmd = /opt/keri/bin/run-kf-boot.sh
numprocesses = 1
priority = 10
working_dir = /opt/keri/kfboot
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/kf-boot-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/kf-boot-circus.stderr.log
```

### Host 2 only

Append:

```ini
[watcher:kf-registrar]
cmd = /opt/keri/bin/run-kf-registrar.sh
numprocesses = 1
priority = 15
working_dir = /opt/keri/registrar
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/kf-registrar-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/kf-registrar-circus.stderr.log
```

### Host 3 only

Append:

```ini
[watcher:kf-observer]
cmd = /opt/keri/bin/run-kf-observer.sh
numprocesses = 1
priority = 15
working_dir = /opt/keri/observer
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/kf-observer-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/kf-observer-circus.stderr.log
```

### Host 4 only

Append:

```ini
[watcher:kf-mailbox]
cmd = /opt/keri/bin/run-kf-mailbox.sh
numprocesses = 1
priority = 15
working_dir = /opt/keri/mailbox
user = keri
copy_env = True
rlimit_nofile = 65536
stdout_stream.class = FileStream
stdout_stream.filename = /var/log/keri/kf-mailbox-circus.stdout.log
stderr_stream.class = FileStream
stderr_stream.filename = /var/log/keri/kf-mailbox-circus.stderr.log
```

On **every** host:

```bash
sudo mkdir -p /etc/circus
sudo chown root:keri /etc/circus/circus.ini
sudo chmod 640 /etc/circus/circus.ini
```

---

## 9. systemd unit for Circus

On **every** host, create **`/etc/systemd/system/circus.service`** (root-owned **`644`** is typical):

```ini
[Unit]
Description=Circus process manager (witopnet, watopnet, and host-local KF services)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=keri
Group=keri
ExecStart=/opt/circus/venv/bin/circusd /etc/circus/circus.ini
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start in this order for a cold cluster:

1. Host 4 (wit/wat + mailbox).
2. Host 3 (wit/wat + observer; copy observer bootstrap to Host 2).
3. Host 2 (wit/wat + registrar; introduces the observer KEL).
4. Host 1 (wit/wat + **kf-boot**).

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now circus
sudo systemctl status circus
```

---

## 10. nginx on Host 1: TLS and reverse proxy to kf-boot

**kf-boot** listens on **`127.0.0.1:9723`**. nginx on Host 1 terminates TLS on **443** and proxies **`/health`**, **`/bootstrap/config`**, **`/onboarding`**, and **`/account`** to that upstream.

(The Docker compose stack skips nginx and publishes **`9723`** directly for local testing. On real hosts prefer nginx + TLS and keep **`9723`** off the public interface.)

Witness and watcher HTTP in this guide stay on their high ports over HTTP. If you put TLS in front of those ports, change **`curls`** and the matching **`KF_BOOT_*` public URL** together.

Create a webroot for ACME HTTP-01 (if you use it):

```bash
sudo mkdir -p /var/www/certbot
```

Obtain certificates (example with **certbot** and **`boot.example.com`**):

```bash
sudo certbot certonly --webroot -w /var/www/certbot -d boot.example.com
```

Create **`/etc/nginx/sites-available/kf-boot.conf`** (replace **`boot.example.com`** and certificate paths):

```nginx
upstream kf_boot {
    server 127.0.0.1:9723;
    keepalive 8;
}

server {
    listen 80;
    listen [::]:80;
    server_name boot.example.com;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://boot.example.com$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name boot.example.com;

    ssl_certificate     /etc/letsencrypt/live/boot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/boot.example.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 64m;

    location = /health {
        proxy_pass http://kf_boot;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
    }

    location = /bootstrap/config {
        proxy_pass http://kf_boot;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
    }

    location ^~ /onboarding {
        proxy_pass http://kf_boot;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    location ^~ /account {
        proxy_pass http://kf_boot;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    location / {
        return 404;
    }
}
```

Enable the site and reload nginx:

```bash
sudo ln -sf /etc/nginx/sites-available/kf-boot.conf /etc/nginx/sites-enabled/kf-boot.conf
sudo nginx -t && sudo systemctl reload nginx
```

Comment out **`ssl_dhparam`** if that file does not exist on your system. If this vhost also serves other content at **`/`**, merge these **`location`** blocks into your existing **`server`** and drop or adjust the final **`location /`** block.

---

## 11. Firewall

On **every** host, allow SSH plus the witness and watcher **HTTP** ports. Admit the **boot** ports only from Host 1. That includes Host 1 itself, so **kf-boot** can open `192.0.2.11:5631` and `192.0.2.11:7631`.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 5632/tcp
sudo ufw allow 7632/tcp
sudo ufw allow from 192.0.2.11 to any port 5631 proto tcp
sudo ufw allow from 192.0.2.11 to any port 7631 proto tcp
```

On **Host 1** only, also allow HTTP and HTTPS for nginx. Leave **9723** closed; clients reach **kf-boot** through **443**.

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

On **Host 2** only, allow the registrar **external** HTTP port for the observer (and any operator tooling). Optionally restrict **6632** to Host 3. Leave **6631** (registrar boot/admin) closed to the public internet unless you have a separate admin path.

```bash
sudo ufw allow from 192.0.2.13 to any port 6632 proto tcp
# during bring-up / ops from Host 1, you may also need:
sudo ufw allow from 192.0.2.11 to any port 6632 proto tcp
```

On **Host 3** only, allow the observer HTTP port for wallets/verifiers (tighten to known client nets when you can):

```bash
sudo ufw allow 6633/tcp
```

On **Host 4** only, allow the mailbox **public** port to clients, and the mailbox **boot** port only from Host 1:

```bash
sudo ufw allow 6732/tcp
sudo ufw allow from 192.0.2.11 to any port 6731 proto tcp
```

Then, on **every** host:

```bash
sudo ufw enable
```

When you replace the documentation addresses, change **`192.0.2.11`** / **`192.0.2.13`** in the allow-from rules to the real Host 1 / Host 3 addresses. Tighten **5632**, **7632**, **6732**, and **6633** to your client networks when you know them.

---

## 12. Verification

From **Host 1**, after Circus is up on all four hosts:

- **Witness boot (×4):** `curl -i http://192.0.2.11:5631/health`, and the same path on **`192.0.2.12`**, **`192.0.2.13`**, and **`192.0.2.14`**. Expect a healthy response for your **`witopnet`** build (often **204**).
- **Witness public HTTP:** from a **client** machine (or Host 1 if that address is client-reachable), open each **`public_url`** host:port (for example `curl -i http://192.0.2.11:5632/`). A non-connection-failure HTTP status means the listen + **`curls`** path is up.
- **Watcher boot on Host 1:** `GET /health` is not defined on the watcher boot port for many **`watopnet`** builds. Use `curl -i http://192.0.2.11:7631/watchers` and expect a non-connection failure (often **405 Method Not Allowed**). Repeat for Hosts 2–4 if needed.
- **kf-mailbox (Host 4):** from Host 1, `curl -sS -i http://192.0.2.14:6731/health` → healthy (often **204** or **200**). From a client: `curl -sS -i http://192.0.2.14:6732/` → non-connection-failure.
- **kf-boot:** `curl -sS http://127.0.0.1:9723/health` → JSON with **`"status":"ok"`**. `curl -sS http://127.0.0.1:9723/bootstrap/config` → JSON whose **`bootstrap.account_options`** includes **`3-of-4`** with **`witness_count`** **4** and **`toad`** **3**, and **`bootstrap.mailbox_enabled`** **true** when **`KF_BOOT_MBX_BOOT_URL`** is set.
- Through nginx: `https://boot.example.com/health` and **`/bootstrap/config`**.
- **kf-registrar (Host 2):** `curl -sS -i http://192.0.2.12:6632/health` → healthy.
- **kf-observer (Host 3):** `curl -sS -i http://192.0.2.13:6633/health` → healthy.

A witness boot check that fails with "connection refused" from Host 1 means that host’s **`--boothost`** is wrong, or the firewall is not admitting **5631** from Host 1. A wallet error like **Network is unreachable** to a **`public_url`** means clients cannot route to the address in **`curls`** — fix **`PUBLIC_*`** / **`public_url`**, not the boot plane.

If the registrar fails on start with a missing bootstrap file, re-copy **`observer.aid`** / **`observer.kel`** from Host 3. If **`POST /introductions`** returns **403**, the AID in **`--observer`** / config does not match the KEL you posted.

For Locksmith against this pool, point development surfaces at the **kf-boot** public onboarding/account URLs (through nginx, or `http://127.0.0.1:9723/...` when testing without TLS). Observer queries use Host 3’s public observer URL (**`http://192.0.2.13:6633/`** in this example).

---

## 13. Operations

- Back up **`/opt/keri`** on **each** host. Witness KERI data for **`--base witopnet`** lives under **`/opt/keri/witness/config`**. Watcher data lives under **`/opt/keri/watcher/config`**. On Host 1, also back up **`KF_BOOT_DB_PATH`** (`/opt/keri/kfboot/var`) and **`KF_BOOT_KERI_DIR`** (`/opt/keri/kfboot/keri`). On Host 2 back up **`/opt/keri/registrar`**. On Host 3 back up **`/opt/keri/observer`** (including **`bootstrap/`**). On Host 4 back up **`/opt/keri/mailbox`**.
- Keep the registrar and observer **passcodes** in a secret store; losing them means you cannot reopen those keystores with **`--no-prompt`**.
- Rotate or cap logs under **`/var/log/keri/`** on each host.
- Moving the allocation watcher to another host means changing **`KF_BOOT_WAT_BOOT_URL`** and **`KF_BOOT_WAT_PUBLIC_URL`** to that host’s watcher URLs and restarting **kf-boot**. Existing accounts keep the watcher they were given.
- Moving mailbox to another host means changing **`KF_BOOT_MBX_BOOT_URL`**, opening that host’s **6731** only to Host 1, and ensuring **`--public-url`** still matches what wallets can reach.

---

## Local Docker

To run this four-host plan on one machine without four VMs, use **`docker-compose.yml`**. It matches the DigitalOcean co-location (**kf-boot** / **kf-registrar** / **kf-observer** / **kf-mailbox** on Hosts 1–4) with two deliberate differences for a Docker Desktop / localhost client:

1. **Boot plane** stays on `192.0.2.11`–`192.0.2.14` so **kf-boot** inside `host-1` can reach every witness/watcher/mailbox boot port.
2. **Public plane** uses **published host ports** so Locksmith on the Docker host can open the URLs **kf-boot** returns.

| Host | Boot (in-network) | Witness public (from Docker host) | Watcher public | Extra |
|------|-------------------|-----------------------------------|----------------|-------|
| Host 1 | `192.0.2.11` | `http://127.0.0.1:5632` | `http://127.0.0.1:7632` | kf-boot `:9723` |
| Host 2 | `192.0.2.12` | `http://127.0.0.1:25632` | `http://127.0.0.1:27632` | kf-registrar `:6632` |
| Host 3 | `192.0.2.13` | `http://127.0.0.1:35632` | `http://127.0.0.1:37632` | kf-observer `:6633` |
| Host 4 | `192.0.2.14` | `http://127.0.0.1:45632` | `http://127.0.0.1:47632` | kf-mailbox `:6732` |

```bash
cd /path/to/kf-boot
docker compose up -d --build
curl -sS http://127.0.0.1:9723/health
curl -sS http://127.0.0.1:6731/health
curl -sS http://127.0.0.1:6632/health
curl -sS http://127.0.0.1:6633/health
docker compose logs -f host-1
docker compose exec host-1 bash   # or sh
docker compose down -v
```

The image installs the same GitHub sources as §4 (`keripy`, `witness-hk`, `watcher-hk`, `kf-boot`, `kf-registrar`, `kf-observer`, `kf-mailbox`).

Compose env equivalents of this guide:

- **`HOST_IP`** → `--boothost` / boot URLs  
- **`PUBLIC_WITNESS_URL`** / **`PUBLIC_WATCHER_URL`** → **`curls`** and **`public_url`**  
- **`KF_BOOT_WITNESS_BACKENDS`** → `id|boot_url|public_url` with the split above  
- **`KF_BOOT_ONBOARDING_PUBLIC_URL`** / **`KF_BOOT_ACCOUNT_PUBLIC_URL`** → `http://127.0.0.1:9723/onboarding` and `.../account` (no nginx in compose)
- **`KF_BOOT_MBX_BOOT_URL`** / **`PUBLIC_MAILBOX_URL`** → mailbox boot (`192.0.2.14:6731`) + public (`127.0.0.1:6732`)
- **`PUBLIC_REGISTRAR_URL`** / **`PUBLIC_OBSERVER_URL`** / **`REGISTRAR_URL`** → registrar/observer curls and observer→registrar poll URL

When you move from Docker to real hosts, keep the same variable split; set **`PUBLIC_*`** to addresses your wallets can route to, and **`HOST_IP`** / boot URLs to addresses **kf-boot** can route to. For observer→registrar without a shared volume, use the AID+KEL copy + **`POST /introductions`** flow in §6–§7.
