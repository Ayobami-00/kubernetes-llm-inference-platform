FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS development
WORKDIR /web
COPY apps/management/web/package*.json ./
RUN npm ci
COPY apps/management/web .
CMD ["npm", "run", "dev", "--", "--port", "5173"]

FROM development AS build
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.30.6-alpine-slim@sha256:5d8bb9437d74a600ec5dbf90c035451c724dc33eac8d3a71078f6f8612c345f6 AS production
COPY --from=build /web/dist /usr/share/nginx/html
COPY apps/management/web/nginx.conf.template /etc/nginx/templates/default.conf.template
ENV API_UPSTREAM=api:8000
EXPOSE 8080
