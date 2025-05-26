from fastapi import FastAPI, HTTPException, Depends, Request, Form, File, UploadFile, Query
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import sqlite3
import os
import hashlib
import imagehash
from PIL import Image
import io
import re
from typing import Optional, List
import uvicorn
from datetime import datetime
import secrets
import bcrypt
from pathlib import Path
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Waifu/Husbando Gallery")

# Create directories
os.makedirs("images", exist_ok=True)
os.makedirs("uploads", exist_ok=True)
os.makedirs("templates", exist_ok=True)
os.makedirs("static", exist_ok=True)

# Mount static files
app.mount("/images", StaticFiles(directory="images"), name="images")
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# Database setup
def init_db():
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
        CREATE TABLE IF NOT EXISTS admin_sessions (
            session_id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    
    # Create indexes for better search performance
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_name ON characters(name)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_anime ON characters(anime)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_phash ON characters(phash)')
    
    conn.commit()
    conn.close()

init_db()

# Admin password (in production, use environment variable)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_PASSWORD_HASH = bcrypt.hashpw(ADMIN_PASSWORD.encode('utf-8'), bcrypt.gensalt())

def verify_admin_session(session_id: str) -> bool:
    conn = sqlite3.connect('waifu_gallery.db')
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM admin_sessions WHERE session_id = ?', (session_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def create_admin_session() -> str:
    session_id = secrets.token_urlsafe(32)
    conn = sqlite3.connect('waifu_gallery.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO admin_sessions (session_id) VALUES (?)', (session_id,))
    conn.commit()
    conn.close()
    return session_id

def compute_phash(image_path: str) -> str:
    """Compute perceptual hash of an image"""
    try:
        with Image.open(image_path) as img:
            return str(imagehash.phash(img))
    except Exception as e:
        logger.error(f"Error computing phash for {image_path}: {e}")
        return ""

def parse_caption(caption: str) -> tuple:
    """Parse caption to extract name and anime"""
    if not caption:
        return "Unknown", "Unknown"
    
    # Try different delimiters
    delimiters = [' - ', ' | ', ': ', ' : ', ' from ']
    
    for delimiter in delimiters:
        if delimiter in caption:
            parts = caption.split(delimiter, 1)
            if len(parts) == 2:
                name = parts[0].strip()
                anime = parts[1].strip()
                # Remove emojis and extra whitespace
                name = re.sub(r'[^\w\s-]', '', name).strip()
                anime = re.sub(r'[^\w\s-]', '', anime).strip()
                return name or "Unknown", anime or "Unknown"
    
    # If no delimiter found, try to extract from parentheses
    match = re.search(r'(.+?)\s*\((.+?)\)', caption)
    if match:
        name = match.group(1).strip()
        anime = match.group(2).strip()
        return name or "Unknown", anime or "Unknown"
    
    # Default fallback
    clean_caption = re.sub(r'[^\w\s-]', '', caption).strip()
    return clean_caption or "Unknown", "Unknown"

def hamming_distance(hash1: str, hash2: str) -> int:
    """Calculate Hamming distance between two hashes"""
    if len(hash1) != len(hash2):
        return float('inf')
    return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, page: int = Query(1, ge=1), search: str = Query("")):
    conn = sqlite3.connect('waifu_gallery.db')
    cursor = conn.cursor()
    
    # Build search query
    if search:
        query = '''
            SELECT id, filename, name, anime FROM characters 
            WHERE name LIKE ? OR anime LIKE ? 
            ORDER BY created_at DESC 
            LIMIT 20 OFFSET ?
        '''
        search_pattern = f"%{search}%"
        cursor.execute(query, (search_pattern, search_pattern, (page - 1) * 20))
    else:
        query = '''
            SELECT id, filename, name, anime FROM characters 
            ORDER BY created_at DESC 
            LIMIT 20 OFFSET ?
        '''
        cursor.execute(query, ((page - 1) * 20,))
    
    characters = cursor.fetchall()
    
    # Get total count for pagination
    if search:
        cursor.execute('SELECT COUNT(*) FROM characters WHERE name LIKE ? OR anime LIKE ?', 
                      (search_pattern, search_pattern))
    else:
        cursor.execute('SELECT COUNT(*) FROM characters')
    
    total_count = cursor.fetchone()[0]
    conn.close()
    
    total_pages = (total_count + 19) // 20  # Ceiling division
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "characters": characters,
        "current_page": page,
        "total_pages": total_pages,
        "search": search,
        "total_count": total_count
    })

