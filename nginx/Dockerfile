FROM nginx:alpine

# inotify-tools provides inotifywait for watching the jupyter-locations directory
RUN apk add --no-cache inotify-tools

# Main server block
COPY conf/aistudio.conf /etc/nginx/conf.d/aistudio.conf

# Remove default site to avoid conflicts
RUN rm -f /etc/nginx/conf.d/default.conf

# Directory for per-instance Jupyter location files (shared volume with worker)
RUN mkdir -p /etc/nginx/jupyter-locations

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8443

ENTRYPOINT ["/entrypoint.sh"]
