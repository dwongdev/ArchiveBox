# This is the Dockerfile for ArchiveBox, it bundles the following main dependencies:
#     python3.13, uv, python3-ldap
#     curl, wget, git, dig, ping, tree, nano
#     node, npm
#     ArchiveBox and plugin runtime dependencies installed by archivebox init --install
# Usage:
#     git clone https://github.com/ArchiveBox/ArchiveBox && cd ArchiveBox
#     docker build . -t archivebox
#     docker run -v "$PWD/data":/data archivebox init
#     docker run -v "$PWD/data":/data archivebox add 'https://example.com'
#     docker run -v "$PWD/data":/data -it archivebox manage createsuperuser
#     docker run -v "$PWD/data":/data -p 8000:8000 archivebox server
#     docker buildx build . --platform=linux/amd64,linux/arm64 --push -t archivebox/archivebox:dev -t archivebox/archivebox:sha-abc123
# Read more here: https://github.com/ArchiveBox/ArchiveBox#archivebox-development


#########################################################################################

### Example: Using ArchiveBox in your own project's Dockerfile ########

# FROM python:3.13-slim
# WORKDIR /data
# RUN pip install archivebox>=0.9.0   # use latest release here
# RUN archivebox install
# RUN useradd -ms /bin/bash archivebox && chown -R archivebox /data

#########################################################################################

ARG TARGETPLATFORM
ARG TARGETOS
ARG TARGETARCH
ARG TARGETVARIANT=

FROM archivebox/sonic:1.4.9 AS sonic
FROM ubuntu:24.04

LABEL name="archivebox" \
    maintainer="Nick Sweeting <dockerfile@archivebox.io>" \
    description="All-in-one self-hosted internet archiving solution" \
    homepage="https://github.com/ArchiveBox/ArchiveBox" \
    documentation="https://github.com/ArchiveBox/ArchiveBox/wiki/Docker" \
    org.opencontainers.image.title="ArchiveBox" \
    org.opencontainers.image.vendor="ArchiveBox" \
    org.opencontainers.image.description="All-in-one self-hosted internet archiving solution" \
    org.opencontainers.image.source="https://github.com/ArchiveBox/ArchiveBox" \
    com.docker.image.source.entrypoint="Dockerfile" \
    # TODO: release ArchiveBox as a Docker Desktop extension (requires these labels):
    # https://docs.docker.com/desktop/extensions-sdk/architecture/metadata/
    com.docker.desktop.extension.api.version=">= 1.4.7" \
    com.docker.desktop.extension.icon="https://archivebox.io/icon.png" \
    com.docker.extension.publisher-url="https://archivebox.io" \
    com.docker.extension.screenshots='[{"alt": "Screenshot of Admin UI", "url": "https://github.com/ArchiveBox/ArchiveBox/assets/511499/e8e0b6f8-8fdf-4b7f-8124-c10d8699bdb2"}]' \
    com.docker.extension.detailed-description='See here for detailed documentation: https://wiki.archivebox.io' \
    com.docker.extension.changelog='See here for release notes: https://github.com/ArchiveBox/ArchiveBox/releases' \
    com.docker.extension.categories='database,utility-tools'

ARG TARGETPLATFORM
ARG TARGETOS
ARG TARGETARCH
ARG TARGETVARIANT
######### Environment Variables #################################

# Global build-time and runtime environment constants + default pkg manager config
ENV TZ=UTC \
    LANGUAGE=en_US:en \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    APT_KEY_DONT_WARN_ON_DANGEROUS_USAGE=1 \
    PYTHONIOENCODING=UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    npm_config_loglevel=error

# Language Version config
ENV PYTHON_VERSION=3.13 \
    NODE_VERSION=22.22.3

# Non-root User config
ENV ARCHIVEBOX_USER="archivebox" \
    DEFAULT_PUID=911 \
    DEFAULT_PGID=911 \
    IN_DOCKER=True

# ArchiveBox Source Code + Lib + Data paths
ENV CODE_DIR=/app \
    DATA_DIR=/data \
    LIB_DIR=/opt/archivebox/lib \
    LIB_BIN_DIR=/opt/archivebox/lib/bin \
    ABXPKG_LIB_DIR=/opt/archivebox/lib \
    PLAYWRIGHT_BROWSERS_PATH=/browsers

# Bash SHELL config
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
SHELL ["/bin/bash", "-o", "pipefail", "-o", "errexit", "-o", "errtrace", "-o", "nounset", "-c"] 

######### System Environment ####################################

# Detect ArchiveBox version number by reading pyproject.toml (also serves to invalidate the entire build cache whenever pyproject.toml changes)
WORKDIR "$CODE_DIR"

