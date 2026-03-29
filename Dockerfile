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
RUN patch -d /build/fastd -p1 < /tmp/fastd-urandom.patch
RUN patch -d /build/fastd -p1 < /tmp/fastd-log-format.patch
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
RUN npm install --no-audit --no-fund
RUN npm run build


FROM alpine:3.23 AS runtime-base

ENV TZ=Europe/Berlin

RUN apk add --no-cache \
    bash \
    bridge-utils \
    iproute2 \
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
    wireguard-tools-wg

RUN ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime \
 && echo "$TZ" > /etc/timezone

COPY --from=builder /usr/local /usr/local
COPY --from=builder /build/firmware/feeds/pool/bmxd/sources/bmxd /usr/bin/bmxd

RUN mkdir -p /data /run/freifunk/fastd/peers /run/freifunk/wireguard /run/freifunk/bmxd /run/freifunk/sysinfo /run/freifunk/www /usr/local/share/freifunk /usr/lib/bmxd /etc/service

COPY config/defaults.yaml /usr/local/share/freifunk/defaults.yaml
COPY config/nginx.conf /etc/nginx/nginx.conf
COPY --from=builder /build/ui/dist/ /usr/local/share/freifunk/ui/
COPY --from=builder /build/firmware/files/common/usr/lib/license/agreement-de.txt /usr/local/share/freifunk/agreement-de.txt
COPY --from=builder /build/firmware/files/common/usr/lib/license/pico-de.txt /usr/local/share/freifunk/pico-de.txt
COPY --from=builder /build/firmware/license/gpl2-en.txt /usr/local/share/freifunk/gpl2.txt
COPY --from=builder /build/firmware/license/gpl3-en.txt /usr/local/share/freifunk/gpl3.txt
COPY scripts/backbone_runtime.py scripts/mesh-status.py scripts/node_config.py scripts/registrar.py scripts/sysinfo.py scripts/wireguard_status.py /usr/local/bin/
COPY scripts/fastd-backbone-cmd.sh /usr/lib/fastd/backbone-cmd.sh
COPY scripts/bmxd-launcher.sh /usr/local/bin/bmxd-launcher.sh
COPY scripts/bmxd-gateway.py /usr/lib/bmxd/bmxd-gateway.py
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY scripts/runit/ /etc/service/

RUN chmod +x \
    /usr/local/bin/docker-entrypoint.sh \
    /usr/local/bin/mesh-status.py \
    /usr/local/bin/registrar.py \
    /usr/local/bin/sysinfo.py \
    /usr/local/bin/wireguard_status.py \
    /usr/lib/fastd/backbone-cmd.sh \
    /usr/local/bin/bmxd-launcher.sh \
    /usr/lib/bmxd/bmxd-gateway.py \
    /etc/service/registrar/run \
    /etc/service/sysinfo/run \
    /etc/service/fastd/run \
    /etc/service/wireguard/run \
    /etc/service/bmxd/run \
    /etc/service/mesh-status/run \
    /etc/service/nginx/run

VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD []


FROM runtime-base AS tests

COPY scripts/backbone_runtime.py scripts/bmxd-gateway.py scripts/mesh-status.py scripts/node_config.py scripts/registrar.py scripts/run_gateway_script.py scripts/sysinfo.py scripts/wireguard_status.py /opt/freifunk-tests/scripts/
COPY tests/ /opt/freifunk-tests/tests/
RUN cd /opt/freifunk-tests \
 && python3 -m unittest discover -v -s tests -t . \
 && touch /tmp/tests-passed


FROM runtime-base AS final

COPY --from=tests /tmp/tests-passed /tmp/tests-passed
