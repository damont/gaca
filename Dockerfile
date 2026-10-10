# Three stages: the API, the frontend build (thrown away), and the frontend
# serve (nginx). `docker compose` builds `backend` and `frontend` as separate
# images from this one file via `target:` — see docker-compose.yml.

# --- Backend ---
FROM python:3.12-slim AS backend

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependencies first, project code second — two layers instead of one, so
# `docker compose build` skips the (slow) dependency resolve on every code
# change and only re-runs the (fast) project install. `--no-install-project`
# is uv's own pattern for this: gaca_api/ isn't copied in yet, so there is no
# project to install on the first pass, only pyproject.toml + uv.lock to sync
# dependencies from.
COPY gaca-api/pyproject.toml gaca-api/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY gaca-api/gaca_api/ gaca_api/
RUN uv sync --frozen --no-dev

EXPOSE 41700
# The console entry point (gaca_api.main:run), not a bare uvicorn invocation —
# it reads GACA_API_HOST / GACA_API_PORT from settings, which is how
# docker-compose.yml tells it to bind 0.0.0.0 instead of the loopback-only
# default that's correct for local dev.
CMD ["uv", "run", "gaca-api"]

# --- Frontend Build ---
FROM node:20-slim AS frontend-build

WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
# Public by design (see frontend/.env.example) — safe as a build arg. Vite
# bakes VITE_ vars into the bundle at build time, so this has to be set here,
# not as a runtime env var on the nginx container below.
ARG VITE_GOOGLE_CLIENT_ID=
ENV VITE_GOOGLE_CLIENT_ID=$VITE_GOOGLE_CLIENT_ID
RUN npm run build

# --- Frontend Serve ---
FROM nginx:alpine AS frontend

COPY --from=frontend-build /app/dist /usr/share/nginx/html
# A template, not a finished config: the nginx image fills in ${GACA_API_UPSTREAM} at start.
COPY frontend/nginx.conf /etc/nginx/templates/default.conf.template
ENV GACA_API_UPSTREAM=api:41700
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
