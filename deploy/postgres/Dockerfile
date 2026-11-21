FROM groonga/pgroonga:4.0.8-debian-17

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-pgvector \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' postgresql-17-pgvector)" ge 0.8.7 \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0644 extensions.sql /docker-entrypoint-initdb.d/10-hindsight.sql
