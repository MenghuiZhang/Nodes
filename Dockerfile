FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/lichao199208/Nodes" \
      org.opencontainers.image.revision="1216835354d0c68b768b59ce137b75f40fe3e9ac" \
      org.opencontainers.image.version="1216835-yunxin4-dashboard"

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
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

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    # Camoufox 浏览器 + fpgen 指纹模型 + GeoIP 库烘进镜像（约 150MB，运行时不再下载）
    && python -m camoufox fetch \
    && python -c "import camoufox.fpgen_model as fm; fm.ensure_fpgen_model(force=True)" \
    # Playwright Firefox 驱动补丁：修复 Turnstile 跨域 iframe 导航/销毁竞态杀死
    # 驱动进程的上游 bug（microsoft/playwright#13444）。补丁点失配会让构建失败。
    && python deploy/patch_playwright_driver.py \
    && test -x "$(python -m camoufox path)" \
    && echo "BROWSER_BINARY_OK"

COPY . ./
RUN mkdir -p /app/account /app/node /app/web-data

COPY entrypoint.sh /usr/local/bin/nodes-entrypoint
RUN chmod 755 /usr/local/bin/nodes-entrypoint

ENTRYPOINT ["/usr/local/bin/nodes-entrypoint"]
CMD ["python", "proxyscrape_register.py"]
