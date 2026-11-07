FROM alpine:3.23 AS builder

ARG LIBUECC_VERSION=v7
ARG FASTD_VERSION=v23
ARG FIRMWARE_REPO=https://github.com/Freifunk-Dresden/firmware-freifunk-dresden.git
ARG FIRMWARE_TAG=T_FIRMWARE_8.2.0

RUN apk add --no-cache \
    bison \
    build-base \
    cmake \
    git \
    json-c-dev \
    libcap-dev \
    libsodium-dev \
    linux-headers \
    meson \
    ninja \
    nodejs \
    npm \
    openssl-dev \
    pkgconf

ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig

WORKDIR /build

RUN git clone --branch "$LIBUECC_VERSION" --depth 1 https://github.com/neocturne/libuecc.git
RUN cmake -S /build/libuecc -B /build/libuecc-build \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/usr/local \
 && cmake --build /build/libuecc-build \
 && cmake --install /build/libuecc-build

RUN git clone --branch "$FASTD_VERSION" --depth 1 https://github.com/neocturne/fastd.git
COPY patches/fastd-urandom.patch /tmp/fastd-urandom.patch
COPY patches/fastd-log-format.patch /tmp/fastd-log-format.patch
COPY patches/fastd-close-fds.patch /tmp/fastd-close-fds.patch
RUN patch -d /build/fastd -p1 < /tmp/fastd-urandom.patch
RUN patch -d /build/fastd -p1 < /tmp/fastd-log-format.patch
RUN patch -d /build/fastd -p1 < /tmp/fastd-close-fds.patch
RUN meson setup /build/fastd-build /build/fastd \
    -Dbuildtype=release \
    -Dbuild_tests=false \
    -Doffload_l2tp=disabled \
 && meson compile -C /build/fastd-build \
 && meson install -C /build/fastd-build

RUN git clone --branch "$FIRMWARE_TAG" --depth 1 "$FIRMWARE_REPO" /build/firmware
RUN test -f /build/firmware/feeds/pool/bmxd/sources/Makefile
COPY patches/bmxd-msghdr-init.patch /tmp/bmxd-msghdr-init.patch
RUN patch -d /build/firmware/feeds/pool/bmxd/sources -p1 < /tmp/bmxd-msghdr-init.patch
RUN make -C /build/firmware/feeds/pool/bmxd/sources clean all

WORKDIR /build/ui
COPY ui/package.json /build/ui/package.json
COPY ui/tsconfig.json /build/ui/tsconfig.json
COPY ui/vite.config.ts /build/ui/vite.config.ts
COPY ui/index.html /build/ui/index.html
COPY ui/src/ /build/ui/src/
COPY ui/tests/ /build/ui/tests/
RUN npm install --no-audit --no-fund
RUN npm test && npm run build


FROM alpine:3.23 AS staging

# Assemble the complete filesystem layout in /staging
RUN mkdir -p \
    /staging/data \
    /staging/etc/nginx \
    /staging/etc/service \
    /staging/run/freifunk/fastd/peers \
    /staging/run/freifunk/wireguard \
    /staging/run/freifunk/bmxd \
    /staging/run/freifunk/sysinfo \
    /staging/run/freifunk/www \
    /staging/usr/bin \
    /staging/usr/lib/bmxd \
    /staging/usr/lib/fastd \
    /staging/usr/local/bin \
    /staging/usr/local/lib \
    /staging/usr/local/share/freifunk/ui

# Binaries from builder (only what we need)
COPY --from=builder /usr/local/bin/fastd /staging/usr/local/bin/
COPY --from=builder /usr/local/lib/libuecc.so* /staging/usr/local/lib/
COPY --from=builder /build/firmware/feeds/pool/bmxd/sources/bmxd /staging/usr/bin/

# UI build output
COPY --from=builder /build/ui/dist/ /staging/usr/local/share/freifunk/ui/

# License texts
COPY --from=builder /build/firmware/files/common/usr/lib/license/agreement-de.txt /staging/usr/local/share/freifunk/
COPY --from=builder /build/firmware/files/common/usr/lib/license/pico-de.txt /staging/usr/local/share/freifunk/
COPY --from=builder /build/firmware/license/gpl2-en.txt /staging/usr/local/share/freifunk/gpl2.txt
COPY --from=builder /build/firmware/license/gpl3-en.txt /staging/usr/local/share/freifunk/gpl3.txt

# Config files
COPY config/defaults.yaml /staging/usr/local/share/freifunk/defaults.yaml
COPY config/ca-root-ffdd.crt /staging/usr/local/share/freifunk/ca-root-ffdd.crt
COPY config/nginx.conf /staging/etc/nginx/nginx.conf

