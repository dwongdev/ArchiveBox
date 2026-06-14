# syntax=docker/dockerfile:1.7

# Multistage ArchiveBox Dockerfile that consumes the abx-dl runtime image.
# abx-dl owns Python, Node, Chromium, and downloader plugin runtimes.
# ArchiveBox owns ripgrep, sonic, supervisor, Django, and the app runtime.
# Build abx-dl first, then point this file at it:
#   docker buildx build ../abx-dl -f ../abx-dl/Dockerfile \
#       --build-context abxbus=../abxbus \
#       --build-context abxpkg=../abxpkg \
#       --build-context abx-plugins=../abx-plugins \
#       -t archivebox/abx-dl:dev
#   docker buildx build . -f Dockerfile \
#       --build-arg ABX_DL_IMAGE=archivebox/abx-dl:latest \
#       -t archivebox:multistage

ARG ABX_DL_IMAGE=archivebox/abx-dl:latest

FROM archivebox/sonic:1.4.9 AS sonic
FROM ${ABX_DL_IMAGE} AS archivebox-runtime-base

ARG TARGETPLATFORM
ARG TARGETOS
ARG TARGETARCH
ARG TARGETVARIANT

ENV TZ=UTC \
    LANGUAGE=en_US:en \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    APT_KEY_DONT_WARN_ON_DANGEROUS_USAGE=1 \
    PYTHONIOENCODING=UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_COMPILE=1 \
    PIP_ONLY_BINARY=aiohttp \
    npm_config_loglevel=error

ENV PYTHON_VERSION=3.13 \
    NODE_VERSION=24

ENV ARCHIVEBOX_USER=archivebox \
    DEFAULT_ARCHIVEBOX_UID=911 \
    DEFAULT_ARCHIVEBOX_GID=911 \
    IN_DOCKER=True

ENV CODE_DIR=/app \
    DATA_DIR=/data \
    CONFIG_DIR=/opt/archivebox \
    LIB_DIR=/opt/archivebox/lib \
    ABXPKG_LIB_DIR=/opt/archivebox/lib \
    PLAYWRIGHT_BROWSERS_PATH=/opt/archivebox/lib/playwright/cache \
    PERSONAS_DIR=/data/personas \
    CHROME_USER_DATA_DIR=/data/personas/Default/chrome_profile \
    CHROME_HEADLESS=true \
    CHROME_SANDBOX=false \
    CHROME_ISOLATION=crawl \
    CHROME_ARGS_EXTRA='["--disable-gpu","--disable-features=Translate,OptimizationGuideModelDownloading,MediaRouter"]'

ENV TMP_DIR=/tmp/archivebox \
    PIP_VENV_PYTHON=/venv/bin/python3 \
    GOOGLE_API_KEY=no \
    GOOGLE_DEFAULT_CLIENT_ID=no \
    GOOGLE_DEFAULT_CLIENT_SECRET=no

ENV HOME=/home/archivebox \
    XDG_CONFIG_HOME=/home/archivebox/.config \
    XDG_CACHE_HOME=/opt/archivebox/lib/cache \
    ABXPKG_INSTALL_TIMEOUT=600 \
    ABXPKG_POSTINSTALL_SCRIPTS=True \
    ABXPKG_MIN_RELEASE_AGE=0 \
    TIMEOUT=600

ENV UV_COMPILE_BYTECODE=false \
    UV_PYTHON_PREFERENCE=managed \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/venv \
    VIRTUAL_ENV=/venv \
    PATH="/venv/bin:/opt/node/bin:$PATH"

SHELL ["/bin/bash", "-o", "pipefail", "-o", "errexit", "-o", "errtrace", "-o", "nounset", "-c"]
WORKDIR "$CODE_DIR"

RUN cp /VERSION.txt /ABX-DL-VERSION.txt \
    && (echo "[i] Docker build for ArchiveBox multistage starting..." \
    && echo "PLATFORM=${TARGETPLATFORM} ARCH=$(uname -m) (${TARGETARCH} ${TARGETVARIANT})" \
    && echo "BUILD_START_TIME=$(date +"%Y-%m-%d %H:%M:%S %s") TZ=${TZ} LANG=${LANG}" \
    && uname -a \
    && sed -n '1,7p' /etc/os-release \
    && which node && node --version \
    && which uv && uv self version \
    ) | tee -a /VERSION.txt

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked,id=apt-$TARGETARCH$TARGETVARIANT \
    echo "[+] APT Installing ArchiveBox search dependency ripgrep for $TARGETPLATFORM..." \
    && apt-get update -qq \
    && apt-get install -qq -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*

