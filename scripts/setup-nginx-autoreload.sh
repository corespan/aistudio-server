#!/bin/bash
set -e

sudo apt install -y inotify-tools

sudo tee /etc/systemd/system/nginx-jupyter-reload.service << 'EOF'
[Unit]
Description=Auto-reload nginx when Jupyter location configs change
After=nginx.service

[Service]
ExecStart=/bin/bash -c 'while inotifywait -e create,delete /etc/nginx/jupyter-locations/; do nginx -s reload; done'
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now nginx-jupyter-reload

echo "Done. nginx auto-reload service is running."
sudo systemctl status nginx-jupyter-reload --no-pager
