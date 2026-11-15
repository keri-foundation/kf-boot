#!/usr/bin/env bash
# hio defaults to SysLogHandler(/dev/log). Slim images have no syslog socket.
if [[ ! -e /dev/log ]]; then
  python - <<'PY' &
import select
import socket

sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
sock.bind("/dev/log")
while True:
    select.select([sock], [], [])
    try:
        sock.recv(65535)
    except OSError:
        pass
PY
fi
