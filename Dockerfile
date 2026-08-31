# Use a lightweight official Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your source code into the container
COPY src/ ./src/

# By default, run the extractor or transformer (we will update this for Kafka later)
CMD ["python", "-m", "src.etl.extractor"]