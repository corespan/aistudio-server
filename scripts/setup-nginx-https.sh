#!/bin/bash
set -e

DOMAIN="corespan.ddnsgeek.com"

# Install certbot
sudo apt install -y certbot python3-certbot-nginx

# Get/renew cert via nginx plugin
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m muraharirao@corespan.ai

# Write HTTPS server block with API proxy
sudo tee /etc/nginx/conf.d/aistudio-api.conf > /dev/null << EOF
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name $DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;

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

echo "Done. HTTPS is live at https://$DOMAIN"
echo "Update your Vercel env: VITE_API_BASE_URL=https://$DOMAIN"