# Force apt to leave downloaded binaries in /var/cache/apt (massively speeds up back-to-back Docker builds)
RUN echo 'Binary::apt::APT::Keep-Downloaded-Packages "1";' > /etc/apt/apt.conf.d/99keep-cache \
    && echo 'APT::Install-Recommends "0";' > /etc/apt/apt.conf.d/99no-intall-recommends \
    && echo 'APT::Install-Suggests "0";' > /etc/apt/apt.conf.d/99no-intall-suggests \
    && rm -f /etc/apt/apt.conf.d/docker-clean

# Print debug info about build and save it to disk, for human eyes only, not used by anything else
RUN (echo "[i] Docker build for ArchiveBox starting..." \
    && echo "PLATFORM=${TARGETPLATFORM} ARCH=$(uname -m) ($(uname -s) ${TARGETARCH} ${TARGETVARIANT})" \
    && echo "BUILD_START_TIME=$(date +"%Y-%m-%d %H:%M:%S %s") TZ=${TZ} LANG=${LANG}" \
    && echo \
    && echo "PYTHON=${PYTHON_VERSION} NODE=${NODE_VERSION} PATH=${PATH}" \
    && echo "CODE_DIR=${CODE_DIR} DATA_DIR=${DATA_DIR}" \
    && echo \
    && uname -a \
    && sed -n '1,7p' /etc/os-release \
    && which bash && bash --version | sed -n '1p' \
    && which dpkg && dpkg --version | sed -n '1p' \
    && echo -e '\n\n' && env && echo -e '\n\n' \
    ) | tee -a /VERSION.txt

# Create non-privileged user for archivebox and chrome
RUN echo "[*] Setting up $ARCHIVEBOX_USER user uid=${DEFAULT_PUID}..." \
    && groupadd --system $ARCHIVEBOX_USER \
    && useradd --system --create-home --gid $ARCHIVEBOX_USER --groups audio,video $ARCHIVEBOX_USER \
    && usermod -u "$DEFAULT_PUID" "$ARCHIVEBOX_USER" \
    && groupmod -g "$DEFAULT_PGID" "$ARCHIVEBOX_USER" \
    && echo -e "\nARCHIVEBOX_USER=$ARCHIVEBOX_USER PUID=$(id -u $ARCHIVEBOX_USER) PGID=$(id -g $ARCHIVEBOX_USER)\n\n" \
    | tee -a /VERSION.txt
    # DEFAULT_PUID and DEFAULT_PID are overridden by PUID and PGID in /bin/docker_entrypoint.sh at runtime
    # https://docs.linuxserver.io/general/understanding-puid-and-pgid

# Install system apt dependencies (adding backports to access more recent apt updates)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked,id=apt-$TARGETARCH$TARGETVARIANT \
    echo "[+] APT Installing base system dependencies for $TARGETPLATFORM..." \
    && mkdir -p /etc/apt/keyrings \
    && apt-get update -qq \
    && apt-get install -qq -y \
        # 1. packaging dependencies
        apt-transport-https ca-certificates apt-utils gnupg2 curl wget \
        # 2. docker and init system dependencies
        zlib1g-dev dumb-init gosu cron unzip grep dnsutils git python3.12-venv \
        # 3. frivolous CLI helpers to make debugging failed archiving easier
        tree nano iputils-ping \
        # nano iputils-ping dnsutils htop procps jq yq
    && rm -rf /var/lib/apt/lists/*

# Install sonic search backend
COPY --from=sonic /usr/local/bin/sonic /usr/local/bin/sonic
COPY --chown=root:root --chmod=755 "etc/sonic.cfg" /etc/sonic.cfg
RUN (which sonic && sonic --version) | tee -a /VERSION.txt

######### Language Environments ####################################

# Set up Python environment
# NOT NEEDED because we're using a pre-built python image, keeping this here in case we switch back to custom-building our own:
#RUN --mount=type=cache,target=/var/cache/apt,sharing=locked,id=apt-$TARGETARCH$TARGETVARIANT \
#    --mount=type=cache,target=/root/.cache/pip,sharing=locked,id=pip-$TARGETARCH$TARGETVARIANT \
# RUN echo "[+] APT Installing PYTHON $PYTHON_VERSION for $TARGETPLATFORM (skipped, provided by base image)..." \
    # && apt-get update -qq \
    # && apt-get install -qq -y --no-upgrade \
    #     python${PYTHON_VERSION} python${PYTHON_VERSION}-minimal python3-pip python${PYTHON_VERSION}-venv pipx \
    # && rm -rf /var/lib/apt/lists/* \
    # tell PDM to allow using global system python site packages
    # && rm /usr/lib/python3*/EXTERNALLY-MANAGED \
    # && ln -s "$(which python${PYTHON_VERSION})" /usr/bin/python \
    # create global virtual environment GLOBAL_VENV to use (better than using pip install --global)
    # && python3 -m venv --system-site-packages --symlinks $GLOBAL_VENV \
    # && python3 -m venv --system-site-packages $GLOBAL_VENV \
    # && python3 -m venv $GLOBAL_VENV \
    # install global dependencies / python build dependencies in GLOBAL_VENV
    # && pip install --upgrade pip setuptools wheel \
    # Save version info
    # && ( \
    #     which python3 && python3 --version | grep " $PYTHON_VERSION" \
    #     && which pip && pip --version \
    #     # && which pdm && pdm --version \
    #     && echo -e '\n\n' \
    # ) | tee -a /VERSION.txt


