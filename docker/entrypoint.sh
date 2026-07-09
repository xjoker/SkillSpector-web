#!/usr/bin/env sh
set -e

case "${1:-}" in
    web)
        shift
        exec skillspector-web --host 0.0.0.0 "$@"
        ;;
    upload-mcp)
        shift
        exec skillspector-upload-mcp --transport http --host 0.0.0.0 "$@"
        ;;
    skillspector|skillspector-web|skillspector-upload-mcp)
        exec "$@"
        ;;
    *)
        exec skillspector "$@"
        ;;
esac
