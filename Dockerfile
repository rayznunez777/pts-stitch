# PTS Digitizer stitch service — Ink/Stitch behind a small HTTP API.
#
# Two stages. The first has the compilers needed to build the few dependencies
# with no prebuilt wheel; the second keeps only what runs.
#
# wxPython is not installed. Ink/Stitch imports its GUI modules whenever any
# extension loads, even headless ones, so wx has to exist — but nothing on this
# path calls it. A stub satisfies the import and saves several hundred
# megabytes of wxPython and GTK, which is what was timing out on the push to
# Cloudflare's registry. With no wx there is also no display to arrange, so
# Xvfb goes too.

# ---------------------------------------------------------------- builder
FROM ubuntu:24.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev \
        build-essential pkg-config git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# lxml is pinned below 6 because inkex will not accept it otherwise
RUN pip install --no-cache-dir --target /opt/pydeps \
        "numpy==2.2.6" "shapely>=2.0.0" "lxml>=4.5.0,<6.0.0" \
        networkx platformdirs jinja2 tomli colormath2 fonttools \
        "trimesh>=3.15.2" diskcache pystitch flask gunicorn

# inkex without its dependency chain: PyGObject is only touched by drawing
# features this service never reaches, and the rest are listed by hand
RUN pip install --no-cache-dir --target /opt/pydeps \
        Pillow "cssselect>=1.2.0,<2.0.0" "packaging>=20.3" "pySerial>=3.4,<4.0" \
        "pyparsing>=3.0.9" "scour>=0.37,<0.38" "tinycss2>=1.0.1,<2.0.0"
RUN pip install --no-cache-dir --target /opt/pydeps --no-deps \
        "inkex @ git+https://gitlab.com/inkscape/extensions.git@EXTENSIONS_AT_INKSCAPE_1.4.1"

RUN git clone --depth 1 https://github.com/inkstitch/inkstitch.git /opt/inkstitch \
    && rm -rf /opt/inkstitch/.git /opt/inkstitch/fonts /opt/inkstitch/tests \
              /opt/inkstitch/inx /opt/inkstitch/images /opt/inkstitch/symbols

# ---------------------------------------------------------------- runtime
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    INKSTITCH_DIR=/opt/inkstitch \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/stubs:/opt/pydeps \
    USE_XVFB=0 \
    START_TIMEOUT=180 \
    JOB_TIMEOUT=600

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY stubs /opt/stubs
COPY --from=builder /opt/pydeps /opt/pydeps
COPY --from=builder /opt/inkstitch /opt/inkstitch

# fail the build here rather than at runtime if anything is missing
RUN python3 -c "import wx, inkex, shapely, networkx, pystitch, flask; print('imports ok')"

WORKDIR /srv
COPY server.py worker.py /srv/

EXPOSE 8080
CMD ["python3", "-m", "gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "-t", "900", "server:app"]
