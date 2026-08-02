#!/bin/bash
set -e

DOMAIN="corespan.ddnsgeek.com"

# Add nginx server block on port 8443 (plain HTTP).
# SSL is terminated by pfSense HAProxy — no cert needed on this server.
sudo tee /etc/nginx/conf.d/aistudio-api.conf > /dev/null << EOF
server {
    listen 8443;
    listen [::]:8443;
    server_name $DOMAIN;

    # API proxy
    location /api/ {
        proxy_pass http://localhost:8002/api/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /health {
        proxy_pass http://localhost:8002/health;
    }

    # Jupyter instances
    include /etc/nginx/jupyter-locations/*.conf;
}
EOF

sudo nginx -t && sudo nginx -s reload

echo "Done. API + Jupyter live at https://$DOMAIN:8443"
echo "Update Vercel env: VITE_API_URL=https://$DOMAIN:8443"
