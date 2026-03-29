#!/usr/bin/env bash
# deploy.sh — pull latest code and restart the stack
# Run on the server from /opt/untenderscout
set -euo pipefail

echo "==> Pulling latest code..."
git pull origin main

echo "==> Rebuilding and restarting containers..."
docker compose up -d --build --remove-orphans

echo "==> Pruning old images..."
docker image prune -f

echo "==> Done. Health check:"
sleep 2
curl -sf http://localhost/api/health && echo "" || echo "WARNING: health check failed"
