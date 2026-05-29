# =============================================================================
# Frontend Dockerfile — Next.js
# =============================================================================
# Multi-stage build for smaller final image.

FROM node:20-alpine AS base
WORKDIR /app

# Install dependencies (cache-friendly layer)
COPY package.json package-lock.json* ./
RUN npm ci

# Copy application code
COPY . .

# Expose port
EXPOSE 3000

# Development mode with hot-reload
CMD ["npm", "run", "dev"]
