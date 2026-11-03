FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS development
WORKDIR /web
COPY apps/management/web/package*.json ./
RUN npm ci
COPY apps/management/web .
CMD ["npm", "run", "dev", "--", "--port", "5173"]

FROM development AS build
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28-alpine@sha256:7377697a821c131a924a7105fafbe7414db4e9fcc77a6f08f776f33f141ec3f8 AS production
COPY --from=build /web/dist /usr/share/nginx/html
COPY apps/management/web/nginx.conf.template /etc/nginx/templates/default.conf.template
ENV API_UPSTREAM=api:8000
EXPOSE 8080