@app.post("/reverse-search")
async def reverse_search(file: UploadFile = File(...)):
    if not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    try:
        # Save uploaded file temporarily
        content = await file.read()
        temp_path = f"uploads/temp_{secrets.token_hex(8)}.jpg"
        
        with open(temp_path, "wb") as f:
            f.write(content)
        
        # Compute phash of uploaded image
        upload_phash = compute_phash(temp_path)
        
        # Clean up temp file
        os.remove(temp_path)
        
        if not upload_phash:
            raise HTTPException(status_code=400, detail="Could not process image")
        
        # Find similar images in database
        conn = sqlite3.connect('waifu_gallery.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id, filename, name, anime, phash FROM characters')
        all_characters = cursor.fetchall()
        conn.close()
        
        matches = []
        for char_id, filename, name, anime, stored_phash in all_characters:
            if stored_phash:
                distance = hamming_distance(upload_phash, stored_phash)
                if distance <= 5:  # Threshold for similarity
                    matches.append({
                        'id': char_id,
                        'filename': filename,
                        'name': name,
                        'anime': anime,
                        'similarity': 1 - (distance / 64)  # Convert to similarity score
                    })
        
        # Sort by similarity
        matches.sort(key=lambda x: x['similarity'], reverse=True)
        
        return {"matches": matches[:10]}  # Return top 10 matches
        
    except Exception as e:
        logger.error(f"Reverse search error: {e}")
        raise HTTPException(status_code=500, detail="Error processing image")

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    if bcrypt.checkpw(password.encode('utf-8'), ADMIN_PASSWORD_HASH):
        session_id = create_admin_session()
        response = RedirectResponse(url="/admin", status_code=302)
        response.set_cookie(key="admin_session", value=session_id, httponly=True)
        return response
    else:
        raise HTTPException(status_code=401, detail="Invalid password")

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    session_id = request.cookies.get("admin_session")
    if not session_id or not verify_admin_session(session_id):
        return RedirectResponse(url="/admin/login")
    
    conn = sqlite3.connect('waifu_gallery.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM characters')
    total_characters = cursor.fetchone()[0]
    
    cursor.execute('SELECT id, filename, name, anime FROM characters ORDER BY created_at DESC LIMIT 10')
    recent_characters = cursor.fetchall()
    conn.close()
    
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "total_characters": total_characters,
        "recent_characters": recent_characters
    })

@app.post("/admin/upload")
async def admin_upload(
    request: Request,
    name: str = Form(...),
    anime: str = Form(...),
    file: UploadFile = File(...)
):
    session_id = request.cookies.get("admin_session")
    if not session_id or not verify_admin_session(session_id):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    try:
        # Generate unique filename
        filename = f"{secrets.token_hex(8)}_{file.filename}"
        filepath = f"images/{filename}"
        
        # Save file
        content = await file.read()
        with open(filepath, "wb") as f:
            f.write(content)
        
        # Compute phash
        phash = compute_phash(filepath)
        
        # Save to database
        conn = sqlite3.connect('waifu_gallery.db')
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO characters (filename, name, anime, phash) VALUES (?, ?, ?, ?)',
            (filename, name.strip(), anime.strip(), phash)
        )
        conn.commit()
        conn.close()
        
        return {"success": True, "message": "Character uploaded successfully"}
        
    except Exception as e:
        logger.error(f"Upload error: {e}")
        raise HTTPException(status_code=500, detail="Error uploading file")

@app.get("/admin/characters")
async def admin_list_characters(request: Request, page: int = Query(1, ge=1)):
    session_id = request.cookies.get("admin_session")
    if not session_id or not verify_admin_session(session_id):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    conn = sqlite3.connect('waifu_gallery.db')
    cursor = conn.cursor()
    
    cursor.execute(
        'SELECT id, filename, name, anime FROM characters ORDER BY created_at DESC LIMIT 20 OFFSET ?',
        ((page - 1) * 20,)
    )
    characters = cursor.fetchall()
    
    cursor.execute('SELECT COUNT(*) FROM characters')
    total_count = cursor.fetchone()[0]
    conn.close()
    
    total_pages = (total_count + 19) // 20
    
    return templates.TemplateResponse("admin_characters.html", {
        "request": request,
        "characters": characters,
        "current_page": page,
        "total_pages": total_pages
    })

@app.delete("/admin/characters/{character_id}")
async def admin_delete_character(character_id: int, request: Request):
    session_id = request.cookies.get("admin_session")
    if not session_id or not verify_admin_session(session_id):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    conn = sqlite3.connect('waifu_gallery.db')
    cursor = conn.cursor()
    
    # Get filename to delete file
    cursor.execute('SELECT filename FROM characters WHERE id = ?', (character_id,))
    result = cursor.fetchone()
    
    if not result:
        conn.close()
        raise HTTPException(status_code=404, detail="Character not found")
    
    filename = result[0]
    
    # Delete from database
    cursor.execute('DELETE FROM characters WHERE id = ?', (character_id,))
    conn.commit()
    conn.close()
    
    # Delete file
    try:
        os.remove(f"images/{filename}")
    except OSError:
        pass  # File might not exist
    
    return {"success": True, "message": "Character deleted successfully"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)