FROM node:22-alpine AS frontend-build
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/ /app/backend/
COPY comfyui_workflows/ /app/comfyui_workflows/
COPY --from=frontend-build /web/dist /app/frontend/dist

ENV PYTHONPATH=/app/backend
ENV DATA_DIR=/data
ENV MEDIA_DIR=/media
ENV WORKFLOWS_DIR=/app/comfyui_workflows

EXPOSE 8000 8090
WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
