FROM node:22-alpine AS build

WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts && npm cache clean --force
COPY index.html tsconfig.json tsconfig.node.json vite.config.ts ./
COPY src ./src
COPY public ./public

ARG VITE_OIDC_AUTHORIZATION_ENDPOINT
ARG VITE_OIDC_TOKEN_ENDPOINT
ARG VITE_OIDC_CLIENT_ID
ARG VITE_OIDC_SCOPE="openid profile email offline_access"
ARG VITE_OIDC_REDIRECT_URI
ARG VITE_ALLOW_DEVELOPMENT_AUTH=false
ARG VITE_ENABLE_LEGACY_REPLICA=false
ARG VITE_ALLOW_API_OVERRIDE=false
ENV VITE_OIDC_AUTHORIZATION_ENDPOINT=$VITE_OIDC_AUTHORIZATION_ENDPOINT \
    VITE_OIDC_TOKEN_ENDPOINT=$VITE_OIDC_TOKEN_ENDPOINT \
    VITE_OIDC_CLIENT_ID=$VITE_OIDC_CLIENT_ID \
    VITE_OIDC_SCOPE=$VITE_OIDC_SCOPE \
    VITE_OIDC_REDIRECT_URI=$VITE_OIDC_REDIRECT_URI \
    VITE_ALLOW_DEVELOPMENT_AUTH=$VITE_ALLOW_DEVELOPMENT_AUTH \
    VITE_ENABLE_LEGACY_REPLICA=$VITE_ENABLE_LEGACY_REPLICA \
    VITE_ALLOW_API_OVERRIDE=$VITE_ALLOW_API_OVERRIDE
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29-alpine

COPY --from=build /src/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

ENV NGINX_PORT=8080 \
    CONTROL_PLANE_UPSTREAM=http://cloud-parity:8091 \
    OIDC_CONNECT_SRC=https://idp.example.com

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
