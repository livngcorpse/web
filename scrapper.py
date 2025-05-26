import asyncio
import os
import sqlite3
import logging
from datetime import datetime
import re
import imagehash
from PIL import Image
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto
import requests
from pathlib import Path

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class TelegramScraper:
    def __init__(self, api_id: int, api_hash: str, phone: str, channel_username: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.channel_username = channel_username
        self.client = TelegramClient('session', api_id, api_hash)
        
        # Ensure directories exist
        os.makedirs("images", exist_ok=True)
        os.makedirs("sessions", exist_ok=True)
        
    async def init_client(self):
        """Initialize and authenticate Telegram client"""
        await self.client.start(phone=self.phone)
        logger.info("Telegram client initialized successfully")
        
    def init_database(self):
        """Initialize database tables"""
        conn = sqlite3.connect('waifu_gallery.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                name TEXT NOT NULL,
                anime TEXT NOT NULL,
                phash TEXT NOT NULL,
                message_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(filename)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scraper_state (
                id INTEGER PRIMARY KEY,
                last_message_id INTEGER DEFAULT 0,
                channel_name TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create indexes for better performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_message_id ON characters(message_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_name ON characters(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_anime ON characters(anime)')
        
        conn.commit()
        conn.close()
        
    def get_last_message_id(self) -> int:
        """Get the last processed message ID"""
        conn = sqlite3.connect('waifu_gallery.db')
        cursor = conn.cursor()
        cursor.execute('SELECT last_message_id FROM scraper_state WHERE channel_name = ?', (self.channel_username,))
        result = cursor.fetchone()
        conn.close()
        
        return result[0] if result else 0
        
    def update_last_message_id(self, message_id: int):
        """Update the last processed message ID"""
        conn = sqlite3.connect('waifu_gallery.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO scraper_state (id, last_message_id, channel_name, updated_at)
            VALUES ((SELECT id FROM scraper_state WHERE channel_name = ?), ?, ?, ?)
        ''', (self.channel_username, message_id, self.channel_username, datetime.now()))
        conn.commit()
        conn.close()
        
    def parse_caption(self, caption: str) -> tuple:
        """Parse caption to extract name and anime"""
        if not caption:
            return "Unknown", "Unknown"
        
        # Clean up caption - remove excessive whitespace and newlines
        caption = re.sub(r'\s+', ' ', caption.strip())
        
        # Try different delimiters in order of preference
        delimiters = [' - ', ' | ', ': ', ' : ', ' from ', ' (', '(']
        
        for delimiter in delimiters:
            if delimiter in caption:
                if delimiter.startswith(' (') or delimiter == '(':
                    # Handle parentheses specially
                    match = re.search(r'(.+?)\s*\((.+?)\)', caption)
                    if match:
                        name_part = match.group(1).strip()
                        anime_part = match.group(2).strip()
                    else:
                        continue
                else:
                    parts = caption.split(delimiter, 1)
                    if len(parts) != 2:
                        continue
                    name_part = parts[0].strip()
                    anime_part = parts[1].strip()
                
                # Clean up extracted parts
                name = self.clean_text(name_part)
                anime = self.clean_text(anime_part)
                
                # Remove common prefixes/suffixes
                anime = re.sub(r'\).*$', '', anime)  # Remove anything after closing parenthesis
                
                if name and anime and len(name) > 1 and len(anime) > 1:
                    return name, anime
        
        # Fallback: try to extract from hashtags
        hashtags = re.findall(r'#(\w+)', caption)
        if len(hashtags) >= 2:
            return hashtags[0], hashtags[1]
        
        # Last resort: use the whole caption as name
        clean_caption = self.clean_text(caption)
        return clean_caption[:50] if clean_caption else "Unknown", "Unknown"
    
    def clean_text(self, text: str) -> str:
        """Clean text by removing emojis and special characters"""
        if not text:
            return ""
        
        # Remove emojis and special characters, keep alphanumeric, spaces, and common punctuation
        text = re.sub(r'[^\w\s\-\.\!\?\,\:\;]', '', text)
        # Remove excessive whitespace
        text = re.sub(r'\s+', ' ', text.strip())
        return text
    
    def compute_phash(self, image_path: str) -> str:
        """Compute perceptual hash of an image"""
        try:
            with Image.open(image_path) as img:
                # Convert to RGB if necessary
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                return str(imagehash.phash(img))
        except Exception as e:
            logger.error(f"Error computing phash for {image_path}: {e}")
            return ""
    
    def is_duplicate_image(self, phash: str, threshold: int = 3) -> bool:
        """Check if image with similar hash already exists"""
        if not phash:
            return False
            
        conn = sqlite3.connect('waifu_gallery.db')
        cursor = conn.cursor()
        cursor.execute('SELECT phash FROM characters WHERE phash != ""')
        existing_hashes = cursor.fetchall()
        conn.close()
        
        for (existing_hash,) in existing_hashes:
            if existing_hash and self.hamming_distance(phash, existing_hash) <= threshold:
                return True
        return False
    
    def hamming_distance(self, hash1: str, hash2: str) -> int:
        """Calculate Hamming distance between two hashes"""
        if len(hash1) != len(hash2):
            return float('inf')
        return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))
    
    async def download_and_process_image(self, message, filename: str) -> dict:
        """Download image and extract metadata"""
        try:
            image_path = f"images/{filename}"
            
            # Download image
            await self.client.download_media(message.media, image_path)
            logger.info(f"Downloaded image: {filename}")
            
            # Compute perceptual hash
            phash = self.compute_phash(image_path)
            
            # Check for duplicates
            if self.is_duplicate_image(phash):
                logger.info(f"Duplicate image detected, skipping: {filename}")
                os.remove(image_path)  # Clean up
                return None
            
            # Parse caption
            caption = message.message or ""
            name, anime = self.parse_caption(caption)
            
            return {
                'filename': filename,
                'name': name,
                'anime': anime,
                'phash': phash,
                'message_id': message.id,
                'caption': caption
            }
            
        except Exception as e:
            logger.error(f"Error processing image {filename}: {e}")
            # Clean up failed download
            image_path = f"images/{filename}"
            if os.path.exists(image_path):
                os.remove(image_path)
            return None
    
    def save_to_database(self, image_data: dict):
        """Save image metadata to database"""
        conn = sqlite3.connect('waifu_gallery.db')
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO characters (filename, name, anime, phash, message_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                image_data['filename'],
                image_data['name'],
                image_data['anime'],
                image_data['phash'],
                image_data['message_id']
            ))
            conn.commit()
            logger.info(f"Saved to database: {image_data['name']} from {image_data['anime']}")
            
        except sqlite3.IntegrityError as e:
            logger.warning(f"Database integrity error (likely duplicate): {e}")
        except Exception as e:
            logger.error(f"Database error: {e}")
        finally:
            conn.close()
    
    async def scrape_channel(self, limit: int = 100, reverse: bool = True):
        """Scrape messages from the Telegram channel"""
        try:
            # Get channel entity
            channel = await self.client.get_entity(self.channel_username)
            logger.info(f"Connected to channel: {channel.title}")
            
            last_message_id = self.get_last_message_id()
            logger.info(f"Starting from message ID: {last_message_id}")
            
            processed_count = 0
            new_images_count = 0
            
            # Iterate through messages
            async for message in self.client.iter_messages(
                channel, 
                limit=limit, 
                reverse=reverse,
                min_id=last_message_id
            ):
                
                # Skip if no media or not a photo
                if not message.media or not isinstance(message.media, MessageMediaPhoto):
                    continue
                
                processed_count += 1
                
                # Generate filename
                timestamp = message.date.strftime("%Y%m%d_%H%M%S")
                filename = f"{timestamp}_{message.id}.jpg"
                
                # Check if already processed
                conn = sqlite3.connect('waifu_gallery.db')
                cursor = conn.cursor()
                cursor.execute('SELECT 1 FROM characters WHERE message_id = ?', (message.id,))
                exists = cursor.fetchone()
                conn.close()
                
                if exists:
                    logger.info(f"Message {message.id} already processed, skipping")
                    continue
                
                # Download and process image
                image_data = await self.download_and_process_image(message, filename)
                
                if image_data:
                    self.save_to_database(image_data)
                    new_images_count += 1
                
                # Update last processed message ID
                self.update_last_message_id(message.id)
                
                # Log progress every 10 messages
                if processed_count % 10 == 0:
                    logger.info(f"Processed {processed_count} messages, found {new_images_count} new images")
            
            logger.info(f"Scraping completed. Processed {processed_count} messages, added {new_images_count} new images")
            
        except Exception as e:
            logger.error(f"Error during scraping: {e}")
            raise
    
    async def run_scraper(self, limit: int = 100):
        """Main scraper function"""
        try:
            logger.info("Starting Telegram scraper...")
            self.init_database()
            await self.init_client()
            await self.scrape_channel(limit=limit)
            logger.info("Scraping completed successfully")
            
        except Exception as e:
            logger.error(f"Scraper failed: {e}")
            raise
        finally:
            await self.client.disconnect()

async def main():
    # Configuration from environment variables
    API_ID = int(os.getenv('TELEGRAM_API_ID', '0'))
    API_HASH = os.getenv('TELEGRAM_API_HASH', '')
    PHONE = os.getenv('TELEGRAM_PHONE', '')
    CHANNEL = os.getenv('TELEGRAM_CHANNEL', '')
    LIMIT = int(os.getenv('SCRAPER_LIMIT', '100'))
    
    if not all([API_ID, API_HASH, PHONE, CHANNEL]):
        logger.error("Missing required environment variables")
        logger.error("Required: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, TELEGRAM_CHANNEL")
        return
    
    scraper = TelegramScraper(API_ID, API_HASH, PHONE, CHANNEL)
    await scraper.run_scraper(limit=LIMIT)

if __name__ == "__main__":
    asyncio.run(main())