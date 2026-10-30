# syntax=docker/dockerfile:1
#
# QXC Equipment Intelligence container.
#
# Stage 1 builds db/qxc.db from the two USAspending CSVs so the runtime image
# ships a ready-to-serve database and never needs the raw files. The CSVs are
# gitignored; put them in data/raw/ before building (CI pulls them from S3).

FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY db/build_db.py db/
COPY data/raw/ data/raw/
RUN test -f data/raw/prime_transactions_checkpoint_1.csv \
 && test -f data/raw/subawards_checkpoint_1.csv \
 || (echo "ERROR: put the two checkpoint CSVs in data/raw/ before building" >&2; exit 1)
RUN python db/build_db.py \
 && python -c "import json,sqlite3,datetime; c=sqlite3.connect('db/qxc.db'); \
latest=c.execute('select max(latest_action_date) from awards').fetchone()[0]; \
n=c.execute('select count(*) from awards').fetchone()[0]; \
json.dump({'built_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'latest_action_date': latest, 'awards': n}, open('db/build_info.json','w'))"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PORT=8080 WEB_CONCURRENCY=2 GUNICORN_THREADS=4
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && groupadd --system qxc && useradd --system --gid qxc --uid 10001 --no-create-home qxc
COPY --from=builder /build/db/qxc.db /build/db/build_info.json db/
COPY app/ app/
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh && chown -R qxc:qxc /app
USER qxc
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request,os; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/health', timeout=4).status==200 else 1)"
ENTRYPOINT ["/entrypoint.sh"]
