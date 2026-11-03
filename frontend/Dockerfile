# Multi-stage build: compile the Vite/React SPA, then serve the static
# output with nginx. VITE_API_BASE_URL is a BUILD-time value (Vite inlines
# import.meta.env.* at build time, not runtime) — it must be set as a
# Docker build-arg to point at wherever the backend is actually reachable
# from the end user's browser (not from inside this container's network),
# since API calls happen client-side. See docker-compose.yml for the
# default used in the bundled deployment-like setup.

FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ARG VITE_API_BASE_URL=http://localhost:8000/api/v1
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.27-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://localhost:80/ || exit 1