# Scripts
COPY scripts/backbone_runtime.py scripts/mesh-status.py scripts/node_config.py scripts/system_resources.py scripts/uplink_routes.py scripts/lan_mesh_access.py scripts/node_identity.py scripts/mesh_interfaces.py scripts/mesh_firewall.py scripts/registration_tls.py scripts/registrar.py scripts/sysinfo.py scripts/wireguard_status.py /staging/usr/local/bin/
COPY scripts/fastd-backbone-cmd.sh /staging/usr/lib/fastd/backbone-cmd.sh
COPY scripts/bmxd-launcher.sh /staging/usr/local/bin/bmxd-launcher.sh
COPY scripts/bmxd-gateway.py /staging/usr/lib/bmxd/bmxd-gateway.py
COPY scripts/docker-entrypoint.sh /staging/usr/local/bin/docker-entrypoint.sh
COPY scripts/runit/ /staging/etc/service/

RUN chmod +x \
    /staging/usr/local/bin/docker-entrypoint.sh \
    /staging/usr/local/bin/mesh-status.py \
    /staging/usr/local/bin/registrar.py \
    /staging/usr/local/bin/sysinfo.py \
    /staging/usr/local/bin/wireguard_status.py \
    /staging/usr/lib/fastd/backbone-cmd.sh \
    /staging/usr/local/bin/bmxd-launcher.sh \
    /staging/usr/lib/bmxd/bmxd-gateway.py \
    /staging/etc/service/registrar/run \
    /staging/etc/service/sysinfo/run \
    /staging/etc/service/fastd/run \
    /staging/etc/service/wireguard/run \
    /staging/etc/service/bmxd/run \
    /staging/etc/service/mesh-status/run \
    /staging/etc/service/nginx/run


FROM staging AS tests

RUN apk add --no-cache py3-yaml python3
COPY scripts/docker-entrypoint.sh scripts/backbone_runtime.py scripts/bmxd-gateway.py scripts/mesh-status.py scripts/node_config.py scripts/system_resources.py scripts/uplink_routes.py scripts/lan_mesh_access.py scripts/node_identity.py scripts/mesh_interfaces.py scripts/mesh_firewall.py scripts/registration_tls.py scripts/registrar.py scripts/run_gateway_script.py scripts/sysinfo.py scripts/wireguard_status.py /opt/freifunk-tests/scripts/
COPY tests/ /opt/freifunk-tests/tests/
COPY config/ca-root-ffdd.crt /opt/freifunk-tests/config/ca-root-ffdd.crt
COPY scripts/fastd-backbone-cmd.sh /opt/freifunk-tests/scripts/fastd-backbone-cmd.sh
COPY scripts/runit/fastd/run /opt/freifunk-tests/scripts/runit/fastd/run
RUN cd /opt/freifunk-tests \
 && python3 -m unittest discover -v -s tests -t .


FROM alpine:3.23 AS final

ENV TZ=Europe/Berlin
# Image execution is known to be container-scoped, even with private cgroup
# namespaces and serviceLinks disabled. Native Python execution may use auto/host.
ENV SYSINFO_RESOURCE_SCOPE=container

RUN apk add --no-cache \
    bash \
    bridge-utils \
    iproute2 \
    iptables \
    iputils \
    json-c \
    libcap \
    libsodium \
    nginx \
    openssl \
    py3-yaml \
    python3 \
    runit \
    tcpdump \
    tzdata \
    wireguard-tools-wg \
 && ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime \
 && echo "$TZ" > /etc/timezone

# Single COPY from staging: all freifunk files in one layer
COPY --from=tests /staging/ /

# Identity belongs to the built image, not runtime environment or its host checkout.
# Keep these args late so a new revision reuses dependency/UI/test build caches.
ARG BUILD_REF_TYPE
ARG BUILD_REF
ARG BUILD_COMMIT
ARG BUILD_VERSION
LABEL org.opencontainers.image.revision=$BUILD_COMMIT \
      org.opencontainers.image.version=$BUILD_VERSION \
      org.opencontainers.image.ref.name=$BUILD_REF
RUN python3 -c 'import json,sys; from pathlib import Path; Path("/usr/local/share/freifunk/build-identity.json").write_text(json.dumps(dict(zip(("ref_type", "ref", "commit", "version"), sys.argv[1:])))+"\n")' \
    "$BUILD_REF_TYPE" "$BUILD_REF" "$BUILD_COMMIT" "$BUILD_VERSION"

VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD []
