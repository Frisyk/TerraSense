FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production PORT=3001 HOSTNAME="0.0.0.0"
COPY --from=build /app ./
EXPOSE 3001
CMD ["sh", "-c", "[ -f .next/BUILD_ID ] || npm run build; exec npm run start"]

