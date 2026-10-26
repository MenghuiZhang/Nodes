# syntax=docker/dockerfile:1

# =============================================================================
# 阶段 1：依赖 + Camoufox 浏览器下载（体积都在这里，但只有必要部分进最终镜像）
# =============================================================================
FROM python:3.12-slim-bookworm AS builder

ARG CAMOUFOX_INSTALL=online
ARG CAMOUFOX_VERSION=""
ARG CAMOUFOX_ZIP_URL=""
ARG CAMOUFOX_MODEL_URL=""
# 直连 URL 且文件名不是 camoufox-<version>-<build>-... 时，用它显式声明版本，如 156.0.1-beta.36
ARG CAMOUFOX_BROWSER_SPEC=""
ENV CAMOUFOX_BROWSER_SPEC=${CAMOUFOX_BROWSER_SPEC}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
COPY deploy/patch_playwright_driver.py deploy/patch_playwright_driver.py
RUN python -m venv "$VIRTUAL_ENV" \
    && python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    # Playwright Firefox 驱动补丁：修复 Turnstile 跨域 iframe 导航/销毁竞态杀死
    # 驱动进程的上游 bug（microsoft/playwright#13444）。补丁点失配会让构建失败。
    && python deploy/patch_playwright_driver.py

# Camoufox 安装方式（build-arg），优先级：指定 URL > online > fetch 默认
#   CAMOUFOX_ZIP_URL    — 直接下载浏览器 zip（官方 release / 自建镜像 / ghproxy 等反代），
#                         路径最可控、不查版本、不同步仓库
#   CAMOUFOX_MODEL_URL  — 可选，配套的 fpgen 模型 zip；不填则跳过模型（指纹功能受限）
#   CAMOUFOX_INSTALL    — online（camoufox fetch / set）
#   CAMOUFOX_VERSION    — online 模式下的仓库/版本，如 Official/stable/156.0.1-beta.36
RUN set -eux; \
    if [ -n "$CAMOUFOX_ZIP_URL" ]; then \
        zip_name="$(basename "${CAMOUFOX_ZIP_URL%%\?*}")"; \
        case "$zip_name" in \
            *.zip) : ;; \
            *) zip_name="camoufox-${CAMOUFOX_VERSION:-browser}.zip" ;; \
        esac; \
        curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 -o "/tmp/$zip_name" "$CAMOUFOX_ZIP_URL"; \
        if [ -n "$CAMOUFOX_MODEL_URL" ]; then \
            curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 -o /tmp/model-release.zip "$CAMOUFOX_MODEL_URL"; \
        fi; \
        python deploy/install_camoufox_zip.py "/tmp/$zip_name" ${CAMOUFOX_MODEL_URL:+/tmp/model-release.zip}; \
    elif [ "$CAMOUFOX_INSTALL" = "online" ]; then \
        if [ -n "$CAMOUFOX_VERSION" ]; then \
            python -m camoufox set "$CAMOUFOX_VERSION"; \
        else \
            python -m camoufox fetch; \
        fi; \
        python -c "import camoufox.fpgen_model as fm; fm.ensure_fpgen_model(force=True)"; \
    else \
        python -m camoufox fetch; \
        python -c "import camoufox.fpgen_model as fm; fm.ensure_fpgen_model(force=True)"; \
    fi; \
    rm -rf /tmp/camoufox*.zip /tmp/model-release.zip /tmp/camoufox_extract
RUN test -x "$(python -m camoufox path)" && echo "BROWSER_BINARY_OK"

# =============================================================================
# 阶段 2：运行镜像（只带 venv + 浏览器缓存 + 运行代码）
# =============================================================================
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/lichao199208/Nodes" \
      org.opencontainers.image.description="Nodes: ProxyScrape registration & proxy pool dashboard"

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    DISPLAY=:99

# Camoufox（Firefox 内核）运行所需的系统库（不含编译工具链）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        libasound2 \
        libdbus-glib-1-2 \
        libegl1 \
        libgbm1 \
        libgl1-mesa-dri \
        libgtk-3-0 \
        libnss3 \
        libx11-xcb1 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxfixes3 \
        libxi6 \
        libxkbcommon0 \
        libxrandr2 \
        libxrender1 \
        libxss1 \
        libxt6 \
        libxtst6 \
        lsof \
        xauth \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复用 builder 的 venv 与浏览器缓存，最终镜像不再下载/解压
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /root/.cache/camoufox /root/.cache/camoufox
ENV PATH=/app/.venv/bin:$PATH
RUN test -x "$(python -m camoufox path)" && echo "BROWSER_BINARY_OK"

COPY . ./
RUN mkdir -p /app/account /app/node /app/web-data

COPY entrypoint.sh /usr/local/bin/nodes-entrypoint
RUN chmod 755 /usr/local/bin/nodes-entrypoint

ENTRYPOINT ["/usr/local/bin/nodes-entrypoint"]
CMD ["python", "proxyscrape_register.py"]
