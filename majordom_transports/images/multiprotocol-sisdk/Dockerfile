# Slim multi-PAN host stack (cpcd + zigbeed + otbr-agent) from Silicon Labs' first-party SiSDK debs.
#
# Matches RCPs built on the same SiSDK release (CPC protocol v6; our MG26 runs SiSDK 2025.6.3, CPC 4.7.1).
# Host programs and RCP firmware must come from ONE SDK release: do not mix with gsdk-4.4.x radios.
#   docker buildx build --platform linux/arm64 -t majordom/multiprotocol:sisdk-2025.6.3 --load .
ARG DEBIAN=debian:bookworm-slim

# Unpacks the debs only: their maintainer scripts install bind9, nodejs, radvd... and compile mDNSResponder.
# Runs on the build platform; it only picks the target architecture's debs.
FROM --platform=$BUILDPLATFORM ${DEBIAN} AS pkgs
ARG TARGETARCH
ARG SISDK=v2025.6.3
ARG SISDK_DEBS_SHA256=7ecbd91cdae94da1ec9eb4817a6bc1fe73741106a0c70a40879956bc09833c0e
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/debs.zip "https://github.com/SiliconLabs/simplicity_sdk/releases/download/${SISDK}/debian-bookworm.zip" \
 && echo "${SISDK_DEBS_SHA256}  /tmp/debs.zip" | sha256sum -c - \
 && unzip -q /tmp/debs.zip -d /tmp \
 && for p in libcpc3 cpcd zigbeed ot-br-posix; do dpkg-deb -x /tmp/debian-bookworm/deb/${p}_*_${TARGETARCH}.deb /out; done

# otbr-agent links libdns_sd.so: build the mDNSResponder its deb ships as source (what its postinst does).
FROM ${DEBIAN} AS mdns
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev make && rm -rf /var/lib/apt/lists/*
COPY --from=pkgs /out/usr/src/mDNSResponder /src
RUN make -C /src/mDNSPosix os=linux tls=no Daemon libdns_sd -j"$(nproc)"

# bellows forms the Zigbee coordinator in-container (see entrypoint.sh). zigbeed 8.2.2 speaks EZSP v18,
# first supported by bellows 0.49; pinned to the Hub's lock so both ends agree.
FROM ${DEBIAN} AS py
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir bellows==0.49.2 zigpy==2.0.1 aiosqlite==0.21.0 \
 && /opt/venv/bin/pip uninstall -y pip setuptools

FROM ${DEBIAN}
ARG SISDK=v2025.6.3
LABEL org.opencontainers.image.title="MajorDom multi-PAN host stack" \
      org.opencontainers.image.description="cpcd 4.7.1, zigbeed 8.2.2, ot-br-posix 2.7.3.0 from Silicon Labs SiSDK ${SISDK}" \
      org.opencontainers.image.source="https://github.com/majordom-systems/majordom-transports" \
      io.majordom.sisdk="${SISDK}"
RUN apt-get update && apt-get install -y --no-install-recommends \
      dbus gpiod ipset iproute2 iptables libdbus-1-3 libjsoncpp25 libmbedcrypto7 libnetfilter-queue1 \
      libprotobuf-lite32 libreadline8 procps python3 socat \
 && rm -rf /var/lib/apt/lists/*
COPY --from=pkgs /out/usr/lib/ /usr/lib/
COPY --from=pkgs /out/usr/bin/cpcd /usr/bin/
COPY --from=pkgs /out/usr/local/bin/zigbeed /usr/local/bin/
COPY --from=pkgs /out/usr/local/etc/zigbeed.conf /usr/local/etc/
COPY --from=pkgs /out/usr/sbin/otbr-agent /out/usr/sbin/ot-ctl /usr/sbin/
COPY --from=pkgs /out/etc/dbus-1/system.d/otbr-agent.conf /etc/dbus-1/system.d/
COPY --from=pkgs /out/usr/share/doc/zigbeed/copyright /usr/share/doc/silabs/MSLA
COPY --from=mdns /src/mDNSPosix/build/prod/mdnsd /usr/sbin/
COPY --from=mdns /src/mDNSPosix/build/prod/libdns_sd.so /usr/lib/
COPY --from=mdns /src/LICENSE /usr/share/doc/mDNSResponder/LICENSE
COPY --from=py /opt/venv /opt/venv
COPY LICENSES /usr/share/doc/majordom-multiprotocol/
COPY cpcd.conf.tmpl /usr/share/majordom/cpcd.conf.tmpl
COPY entrypoint.sh /entrypoint.sh
ENV PATH=/opt/venv/bin:$PATH
# Fails the build when a copied binary misses a shared library.
RUN ldconfig && ! ldd /usr/bin/cpcd /usr/local/bin/zigbeed /usr/sbin/otbr-agent /usr/sbin/ot-ctl /usr/sbin/mdnsd | grep "not found" \
 && cpcd --version | head -1 && bellows --help >/dev/null
ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
