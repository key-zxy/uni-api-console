#!/bin/sh
# Fixed container bootstrap only; never called by the web application.
set -eu
trap 'exit 0' TERM INT
while [ ! -s "${UNI_API_CONFIG_PATH:-/config/api.yaml}" ]; do
    sleep 1 &
    wait $! || :
done
trap - TERM INT
exec "$@"
