Developer Guide
===============

``kf-boot`` is the KERI Foundation boot service for hosted witness and watcher
onboarding. It provides public bootstrap discovery, authenticated onboarding,
approved-account management, and signed boot-server replies.

Environment
-----------

kf-boot requires Python ``>=3.14.0`` and ``libsodium``.

**macOS:**

.. code-block:: bash

   brew install libsodium

**Ubuntu/Debian:**

.. code-block:: bash

   sudo apt-get install libsodium-dev

Setup
-----

From the repository root:

.. code-block:: bash

   python3.14 -m venv .venv
   source .venv/bin/activate
   python -m pip install -U pip setuptools wheel
   python -m pip install -e ".[dev]"

Running the Boot Service
------------------------

Set environment variables pointing to the witness and watcher services:

.. code-block:: bash

   export KF_BOOT_HOST=127.0.0.1
   export KF_BOOT_PORT=9723
   export KF_BOOT_WIT_BOOT_URL=http://127.0.0.1:5631
   export KF_BOOT_WIT_PUBLIC_URL=http://127.0.0.1:5632
   export KF_BOOT_WAT_BOOT_URL=http://127.0.0.1:7631
   export KF_BOOT_WAT_PUBLIC_URL=http://127.0.0.1:7632

Then start the service:

.. code-block:: bash

   kf-boot

The witness and watcher services must already be running. See the
`witness-hk <https://github.com/keri-foundation/witness-hk>`_ and
`watcher-hk <https://github.com/keri-foundation/watcher-hk>`_ developer
guides for their setup.

Architecture
------------

kf-boot implements two public trust domains:

- **Onboarding surface** (``/onboarding``): ephemeral first contact. Controllers
  start onboarding sessions, select witness/watcher profiles, and provision
  resources.

- **Approved-account surface** (``/account``): management for already-onboarded
  accounts. Lists provisioned witnesses and watchers, updates resources, and
  deletes accounts.

The service communicates with witness and watcher boot APIs to allocate and
deallocate resources. It does not create local AIDs, hold keys, sign on behalf
of users, or manage watcher protocol flows.

End-to-End Walkthrough
----------------------

Step 1: Start dependencies
~~~~~~~~~~~~~~~~~~~~~~~~~~

Ensure witness and watcher services are running:

.. code-block:: bash

   # Terminal 1: witness
   witopnet marshal start --config-dir /path/to/witness-config \
     --base witopnet --host 127.0.0.1 --http 5632 --boothost 127.0.0.1 --bootport 5631

   # Terminal 2: watcher
   watcher -H 7632 -t 7631 --config-dir /path/to/watcher-config

Step 2: Start kf-boot
~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Terminal 3
   export KF_BOOT_HOST=127.0.0.1
   export KF_BOOT_PORT=9723
   export KF_BOOT_WIT_BOOT_URL=http://127.0.0.1:5631
   export KF_BOOT_WIT_PUBLIC_URL=http://127.0.0.1:5632
   export KF_BOOT_WAT_BOOT_URL=http://127.0.0.1:7631
   export KF_BOOT_WAT_PUBLIC_URL=http://127.0.0.1:7632
   kf-boot

.. note::

   The ``sleep infinity`` placeholder in the E2E test harness is a known gap.
   The real kf-boot command requires the full witness/watcher stack.

Step 3: Start an onboarding session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   curl -X POST http://127.0.0.1:9723/onboarding \
     -H "Content-Type: application/json" \
     -H "CESR-ATTACHMENT: <endorsement-bytes>" \
     --data-binary @inception-event.cesr

The server returns a signed reply with session metadata including the
session ID.

Step 4: Complete session and check status
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   curl -X POST http://127.0.0.1:9723/onboarding \
     -H "Content-Type: application/json" \
     -H "CESR-ATTACHMENT: <endorsement-bytes>" \
     --data-binary @session-status-exn.cesr

The reply includes the session state including provisioned witness and
watcher endpoints.

Step 5: Manage approved accounts
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Once onboarded, query account resources:

.. code-block:: bash

   curl -X POST http://127.0.0.1:9723/account \
     -H "Content-Type: application/json" \
     -H "CESR-ATTACHMENT: <endorsement-bytes>" \
     --data-binary @account-query.cesr

HTTP API Reference
------------------

All endpoints use the CESR content type. Requests include a CESR body with
endorsement attachments in the ``CESR-ATTACHMENT`` header.

Onboarding (``/onboarding``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 10 20 70

   * - Method
     - Path
     - Description
   * - ``POST``
     - ``/onboarding``
     - Primary onboarding endpoint. Routes via EXN message ``r`` field:
       ``/onboarding/session/start``, ``/onboarding/session/status``,
       ``/onboarding/session/complete``, ``/onboarding/account/create``

Account (``/account``)
~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 10 20 70

   * - Method
     - Path
     - Description
   * - ``POST``
     - ``/account``
     - Approved-account management. Routes via EXN message ``r`` field:
       ``/account/witnesses``, ``/account/watchers``, ``/account/delete``

Testing
-------

.. code-block:: bash

   pip install -e ".[dev]"
   pytest tests/

Tests use Falcon's ``TestClient`` for in-process HTTP testing with fake
witness and watcher boot backends. No external services are required.

.. note::

   The test suite currently has failures related to KERI v2 CESR
   compatibility. See draft PR
   `#21 <https://github.com/keri-foundation/kf-boot/pull/21>`_ for the
   current state of v2 fixes.

.. _troubleshooting:

Troubleshooting
---------------

**"Connection refused" on witness/watcher boot URLs**
    Ensure the witness and watcher services are running and their boot
    ports (5631, 7631) are accessible.

**CESR parsing errors or "Invalid prefixer code"**
    This is a known KERI v2 compatibility issue. Under v2-default keripy,
    exchange messages produce CESR-encoded serders that the server
    struggles to parse. See PR #21 for the current fix.

**ImportError: libsodium not found**
    Install libsodium: ``brew install libsodium`` (macOS) or
    ``sudo apt-get install libsodium-dev`` (Ubuntu/Debian).

**ModuleNotFoundError: No module named 'kfboot'**
    Install the package in development mode: ``pip install -e .`` from the
    repository root.

Building the Docs
-----------------

From the repository root:

.. code-block:: bash

   pip install -e .
   pip install sphinx sphinx-rtd-theme
   cd docs
   sphinx-build -b dirhtml . _build/html

To do a clean rebuild:

.. code-block:: bash

   rm -rf _build
