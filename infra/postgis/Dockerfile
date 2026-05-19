FROM postgis/postgis:16-3.4

# Build h3-pg from source (zachasme/h3-pg v4.x) — native h3index type in SQL
ARG H3_PG_VERSION=v4.2.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
        cmake \
        git \
        postgresql-server-dev-16 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
RUN git clone --branch ${H3_PG_VERSION} --recurse-submodules --depth 1 \
        https://github.com/zachasme/h3-pg.git \
    && cd h3-pg \
    && cmake -B build \
    && cmake --build build \
    && cmake --install build \
    && cd / && rm -rf /tmp/h3-pg

# Auto-apply migrations on first init
COPY migrations/ /docker-entrypoint-initdb.d/