FROM archivebox-runtime-base AS archivebox-builder

WORKDIR "$CODE_DIR"
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked,id=apt-$TARGETARCH$TARGETVARIANT \
    --mount=type=cache,target=/root/.cache/uv,sharing=locked,id=uv-$TARGETARCH$TARGETVARIANT \
    --mount=type=bind,source=pyproject.toml,target=/app/pyproject.toml \
    <<'EOF'
echo "[+] UV Installing ArchiveBox dependencies from pyproject.toml..."
echo 'Binary::apt::APT::Keep-Downloaded-Packages "1";' > /etc/apt/apt.conf.d/99keep-cache
echo 'APT::Install-Recommends "0";' > /etc/apt/apt.conf.d/99no-install-recommends
echo 'APT::Install-Suggests "0";' > /etc/apt/apt.conf.d/99no-install-suggests
rm -f /etc/apt/apt.conf.d/docker-clean
apt-get update -qq
apt-get install -qq -y --no-install-recommends \
    build-essential gcc libldap2-dev libsasl2-dev libssl-dev
/usr/bin/uv venv --clear /venv --python "${PYTHON_VERSION}"
/usr/bin/uv pip install setuptools pip wheel

mkdir -p /tmp/archivebox-uv-project
/venv/bin/python - <<'PY'
from pathlib import Path
import json
import re
import urllib.request

source = Path("/app/pyproject.toml")
target = Path("/tmp/archivebox-uv-project/pyproject.toml")
text = source.read_text()
text = text.replace(
    'environments = ["sys_platform == \'darwin\'", "sys_platform == \'linux\'"]',
    'environments = ["sys_platform == \'linux\'"]',
)

# Docker builds need the just-published internal abx wheels immediately, but
# PyPI simple can lag the version JSON endpoints by tens of minutes. Generate a
# Docker-only dependency view from the version JSON so the published package
# metadata stays normal while image builds remain resumable after a release.
for package in ("abxpkg", "abx-plugins", "abx-dl"):
    match = re.search(rf'"{re.escape(package)}>=(?P<version>[^"]+)"', text)
    if not match:
        continue
    version = match.group("version")
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/{version}/json", timeout=20) as response:
        data = json.load(response)
    wheel_url = next(url["url"] for url in data["urls"] if url["filename"].endswith(".whl"))
    text = re.sub(
        rf'"{re.escape(package)}>=[^"]+"',
        f'"{package} @ {wheel_url}"',
        text,
        count=1,
    )

target.write_text(text)
PY

/usr/bin/uv sync \
    --project /tmp/archivebox-uv-project \
    --refresh \
    --no-dev \
    --inexact \
    --no-install-project \
    --no-install-workspace \
    --no-sources
