# setup.sh
#!/bin/bash

# Setup script for the Waifu Gallery application
# Run this script to set up the application for the first time

set -e

echo "🎌 Waifu Gallery Setup Script 🎌"
echo "================================="

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker first."
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi

# Create necessary directories
echo "📁 Creating directories..."
mkdir -p images uploads static sessions ssl

# Copy environment file if it doesn't exist
if [ ! -f .env ]; then
    echo "📝 Creating environment file..."
    cp .env.example .env
    echo "⚠️  Please edit .env file with your Telegram API credentials!"
fi

# Set permissions
chmod +x run_scraper.sh
chmod 644 .env

# Build Docker images
echo "🐳 Building Docker images..."
docker-compose build

echo "✅ Setup completed!"
echo ""
echo "Next steps:"
echo "1. Edit .env file with your Telegram API credentials"
echo "2. Get Telegram API credentials from https://my.telegram.org"
echo "3. Run: docker-compose up -d web"
echo "4. Access the application at http://localhost:8000"
echo "5. Run scraper: ./run_scraper.sh"
echo ""
echo "For production deployment with NGINX:"
echo "docker-compose --profile production up -d"