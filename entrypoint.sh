#!/bin/sh
set -e

# entrypoint: run fastmcp with --reload in dev, without in prod
if [ "${OME_APP_ENV:-dev}" = "prod" ]; then
  exec fastmcp run src/ome_mcp_server.py --transport http --host 0.0.0.0 --port 8000
else
  exec fastmcp run src/ome_mcp_server.py --reload --transport http --host 0.0.0.0 --port 8000
fi
