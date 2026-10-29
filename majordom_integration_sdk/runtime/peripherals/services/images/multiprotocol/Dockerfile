# Thin derived image over Silicon Labs' first-party multiprotocol image.
#
# The ONLY thing added is the Zigbee coordinator-forming tool. EZSP `form` (a latency-sensitive
# radio-level sequence) cannot traverse the zigbeed->pty->socat->TCP bridge — it loses ASH frames
# and times out — so the coordinator must be formed IN-CONTAINER, directly on the pty, at startup
# (entrypoint.sh does this when the NCP reports NOT_JOINED). Baking bellows in keeps first boot
# offline (no runtime pip), matching the "complete image" build principle.
#
# Versions are pinned to the proven combo (py3.10 base caps bellows at 0.46.x):
#   bellows 0.46.2 + zigpy 0.84.0 + aiosqlite 0.22.1
# (a cosmetic aiosqlite ".join" AttributeError prints at DB-shutdown AFTER the network is committed;
# the entrypoint verifies success via `bellows info`, not the exit code.)
FROM siliconlabsinc/multiprotocol:gsdk-4.4.6-arm64

RUN pip3 install --no-cache-dir bellows==0.46.2 zigpy==0.84.0 aiosqlite==0.22.1
