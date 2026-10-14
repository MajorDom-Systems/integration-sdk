#!/usr/bin/env bash
# Concurrent-multiprotocol harness entrypoint (Silicon Labs multi-PAN RCP).
#
# Brings up, in order, inside the SiSDK multi-PAN image (Dockerfile next to this file):
#   dbus  ->  cpcd  ->  socat(EZSP pty pair)  ->  zigbeed(iid=1)  ->  socat(EZSP->TCP :9999)
#             ->  otbr-agent(iid=2, REST :8081)
# so that ONE EFR32 multi-PAN RCP serves Zigbee (EZSP over TCP for bellows) and Thread (OTBR)
# at the same time. Idempotent: Zigbee network state lives in the NCP flash and Thread state in
# the mounted OTBR datadir, so a restart re-adopts the existing networks instead of re-forming.
#
# Env (with defaults):
#   RADIO_DEVICE   /dev/ttyRCP      the RCP as mapped into the container, or its /dev/serial/by-id path
#                                   (resolved at every start: the stick's tty changes when USB re-enumerates)
#   RADIO_BAUD     460800           multi-PAN RCP UART baud
#   RADIO_HARDFLOW true             RTS/CTS flow control (false on a bare UART without control lines)
#   RADIO_RESET_GPIO                <gpiochip>:<line> wired to the RCP's RESETn (e.g. gpiochip0:23 on the CM4):
#                                   pulsed while cpcd waits, so it syncs on the reset reason
#   EZSP_TCP_PORT  9999             bellows connects to socket://<host>:9999
#   OTBR_REST_PORT 8081             Matter server reads the Thread dataset here
#   BACKBONE_IF    eth0             OTBR backbone interface
#   ZIGBEE_FORM    1                form a Zigbee network if the NCP has none
#   THREAD_FORM    1                form a Thread network if OTBR has none
#   THREAD_DATASET                  join this network (active dataset, hex) instead of forming one
#   CHANNEL        15               Zigbee and Thread form on it: one multi-PAN radio listens on one channel,
#                                   networks on two channels lose frames (a Thread device then can't attach).
#                                   Our Thread network is moved to Zigbee's channel when they differ
set -euo pipefail

RADIO_DEVICE=${RADIO_DEVICE:-/dev/ttyRCP}
RADIO_BAUD=${RADIO_BAUD:-460800}
RADIO_HARDFLOW=${RADIO_HARDFLOW:-true}
RADIO_RESET_GPIO=${RADIO_RESET_GPIO:-}
EZSP_TCP_PORT=${EZSP_TCP_PORT:-9999}
CHANNEL=${CHANNEL:-15}
OTBR_REST_PORT=${OTBR_REST_PORT:-8081}
BACKBONE_IF=${BACKBONE_IF:-eth0}
ZIGBEE_FORM=${ZIGBEE_FORM:-1}
THREAD_FORM=${THREAD_FORM:-1}

log() { echo "[multiprotocol] $*"; }

# 0. Kernel prerequisites that OTBR's Backbone-Router/ND-proxy needs (harmless if already set).
sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1 || true
sysctl -w net.ipv4.ip_forward=1          >/dev/null 2>&1 || true
# Forwarding makes Linux ignore router advertisements unless accept_ra=2: keep learning the routes
# other border routers announce on the backbone (route info up to /64).
sysctl -w "net.ipv6.conf.${BACKBONE_IF}.accept_ra=2" >/dev/null 2>&1 || true
sysctl -w "net.ipv6.conf.${BACKBONE_IF}.accept_ra_rt_info_max_plen=64" >/dev/null 2>&1 || true

# 1. System D-Bus — otbr-agent EMERG-crashes without it.
mkdir -p /run/dbus
[ -S /run/dbus/system_bus_socket ] || dbus-daemon --system --fork
mdnsd 2>/dev/null || true

# 2. cpcd owns the serial RCP and multiplexes it (CPC). A mounted /etc/cpcd.conf wins over the template.
[ "${RADIO_DEVICE}" = /dev/ttyRCP ] || ln -sf "$(readlink -f "${RADIO_DEVICE}")" /dev/ttyRCP
[ -f /etc/cpcd.conf ] || sed -e "s/@RADIO_BAUD@/${RADIO_BAUD}/" -e "s/@RADIO_HARDFLOW@/${RADIO_HARDFLOW}/" \
  /usr/share/majordom/cpcd.conf.tmpl >/etc/cpcd.conf
