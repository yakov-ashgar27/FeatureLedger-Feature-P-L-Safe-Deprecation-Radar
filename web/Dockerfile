FROM node:22-alpine AS build
WORKDIR /app
COPY web/package.json ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app ./
USER node
EXPOSE 3000
# rewrites in next.config.mjs read API_URL at start time
CMD ["npm", "run", "start"]