# Set up Node environment from the official platform tarball. This avoids
# NodeSource apt dependencies pulling Ubuntu's python3-minimal postinst into
# emulated Docker builds.
RUN --mount=type=cache,target=/root/.npm,sharing=locked,id=npm-$TARGETARCH$TARGETVARIANT \
    case "$TARGETARCH" in \
        amd64) NODE_DIST_ARCH="x64" ;; \
        arm64) NODE_DIST_ARCH="arm64" ;; \
        *) echo "Unsupported TARGETARCH=$TARGETARCH for Node binary install" >&2; exit 1 ;; \
    esac \
    && NODE_TARBALL="node-v${NODE_VERSION}-linux-${NODE_DIST_ARCH}.tar.gz" \
    && echo "[+] Installing NODE $NODE_VERSION for linux/${NODE_DIST_ARCH}..." \
    && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL}" \
    && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" \
    && grep "  ${NODE_TARBALL}$" SHASUMS256.txt | sha256sum -c - \
    && tar -xzf "$NODE_TARBALL" -C /usr/local --strip-components=1 --no-same-owner \
    && rm "$NODE_TARBALL" SHASUMS256.txt \
    # Save version info
    && ( \
        which node && node --version \
        && which npm && npm --version \
        && echo -e '\n\n' \
    ) | tee -a /VERSION.txt


# Set up uv and main app /venv
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/bin sh
ENV UV_COMPILE_BYTECODE=0 \
    UV_PYTHON_PREFERENCE=managed \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/venv
WORKDIR "$CODE_DIR"
# COPY --chown=root:root --chmod=755 pyproject.toml "$CODE_DIR/"
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked,id=uv-$TARGETARCH$TARGETVARIANT \
    echo "[+] UV Creating /venv using python ${PYTHON_VERSION} for ${TARGETPLATFORM}..." \
    && uv venv /venv --python ${PYTHON_VERSION}
ENV VIRTUAL_ENV=/venv PATH="/venv/bin:$PATH"
RUN uv pip install setuptools pip \
    && ( \
        which python3 && python3 --version \
        && which uv && uv self version \
        && uv python find --system && uv python find \
        && echo -e '\n\n' \
    ) | tee -a /VERSION.txt


######### ArchiveBox & Extractor Dependencies ##################################

# Install ArchiveBox C-compiled/apt-installed Python dependencies in app /venv (currently only used for python-ldap)
WORKDIR "$CODE_DIR"
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked,id=apt-$TARGETARCH$TARGETVARIANT \
    --mount=type=cache,target=/root/.cache/uv,sharing=locked,id=uv-$TARGETARCH$TARGETVARIANT \
    #--mount=type=cache,target=/root/.cache/pip,sharing=locked,id=pip-$TARGETARCH$TARGETVARIANT \
    echo "[+] APT Installing + Compiling python3-ldap for PIP archivebox[ldap] on ${TARGETPLATFORM}..." \
    && apt-get update -qq \
    && apt-get install -qq -y --no-install-recommends \
        build-essential gcc \
        python3-dev libssl-dev libldap2-dev libsasl2-dev python3-ldap \
        python3-msgpack python3-mutagen python3-regex python3-pycryptodome procps \
    && uv pip install \
        "python-ldap>=3.4.3" \
    && apt-get purge -y \
        python3-dev build-essential gcc \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*