log "starting cpcd on ${RADIO_DEVICE} @ ${RADIO_BAUD}, hardflow ${RADIO_HARDFLOW}"
cpcd -c /etc/cpcd.conf >/var/log/cpcd.log 2>&1 &
if [ -n "${RADIO_RESET_GPIO}" ]; then
  # Once cpcd has opened the port and waits to connect: assert RESETn for 0.2 s, then hold it released.
  # The holder keeps the line driven high while the container runs; on exit the line falls back to
  # the RCP's own pull-up.
  sleep 1.5
  gpioset --mode=time --usec=200000 "${RADIO_RESET_GPIO%%:*}" "${RADIO_RESET_GPIO##*:}=0"
  gpioset --mode=signal "${RADIO_RESET_GPIO%%:*}" "${RADIO_RESET_GPIO##*:}=1" &
fi
# wait for the secondary (RCP) to attach
for _ in $(seq 1 30); do grep -q "Connected to Secondary" /var/log/cpcd.log 2>/dev/null && break; sleep 1; done
grep -q "Connected to Secondary" /var/log/cpcd.log || { log "cpcd never reached the RCP"; cat /var/log/cpcd.log; exit 1; }
sleep 1
log "cpcd attached: $(grep -oE 'Secondary (Protocol|CPC) v[^ ]+' /var/log/cpcd.log | paste -sd ' ')"

# 3. Zigbee side: zigbeed (CPC iid=1) presents EZSP on a pty. Run it from a PERSISTED state dir:
# zigbeed writes its emulated-NCP tokens to `host_token.nvm` relative to its cwd, so a cwd on a
# mounted volume makes the formed coordinator survive container recreation — otherwise every
# recreate re-forms a NEW network (new PAN id) and orphans already-paired devices.
ZB_HOST_PTY=/dev/ttyZigbeeNCP
ZB_STATE_DIR=${ZB_STATE_DIR:-/var/lib/zigbee}
mkdir -p "${ZB_STATE_DIR}"
socat pty,link=${ZB_HOST_PTY},raw,echo=0 pty,link=/tmp/ttyZigbeeNCP,raw,echo=0 &
sleep 1
( cd "${ZB_STATE_DIR}" && exec zigbeed --radio-url "spinel+cpc://cpcd_0?iid=1&iid-list=0" \
  --ezsp-interface /tmp/ttyZigbeeNCP ) >/var/log/zigbeed.log 2>&1 &
sleep 2

# 4. Thread side FIRST: otbr-agent (CPC iid=2) + REST, then form the Thread network. Bringing Thread
# up before forming Zigbee WARMS THE SHARED RADIO — forming Zigbee in the first seconds after zigbeed
# starts (cold radio) reliably times out, but with Thread already running it succeeds.
otbr-agent -I wpan0 -B "${BACKBONE_IF}" \
  --rest-listen-address 0.0.0.0 --rest-listen-port "${OTBR_REST_PORT}" \
  "spinel+cpc://cpcd_0?iid=2&iid-list=0" >/var/log/otbr.log 2>&1 &
sleep 4
log "otbr-agent up; Thread REST on :${OTBR_REST_PORT}"

if [ "${THREAD_FORM}" = "1" ]; then
  # Decide on the saved dataset, not the state: a restored network is still "detached" right after
  # start, and forming then would replace it (new network, orphaned devices).
  if ot-ctl dataset active -x 2>/dev/null | grep -qE "^[0-9a-fA-F]{20,}"; then
    log "Thread network restored"
  elif [ -n "${THREAD_DATASET:-}" ]; then
    log "joining the configured Thread network"
    ot-ctl dataset set active "${THREAD_DATASET}" >/dev/null 2>&1 || true
  else
    log "forming Thread network"
    ot-ctl dataset init new     >/dev/null 2>&1 || true
    ot-ctl dataset channel "${CHANNEL}" >/dev/null 2>&1 || true
    ot-ctl dataset commit active >/dev/null 2>&1 || true
  fi
  ot-ctl ifconfig up          >/dev/null 2>&1 || true
  ot-ctl thread start         >/dev/null 2>&1 || true
fi

