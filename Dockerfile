# Pin to a specific patch version for reproducible builds.
# To pick up security patches, bump this version and rebuild.
FROM python:3.12.13

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core utilities
    coreutils findutils grep sed gawk diffutils patch \
    less file tree bc man-db \
    # Networking
    curl wget net-tools iputils-ping dnsutils netcat-openbsd socat telnet \
    openssh-client rsync \
    # Editors
    vim nano \
    # Version control
    git \
    # Build tools
    build-essential cmake make \
    # Scripting & languages
    perl ruby-full lua5.4 \
    # Data processing
    jq xmlstarlet sqlite3 \
    # Media & documents
    ffmpeg pandoc imagemagick texlive-latex-base \
    # Compression
    zip unzip tar gzip bzip2 xz-utils zstd p7zip-full \
    # System
    procps htop lsof strace sysstat \
    sudo tmux screen tini iptables ipset dnsmasq \
    ca-certificates gnupg apt-transport-https \
    # Capabilities (needed for setcap on Python binary)
    libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# OfficeCLI (pinned + checksum verified)
ARG OFFICECLI_VERSION=v1.0.44
ARG OFFICECLI_REPO=iOfficeAI/OfficeCLI
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) officecli_asset="officecli-linux-x64" ;; \
      arm64) officecli_asset="officecli-linux-arm64" ;; \
      *) echo "Unsupported architecture for OfficeCLI: $arch" >&2; exit 1 ;; \
    esac; \
    officecli_base_url="https://github.com/${OFFICECLI_REPO}/releases/download/${OFFICECLI_VERSION}"; \
    curl -fsSL "${officecli_base_url}/${officecli_asset}" -o /usr/local/bin/officecli; \
    curl -fsSL "${officecli_base_url}/SHA256SUMS" -o /tmp/officecli-SHA256SUMS; \
    officecli_expected="$(grep " ${officecli_asset}\$" /tmp/officecli-SHA256SUMS | awk '{print $1}')"; \
    officecli_actual="$(sha256sum /usr/local/bin/officecli | awk '{print $1}')"; \
    test -n "${officecli_expected}"; \
    test "${officecli_expected}" = "${officecli_actual}"; \
    chmod 0755 /usr/local/bin/officecli; \
    rm -f /tmp/officecli-SHA256SUMS; \
    /usr/local/bin/officecli --version

# Node.js (LTS)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + Compose + Buildx (mount socket at runtime for access)
RUN curl -fsSL https://get.docker.com | sh

# Uncomment to apply security patches beyond what the base image provides.
# Not recommended for reproducible builds; prefer bumping the base image tag.
# RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*


WORKDIR /app

RUN pip install --no-cache-dir \
    numpy pandas scipy scikit-learn \
    matplotlib seaborn plotly \
    jupyter ipython \
    requests beautifulsoup4 lxml \
    sqlalchemy psycopg2-binary \
    pyyaml toml jsonlines \
    tqdm rich \
    openpyxl weasyprint \
    python-docx python-pptx pypdf csvkit

COPY . .
# setcap MUST run in the same layer as the Python binary to avoid
# overlay2 copy-up corruption of libpython3.12.so ("file too short").
RUN pip install --no-cache-dir . \
    && setcap cap_setgid+ep $(readlink -f $(which python3))

RUN useradd -m -s /bin/bash user && echo 'user ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

USER user
ENV SHELL=/bin/bash
ENV PATH="/home/user/.local/bin:${PATH}"
WORKDIR /home/user

EXPOSE 8000

COPY entrypoint.sh /app/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["run"]
