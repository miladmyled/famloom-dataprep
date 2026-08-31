# Use a lightweight official Python image
FROM python:3.11-slim

# Set environment variables for unbuffered output and UTF-8 encoding
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and entrypoints into the container
COPY src/ ./src/
COPY main.py .
COPY consumer_main.py .
COPY janitor.py .

# Trigger the ETL orchestrator by default for Kubernetes CronJob execution
CMD ["python", "main.py"]