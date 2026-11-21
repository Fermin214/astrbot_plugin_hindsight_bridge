FROM groonga/pgroonga:4.0.8-debian-17

RUN printf '%s\n' 'deb [signed-by=/usr/local/share/keyrings/postgres.gpg.asc] https://apt-archive.postgresql.org/pub/repos/apt trixie-pgdg-archive main' \
        > /etc/apt/sources.list.d/pgdg-archive.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-pgvector=0.8.6-1.pgdg13+1 \
    && rm /etc/apt/sources.list.d/pgdg-archive.list \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0644 extensions.sql /docker-entrypoint-initdb.d/10-hindsight.sql
