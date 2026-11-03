FROM node:24-bookworm-slim
WORKDIR /web
COPY apps/management/web/package*.json ./
RUN npm ci
COPY apps/management/web .
CMD ["npm", "run", "dev", "--", "--port", "5173"]
