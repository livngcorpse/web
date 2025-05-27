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
import secrets
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
        
        # Use sessions directory for session files
        session_path = os.path.join("sessions", "scraper_session")
        self.client = TelegramClient(session_path, api_id, api_hash)
        
        # Ensure directories exist
        os.makedirs("images", exist_ok=True)
        os.makedirs("sessions", exist_ok=True)
        
    async def init_client(self):
        """Initialize and authenticate Telegram client"""
        try:
            await self.client.start(phone=self.phone)
            
            # Test if we're authenticated
            me = await self.client.get_me()
            logger.info(f"Authenticated as: {me.first_name} {me.last_name or ''} (@{me.username or 'no username'})")
            
        except Exception as e:
            logger.error(f"Failed to authenticate Telegram client: {e}")
            raise
        
    def init_database(self):
        """Initialize database tables - ensure compatibility with main.py"""
        conn = sqlite3.connect('waifu_gallery.db')
        cursor = conn.cursor()
        
        # Use the same schema as main.py
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
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_phash ON characters(phash)')
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
        
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
            VALUES (
                (SELECT id FROM scraper_state WHERE channel_name = ?), 
                ?, ?, ?
            )
        ''', (self.channel_username, message_id, self.channel_username, datetime.now()))
        conn.commit()
        conn.close()
        
    def parse_caption(self, caption: str) -> tuple:
        """Parse caption to extract name and anime with improved logic"""
        if not caption:
            return "Unknown", "Unknown"
        
        # Clean up caption - remove excessive whitespace and newlines
        caption = re.sub(r'\s+', ' ', caption.strip())
        
        # Remove common prefixes like "Waifu:", "Husbando:", etc.
        caption = re.sub(r'^(waifu|husbando|character):\s*', '', caption, flags=re.IGNORECASE)
        
        # Try different delimiters in order of preference
        delimiters = [
            ' - ',     # Most common: "Nico Robin - One Piece"
            ' | ',     # Alternative: "Nico Robin | One Piece"
            ' from ',  # English: "Nico Robin from One Piece"
            ' (',      # Parentheses: "Nico Robin (One Piece)"
            ': ',      # Colon: "Character: Nico Robin Anime: One Piece"
            ' : ',     # Spaced colon
        ]
        
        for delimiter in delimiters:
            if delimiter in caption:
                if delimiter == ' (':
                    # Handle parentheses specially
                    match = re.search(r'^(.+?)\s*\(([^)]+)\)', caption)
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
                
                # Remove unwanted suffixes from anime
                anime = re.sub(r'\).*$', '', anime)  # Remove anything after closing parenthesis
                anime = re.sub(r'#.*$', '', anime)    # Remove hashtags
                
                # Validate extracted data
                if name and anime and len(name) > 1 and len(anime) > 1:
                    # Ensure name isn't too long (probably not a character name)
                    if len(name) <= 50 and len(anime) <= 50:
                        return name.title(), anime.title()
        
        # Try to extract from hashtags as fallback
        hashtags = re.findall(r'#(\w+)', caption)
        if len(hashtags) >= 2:
            # First hashtag might be character, second might be anime
            name = self.clean_text(hashtags[0])
            anime = self.clean_text(hashtags[1])
            if name and anime:
                return name.title(), anime.title()
        
        # Try to extract character and anime keywords
        char_match = re.search(r'(?:character|char|name):\s*([^,\n]+)', caption, re.IGNORECASE)
        anime_match = re.search(r'(?:anime|series|from):\s*([^,\n]+)', caption, re.IGNORECASE)
        
        if char_match and anime_match:
            name = self.clean_text(char_match.group(1))
            anime = self.clean_text(anime_match.group(1))
            if name and anime:
                return name.title(), anime.title()
        
        # Last resort: use first meaningful part as character name
        clean_caption = self.clean_text(caption)
        if clean_caption and len(clean_caption) <= 50:
            return clean_caption.title(), "Unknown"
        
        return "Unknown", "Unknown"
    
    def clean_text(self, text: str) -> str:
        """Clean text by removing emojis and special characters"""
        if not text:
            return ""
        
        # Remove URLs
        text = re.sub(r'http[s]?://\S+', '', text)
        
        # Remove emojis and special characters, keep alphanumeric, spaces, and basic punctuation
        text = re.sub(r'[^\w\s\-\.\!\?\,\:\;\'\"]', ' ', text)
        
        # Remove excessive whitespace
        text = re.sub(r'\s+', ' ', text.strip())
        
        # Remove common noise words
        noise_words = ['waifu', 'husbando', 'character', 'anime', 'from', 'of', 'the']
        words = text.split()
        cleaned_words = [word for word in words if word.lower() not in noise_words or len(words) <= 2]
        
        return ' '.join(cleaned_words).strip()
    
    def compute_phash(self, image_path: str) -> str:
        """Compute perceptual hash of an image"""
        try:
            with Image.open(image_path) as img:
                # Convert to RGB if necessary
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # Resize image for consistent hashing (optional but recommended)
                img = img.resize((256, 256), Image.Resampling.LANCZOS)
                
                return str(imagehash.phash(img, hash_size=8))
        except Exception as e:
            logger.error(f"Error computing phash for {image_path}: {e}")
            return ""
    
    def is_duplicate_image(self, phash: str, threshold: int = 5) -> bool:
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
                logger.info(f"Found duplicate image (distance: {self.hamming_distance(phash, existing_hash)})")
                return True
        return False
    
    def hamming_distance(self, hash1: str, hash2: str) -> int:
        """Calculate Hamming distance between two hashes"""
        if len(hash1) != len(hash2):
            return float('inf')
        return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))
    
    async def download_and_process_image(self, message, filename: str) -> dict:
        """Download image and extract metadata"""
        image_path = f"images/{filename}"
        
        try:
            # Download image
            await self.client.download_media(message.media, image_path)
            logger.info(f"Downloaded image: {filename}")
            
            # Verify image was downloaded successfully
            if not os.path.exists(image_path) or os.path.getsize(image_path) == 0:
                logger.error(f"Failed to download image: {filename}")
                return None
            
            # Compute perceptual hash
            phash = self.compute_phash(image_path)
            
            if not phash:
                logger.warning(f"Could not compute hash for {filename}")
                os.remove(image_path)
                return None
            
            # Check for duplicates
            if self.is_duplicate_image(phash):
                logger.info(f"Duplicate image detected, skipping: {filename}")
                os.remove(image_path)  # Clean up
                return None
            
            # Parse caption
            caption = message.message or ""
            name, anime = self.parse_caption(caption)
            
            logger.info(f"Parsed: {name} from {anime} (Caption: {caption[:50]}...)")
            
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
            logger.info(f"✅ Saved to database: {image_data['name']} from {image_data['anime']}")
            
        except sqlite3.IntegrityError as e:
            logger.warning(f"Database integrity error (likely duplicate filename): {e}")
            # Remove the file if database insert fails
            image_path = f"images/{image_data['filename']}"
            if os.path.exists(image_path):
                os.remove(image_path)
        except Exception as e:
            logger.error(f"Database error: {e}")
            # Remove the file if database insert fails
            image_path = f"images/{image_data['filename']}"
            if os.path.exists(image_path):
                os.remove(image_path)
        finally:
            conn.close()
    
    async def scrape_channel(self, limit: int = 100):
        """Scrape messages from the Telegram channel"""
        try:
            # Get channel entity
            try:
                channel = await self.client.get_entity(self.channel_username)
                logger.info(f"Connected to channel: {getattr(channel, 'title', self.channel_username)}")
            except Exception as e:
                logger.error(f"Could not connect to channel {self.channel_username}: {e}")
                logger.info("Make sure you're a member of the channel and the username is correct")
                return
            
            last_message_id = self.get_last_message_id()
            logger.info(f"Starting from message ID: {last_message_id}")
            
            processed_count = 0
            new_images_count = 0
            skipped_count = 0
            
            # Iterate through messages (from newest to oldest by default)
            async for message in self.client.iter_messages(
                channel, 
                limit=limit,
                min_id=last_message_id
            ):
                
                # Skip if no media or not a photo
                if not message.media or not isinstance(message.media, MessageMediaPhoto):
                    continue
                
                processed_count += 1
                
                # Check if already processed by message_id
                conn = sqlite3.connect('waifu_gallery.db')
                cursor = conn.cursor()
                cursor.execute('SELECT 1 FROM characters WHERE message_id = ?', (message.id,))
                exists = cursor.fetchone()
                conn.close()
                
                if exists:
                    skipped_count += 1
                    logger.debug(f"Message {message.id} already processed, skipping")
                    continue
                
                # Generate unique filename
                timestamp = message.date.strftime("%Y%m%d_%H%M%S")
                random_suffix = secrets.token_hex(4)
                filename = f"{timestamp}_{message.id}_{random_suffix}.jpg"
                
                # Download and process image
                image_data = await self.download_and_process_image(message, filename)
                
                if image_data:
                    self.save_to_database(image_data)
                    new_images_count += 1
                else:
                    skipped_count += 1
                
                # Update last processed message ID
                self.update_last_message_id(message.id)
                
                # Log progress every 5 messages
                if processed_count % 5 == 0:
                    logger.info(f"Progress: {processed_count} processed, {new_images_count} new images, {skipped_count} skipped")
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.5)
            
            logger.info(f"🎉 Scraping completed!")
            logger.info(f"📊 Summary: {processed_count} messages processed, {new_images_count} new images added, {skipped_count} skipped")
            
            # Update database stats
            conn = sqlite3.connect('waifu_gallery.db')
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM characters')
            total_characters = cursor.fetchone()[0]
            conn.close()
            logger.info(f"📚 Total characters in database: {total_characters}")
            
        except Exception as e:
            logger.error(f"Error during scraping: {e}")
            raise
    
    async def run_scraper(self, limit: int = 100):
        """Main scraper function"""
        try:
            logger.info("🚀 Starting Telegram scraper...")
            self.init_database()
            await self.init_client()
            await self.scrape_channel(limit=limit)
            logger.info("✅ Scraping completed successfully")
            
        except KeyboardInterrupt:
            logger.info("Scraping interrupted by user")
        except Exception as e:
            logger.error(f"❌ Scraper failed: {e}")
            raise
        finally:
            if self.client.is_connected():
                await self.client.disconnect()
                logger.info("Telegram client disconnected")

async def main():
    # Configuration from environment variables
    API_ID = os.getenv('TELEGRAM_API_ID', '')
    API_HASH = os.getenv('TELEGRAM_API_HASH', '')
    PHONE = os.getenv('TELEGRAM_PHONE', '')
    CHANNEL = os.getenv('TELEGRAM_CHANNEL', '')
    LIMIT = int(os.getenv('SCRAPER_LIMIT', '100'))
    
    # Validate configuration
    if not API_ID or not API_ID.isdigit():
        logger.error("❌ TELEGRAM_API_ID is missing or invalid")
        return
    
    if not API_HASH:
        logger.error("❌ TELEGRAM_API_HASH is missing")
        return
        
    if not PHONE:
        logger.error("❌ TELEGRAM_PHONE is missing")
        return
        
    if not CHANNEL:
        logger.error("❌ TELEGRAM_CHANNEL is missing")
        return
    
    logger.info(f"🔧 Configuration:")
    logger.info(f"   API ID: {API_ID}")
    logger.info(f"   Phone: {PHONE}")
    logger.info(f"   Channel: {CHANNEL}")
    logger.info(f"   Limit: {LIMIT}")
    
    try:
        scraper = TelegramScraper(int(API_ID), API_HASH, PHONE, CHANNEL)
        await scraper.run_scraper(limit=LIMIT)
    except Exception as e:
        logger.error(f"Failed to run scraper: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)