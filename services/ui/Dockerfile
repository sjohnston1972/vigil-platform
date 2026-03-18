FROM node:20-slim AS builder

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html

# nginx config to support React Router (serve index.html for all routes)
RUN printf 'server {\n  listen 80;\n  root /usr/share/nginx/html;\n  location /health {\n    return 200 "healthy";\n    add_header Content-Type text/plain;\n  }\n  location / {\n    try_files $uri $uri/ /index.html;\n  }\n}\n' > /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s CMD wget -qO- http://localhost/health || exit 1
