#!/bin/sh
set -e

mkdir -p /etc/nginx/jupyter-locations

# Validate config before starting
nginx -t

# Start nginx (foreground)
nginx -g "daemon off;" &
NGINX_PID=$!

# Watch jupyter-locations and reload nginx on any file create/delete
inotifywait -m -e create,delete,moved_to,moved_from \
    /etc/nginx/jupyter-locations/ 2>/dev/null |
while read -r directory event filename; do
    echo "[nginx-reload] $event: $filename"
    nginx -s reload
done &

wait $NGINX_PID
