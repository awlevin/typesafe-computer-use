# The sandbox: a Linux computer for the agent to drive, so experiments never touch the host.
# A virtual display (Xvfb) with a window manager, Chromium on it, and noVNC to watch it from
# a browser tab on the host. The project source is mounted at /work, not copied, so an edit on
# the host takes effect in the next run without a rebuild.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb openbox x11vnc novnc websockify supervisor \
      chromium fonts-dejavu-core fonts-liberation fonts-noto-color-emoji \
      xdotool x11-utils procps \
 && rm -rf /var/lib/apt/lists/*

COPY sandbox/chromium /usr/local/bin/chromium
COPY sandbox/sandbox-reset /usr/local/bin/sandbox-reset
COPY sandbox/supervisord.conf /etc/supervisor/conf.d/sandbox.conf
RUN chmod 755 /usr/local/bin/chromium /usr/local/bin/sandbox-reset \
 && useradd --create-home --uid 1000 agent \
 && install -d -o agent -g agent /opt/venv /home/agent/.cache

# The agent's own environment lives in a volume, apart from the host's .venv in the mounted
# source, which holds macOS builds.
ENV DISPLAY=:99 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12

WORKDIR /work
CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
