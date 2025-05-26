# run_scraper.sh
#!/bin/bash

# Script to run the Telegram scraper
# Usage: ./run_scraper.sh

set -e

# Load environment variables
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# Check required environment variables
if [ -z "$TELEGRAM_API_ID" ] || [ -z "$TELEGRAM_API_HASH" ] || [ -z "$TELEGRAM_PHONE" ] || [ -z "$TELEGRAM_CHANNEL" ]; then
    echo "Error: Missing required environment variables"
    echo "Please set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, and TELEGRAM_CHANNEL"
    exit 1
fi

# Run scraper with Docker Compose
echo "Starting Telegram scraper..."
docker-compose run --rm scraper

echo "Scraper completed successfully!"