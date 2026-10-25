FROM groonga/pgroonga:4.0.8-debian-17

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-pgvector=0.8.6-1.pgdg13+1 \
    && rm -rf /var/lib/apt/lists/*

COPY extensions.sql /docker-entrypoint-initdb.d/10-hindsight.sql
