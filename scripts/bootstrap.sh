#!/usr/bin/env bash
# bootstrap.sh — run once on a fresh Ubuntu 22.04 / 24.04 Droplet as root
# Usage:  bash bootstrap.sh

set -euo pipefail

echo "==> Updating packages..."
apt-get update -y && apt-get upgrade -y

echo "==> Installing Docker..."
apt-get install -y ca-certificates curl gnupg lsb-release git

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

systemctl enable --now docker

echo "==> Docker version: $(docker --version)"
echo "==> Docker Compose version: $(docker compose version)"

echo ""
echo "==> Done. Next steps:"
echo "    1. git clone <your-repo-url> /opt/untenderscout"
echo "    2. cd /opt/untenderscout"
echo "    3. cp .env.example .env && nano .env   # add ANTHROPIC_API_KEY"
echo "    4. docker compose up -d --build"
echo "    5. curl http://localhost/api/health"
