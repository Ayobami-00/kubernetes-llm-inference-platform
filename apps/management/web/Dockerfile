FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS development
WORKDIR /web
COPY apps/management/web/package*.json ./
RUN npm ci
COPY apps/management/web .
CMD ["npm", "run", "dev", "--", "--port", "5173"]

FROM development AS build
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.30.6-alpine@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03 AS production
COPY --from=build /web/dist /usr/share/nginx/html
COPY apps/management/web/nginx.conf.template /etc/nginx/templates/default.conf.template
ENV API_UPSTREAM=api:8000
EXPOSE 8080
