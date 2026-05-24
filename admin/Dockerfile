FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn pydantic
COPY admin/main.py .
COPY admin/admin.html .
# Copy admin.html to the shared data volume on startup
CMD ["sh", "-c", "cp /app/admin.html /data/admin.html && uvicorn main:app --host 0.0.0.0 --port 8080"]