(find /venv/lib/python3.*/site-packages -type f -name '*.so' -exec strip --strip-unneeded {} + 2>/dev/null || true)
rm -f /venv/bin/uv /venv/bin/uvx
apt-get purge -y build-essential gcc libldap2-dev libsasl2-dev libssl-dev
apt-get autoremove -y
rm -rf /var/lib/apt/lists/*
EOF

COPY --chown=root:root --chmod=755 "." "$CODE_DIR/"
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked,id=uv-$TARGETARCH$TARGETVARIANT \
    echo "[*] Installing ArchiveBox Python source code from $CODE_DIR..." \
    && COMMIT_HASH="$( \
        if [[ -f "$CODE_DIR/.git/HEAD" ]]; then \
            HEAD_REF="$(cat "$CODE_DIR/.git/HEAD")"; \
            if [[ "$HEAD_REF" =~ ^[0-9a-fA-F]{40}$ ]]; then \
                echo "$HEAD_REF"; \
            elif [[ "$HEAD_REF" == ref:\ * ]]; then \
                REF_PATH="${HEAD_REF#ref: }"; \
                cat "$CODE_DIR/.git/$REF_PATH" 2>/dev/null || awk -v ref="$REF_PATH" '$2 == ref {print $1}' "$CODE_DIR/.git/packed-refs" 2>/dev/null || true; \
            fi; \
        fi)" \
    && if [[ "$COMMIT_HASH" =~ ^[0-9a-fA-F]{40}$ ]]; then echo "COMMIT_HASH=$COMMIT_HASH" | tee -a /VERSION.txt; fi \
    && /usr/bin/uv pip install --no-deps "$CODE_DIR" \
    && rm -f /venv/bin/uv /venv/bin/uvx \
    && (/usr/bin/uv pip show archivebox && which archivebox) | tee -a /VERSION.txt \
    && rm -rf "$CODE_DIR/.git"

FROM archivebox-runtime-base

LABEL name="archivebox" \
    maintainer="Nick Sweeting <dockerfile@archivebox.io>" \
    description="All-in-one self-hosted internet archiving solution" \
    homepage="https://github.com/ArchiveBox/ArchiveBox" \
    documentation="https://github.com/ArchiveBox/ArchiveBox/wiki/Docker" \
    org.opencontainers.image.title="ArchiveBox" \
    org.opencontainers.image.vendor="ArchiveBox" \
    org.opencontainers.image.description="All-in-one self-hosted internet archiving solution" \
    org.opencontainers.image.source="https://github.com/ArchiveBox/ArchiveBox" \
    com.docker.image.source.entrypoint="Dockerfile"

COPY --from=sonic /usr/local/bin/sonic /usr/local/bin/sonic
COPY --chown=root:root --chmod=755 "etc/sonic.cfg" /etc/sonic.cfg

COPY --from=archivebox-builder /opt/uv/python /opt/uv/python
COPY --from=archivebox-builder /venv /venv
COPY --from=archivebox-builder /app /app
COPY --from=archivebox-builder /VERSION.txt /VERSION.txt

RUN echo "[*] Setting up $ARCHIVEBOX_USER user uid=${DEFAULT_ARCHIVEBOX_UID}..." \
    && printf 'export PATH="/venv/bin:/opt/node/bin:$PATH"\n' > /etc/profile.d/archivebox-path.sh \
    && ln -sf /venv/bin/archivebox /usr/local/bin/archivebox \
    && ln -sf /venv/bin/daphne /usr/local/bin/daphne \
    && ln -sf /venv/bin/supervisord /usr/local/bin/supervisord \
    && ln -sf /venv/bin/supervisorctl /usr/local/bin/supervisorctl \
    && getent group "$ARCHIVEBOX_USER" >/dev/null || groupadd --system "$ARCHIVEBOX_USER" \
    && id -u "$ARCHIVEBOX_USER" >/dev/null 2>&1 || useradd --system --create-home --gid "$ARCHIVEBOX_USER" --groups audio,video "$ARCHIVEBOX_USER" \
    && usermod --append --groups audio,video "$ARCHIVEBOX_USER" \
    && [[ "$(id -u "$ARCHIVEBOX_USER")" == "$DEFAULT_ARCHIVEBOX_UID" ]] || usermod -u "$DEFAULT_ARCHIVEBOX_UID" "$ARCHIVEBOX_USER" \
    && [[ "$(id -g "$ARCHIVEBOX_USER")" == "$DEFAULT_ARCHIVEBOX_GID" ]] || groupmod -g "$DEFAULT_ARCHIVEBOX_GID" "$ARCHIVEBOX_USER" \
    && (which sonic && sonic --version) | tee -a /VERSION.txt \
    && install -d -o "$DEFAULT_ARCHIVEBOX_UID" -g "$DEFAULT_ARCHIVEBOX_GID" "$DATA_DIR" "$TMP_DIR" "$CONFIG_DIR" "$LIB_DIR" "$PLAYWRIGHT_BROWSERS_PATH" \
    && install -d -o "$DEFAULT_ARCHIVEBOX_UID" -g "$DEFAULT_ARCHIVEBOX_GID" "/home/$ARCHIVEBOX_USER" \
    && chown "$DEFAULT_ARCHIVEBOX_UID:$DEFAULT_ARCHIVEBOX_GID" "$DATA_DIR" "$TMP_DIR" "$LIB_DIR" "$PLAYWRIGHT_BROWSERS_PATH" \
    && openssl rand -hex 16 > /etc/machine-id \
    && echo -e "\nARCHIVEBOX_USER=$ARCHIVEBOX_USER ARCHIVEBOX_UID=$(id -u "$ARCHIVEBOX_USER") ARCHIVEBOX_GID=$(id -g "$ARCHIVEBOX_USER")" | tee -a /VERSION.txt \
    && echo -e "TMP_DIR=$TMP_DIR\nLIB_DIR=$LIB_DIR\nPLAYWRIGHT_BROWSERS_PATH=$PLAYWRIGHT_BROWSERS_PATH\nMACHINE_ID=$(cat /etc/machine-id)\n" | tee -a /VERSION.txt

WORKDIR "$DATA_DIR"
RUN echo "[+] Initializing image collection..." \
    && find "$DATA_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && archivebox init \
    && (chown "$DEFAULT_ARCHIVEBOX_UID:$DEFAULT_ARCHIVEBOX_GID" \
        "$DATA_DIR" "$DATA_DIR"/.archivebox_id "$DATA_DIR"/ArchiveBox.conf "$DATA_DIR"/index.sqlite3 \
        "$DATA_DIR"/logs "$DATA_DIR"/logs/* "$DATA_DIR"/sources \
        "$DATA_DIR"/archive "$DATA_DIR"/archive/users "$DATA_DIR"/personas \
        "$DATA_DIR"/tmp "$DATA_DIR"/tmp/* \
        "$CONFIG_DIR" "$CONFIG_DIR"/config.env "$CONFIG_DIR"/derived.env \
        "$TMP_DIR" "$LIB_DIR" "$PLAYWRIGHT_BROWSERS_PATH" \
        2>/dev/null || true) \
    && find "$TMP_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

RUN chmod +x "$CODE_DIR"/bin/*.sh \
    && chmod g+w "$TMP_DIR" "$LIB_DIR" "$PLAYWRIGHT_BROWSERS_PATH"

RUN "$LIB_DIR/playwright/bin/chromium" --version | tee -a /VERSION.txt \
    && "$LIB_DIR/uv/packages/papers-dl/venv/bin/papers-dl" --version | tee -a /VERSION.txt \
    && /usr/bin/rg --version | head -1 | tee -a /VERSION.txt \
    && /usr/local/bin/sonic --version | tee -a /VERSION.txt \
    && /venv/bin/supervisord --version | tee -a /VERSION.txt \
    && for forbidden_bin in gcc g++ make; do ! command -v "$forbidden_bin" || (echo "Unexpected build tool in runtime: $forbidden_bin=$(command -v "$forbidden_bin")" >&2 && exit 1); done \
    && stat -c "%U:%G %a %n" "$CONFIG_DIR" "$LIB_DIR" "$PLAYWRIGHT_BROWSERS_PATH" \
    && setpriv --reuid="$ARCHIVEBOX_USER" --regid="$ARCHIVEBOX_USER" --init-groups test -w "$CONFIG_DIR" \
    && setpriv --reuid="$ARCHIVEBOX_USER" --regid="$ARCHIVEBOX_USER" --init-groups test -w "$LIB_DIR" \
    && setpriv --reuid="$ARCHIVEBOX_USER" --regid="$ARCHIVEBOX_USER" --init-groups archivebox version 2>&1 | tee -a /VERSION.txt \
    && setpriv --reuid="$ARCHIVEBOX_USER" --regid="$ARCHIVEBOX_USER" --init-groups archivebox install \
    && rm -rf /root/.cache /var/cache/apt/* /var/lib/apt/lists/*

RUN (echo -e "\n\n[√] Finished ArchiveBox multistage Docker build successfully." \
    && echo -e "PLATFORM=${TARGETPLATFORM} ARCH=$(uname -m) (${TARGETARCH} ${TARGETVARIANT})" \
    && echo -e "BUILD_END_TIME=$(date +"%Y-%m-%d %H:%M:%S %s")\n\n" \
    ) | tee -a /VERSION.txt

WORKDIR "$DATA_DIR"
VOLUME "$DATA_DIR"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=20s --retries=15 \
    CMD curl --fail --silent --show-error --max-time 5 --connect-timeout 2 'http://admin.archivebox.localhost:8000/health/' | grep -q 'OK'

ENTRYPOINT ["dumb-init", "--", "/app/bin/docker_entrypoint.sh"]
CMD ["archivebox", "server", "--init", "0.0.0.0:8000"]
