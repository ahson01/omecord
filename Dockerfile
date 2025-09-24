# Use official Python image
FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Install system dependencies (if any needed for discord.py voice)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py ./

# Copy .env if present (optional, for local dev)
# COPY .env ./

# Expose metrics port (Prometheus)
EXPOSE 8000

# Run the bot
CMD ["python", "bot.py"] 