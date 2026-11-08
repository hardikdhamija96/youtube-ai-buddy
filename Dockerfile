FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
WORKDIR /app/backend
ENV DB_PATH=/data/app.db FRONTEND_DIR=/app/frontend
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
