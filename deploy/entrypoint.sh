#!/bin/sh
# Container entrypoint: optionally seed a persistent database location, then
# serve the app with gunicorn.
#
# QXC_DB_PATH (optional): if set, the app reads/writes its SQLite file there
# instead of the copy baked into the image. On first boot (file absent) the
# baked copy is copied over so a mounted volume starts populated. Note the
# baked copy is only used for seeding: a refreshed image does NOT update an
# existing file at QXC_DB_PATH.
set -eu

if [ -n "${QXC_DB_PATH:-}" ] && [ ! -f "$QXC_DB_PATH" ]; then
  mkdir -p "$(dirname "$QXC_DB_PATH")"
  cp /app/db/qxc.db "$QXC_DB_PATH"
  echo "entrypoint: seeded $QXC_DB_PATH from image"
fi

exec gunicorn \
  --chdir /app/app app:app \
  --bind "0.0.0.0:${PORT:-8080}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout 60 \
  --access-logfile - \
  --error-logfile - \
  "$@"