# 5. Zigbee coordinator: form ON THE PTY if the NCP has no network. EZSP `form` is a latency-sensitive
# radio-level sequence and CANNOT traverse the EZSP->TCP bridge (ASH frame loss -> networkInit timeout
# / ERR_FLASH_VERIFY_FAILED), so it must run here, in-container, on the pty, as the SINGLE reader,
# BEFORE we expose TCP. bellows is baked into the image (see Dockerfile).
#
# Idempotency + success detection deliberately AVOID a separate `bellows info` probe: `info` is itself
# flaky on this stack (ASH sync), and a probe that hangs and gets `timeout`-killed leaves zigbeed's
# ASH desynced, which poisons every following call. Instead:
#   * idempotency = a persistent MARKER in the state volume (no bellows call on the already-formed path)
#   * success     = the form's OWN exit status: 0 only once the network is committed (bellows 0.49 +
#                   aiosqlite 0.21 no longer crash after it). A crash before forming, e.g. an EZSP version
#                   bellows lacks, exits non-zero and is retried, never marked as formed.
ZB_MARKER="${ZB_STATE_DIR}/.coordinator-formed"
if [ "${ZIGBEE_FORM}" = "1" ]; then
  if [ -f "${ZB_MARKER}" ]; then
    log "Zigbee coordinator already formed (marker present) — skipping form"
  else
    log "Forming Zigbee coordinator on ${ZB_HOST_PTY}"
    formed=0
    for attempt in 1 2 3 4; do
      : > /tmp/zbform.db
      rc=0
      timeout 120 bellows -d "${ZB_HOST_PTY}" form --channel "${CHANNEL}" --database /tmp/zbform.db >/var/log/zigbee-form.log 2>&1 || rc=$?
      if [ "${rc}" = 0 ]; then
        echo "${CHANNEL}" > "${ZB_MARKER}"; formed=1; break
      fi
      log "Zigbee form attempt ${attempt} unconfirmed (rc=${rc}) — retrying"; sleep 8
    done
    if [ "${formed}" = 1 ]; then log "Zigbee coordinator formed"; else log "WARN: Zigbee form unconfirmed after retries — see /var/log/zigbee-form.log"; fi
  fi
fi

# 5b. Our Thread network follows Zigbee's channel (one restored from before CHANNEL changed would sit on
# another). Through a pending dataset with a delay, so attached Thread devices move along.
ZB_CHANNEL=$(cat "${ZB_MARKER}" 2>/dev/null || true)  # empty from older images: formed on CHANNEL
ZB_CHANNEL=${ZB_CHANNEL:-${CHANNEL}}
TH_CHANNEL=$(ot-ctl channel 2>/dev/null | head -1 | tr -dc 0-9 || true)
if [ "${THREAD_FORM}" = "1" ] && [ -n "${TH_CHANNEL}" ] && [ "${TH_CHANNEL}" != "${ZB_CHANNEL}" ]; then
  if [ -n "${THREAD_DATASET:-}" ]; then
    log "WARN: the joined Thread network is on channel ${TH_CHANNEL}, Zigbee on ${ZB_CHANNEL}: both will lose frames"
  else
    for _ in $(seq 1 30); do ot-ctl state 2>/dev/null | grep -qE "leader|router|child" && break; sleep 1; done
    log "moving Thread from channel ${TH_CHANNEL} to ${ZB_CHANNEL}, Zigbee's"
    now=$(date +%s)
    if ot-ctl dataset init active >/dev/null && ot-ctl dataset channel "${ZB_CHANNEL}" >/dev/null \
      && ot-ctl dataset activetimestamp "${now}" >/dev/null && ot-ctl dataset pendingtimestamp "${now}" >/dev/null \
      && ot-ctl dataset delay 10000 >/dev/null && ot-ctl dataset commit pending >/dev/null; then
      log "Thread changes channel in 10 s"
    else
      log "WARN: the Thread channel change was refused"
    fi
  fi
fi

# 6. Now expose EZSP over TCP for the Hub/bellows (socket://<host>:${EZSP_TCP_PORT}); steady-state
# load + commands tolerate the bridge even though forming could not.
socat "tcp-listen:${EZSP_TCP_PORT},reuseaddr,fork" file:/dev/ttyZigbeeNCP,raw,echo=0 &
log "zigbeed up; EZSP exposed on tcp:${EZSP_TCP_PORT} (bellows: socket://<host>:${EZSP_TCP_PORT})"

# Thread TX power (HARNESS_TX_DBM). On a multi-PAN RCP the radio transmits at Zigbee's power: `ot-ctl
# txpower` reads back but isn't applied (observed 2026-10-13). Lower Zigbee's tx_power instead (zigpy's -10 dBm
# minimum gives about -11 dBm). BLE TX and receiver sensitivity are not tunable.
if [ -n "${HARNESS_TX_DBM:-}" ]; then
  log "setting Thread TX power to ${HARNESS_TX_DBM} dBm (clamps to radio min)"
  ot-ctl txpower "${HARNESS_TX_DBM}" >/dev/null 2>&1 || true
  log "Thread TX now: $(ot-ctl txpower 2>/dev/null | head -1)"
fi

log "harness ready — Zigbee EZSP tcp:${EZSP_TCP_PORT}, Thread OTBR REST :${OTBR_REST_PORT}"

# 6. Supervise: if any core daemon dies, exit non-zero so the container restart policy recycles us.
while true; do
  for p in cpcd zigbeed otbr-agent; do
    pgrep -x "$p" >/dev/null || { log "$p died — exiting for restart"; exit 1; }
  done
  sleep 5
done
