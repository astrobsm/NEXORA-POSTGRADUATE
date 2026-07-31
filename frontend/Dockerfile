# syntax=docker/dockerfile:1.7
# --------------------------------------------------------------------------
# Residency Training Console — web client
# --------------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Lockfile-first so a source change does not reinstall the dependency tree.
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY . .

# The API base is baked in at build time. Deployments that front both services
# with the same Nginx leave this at the default relative path.
ARG VITE_API_BASE=/api/v1
ARG VITE_SHOW_DEMO_ACCOUNTS=false
ENV VITE_API_BASE=$VITE_API_BASE \
    VITE_SHOW_DEMO_ACCOUNTS=$VITE_SHOW_DEMO_ACCOUNTS

RUN npm run build

# --------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

RUN apk add --no-cache curl \
    && rm -rf /usr/share/nginx/html/*

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
