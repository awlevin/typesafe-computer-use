# The sandbox: a Linux computer for the agent to drive, so experiments never touch the host.
# A virtual display (Xvfb) with a window manager, Chromium on it, and noVNC to watch it from
# a browser tab on the host. The project source is mounted at /work, not copied, so an edit on
# the host takes effect in the next run without a rebuild.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb openbox x11vnc novnc websockify supervisor \
      chromium fonts-dejavu-core fonts-liberation fonts-noto-color-emoji \
      xdotool x11-utils procps imagemagick \
 && rm -rf /var/lib/apt/lists/*

# The idle desktop says what it is. A bare virtual display is solid black, which looks the same
# in noVNC as a sandbox that is not running.
RUN mkdir -p /usr/share/sandbox \
 && convert -size 1440x900 xc:'#1d2129' -gravity center -font DejaVu-Sans -fill '#e6e9ef' \
      -pointsize 34 -annotate +0-24 'typesafe-computer-use sandbox' \
      -fill '#8b93a3' -pointsize 20 -annotate +0+28 'idle. runs appear here: scripts/sandbox run clicker-bench loop --headed' \
      /usr/share/sandbox/idle.png

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
