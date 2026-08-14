#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip nginx

sudo install -d -o ubuntu -g ubuntu /opt/tmall-dashboard
sudo install -d -o ubuntu -g ubuntu /opt/tmall-dashboard/data
sudo install -d -o ubuntu -g ubuntu /opt/tmall-dashboard/.cache
sudo install -d -o ubuntu -g ubuntu /opt/tmall-dashboard/releases

cd /opt/tmall-dashboard
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip wheel -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv/bin/python -m pip install -r current/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

sudo install -m 644 current/deploy/tmall-dashboard.service /etc/systemd/system/tmall-dashboard.service
sudo install -m 644 current/deploy/nginx-tmall-dashboard.conf /etc/nginx/sites-available/tmall-dashboard
sudo ln -sfn /etc/nginx/sites-available/tmall-dashboard /etc/nginx/sites-enabled/tmall-dashboard
sudo rm -f /etc/nginx/sites-enabled/default

sudo systemctl daemon-reload
sudo systemctl enable --now tmall-dashboard
sudo nginx -t
sudo systemctl enable --now nginx