# Runtime config used by plugin hooks. Plugin binaries and npm packages are
# installed into LIB_DIR below by archivebox init --install and resolved from
# LIB_DIR by ArchiveBox/abxpkg, not by mutating the container PATH.
ENV PERSONAS_DIR=/data/personas \
    CHROME_EXTENSIONS_DIR=/opt/archivebox/lib/chrome_extensions \
    CHROME_USER_DATA_DIR=/data/personas/Default/chrome_profile \
    CHROME_HEADLESS=true \
    CHROME_SANDBOX=false \
    CHROME_ISOLATION=crawl \
    CHROME_ARGS_EXTRA='["--disable-gpu","--disable-features=Translate,OptimizationGuideModelDownloading,MediaRouter"]'

######### Build Dependencies ####################################


# Install ArchiveBox Python venv dependencies from pyproject.toml.
RUN --mount=type=bind,source=pyproject.toml,target=/app/pyproject.toml \
    --mount=type=cache,target=/var/cache/apt,sharing=locked,id=apt-$TARGETARCH$TARGETVARIANT \
    --mount=type=cache,target=/root/.cache/uv,sharing=locked,id=uv-$TARGETARCH$TARGETVARIANT \
    echo "[+] PIP Installing ArchiveBox dependencies from pyproject.toml..." \
    && apt-get update -qq \
    && apt-get install -qq -y --no-install-recommends build-essential gcc python3-dev \
    && uv --no-cache sync \
        --refresh \
        --no-dev \
        --inexact \
        --all-extras \
        --no-install-project \
        --no-install-workspace \
        --no-sources \
    && apt-get purge -y python3-dev build-essential gcc \
    && apt-get autoremove -y \
    && find /venv -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find /venv -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete \
    && rm -rf /var/lib/apt/lists/*
    # installs the pip packages that archivebox depends on, defined in pyproject.toml dependencies

# Setup ArchiveBox runtime config
ENV TMP_DIR=/tmp/archivebox \
    PIP_VENV_PYTHON=/usr/bin/python3.12 \
    GOOGLE_API_KEY=no \
    GOOGLE_DEFAULT_CLIENT_ID=no \
    GOOGLE_DEFAULT_CLIENT_SECRET=no

WORKDIR "$DATA_DIR"
RUN openssl rand -hex 16 > /etc/machine-id \
    && mkdir -p "$DATA_DIR" \
    && chown "$DEFAULT_PUID:$DEFAULT_PGID" "$DATA_DIR" \
    && mkdir -p "$TMP_DIR" \
    && chown -R "$DEFAULT_PUID:$DEFAULT_PGID" "$TMP_DIR" \
    && mkdir -p "$LIB_DIR" \
    && chown -R "$DEFAULT_PUID:$DEFAULT_PGID" "$LIB_DIR" \
    && mkdir -p "$PLAYWRIGHT_BROWSERS_PATH" \
    && chown "$DEFAULT_PUID:$DEFAULT_PGID" "$PLAYWRIGHT_BROWSERS_PATH" \
    && echo -e "\nTMP_DIR=$TMP_DIR\nLIB_DIR=$LIB_DIR\nPLAYWRIGHT_BROWSERS_PATH=$PLAYWRIGHT_BROWSERS_PATH\nMACHINE_ID=$(cat /etc/machine-id)\n" | tee -a /VERSION.txt

# Pre-bake plugin-managed runtime dependencies using the same abx-dl installer
# path users run later, before copying ArchiveBox source so source-only edits do
# not invalidate the heavy browser/plugin dependency layer.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked,id=apt-$TARGETARCH$TARGETVARIANT \
    --mount=type=cache,target=/root/.cache/uv,sharing=locked,id=uv-$TARGETARCH$TARGETVARIANT \
    --mount=type=cache,target=/root/.npm,sharing=locked,id=npm-$TARGETARCH$TARGETVARIANT \
    --mount=type=cache,target=/root/.cache/puppeteer,sharing=locked,id=puppeteer-$TARGETARCH$TARGETVARIANT \
    --mount=type=cache,target=/root/.cache/ms-playwright,sharing=locked,id=browsers-$TARGETARCH$TARGETVARIANT \
    --mount=type=cache,target=/opt/archivebox/lib,sharing=locked,id=archivebox-lib-$TARGETARCH$TARGETVARIANT \
    echo "[+] Installing plugin runtime dependencies into $LIB_DIR..." \
    && export PERSONAS_DIR="$LIB_DIR/personas" \
    && export CHROME_EXTENSIONS_DIR="$LIB_DIR/chrome_extensions" \
    && export CHROME_USER_DATA_DIR="$LIB_DIR/chrome_profile" \
    && mkdir -p "$LIB_DIR" "$LIB_DIR/chrome_extensions" \
    && apt-get update -qq \
    && if [ "$TARGETARCH" = "arm64" ]; then \
        abxpkg install --binproviders=npm --overrides='{"npm":{"install_args":["playwright@next"]}}' playwright; \
        abxpkg install --no-cache --install-timeout=600 --binproviders=playwright --bin-dir="$LIB_DIR/env/bin" chromium; \
    fi \
    && TIMEOUT=600 PUID=0 PGID=0 abx-dl plugins --install \
    && find "$LIB_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find "$LIB_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete \
    && rm -rf "$LIB_DIR/personas" "$LIB_DIR/chrome_profile" /opt/archivebox/lib-layer \
    && mkdir -p /opt/archivebox/lib-layer \
    && cp -a "$LIB_DIR"/. /opt/archivebox/lib-layer/ \
    && rm -rf /var/lib/apt/lists/* \
    && chown -R "$DEFAULT_PUID:$DEFAULT_PGID" /opt/archivebox/lib-layer

RUN rm -rf "$LIB_DIR" \
    && mv /opt/archivebox/lib-layer "$LIB_DIR" \
    && chown -R "$DEFAULT_PUID:$DEFAULT_PGID" "$LIB_DIR"

# Install ArchiveBox Python package from the checked-out source.
WORKDIR "$CODE_DIR"
COPY --chown=root:root --chmod=755 "." "$CODE_DIR/"
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked,id=uv-$TARGETARCH$TARGETVARIANT \
    echo "[*] Installing ArchiveBox Python source code from $CODE_DIR..." \
    && pip install \
        --no-deps \
        "$CODE_DIR" \
    && ( \
        pip show archivebox \
        && which archivebox \
        && echo -e '\n\n' \
    ) | tee -a /VERSION.txt \
    && find /venv "$CODE_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find /venv "$CODE_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
    # installs archivebox itself, and any other vendored packages in pkgs/*, defined in pyproject.toml workspaces

# Initialize an empty image collection without rerunning dependency installs.
WORKDIR "$DATA_DIR"
RUN echo "[+] Initializing image collection..." \
    && PUID=0 PGID=0 archivebox init \
    && find "$DATA_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find "$DATA_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete \
    && chown -R "$DEFAULT_PUID:$DEFAULT_PGID" "$LIB_DIR" \
    && (chown "$DEFAULT_PUID:$DEFAULT_PGID" \
        "$DATA_DIR" "$DATA_DIR"/.archivebox_id "$DATA_DIR"/ArchiveBox.conf "$DATA_DIR"/index.sqlite3 \
        "$DATA_DIR"/logs "$DATA_DIR"/logs/* "$DATA_DIR"/sources \
        "$DATA_DIR"/archive "$DATA_DIR"/archive/users "$DATA_DIR"/personas \
        "$DATA_DIR"/tmp "$DATA_DIR"/tmp/* \
        2>/dev/null || true)

# Print version for nice docker finish summary
RUN (echo -e "\n\n[√] Finished Docker build successfully. Saving build summary in: /VERSION.txt" \
    && echo -e "PLATFORM=${TARGETPLATFORM} ARCH=$(uname -m) ($(uname -s) ${TARGETARCH} ${TARGETVARIANT})\n" \
    && echo -e "BUILD_END_TIME=$(date +"%Y-%m-%d %H:%M:%S %s")\n\n" \
    ) | tee -a /VERSION.txt

# Verify ArchiveBox is installed and write full version/dependency info.
RUN chmod +x "$CODE_DIR"/bin/*.sh \
    && chown -R "$DEFAULT_PUID:$DEFAULT_PGID" "$LIB_DIR" \
    && chmod g+w "$TMP_DIR" "$LIB_DIR" "$LIB_DIR"/bin "$PLAYWRIGHT_BROWSERS_PATH" \
    && gosu "$ARCHIVEBOX_USER" archivebox install 2>&1 | tee -a /VERSION.txt \
    && gosu "$ARCHIVEBOX_USER" archivebox version 2>&1 | tee -a /VERSION.txt \
    && find /venv "$CODE_DIR" "$LIB_DIR" "$DATA_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find /venv "$CODE_DIR" "$LIB_DIR" "$DATA_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete \
    && rm -rf /root/.cache /var/cache/apt/* /var/lib/apt/lists/*

####################################################

# Expose ArchiveBox's main interfaces to the outside world
WORKDIR "$DATA_DIR"
VOLUME "$DATA_DIR"
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=20s --retries=15 \
    CMD curl --fail --silent --show-error --max-time 5 --connect-timeout 2 'http://admin.archivebox.localhost:8000/health/' | grep -q 'OK'

ENTRYPOINT ["dumb-init", "--", "/app/bin/docker_entrypoint.sh"]
CMD ["archivebox", "server", "--init", "0.0.0.0:8000"]
