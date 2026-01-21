from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, abort
import os
import re
import time
import threading
import requests
import tempfile
import logging
from pathlib import Path
import sqlite3
import json
import shutil
import multiprocessing
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Dict, List, Tuple, Optional
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
from datetime import datetime, timedelta
import hashlib
import pickle
import io


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Flask app initialization
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-this-in-production'
app.config['UPLOAD_FOLDER'] = 'uploads'

# Upload and task limits
MAX_UPLOAD_SIZE_BYTES = 80 * 1024 * 1024 * 1024  # 80 GB
MAX_URLS = 50
TASK_LOG_LIMIT = 200
DEFAULT_SEARCH_WORKERS = max(1, (os.cpu_count() or 2) - 1)
PROCESS_CONTEXT = multiprocessing.get_context("spawn")

app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_SIZE_BYTES

# Access key
ACCESS_KEY = '@BaignX'

# Constants
TELEGRAM_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB (Telegram limit)

# Download progress tracking
download_progress: Dict[str, Dict] = {}
download_progress_lock = threading.Lock()
download_task_controls: Dict[str, Dict] = {}
download_task_controls_lock = threading.Lock()

# Task control for cancellation and logs
search_task_controls: Dict[str, Dict] = {}
search_task_controls_lock = threading.Lock()

# Database setup
DB_PATH = "bot_database.db"

# Search task queue
search_tasks = {}
search_task_lock = threading.Lock()


def append_task_log(task_store: Dict[str, Dict], task_id: str, message: str, lock: threading.Lock) -> None:
    """Append a timestamped log message to a task store."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = f"{timestamp} - {message}"
    with lock:
        task = task_store.get(task_id)
        if not task:
            return
        logs = task.setdefault('logs', [])
        logs.append(entry)
        if len(logs) > TASK_LOG_LIMIT:
            del logs[:-TASK_LOG_LIMIT]


def ensure_search_task_control(task_id: str) -> Dict:
    with search_task_controls_lock:
        control = search_task_controls.get(task_id)
        if not control:
            control = {'cancel_event': threading.Event(), 'logs': []}
            search_task_controls[task_id] = control
        return control


def init_download_task(session_id: str, mode: str, total_urls: int = 1) -> None:
    with download_progress_lock:
        download_progress[session_id] = {
            'status': 'queued',
            'progress': 0,
            'total_size': 0,
            'downloaded': 0,
            'filename': '',
            'speed': 0,
            'eta': 0,
            'error': None,
            'mode': mode,
            'total_urls': total_urls,
            'completed': 0,
            'failed': 0,
            'current_file': '',
            'files': [],
            'logs': []
        }
    with download_task_controls_lock:
        download_task_controls[session_id] = {'cancel_event': threading.Event(), 'logs': []}
    append_task_log(download_progress, session_id, 'Task queued', download_progress_lock)


def get_download_task_control(session_id: str) -> Optional[Dict]:
    with download_task_controls_lock:
        return download_task_controls.get(session_id)


def ensure_download_task_control(session_id: str) -> Dict:
    with download_task_controls_lock:
        control = download_task_controls.get(session_id)
        if not control:
            control = {'cancel_event': threading.Event(), 'logs': []}
            download_task_controls[session_id] = control
        return control


def update_download_progress(session_id: str, **updates) -> None:
    with download_progress_lock:
        if session_id in download_progress:
            download_progress[session_id].update(updates)

def init_db():
    """Initialize the SQLite database with required tables"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Create table if it doesn't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS global_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            file_size INTEGER,
            file_path TEXT,
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expiry_date TIMESTAMP
        )
    ''')
    
    # Check if is_selected column exists, if not, add it
    cursor.execute("PRAGMA table_info(global_files)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'is_selected' not in columns:
        cursor.execute('ALTER TABLE global_files ADD COLUMN is_selected INTEGER DEFAULT 1')
    if 'expiry_date' not in columns:
        cursor.execute('ALTER TABLE global_files ADD COLUMN expiry_date TIMESTAMP')
    
    # Create search tasks table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS search_tasks (
            id TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            search_type TEXT NOT NULL,
            include_schemes INTEGER DEFAULT 0,
            status TEXT DEFAULT 'queued',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            result_count INTEGER DEFAULT 0,
            error_message TEXT,
            total_files INTEGER DEFAULT 0,
            processed_files INTEGER DEFAULT 0,
            total_bytes INTEGER DEFAULT 0,
            processed_bytes INTEGER DEFAULT 0,
            progress REAL DEFAULT 0
        )
    ''')

    cursor.execute("PRAGMA table_info(search_tasks)")
    search_columns = [column[1] for column in cursor.fetchall()]
    if 'total_files' not in search_columns:
        cursor.execute('ALTER TABLE search_tasks ADD COLUMN total_files INTEGER DEFAULT 0')
    if 'processed_files' not in search_columns:
        cursor.execute('ALTER TABLE search_tasks ADD COLUMN processed_files INTEGER DEFAULT 0')
    if 'total_bytes' not in search_columns:
        cursor.execute('ALTER TABLE search_tasks ADD COLUMN total_bytes INTEGER DEFAULT 0')
    if 'processed_bytes' not in search_columns:
        cursor.execute('ALTER TABLE search_tasks ADD COLUMN processed_bytes INTEGER DEFAULT 0')
    if 'progress' not in search_columns:
        cursor.execute('ALTER TABLE search_tasks ADD COLUMN progress REAL DEFAULT 0')
    
    conn.commit()
    conn.close()

def save_file_record(file_name, file_size, file_path):
    """Save file record to database with expiry time"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Calculate expiry time (24 hours from now)
    expiry_time = datetime.now() + timedelta(hours=24)
    
    cursor.execute('''
        INSERT INTO global_files (file_name, file_size, file_path, upload_date, expiry_date)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
    ''', (file_name, file_size, file_path, expiry_time))
    
    conn.commit()
    conn.close()

def get_global_files():
    """Get all global files"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, file_name, file_size, file_path, upload_date 
        FROM global_files 
        WHERE expiry_date IS NULL OR expiry_date >= ?
        ORDER BY upload_date DESC
    ''', (datetime.now(),))
    
    files = cursor.fetchall()
    conn.close()
    
    return files

def delete_file_record(file_id):
    """Delete file record from database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('DELETE FROM global_files WHERE id = ?', (file_id,))
    conn.commit()
    conn.close()


def delete_search_task_record(task_id: str) -> None:
    """Delete a search task record from the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM search_tasks WHERE id = ?', (task_id,))
    conn.commit()
    conn.close()

def get_file_by_id(file_id):
    """Get file record by file_id"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, file_name, file_size, file_path 
        FROM global_files 
        WHERE id = ? AND (expiry_date IS NULL OR expiry_date >= ?)
    ''', (file_id, datetime.now()))
    file_info = cursor.fetchone()
    conn.close()
    
    if file_info:
        return {
            'id': file_info[0],
            'file_name': file_info[1],
            'file_size': file_info[2],
            'file_path': file_info[3]
        }
    return None


def update_file_selection(file_id, is_selected):
    """Update the selected status of a file"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('UPDATE global_files SET is_selected = ? WHERE id = ?', (is_selected, file_id))
    
    conn.commit()
    conn.close()


def get_selected_files():
    """Get all selected files for searching"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, file_name, file_size, file_path, upload_date 
        FROM global_files 
        WHERE is_selected = 1 AND (expiry_date IS NULL OR expiry_date >= ?)
        ORDER BY upload_date DESC
    ''', (datetime.now(),))
    
    files = cursor.fetchall()
    conn.close()
    
    return files


def update_all_files_selection(is_selected):
    """Update the selected status of all files"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('UPDATE global_files SET is_selected = ?', (is_selected,))
    
    conn.commit()
    conn.close()

def create_search_task(query, search_type, include_schemes=False):
    """Create a new search task"""
    task_id = secrets.token_hex(16)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO search_tasks (id, query, search_type, include_schemes, status, progress)
        VALUES (?, ?, ?, ?, 'queued', 0)
    ''', (task_id, query, search_type, 1 if include_schemes else 0))
    conn.commit()
    conn.close()
    ensure_search_task_control(task_id)
    append_task_log(search_task_controls, task_id, 'Task created', search_task_controls_lock)
    return task_id

def get_search_task(task_id):
    """Get search task by ID"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM search_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    conn.close()
    return task

def get_all_search_tasks():
    """Get all search tasks ordered by creation date"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM search_tasks ORDER BY created_at DESC')
    tasks = cursor.fetchall()
    conn.close()
    return tasks

def update_search_task_status(task_id, status, result_count=None, error_message=None):
    """Update search task status"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if status == 'completed':
        cursor.execute('''
            UPDATE search_tasks 
            SET status = ?, completed_at = CURRENT_TIMESTAMP, result_count = ?
            WHERE id = ?
        ''', (status, result_count, task_id))
    elif status == 'error':
        cursor.execute('''
            UPDATE search_tasks 
            SET status = ?, error_message = ?
            WHERE id = ?
        ''', (status, error_message, task_id))
    else:
        cursor.execute('UPDATE search_tasks SET status = ? WHERE id = ?', (status, task_id))
    conn.commit()
    conn.close()


def update_search_task_progress(task_id, processed_files, total_files, processed_bytes, total_bytes):
    """Update progress metrics for a search task."""
    progress = 0.0
    if total_bytes > 0:
        progress = min(100.0, (processed_bytes / total_bytes) * 100.0)
    elif total_files > 0:
        progress = min(100.0, (processed_files / total_files) * 100.0)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE search_tasks
        SET processed_files = ?, total_files = ?, processed_bytes = ?, total_bytes = ?, progress = ?
        WHERE id = ?
    ''', (processed_files, total_files, processed_bytes, total_bytes, progress, task_id))
    conn.commit()
    conn.close()

def process_search_task(task_id):
    """Process a search task in background"""
    try:
        task = get_search_task(task_id)
        if not task:
            return
        
        _, query, search_type, include_schemes, status, _, _, _, _, _, _, _, _, _ = task

        control = ensure_search_task_control(task_id)
        cancel_event = control['cancel_event']
        
        # Update status to processing
        update_search_task_status(task_id, 'processing')
        append_task_log(search_task_controls, task_id, 'Search started', search_task_controls_lock)
        
        # Perform search
        global_files = get_selected_files()
        total_files = len(global_files)
        total_bytes = sum(file_size for _, _, file_size, _, _ in global_files if file_size)
        update_search_task_progress(task_id, 0, total_files, 0, total_bytes)

        user_pass_results = set()
        url_user_pass_results = set()
        url_results = set()
        erc_token_results = set()
        processed_files = 0
        processed_bytes = 0

        with ProcessPoolExecutor(
            max_workers=min(DEFAULT_SEARCH_WORKERS, max(1, total_files)),
            mp_context=PROCESS_CONTEXT
        ) as executor:
            future_map = {}
            for _, file_name, file_size, file_path, _ in global_files:
                if cancel_event.is_set():
                    break
                future = executor.submit(search_file_worker, file_path, search_type, query, bool(include_schemes))
                future_map[future] = (file_name, file_size or 0)

            for future in as_completed(future_map):
                if cancel_event.is_set():
                    break
                file_name, file_size = future_map[future]
                try:
                    up, uup, urls, erc, _ = future.result()
                    user_pass_results.update(up)
                    url_user_pass_results.update(uup)
                    url_results.update(urls)
                    erc_token_results.update(erc)
                except Exception as e:
                    logger.error(f"Error processing search file {file_name}: {e}")
                    append_task_log(search_task_controls, task_id, f"Error processing {file_name}: {e}", search_task_controls_lock)

                processed_files += 1
                processed_bytes += file_size
                update_search_task_progress(task_id, processed_files, total_files, processed_bytes, total_bytes)

        if cancel_event.is_set():
            update_search_task_status(task_id, 'cancelled')
            append_task_log(search_task_controls, task_id, 'Search cancelled', search_task_controls_lock)
            return

        results = {
            'all': list(user_pass_results | url_user_pass_results | url_results | erc_token_results),
            'user_pass': list(user_pass_results),
            'url_user_pass': list(url_user_pass_results),
            'urls': list(url_results),
            'erc_tokens': list(erc_token_results)
        }
        
        # Store results in memory
        with search_task_lock:
            search_tasks[task_id] = results
        
        # Update status to completed
        total_results = len(results.get('all', []))
        update_search_task_status(task_id, 'completed', result_count=total_results)
        update_search_task_progress(task_id, total_files, total_files, total_bytes, total_bytes)
        append_task_log(search_task_controls, task_id, f"Search completed with {total_results} results", search_task_controls_lock)
        
    except Exception as e:
        logger.error(f"Error processing search task {task_id}: {e}")
        update_search_task_status(task_id, 'error', error_message=str(e))
        append_task_log(search_task_controls, task_id, f"Search error: {e}", search_task_controls_lock)

# Initialize the database
init_db()

# Ensure upload directory exists
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])


# Auto-delete expired files
def cleanup_expired_files():
    """Delete files that have expired (older than 24 hours)"""
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Find expired files
            cursor.execute('''
                SELECT id, file_path FROM global_files 
                WHERE expiry_date IS NOT NULL AND expiry_date < ?
            ''', (datetime.now(),))
            
            expired_files = cursor.fetchall()
            
            for file_id, file_path in expired_files:
                # Delete file from filesystem
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Auto-deleted expired file: {file_path}")
                except Exception as e:
                    logger.error(f"Error deleting expired file {file_path}: {e}")
                
                # Delete record from database
                cursor.execute('DELETE FROM global_files WHERE id = ?', (file_id,))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error in cleanup_expired_files: {e}")
        
        # Run cleanup every hour
        time.sleep(3600)

# Run cleanup once on startup to avoid stale entries
def cleanup_expired_files_once():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, file_path FROM global_files 
            WHERE expiry_date IS NOT NULL AND expiry_date < ?
        ''', (datetime.now(),))
        expired_files = cursor.fetchall()
        for file_id, file_path in expired_files:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"Auto-deleted expired file: {file_path}")
            except Exception as e:
                logger.error(f"Error deleting expired file {file_path}: {e}")
            cursor.execute('DELETE FROM global_files WHERE id = ?', (file_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error in cleanup_expired_files_once: {e}")

# Start cleanup thread
cleanup_expired_files_once()
cleanup_thread = threading.Thread(target=cleanup_expired_files, daemon=True)
cleanup_thread.start()


# Clean URL for searching
def clean_url(url):
    # Remove protocol
    url = re.sub(r'^(https?://)?(www\.)?', '', url.lower())
    # Remove path, query strings, etc.
    url = url.split('/')[0]
    return url


def extract_domain_from_url(url: str) -> str:
    """Extract the domain from a URL string safely."""
    try:
        # Ensure scheme for urlparse
        if not re.match(r'^https?://', url, re.IGNORECASE):
            url_to_parse = 'http://' + url
        else:
            url_to_parse = url
        from urllib.parse import urlparse
        parsed = urlparse(url_to_parse)
        host = parsed.netloc or parsed.path.split('/')[0]
        # Strip www.
        host = re.sub(r'^www\.', '', host, flags=re.IGNORECASE)
        return host.lower()
    except Exception:
        return ''

EMAIL_COMBO_PATTERN = re.compile(r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+):([^\s:]+)')
USER_COMBO_PATTERN = re.compile(r'\b([a-zA-Z0-9_.+-]{3,}):([^\s:]+)\b')
URL_PATH_USER_BLOCKLIST = {
    'signup', 'login', 'signin', 'register', 'account', 'accounts', 'auth'
}


def extract_credentials(line):
    """Extract email:pass or user:pass combos with URL/path-aware filtering."""
    line = line.strip()
    if not line:
        return []

    email_matches = EMAIL_COMBO_PATTERN.findall(line)
    if email_matches:
        email, pwd = email_matches[-1]
        return [f"{email.lower()}:{pwd}"]

    has_path = '/' in line or '://' in line
    if has_path:
        parts = [part.strip() for part in line.split(':')]
        if len(parts) < 3:
            return []
        user = parts[-2]
        pwd = parts[-1]
        if not user or not pwd or '/' in user:
            return []
        user_lower = user.lower()
        if user_lower in URL_PATH_USER_BLOCKLIST:
            return []
        if '.' in user and '@' not in user:
            return []
        return [f"{user}:{pwd}"]

    user_matches = USER_COMBO_PATTERN.findall(line)
    if user_matches:
        user, pwd = user_matches[-1]
        user_lower = user.lower()
        if user_lower in URL_PATH_USER_BLOCKLIST:
            return []
        if '@' not in user and '.' in user:
            return []
        return [f"{user}:{pwd}"]

    return []


def extract_urls(line):
    """
    Extract URLs from a line of text using regex
    """
    line = line.strip()
    extracted_urls = []
    
    # Pattern to match URLs with http or https protocols
    url_pattern = re.compile(
        r'https?://'  # http or https
        r'(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}'  # domain
        r'(?::[0-9]{1,5})?'  # optional port
        r'(?:[/?#][^\s]*)?',  # path, query, fragment
        re.IGNORECASE
    )
    
    urls = url_pattern.findall(line)
    extracted_urls.extend(urls)
    
    # Pattern to match URLs without protocol (starting with www.)
    www_pattern = re.compile(
        r'www\.'  # www prefix
        r'(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}'  # domain
        r'(?::[0-9]{1,5})?'  # optional port
        r'(?:[/?#][^\s]*)?',  # path, query, fragment
        re.IGNORECASE
    )
    
    www_urls = www_pattern.findall(line)
    extracted_urls.extend(www_urls)
    
    # Add 'http://' prefix to www URLs to make them proper URLs
    formatted_www = [f"http://{url}" for url in www_urls]
    extracted_urls.extend(formatted_www)
    
    return list(set(extracted_urls))  # Remove duplicates


def extract_erc_tokens(line):
    """
    Extract ERC tokens (like ERC-20, ERC-721) from a line of text
    """
    line = line.strip()
    extracted_tokens = []
    
    # Pattern to match ERC tokens (format: ERC-#### or just a token name that might be ERC-related)
    erc_pattern = re.compile(
        r'\b(erc-\d{1,4}|erc\d{1,4})\b',  # ERC-#### or ERC#### patterns
        re.IGNORECASE
    )
    
    erc_matches = erc_pattern.findall(line)
    for match in erc_matches:
        extracted_tokens.append(match.upper())  # Convert to uppercase for consistency
    
    # Pattern to match Ethereum addresses (often related to ERC tokens)
    eth_address_pattern = re.compile(
        r'0x[a-fA-F0-9]{40}'  # Ethereum address format
    )
    
    eth_addresses = eth_address_pattern.findall(line)
    extracted_tokens.extend(eth_addresses)
    
    return list(set(extracted_tokens))  # Remove duplicates


def search_file_worker(file_path: str, search_type: str, query: str, include_schemes: bool) -> Tuple[set, set, set, set, int]:
    """Search a single file and return result sets plus bytes processed."""
    user_pass_results = set()
    url_user_pass_results = set()
    url_results = set()
    erc_token_results = set()
    bytes_processed = 0

    if search_type == 'domain':
        domain = clean_url(query)
        if not domain or '.' not in domain:
            return user_pass_results, url_user_pass_results, url_results, erc_token_results, bytes_processed
    else:
        domain = ''

    keywords = query.lower() if search_type == 'keyword' else ''

    try:
        with open(file_path, 'rb') as f:
            for raw in f:
                bytes_processed += len(raw)
                line = raw.decode('utf-8', errors='ignore').strip()
                if not line:
                    continue

                if search_type == 'domain':
                    if domain not in line.lower():
                        continue
                else:
                    if keywords not in line.lower():
                        continue

                if (not include_schemes) and re.match(r'^[a-z][a-z0-9+.-]*://', line, re.IGNORECASE) and not re.match(r'^https?://', line, re.IGNORECASE):
                    continue

                credentials = extract_credentials(line)

                for cred in credentials:
                    user_pass_results.add(cred)

                urls = extract_urls(line)
                for url in urls:
                    if search_type == 'domain':
                        if domain in url.lower():
                            url_results.add(url)
                    else:
                        if re.match(r'^https?://', url, re.IGNORECASE):
                            domain_match = extract_domain_from_url(url)
                            if keywords in domain_match or keywords in url.lower():
                                url_results.add(url)

                erc_tokens = extract_erc_tokens(line)
                for token in erc_tokens:
                    if search_type == 'domain':
                        if domain in token.lower():
                            erc_token_results.add(token)
                    else:
                        if keywords in token.lower():
                            erc_token_results.add(token)
    except Exception as e:
        logger.error(f"Error searching file {file_path}: {e}")

    return user_pass_results, url_user_pass_results, url_results, erc_token_results, bytes_processed


# Search by domain - optimized for large files
def search_domain(query, include_schemes: bool = False):
    # Clean and validate the domain
    domain = clean_url(query)
    if not domain or '.' not in domain:
        return {'all': [], 'user_pass': [], 'url_user_pass': [], 'urls': [], 'erc_tokens': []}
    
    # Get selected global files from database
    global_files = get_selected_files()
    if not global_files:
        return {'all': [], 'user_pass': [], 'url_user_pass': [], 'urls': [], 'erc_tokens': []}
    
    # Initialize results
    user_pass_results = []
    url_user_pass_results = []
    url_results = []
    erc_token_results = []
    
    # Perform search
    for file_id, file_name, file_size, file_path, upload_date in global_files:
        try:
            # Read the file in chunks to handle large files
            with open(file_path, 'r', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    
                    if domain in line.lower():
                        # Skip non-http(s) scheme lines if disabled
                        if (not include_schemes) and re.match(r'^[a-z][a-z0-9+.-]*://', line, re.IGNORECASE) and not re.match(r'^https?://', line, re.IGNORECASE):
                            continue
                        # Extract credentials from the line
                        credentials = extract_credentials(line)
                        
                        for cred in credentials:
                            if cred not in user_pass_results:
                                user_pass_results.append(cred)
                        
                        # Extract URLs from the line if it contains the domain
                        urls = extract_urls(line)
                        for url in urls:
                            if domain in url.lower():
                                if url not in url_results:
                                    url_results.append(url)
                        
                        # Extract ERC tokens from the line if it contains the domain
                        erc_tokens = extract_erc_tokens(line)
                        for token in erc_tokens:
                            if domain in token.lower():
                                if token not in erc_token_results:
                                    erc_token_results.append(token)
        except Exception as e:
            logger.error(f"Error searching file {file_name}: {e}")
    
    # Combine results for general display
    all_results = user_pass_results + url_user_pass_results + url_results + erc_token_results
    
    # Remove duplicates while preserving order
    seen = set()
    unique_all = []
    for item in all_results:
        if item not in seen:
            seen.add(item)
            unique_all.append(item)
    
    seen = set()
    unique_user_pass = []
    for item in user_pass_results:
        if item not in seen:
            seen.add(item)
            unique_user_pass.append(item)
    
    seen = set()
    unique_url_user_pass = []
    for item in url_user_pass_results:
        if item not in seen:
            seen.add(item)
            unique_url_user_pass.append(item)
    
    seen = set()
    unique_urls = []
    for item in url_results:
        if item not in seen:
            seen.add(item)
            unique_urls.append(item)
    
    seen = set()
    unique_erc_tokens = []
    for item in erc_token_results:
        if item not in seen:
            seen.add(item)
            unique_erc_tokens.append(item)
    
    return {
        'all': unique_all,
        'user_pass': unique_user_pass,
        'url_user_pass': unique_url_user_pass,
        'urls': unique_urls,
        'erc_tokens': unique_erc_tokens
    }


# Search by keywords - optimized for large files
def search_keywords(keywords, include_schemes: bool = False):
    if not keywords:
        return {'all': [], 'user_pass': [], 'url_user_pass': [], 'urls': [], 'erc_tokens': []}
    kw = keywords.lower()
    
    # Get selected global files from database
    global_files = get_selected_files()
    if not global_files:
        return {'all': [], 'user_pass': [], 'url_user_pass': [], 'urls': [], 'erc_tokens': []}
    
    # Initialize results
    user_pass_results = []
    url_user_pass_results = []
    url_results = []
    erc_token_results = []
    
    # Perform search
    for file_id, file_name, file_size, file_path, upload_date in global_files:
        try:
            # Process file in chunks
            with open(file_path, 'r', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    # Case-insensitive search
                    if kw in line.lower():
                        # Skip non-http(s) scheme lines if disabled
                        if (not include_schemes) and re.match(r'^[a-z][a-z0-9+.-]*://', line, re.IGNORECASE) and not re.match(r'^https?://', line, re.IGNORECASE):
                            continue
                        # Extract all possible credentials from line
                        credentials = extract_credentials(line)

                        for cred in credentials:
                            if cred not in user_pass_results:
                                user_pass_results.append(cred)
                        
                        # Extract URLs from the line if it matches the keyword
                        urls = extract_urls(line)
                        for url in urls:
                            # Only accept http/https and require keyword in domain or full URL
                            if re.match(r'^https?://', url, re.IGNORECASE):
                                domain = extract_domain_from_url(url)
                                if kw in domain or kw in url.lower():
                                    if url not in url_results:
                                        url_results.append(url)
                        
                        # Extract ERC tokens from the line if it matches the keyword
                        erc_tokens = extract_erc_tokens(line)
                        for token in erc_tokens:
                            if kw in token.lower():
                                if token not in erc_token_results:
                                    erc_token_results.append(token)
        except Exception as e:
            logger.error(f"Error searching file {file_name}: {e}")
    
    # Combine results for general display
    all_results = user_pass_results + url_user_pass_results + url_results + erc_token_results
    
    # Remove duplicates while preserving order
    seen = set()
    unique_all = []
    for item in all_results:
        if item not in seen:
            seen.add(item)
            unique_all.append(item)
    
    seen = set()
    unique_user_pass = []
    for item in user_pass_results:
        if item not in seen:
            seen.add(item)
            unique_user_pass.append(item)
    
    seen = set()
    unique_url_user_pass = []
    for item in url_user_pass_results:
        if item not in seen:
            seen.add(item)
            unique_url_user_pass.append(item)
    
    seen = set()
    unique_urls = []
    for item in url_results:
        if item not in seen:
            seen.add(item)
            unique_urls.append(item)
    
    seen = set()
    unique_erc_tokens = []
    for item in erc_token_results:
        if item not in seen:
            seen.add(item)
            unique_erc_tokens.append(item)
    
    return {
        'all': unique_all,
        'user_pass': unique_user_pass,
        'url_user_pass': unique_url_user_pass,
        'urls': unique_urls,
        'erc_tokens': unique_erc_tokens
    }


# Authentication functions and routes
def is_authenticated():
    """Check if the user is authenticated"""
    return session.get('authenticated', False)

@app.route('/')
def index():
    if not is_authenticated():
        return redirect(url_for('access_key'))
    return redirect(url_for('dashboard'))

@app.route('/access_key', methods=['GET', 'POST'])
def access_key():
    if request.method == 'POST':
        access_key_input = request.form.get('access_key', '').strip()
        if access_key_input == ACCESS_KEY:
            session['authenticated'] = True
            flash('Access granted!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid access key. Please try again.', 'error')
    
    return render_template('access_key.html')

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('access_key'))


# No registration or login required - application is public


@app.route('/dashboard')
def dashboard():
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    recent_files = get_global_files()[:5]  # Get 5 most recent files
    
    # Calculate total storage used by files
    total_used_bytes = 0
    formatted_files = []
    for file_id, file_name, file_size, file_path, upload_date in recent_files:
        size_mb = file_size / (1024 * 1024) if file_size else 0
        formatted_files.append((file_id, file_name, f"{size_mb:.2f} MB" if file_size else "Unknown", file_path, upload_date))
        if file_size:
            total_used_bytes += file_size
    
    # Get total storage usage by all files
    all_files = get_global_files()
    total_used_bytes = sum(file_size for _, _, file_size, _, _ in all_files if file_size)
    
    # Get disk usage information
    import shutil
    total, used, free = shutil.disk_usage("/")
    
    # No search history in global mode
    search_history = []
    
    return render_template('dashboard.html', 
                           files=formatted_files, 
                           history=search_history,
                           total_storage=total,
                           used_storage=used,
                           free_storage=free,
                           total_files=len(all_files))


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    if request.method == 'POST':
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        # Check if it's a file upload or URL download
        upload_type = request.form.get('upload_type', 'file')
        
        if upload_type == 'url':
            # Handle URL download
            download_url = request.form.get('download_url', '').strip()
            if not download_url:
                flash('Please provide a download URL', 'error')
                return redirect(request.url)
            
            # Generate unique session ID for progress tracking
            session_id = secrets.token_hex(16)
            session['download_session_id'] = session_id
            
            # Start download in background thread
            thread = threading.Thread(target=download_file_from_url, args=(download_url, session_id))
            thread.daemon = True
            thread.start()
            
            return redirect(url_for('download_progress_page', session_id=session_id))
        
        elif upload_type == 'multi_url':
            # Handle multiple URL downloads
            urls_text = request.form.get('urls_text', '').strip()
            if not urls_text:
                flash('Please provide at least one URL', 'error')
                return redirect(request.url)
            
            # Split URLs by whitespace and filter empty lines
            urls = [url.strip() for url in re.split(r'[\s,]+', urls_text) if url.strip()]
            
            if not urls:
                flash('No valid URLs found', 'error')
                return redirect(request.url)
            
            if len(urls) > MAX_URLS:
                flash(f'Maximum {MAX_URLS} URLs allowed at once', 'error')
                return redirect(request.url)
            
            # Generate unique session ID for batch progress tracking
            session_id = secrets.token_hex(16)
            session['batch_download_session_id'] = session_id
            
            # Start batch download in background thread
            thread = threading.Thread(target=batch_download_files, args=(urls, session_id))
            thread.daemon = True
            thread.start()
            
            return redirect(url_for('batch_download_progress_page', session_id=session_id))
        
        else:
            # Handle file upload
            if 'file' not in request.files:
                if is_ajax:
                    return {'status': 'error', 'message': 'No file selected'}, 400
                flash('No file selected', 'error')
                return redirect(request.url)
            
            file = request.files['file']
            if file.filename == '':
                if is_ajax:
                    return {'status': 'error', 'message': 'No file selected'}, 400
                flash('No file selected', 'error')
                return redirect(request.url)
            
            if file:
                filename = secure_filename(file.filename)
                if not filename.lower().endswith('.txt'):
                    if is_ajax:
                        return {'status': 'error', 'message': 'Only text (.txt) files are supported'}, 400
                    flash('Only text (.txt) files are supported', 'error')
                    return redirect(request.url)
                if request.content_length and request.content_length > MAX_UPLOAD_SIZE_BYTES:
                    if is_ajax:
                        return {'status': 'error', 'message': 'File exceeds the 80 GB upload limit'}, 413
                    flash('File exceeds the 80 GB upload limit', 'error')
                    return redirect(request.url)
                
                # Save to global upload directory
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(file_path)
                
                # Get file size
                file_size = os.path.getsize(file_path)
                if file_size > MAX_UPLOAD_SIZE_BYTES:
                    os.remove(file_path)
                    if is_ajax:
                        return {'status': 'error', 'message': 'File exceeds the 80 GB upload limit'}, 413
                    flash('File exceeds the 80 GB upload limit', 'error')
                    return redirect(request.url)
                
                # Save to database
                save_file_record(filename, file_size, file_path)
                
                if is_ajax:
                    return {
                        'status': 'complete',
                        'message': f'File "{filename}" uploaded successfully. Auto-delete in 24 hours.',
                        'redirect': url_for('files')
                    }
                flash(f'File "{filename}" uploaded successfully! Will auto-delete after 24 hours.', 'success')
                return redirect(url_for('files'))
    
    return render_template('upload.html')


def download_file_from_url(url, session_id):
    """Download file from URL with progress tracking"""
    try:
        file_path = None
        # Initialize progress
        init_download_task(session_id, mode='single', total_urls=1)
        update_download_progress(session_id, status='downloading')
        append_task_log(download_progress, session_id, f"Starting download: {url}", download_progress_lock)
        control = ensure_download_task_control(session_id)
        cancel_event = control['cancel_event']
        
        # Start download
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        # Get filename from URL or Content-Disposition header
        filename = None
        if 'Content-Disposition' in response.headers:
            content_disp = response.headers['Content-Disposition']
            filename_match = re.findall('filename="?([^"]+)"?', content_disp)
            if filename_match:
                filename = filename_match[0]
        
        if not filename:
            filename = url.split('/')[-1].split('?')[0] or 'downloaded_file.txt'
        
        filename = secure_filename(filename)
        if not filename.lower().endswith('.txt'):
            filename += '.txt'
        
        # Get total file size
        total_size = int(response.headers.get('content-length', 0))
        if total_size and total_size > MAX_UPLOAD_SIZE_BYTES:
            raise ValueError('File exceeds the 80 GB upload limit')
        update_download_progress(session_id, total_size=total_size, filename=filename)
        
        # Download file
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        downloaded = 0
        start_time = time.time()
        
        with open(file_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if cancel_event.is_set():
                    append_task_log(download_progress, session_id, "Download cancelled by user", download_progress_lock)
                    raise RuntimeError('cancelled')
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > MAX_UPLOAD_SIZE_BYTES:
                        raise ValueError('File exceeds the 80 GB upload limit')
                    
                    # Update progress
                    elapsed_time = time.time() - start_time
                    speed = downloaded / elapsed_time if elapsed_time > 0 else 0
                    progress = (downloaded / total_size * 100) if total_size > 0 else 0
                    eta = (total_size - downloaded) / speed if speed > 0 else 0
                    
                    update_download_progress(
                        session_id,
                        progress=progress,
                        downloaded=downloaded,
                        speed=speed,
                        eta=eta
                    )
        
        # Save to database
        file_size = os.path.getsize(file_path)
        save_file_record(filename, file_size, file_path)
        
        # Mark as complete
        update_download_progress(session_id, status='complete', progress=100)
        append_task_log(download_progress, session_id, f"Download complete: {filename}", download_progress_lock)
        
    except Exception as e:
        logger.error(f"Error downloading file from URL: {e}")
        status = 'cancelled' if str(e) == 'cancelled' else 'error'
        update_download_progress(session_id, status=status, error=str(e))
        append_task_log(download_progress, session_id, f"Download failed: {e}", download_progress_lock)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as cleanup_error:
                logger.error(f"Error cleaning up failed download {file_path}: {cleanup_error}")


def batch_download_files(urls, session_id):
    """Download multiple files from URLs"""
    try:
        init_download_task(session_id, mode='batch', total_urls=len(urls))
        update_download_progress(session_id, status='downloading')
        append_task_log(download_progress, session_id, f"Starting batch download: {len(urls)} URLs", download_progress_lock)
        control = ensure_download_task_control(session_id)
        cancel_event = control['cancel_event']
        
        for idx, url in enumerate(urls):
            if cancel_event.is_set():
                append_task_log(download_progress, session_id, "Batch download cancelled by user", download_progress_lock)
                raise RuntimeError('cancelled')
            try:
                file_path = None
                update_download_progress(session_id, current_file=url)
                
                response = requests.get(url, stream=True, timeout=30)
                response.raise_for_status()
                
                filename = None
                if 'Content-Disposition' in response.headers:
                    content_disp = response.headers['Content-Disposition']
                    filename_match = re.findall('filename="?([^"]+)"?', content_disp)
                    if filename_match:
                        filename = filename_match[0]
                
                if not filename:
                    filename = url.split('/')[-1].split('?')[0] or f'downloaded_file_{idx+1}.txt'
                
                filename = secure_filename(filename)
                if not filename.lower().endswith('.txt'):
                    filename += '.txt'

                total_size = int(response.headers.get('content-length', 0))
                if total_size and total_size > MAX_UPLOAD_SIZE_BYTES:
                    raise ValueError('File exceeds the 80 GB upload limit')
                
                # Make filename unique if it already exists
                base_name = filename[:-4]
                ext = '.txt'
                counter = 1
                while os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], filename)):
                    filename = f"{base_name}_{counter}{ext}"
                    counter += 1
                
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                
                with open(file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if cancel_event.is_set():
                            append_task_log(download_progress, session_id, "Batch download cancelled by user", download_progress_lock)
                            raise RuntimeError('cancelled')
                        if chunk:
                            f.write(chunk)
                
                file_size = os.path.getsize(file_path)
                save_file_record(filename, file_size, file_path)
                
                with download_progress_lock:
                    download_progress[session_id]['completed'] += 1
                    download_progress[session_id]['files'].append({'name': filename, 'status': 'success'})
                append_task_log(download_progress, session_id, f"Downloaded: {filename}", download_progress_lock)
                
            except Exception as e:
                logger.error(f"Error downloading {url}: {e}")
                with download_progress_lock:
                    download_progress[session_id]['failed'] += 1
                    download_progress[session_id]['files'].append({'name': url, 'status': 'failed', 'error': str(e)})
                append_task_log(download_progress, session_id, f"Failed: {url} ({e})", download_progress_lock)
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as cleanup_error:
                        logger.error(f"Error cleaning up failed download {file_path}: {cleanup_error}")

            with download_progress_lock:
                completed = download_progress[session_id]['completed']
                total = download_progress[session_id]['total_urls']
            update_download_progress(session_id, progress=(completed / total * 100) if total else 0)
        
        update_download_progress(session_id, status='complete')
        append_task_log(download_progress, session_id, "Batch download complete", download_progress_lock)
        
    except Exception as e:
        logger.error(f"Error in batch download: {e}")
        status = 'cancelled' if str(e) == 'cancelled' else 'error'
        update_download_progress(session_id, status=status, error=str(e))
        append_task_log(download_progress, session_id, f"Batch download failed: {e}", download_progress_lock)


@app.route('/download_progress/<session_id>')
def download_progress_page(session_id):
    """Show download progress page"""
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    return render_template('download_progress.html', session_id=session_id)


@app.route('/batch_download_progress/<session_id>')
def batch_download_progress_page(session_id):
    """Show batch download progress page"""
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    return render_template('batch_download_progress.html', session_id=session_id)


@app.route('/api/download_progress/<session_id>')
def get_download_progress(session_id):
    """API endpoint to get download progress"""
    if not is_authenticated():
        return {'error': 'Not authenticated'}, 401
    
    progress = download_progress.get(session_id, {
        'status': 'not_found',
        'progress': 0,
        'error': 'Session not found'
    })
    
    return progress


@app.route('/api/batch_download_progress/<session_id>')
def get_batch_download_progress(session_id):
    """API endpoint to get batch download progress"""
    if not is_authenticated():
        return {'error': 'Not authenticated'}, 401
    
    progress = download_progress.get(session_id, {
        'status': 'not_found',
        'error': 'Session not found'
    })
    
    return progress


@app.route('/api/cancel_download/<session_id>', methods=['POST'])
def cancel_download(session_id):
    if not is_authenticated():
        return {'error': 'Not authenticated'}, 401

    if session_id not in download_progress:
        return {'error': 'Session not found'}, 404
    control = ensure_download_task_control(session_id)
    control['cancel_event'].set()
    if session_id in download_progress and download_progress[session_id]['status'] in {'downloading', 'queued'}:
        update_download_progress(session_id, status='cancelled', error='cancelled')
    append_task_log(download_progress, session_id, 'Cancellation requested', download_progress_lock)
    return {'status': 'cancelled'}


@app.route('/api/cancel_search/<task_id>', methods=['POST'])
def cancel_search(task_id):
    if not is_authenticated():
        return {'error': 'Not authenticated'}, 401

    if not get_search_task(task_id):
        return {'error': 'Task not found'}, 404
    control = ensure_search_task_control(task_id)

    control['cancel_event'].set()
    update_search_task_status(task_id, 'cancelled')
    append_task_log(search_task_controls, task_id, 'Cancellation requested', search_task_controls_lock)
    return {'status': 'cancelled'}


@app.route('/api/delete_search/<task_id>', methods=['POST'])
def delete_search(task_id):
    if not is_authenticated():
        return {'error': 'Not authenticated'}, 401

    if not get_search_task(task_id):
        return {'error': 'Task not found'}, 404

    delete_search_task_record(task_id)
    with search_task_lock:
        search_tasks.pop(task_id, None)
    with search_task_controls_lock:
        search_task_controls.pop(task_id, None)
    return {'status': 'deleted'}


@app.route('/api/start_batch_download', methods=['POST'])
def start_batch_download():
    """API endpoint to start batch download"""
    if not is_authenticated():
        return {'error': 'Not authenticated'}, 401
    
    data = request.get_json()
    urls = data.get('urls', [])
    
    if not urls:
        return {'error': 'No URLs provided'}, 400
    
    if len(urls) > MAX_URLS:
        return {'error': f'Maximum {MAX_URLS} URLs allowed'}, 400
    
    # Generate session ID
    session_id = secrets.token_hex(16)
    
    # Start batch download in background
    thread = threading.Thread(target=batch_download_files, args=(urls, session_id))
    thread.daemon = True
    thread.start()
    
    return {'session_id': session_id}


@app.route('/upload_progress')
def upload_progress():
    # Currently just returns static data for progress bar display on upload page
    return render_template('upload.html', show_progress=True)


@app.route('/search', methods=['GET', 'POST'])
def search():
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    if request.method == 'POST':
        search_type = request.form.get('search_type')
        query = request.form.get('query')
        include_schemes = request.form.get('include_schemes') == 'on'
        
        if not query:
            flash('Please enter a search query', 'error')
            return redirect(request.url)
        
        # Create search task
        task_id = create_search_task(query, search_type, include_schemes)
        
        # Start processing in background
        thread = threading.Thread(target=process_search_task, args=(task_id,))
        thread.daemon = True
        thread.start()
        
        flash('Search task created! Check "My Searches" to view progress.', 'success')
        return redirect(url_for('my_searches'))
    
    return render_template('search.html')


@app.route('/my_searches')
def my_searches():
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    tasks = get_all_search_tasks()
    return render_template('my_searches.html', tasks=tasks)


@app.route('/api/search_task_status/<task_id>')
def get_search_task_status(task_id):
    if not is_authenticated():
        return {'error': 'Not authenticated'}, 401
    
    task = get_search_task(task_id)
    if not task:
        return {'error': 'Task not found'}, 404
    
    task_id, query, search_type, include_schemes, status, created_at, completed_at, result_count, error_message, total_files, processed_files, total_bytes, processed_bytes, progress = task
    control = ensure_search_task_control(task_id)
    logs = control.get('logs', [])
    
    return {
        'id': task_id,
        'query': query,
        'search_type': search_type,
        'status': status,
        'result_count': result_count,
        'error_message': error_message,
        'total_files': total_files,
        'processed_files': processed_files,
        'total_bytes': total_bytes,
        'processed_bytes': processed_bytes,
        'progress': progress,
        'logs': logs
    }


@app.route('/view_search_results/<task_id>')
def view_search_results(task_id):
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    task = get_search_task(task_id)
    if not task:
        flash('Search task not found', 'error')
        return redirect(url_for('my_searches'))
    
    task_id_db, query, search_type, include_schemes, status, created_at, completed_at, result_count, error_message, total_files, processed_files, total_bytes, processed_bytes, progress = task
    
    if status != 'completed':
        flash('Search is not completed yet', 'error')
        return redirect(url_for('my_searches'))
    
    # Get results from memory
    with search_task_lock:
        results = search_tasks.get(task_id, {'all': [], 'user_pass': [], 'url_user_pass': [], 'urls': [], 'erc_tokens': []})
    
    # Store in session for download
    session['current_task_id'] = task_id
    
    return render_template(
        'search_results.html',
        results=results['all'],
        user_pass_results=results.get('user_pass', []),
        url_user_pass_results=results.get('url_user_pass', []),
        url_results=results.get('urls', []),
        erc_token_results=results.get('erc_tokens', []),
        query=query,
        search_type=search_type,
        include_schemes=include_schemes,
        task_id=task_id
    )


@app.route('/files')
def files():
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    files = get_global_files()
    selected_files = get_selected_files()
    
    # Format file sizes for display
    formatted_files = []
    for file_id, file_name, file_size, file_path, upload_date in files:
        size_mb = file_size / (1024 * 1024) if file_size else 0
        formatted_files.append((file_id, file_name, f"{size_mb:.2f} MB" if file_size else "Unknown", file_path, upload_date))
    
    # Extract just the IDs of selected files for template use
    selected_file_ids = [f[0] for f in selected_files]
    
    return render_template('files.html', files=formatted_files, selected_files=selected_files, selected_file_ids=selected_file_ids)


@app.route('/toggle_file_selection/<int:file_id>', methods=['POST'])
def toggle_file_selection(file_id):
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    # Get current selection status
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT is_selected FROM global_files WHERE id = ?', (file_id,))
    result = cursor.fetchone()
    
    if result:
        current_status = result[0]
        new_status = 0 if current_status == 1 else 1
        update_file_selection(file_id, new_status)
        flash(f'File selection updated successfully', 'success')
    else:
        flash('File not found', 'error')
    
    conn.close()
    return redirect(url_for('files'))


@app.route('/toggle_all_files', methods=['POST'])
def toggle_all_files():
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    # Get the action from form data
    action = request.form.get('action', 'toggle')
    
    if action == 'select_all':
        update_all_files_selection(1)  # Select all
        flash('All files selected for search', 'success')
    elif action == 'deselect_all':
        update_all_files_selection(0)  # Deselect all
        flash('All files deselected from search', 'success')
    
    return redirect(url_for('files'))


@app.route('/file/<int:file_id>')
def view_file(file_id):
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    # Get file info from database
    file_info = get_file_by_id(file_id)
    
    if not file_info:
        abort(404)
    
    # Read file content
    try:
        with open(file_info['file_path'], 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception as e:
        logger.error(f"Error reading file: {e}")
        abort(500)
    
    return render_template('view_file.html', content=content, file_name=file_info['file_name'], file_id=file_id)


@app.route('/download/<int:file_id>')
def download_file(file_id):
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    # Get file info from database
    file_info = get_file_by_id(file_id)
    
    if not file_info:
        abort(404)
    
    return send_file(file_info['file_path'], as_attachment=True, download_name=file_info['file_name'])


@app.route('/delete/<int:file_id>', methods=['POST'])
def delete_file(file_id):
    # Get file info from database
    file_info = get_file_by_id(file_id)
    
    if file_info:
        # Delete file from filesystem
        try:
            if os.path.exists(file_info['file_path']):
                os.remove(file_info['file_path'])
                logger.info(f"File {file_info['file_path']} deleted from filesystem")
        except Exception as e:
            logger.error(f"Error deleting file {file_info['file_path']}: {e}")
        
        # Delete record from database
        delete_file_record(file_id)
        flash('File deleted successfully', 'success')
    else:
        flash('File not found', 'error')
    
    return redirect(url_for('files'))


# Removed settings functionality since it was user-specific


# Removed save_search_results functionality since it was user-specific


@app.route('/download_results/<task_id>/<search_type>')
def download_results(task_id, search_type):
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    # Get results from memory
    with search_task_lock:
        results = search_tasks.get(task_id, {'all': [], 'user_pass': [], 'url_user_pass': [], 'urls': [], 'erc_tokens': []})
    
    # Get task info
    task = get_search_task(task_id)
    if not task:
        flash('Search task not found', 'error')
        return redirect(url_for('my_searches'))
    
    _, query, _, _, _, _, _, _, _, _, _, _, _, _ = task
    
    # Select the appropriate result type
    if search_type == 'user_pass':
        result_data = results.get('user_pass', [])
    elif search_type == 'url_user_pass':
        result_data = results.get('url_user_pass', [])
    elif search_type == 'urls':
        result_data = results.get('urls', [])
    elif search_type == 'erc_tokens':
        result_data = results.get('erc_tokens', [])
    else:
        result_data = results.get('all', [])
    
    # Create temp file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Generate filename based on result type
    if search_type == 'user_pass':
        filename = f"user_pass_results_{query.replace(' ', '_')}_{timestamp}.txt"
    elif search_type == 'url_user_pass':
        filename = f"url_user_pass_results_{query.replace(' ', '_')}_{timestamp}.txt"
    elif search_type == 'urls':
        filename = f"url_results_{query.replace(' ', '_')}_{timestamp}.txt"
    elif search_type == 'erc_tokens':
        filename = f"erc_token_results_{query.replace(' ', '_')}_{timestamp}.txt"
    else:
        filename = f"search_results_{query.replace(' ', '_')}_{timestamp}.txt"
    
    # Create temporary file
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as temp_file:
        for result in result_data:
            temp_file.write(result + '\n')
        temp_file_path = temp_file.name
    
    # Return the temporary file for download
    return send_file(temp_file_path, as_attachment=True, download_name=filename, 
                     mimetype='text/plain', conditional=True)


@app.route('/download_results_zip/<task_id>')
def download_results_zip(task_id):
    if not is_authenticated():
        return redirect(url_for('access_key'))

    with search_task_lock:
        results = search_tasks.get(task_id, {'all': [], 'user_pass': [], 'url_user_pass': [], 'urls': [], 'erc_tokens': []})

    task = get_search_task(task_id)
    if not task:
        flash('Search task not found', 'error')
        return redirect(url_for('my_searches'))

    _, query, _, _, _, _, _, _, _, _, _, _, _, _ = task
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"search_results_{query.replace(' ', '_')}_{timestamp}.zip"

    temp_dir = tempfile.mkdtemp()
    files_to_write = {
        'all_results.txt': results.get('all', []),
        'user_pass.txt': results.get('user_pass', []),
        'url_user_pass.txt': results.get('url_user_pass', []),
        'urls.txt': results.get('urls', []),
        'erc_tokens.txt': results.get('erc_tokens', [])
    }

    try:
        for filename, data in files_to_write.items():
            file_path = os.path.join(temp_dir, filename)
            with open(file_path, 'w') as f:
                for line in data:
                    f.write(line + '\n')

        zip_path = os.path.join(temp_dir, zip_name)
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for filename in files_to_write.keys():
                zf.write(os.path.join(temp_dir, filename), arcname=filename)

        return send_file(zip_path, as_attachment=True, download_name=zip_name,
                         mimetype='application/zip', conditional=True)
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception as cleanup_error:
            logger.error(f"Error cleaning up temp zip dir {temp_dir}: {cleanup_error}")


# Template rendering functions
@app.context_processor
def inject_user():
    # No user-specific data in global mode
    return dict(logged_in_user='Public User')

@app.context_processor
def inject_functions():
    return dict(get_selected_files=get_selected_files)


# Create templates directory if it doesn't exist
if not os.path.exists('templates'):
    os.makedirs('templates')


# Write HTML templates
def write_templates():
    # Base template
    base_template = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}Credential Manager{% endblock %}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        :root {
            --bg: #f6f3ed;
            --panel: #ffffff;
            --ink: #17181c;
            --muted: #6b7280;
            --accent: #0ea5a4;
            --accent-2: #f59e0b;
            --danger: #e11d48;
            --border: rgba(20, 20, 22, 0.08);
            --shadow: 0 12px 30px rgba(17, 24, 39, 0.08);
        }
        body {
            font-family: "Space Grotesk", sans-serif;
            color: var(--ink);
            background: radial-gradient(circle at top left, rgba(14, 165, 164, 0.08), transparent 45%),
                        radial-gradient(circle at 15% 60%, rgba(245, 158, 11, 0.08), transparent 50%),
                        var(--bg);
        }
        code, pre {
            font-family: "JetBrains Mono", monospace;
        }
        .app-shell {
            min-height: 100vh;
            display: grid;
            grid-template-columns: 260px 1fr;
        }
        .sidebar {
            background: linear-gradient(180deg, rgba(15, 23, 42, 0.96), rgba(15, 23, 42, 0.86));
            color: #f8fafc;
            padding: 1.5rem 1rem;
            position: sticky;
            top: 0;
            height: 100vh;
        }
        .sidebar .brand {
            font-weight: 700;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            font-size: 0.85rem;
            color: rgba(248, 250, 252, 0.7);
            margin-bottom: 2rem;
        }
        .nav-link {
            color: rgba(248, 250, 252, 0.72);
            border-radius: 14px;
            margin-bottom: 0.25rem;
            padding: 0.75rem 1rem;
        }
        .nav-link.active, .nav-link:hover {
            background: rgba(248, 250, 252, 0.12);
            color: #f8fafc;
        }
        .main-content {
            padding: 2rem 2.5rem 3rem;
        }
        .topbar {
            background: rgba(255, 255, 255, 0.8);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 0.75rem 1.25rem;
            box-shadow: var(--shadow);
        }
        .card {
            border-radius: 18px;
            border: 1px solid var(--border);
            box-shadow: var(--shadow);
            background: var(--panel);
        }
        .card-header {
            background: transparent;
            border-bottom: 1px solid var(--border);
            padding: 1rem 1.25rem;
        }
        .btn-primary {
            background-color: var(--accent);
            border-color: var(--accent);
        }
        .btn-primary:hover {
            background-color: #0c8d8c;
            border-color: #0c8d8c;
        }
        .btn-warning {
            background-color: var(--accent-2);
            border-color: var(--accent-2);
        }
        .badge-soft {
            background: rgba(14, 165, 164, 0.15);
            color: var(--accent);
            border-radius: 999px;
            padding: 0.35rem 0.75rem;
            font-weight: 600;
        }
        .section-title {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            font-size: 1.15rem;
            font-weight: 600;
        }
        .text-muted {
            color: var(--muted) !important;
        }
        .progress {
            border-radius: 999px;
            background: rgba(15, 23, 42, 0.08);
        }
        .progress-bar {
            background: linear-gradient(90deg, var(--accent), #22d3ee);
        }
        .status-pill {
            border-radius: 999px;
            padding: 0.25rem 0.75rem;
            font-size: 0.8rem;
            font-weight: 600;
        }
        .status-queued { background: rgba(245, 158, 11, 0.15); color: #b45309; }
        .status-processing { background: rgba(14, 165, 164, 0.18); color: #0f766e; }
        .status-completed { background: rgba(16, 185, 129, 0.2); color: #047857; }
        .status-error { background: rgba(225, 29, 72, 0.15); color: var(--danger); }
        .status-cancelled { background: rgba(107, 114, 128, 0.2); color: #374151; }
        .code-block {
            background: rgba(15, 23, 42, 0.08);
            border-radius: 14px;
            padding: 1rem;
        }
        .nav-tabs {
            flex-wrap: wrap;
            gap: 0.35rem;
        }
        .nav-tabs .nav-link {
            border-radius: 999px;
        }
        .btn-group {
            display: flex;
            flex-wrap: wrap;
            gap: 0.35rem;
        }
        .btn-group .btn {
            border-radius: 999px;
        }
        .table td .btn-group {
            justify-content: flex-start;
        }
        .form-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
        }
        @media (max-width: 992px) {
            .app-shell {
                grid-template-columns: 1fr;
            }
            .sidebar {
                position: relative;
                height: auto;
                border-radius: 0 0 24px 24px;
            }
            .main-content {
                padding: 1.5rem 1.25rem 2rem;
            }
            .topbar {
                flex-direction: column;
                align-items: flex-start !important;
                gap: 0.75rem;
            }
            .sidebar .nav {
                flex-direction: row;
                flex-wrap: nowrap;
                overflow-x: auto;
                gap: 0.25rem;
            }
            .sidebar .nav-link {
                white-space: nowrap;
            }
            .btn-group {
                flex-wrap: wrap;
                gap: 0.35rem;
            }
            .table {
                font-size: 0.9rem;
            }
            .card-header, .card-body {
                padding: 1rem;
            }
            .progress {
                height: 12px;
            }
        }
        @media (max-width: 576px) {
            .btn {
                width: 100%;
            }
            .btn-group .btn,
            .form-actions .btn {
                width: auto;
                flex: 1 1 auto;
            }
            .table-responsive {
                border-radius: 14px;
            }
        }
    </style>
</head>
<body>
    <div class="app-shell">
        <aside class="sidebar">
            <div class="brand">Credential Vault</div>
            <ul class="nav flex-column">
                <li class="nav-item">
                    <a class="nav-link {% if request.endpoint == 'dashboard' %}active{% endif %}" href="{{ url_for('dashboard') }}">
                        <i class="fas fa-tachometer-alt me-2"></i>Dashboard
                    </a>
                </li>
                <li class="nav-item">
                    <a class="nav-link {% if request.endpoint == 'upload' %}active{% endif %}" href="{{ url_for('upload') }}">
                        <i class="fas fa-upload me-2"></i>Upload
                    </a>
                </li>
                <li class="nav-item">
                    <a class="nav-link {% if request.endpoint == 'search' %}active{% endif %}" href="{{ url_for('search') }}">
                        <i class="fas fa-search me-2"></i>Search
                    </a>
                </li>
                <li class="nav-item">
                    <a class="nav-link {% if request.endpoint == 'files' %}active{% endif %}" href="{{ url_for('files') }}">
                        <i class="fas fa-folder me-2"></i>Files
                    </a>
                </li>
                <li class="nav-item">
                    <a class="nav-link {% if request.endpoint == 'my_searches' %}active{% endif %}" href="{{ url_for('my_searches') }}">
                        <i class="fas fa-wave-square me-2"></i>Tasks
                    </a>
                </li>
            </ul>
        </aside>

        <main class="main-content">
            <div class="topbar d-flex align-items-center justify-content-between mb-4">
                <div class="d-flex align-items-center gap-2">
                    <i class="fas fa-shield-alt text-muted"></i>
                    <span class="fw-semibold">Credential Manager</span>
                    <span class="badge-soft">Live</span>
                </div>
                <div class="d-flex align-items-center gap-3">
                    {% if logged_in_user %}
                    <span class="text-muted">Hello, <strong>{{ logged_in_user }}</strong></span>
                    {% endif %}
                    <a href="{{ url_for('logout') }}" class="btn btn-outline-secondary btn-sm">Logout</a>
                </div>
            </div>

            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ 'danger' if category == 'error' else 'warning' if category == 'info' else 'success' }} alert-dismissible fade show" role="alert">
                            {{ message }}
                            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                        </div>
                    {% endfor %}
                {% endif %}
            {% endwith %}

            {% block content %}{% endblock %}
        </main>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    {% block scripts %}{% endblock %}
</body>
</html>'''
    
    # Login template
    login_template = '''{% extends "base.html" %}
{% block title %}Login - Credential Manager{% endblock %}
{% block content %}
<div class="container d-flex align-items-center justify-content-center" style="min-height: 70vh;">
    <div class="col-md-6 col-lg-4">
        <div class="card shadow">
            <div class="card-header text-center">
                <h3><i class="fas fa-lock me-2"></i>Login</h3>
            </div>
            <div class="card-body">
                <form method="POST">
                    <div class="mb-3">
                        <label for="username" class="form-label">Username</label>
                        <input type="text" class="form-control" id="username" name="username" required>
                    </div>
                    <div class="mb-3">
                        <label for="password" class="form-label">Password</label>
                        <input type="password" class="form-control" id="password" name="password" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">Login</button>
                </form>
                <div class="text-center mt-3">
                    <p>Don't have an account? <a href="{{ url_for('register') }}">Register here</a></p>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}'''
    
    # Register template
    register_template = '''{% extends "base.html" %}
{% block title %}Register - Credential Manager{% endblock %}
{% block content %}
<div class="container d-flex align-items-center justify-content-center" style="min-height: 70vh;">
    <div class="col-md-6 col-lg-4">
        <div class="card shadow">
            <div class="card-header text-center">
                <h3><i class="fas fa-user-plus me-2"></i>Register</h3>
            </div>
            <div class="card-body">
                <form method="POST">
                    <div class="mb-3">
                        <label for="username" class="form-label">Username</label>
                        <input type="text" class="form-control" id="username" name="username" required>
                    </div>
                    <div class="mb-3">
                        <label for="first_name" class="form-label">First Name (Optional)</label>
                        <input type="text" class="form-control" id="first_name" name="first_name">
                    </div>
                    <div class="mb-3">
                        <label for="last_name" class="form-label">Last Name (Optional)</label>
                        <input type="text" class="form-control" id="last_name" name="last_name">
                    </div>
                    <div class="mb-3">
                        <label for="password" class="form-label">Password</label>
                        <input type="password" class="form-control" id="password" name="password" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">Register</button>
                </form>
                <div class="text-center mt-3">
                    <p>Already have an account? <a href="{{ url_for('login') }}">Login here</a></p>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}'''
    
    # Dashboard template
    dashboard_template = '''{% extends "base.html" %}
{% block title %}Dashboard - Credential Manager{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center mb-4">
    <div>
        <h1 class="h2 mb-1">Dashboard</h1>
        <p class="text-muted mb-0">Monitor storage, uploads, and search activity in real time.</p>
    </div>
</div>

<div class="row g-3 mb-4">
    <div class="col-md-4">
        <div class="card">
            <div class="card-body">
                <div class="section-title"><i class="fas fa-database text-muted"></i> Total Files</div>
                <h3 class="mt-2">{{ total_files }}</h3>
                <p class="text-muted mb-0">Auto-deleted after 24 hours.</p>
            </div>
        </div>
    </div>
    <div class="col-md-4">
        <div class="card">
            <div class="card-body">
                <div class="section-title"><i class="fas fa-chart-pie text-muted"></i> Storage Used</div>
                <h3 class="mt-2">{{ (used_storage / (1024 * 1024 * 1024))|round(2) }} GB</h3>
                <p class="text-muted mb-0">Across all uploads.</p>
            </div>
        </div>
    </div>
    <div class="col-md-4">
        <div class="card">
            <div class="card-body">
                <div class="section-title"><i class="fas fa-hdd text-muted"></i> Free Space</div>
                <h3 class="mt-2">{{ (free_storage / (1024 * 1024 * 1024))|round(2) }} GB</h3>
                <p class="text-muted mb-0">System-wide free storage.</p>
            </div>
        </div>
    </div>
</div>

<div class="row">
    <div class="col-md-6 mb-4">
        <div class="card feature-card">
            <div class="card-body text-center">
                <div class="bg-primary text-white rounded-circle d-inline-flex align-items-center justify-content-center" style="width: 60px; height: 60px;">
                    <i class="fas fa-upload fa-2x"></i>
                </div>
                <h5 class="card-title mt-3">Upload Files</h5>
                <p class="card-text">Upload credential files up to 80 GB to local storage for searching and management.</p>
                <a href="{{ url_for('upload') }}" class="btn btn-primary">Upload Now</a>
            </div>
        </div>
    </div>
    <div class="col-md-6 mb-4">
        <div class="card feature-card">
            <div class="card-body text-center">
                <div class="bg-success text-white rounded-circle d-inline-flex align-items-center justify-content-center" style="width: 60px; height: 60px;">
                    <i class="fas fa-search fa-2x"></i>
                </div>
                <h5 class="card-title mt-3">Search Credentials</h5>
                <p class="card-text">Find specific credentials by domain or keyword</p>
                <a href="{{ url_for('search') }}" class="btn btn-success">Search Now</a>
            </div>
        </div>
    </div>
    <div class="col-md-6 mb-4">
        <div class="card feature-card">
            <div class="card-body text-center">
                <div class="bg-warning text-white rounded-circle d-inline-flex align-items-center justify-content-center" style="width: 60px; height: 60px;">
                    <i class="fas fa-folder fa-2x"></i>
                </div>
                <h5 class="card-title mt-3">My Files</h5>
                <p class="card-text">Manage your uploaded credential files in local storage</p>
                <a href="{{ url_for('files') }}" class="btn btn-warning">View Files</a>
            </div>
        </div>
    </div>

</div>

<div class="row mt-5">
    <div class="col-md-6">
        <div class="card">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-history me-2"></i>Recent Files</h5>
            </div>
            <div class="card-body">
                {% if files %}
                <div class="list-group">
                    {% for file_id, file_name, file_size, file_path, upload_date in files %}
                    <a href="{{ url_for('view_file', file_id=file_id) }}" class="list-group-item list-group-item-action">
                        <div class="d-flex w-100 justify-content-between">
                            <h6 class="mb-1">{{ file_name }}</h6>
                            <small>{{ file_size }}</small>
                        </div>
                        <small>Uploaded on {{ upload_date[:10] }}</small>
                    </a>
                    {% endfor %}
                </div>
                {% else %}
                <p class="text-muted">No files uploaded yet.</p>
                {% endif %}
            </div>
        </div>
    </div>
    <div class="col-md-6">
        <div class="card">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-clock me-2"></i>Recent Searches</h5>
            </div>
            <div class="card-body">
                {% if history %}
                <div class="list-group">
                    {% for query, search_type, result_count, search_date in history %}
                    <div class="list-group-item">
                        <div class="d-flex w-100 justify-content-between">
                            <h6 class="mb-1">{{ query }}</h6>
                            <small class="text-muted">{{ search_type.title() }}</small>
                        </div>
                        <p class="mb-1">Results: {{ result_count }}</p>
                        <small class="text-muted">{{ search_date[:16] }}</small>
                    </div>
                    {% endfor %}
                </div>
                {% else %}
                <p class="text-muted">No search history yet.</p>
                {% endif %}
            </div>
        </div>
    </div>
</div>
{% endblock %}'''
    
    # Upload template
    upload_template = '''{% extends "base.html" %}
{% block title %}Upload File - Credential Manager{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center mb-4">
    <div>
        <h1 class="h2 mb-1">Upload</h1>
        <p class="text-muted mb-0">Upload files or queue downloads from direct links.</p>
    </div>
</div>

<div class="row justify-content-center">
    <div class="col-md-8">
        <div class="card">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-file-upload me-2"></i>Upload Credentials File</h5>
            </div>
            <div class="card-body">
                <!-- Upload Type Selector -->
                <ul class="nav nav-tabs mb-3" id="uploadTabs" role="tablist">
                    <li class="nav-item" role="presentation">
                        <button class="nav-link active" id="file-tab" data-bs-toggle="tab" data-bs-target="#file-upload" type="button" role="tab">
                            <i class="fas fa-file me-1"></i>Upload File
                        </button>
                    </li>
                    <li class="nav-item" role="presentation">
                        <button class="nav-link" id="url-tab" data-bs-toggle="tab" data-bs-target="#url-download" type="button" role="tab">
                            <i class="fas fa-link me-1"></i>Download from URL
                        </button>
                    </li>
                    <li class="nav-item" role="presentation">
                        <button class="nav-link" id="multi-url-tab" data-bs-toggle="tab" data-bs-target="#multi-url-download" type="button" role="tab">
                            <i class="fas fa-link me-1"></i>Multiple URLs
                        </button>
                    </li>
                </ul>

                <div class="tab-content" id="uploadTabContent">
                    <!-- File Upload Tab -->
                    <div class="tab-pane fade show active" id="file-upload" role="tabpanel">
                        <form method="POST" enctype="multipart/form-data" id="uploadForm">
                            <input type="hidden" name="upload_type" value="file">
                            <div class="mb-3">
                                <label for="file" class="form-label">Choose a text file to upload</label>
                                <input type="file" class="form-control" id="file" name="file" accept=".txt" required>
                                <div class="form-text">Only text files (.txt) up to 80 GB. Files auto-delete after 24 hours.</div>
                            </div>
                            <button type="submit" class="btn btn-primary" id="uploadBtn">
                                <i class="fas fa-upload me-1"></i>Upload File
                            </button>
                            <a href="{{ url_for('dashboard') }}" class="btn btn-secondary">Cancel</a>
                        </form>
                        <div class="mt-4" id="uploadProgressCard" style="display:none;">
                            <div class="card">
                                <div class="card-body">
                                    <div class="d-flex justify-content-between align-items-center mb-2">
                                        <div class="section-title"><i class="fas fa-cloud-upload-alt text-muted"></i> Upload Progress</div>
                                        <span class="status-pill status-processing" id="uploadStatus">Uploading</span>
                                    </div>
                                    <div class="progress mb-2" style="height: 14px;">
                                        <div id="uploadProgressBar" class="progress-bar" role="progressbar" style="width: 0%;"></div>
                                    </div>
                                    <div class="d-flex justify-content-between text-muted small">
                                        <span id="uploadProgressText">0%</span>
                                        <span id="uploadSpeed">0 MB/s</span>
                                    </div>
                                    <div class="mt-3 code-block small" id="uploadLog"></div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- URL Download Tab -->
                    <div class="tab-pane fade" id="url-download" role="tabpanel">
                        <form method="POST" id="urlDownloadForm">
                            <input type="hidden" name="upload_type" value="url">
                            <div class="mb-3">
                                <label for="download_url" class="form-label">Paste Download URL</label>
                                <input type="url" class="form-control" id="download_url" name="download_url"
                                       placeholder="https://example.com/file.txt" required>
                                <div class="form-text">Paste the direct download link to the file. Files auto-delete after 24 hours.</div>
                            </div>
                            <button type="submit" class="btn btn-success" id="downloadBtn">
                                <i class="fas fa-download me-1"></i>Start Download
                            </button>
                            <a href="{{ url_for('dashboard') }}" class="btn btn-secondary">Cancel</a>
                        </form>
                    </div>

                    <!-- Multiple URL Download Tab -->
                    <div class="tab-pane fade" id="multi-url-download" role="tabpanel">
                        <form method="POST" id="multiUrlDownloadForm">
                            <input type="hidden" name="upload_type" value="multi_url">
                            <div class="mb-3">
                                <label for="urls_text" class="form-label">Paste Multiple Download URLs</label>
                                <textarea class="form-control" id="urls_text" name="urls_text" rows="10"
                                          placeholder="https://clck.ru/3QSC88
https://clck.ru/3QSC88
https://clck.ru/3QSC88" required></textarea>
                                <div class="form-text">Enter one URL per line (or paste separated by spaces/commas). Maximum 50 URLs. Files auto-delete after 24 hours.</div>
                            </div>
                            <button type="submit" class="btn btn-success" id="multiDownloadBtn">
                                <i class="fas fa-download me-1"></i>Start Multiple Downloads
                            </button>
                            <a href="{{ url_for('dashboard') }}" class="btn btn-secondary">Cancel</a>
                        </form>
                    </div>
                </div>
            </div>
        </div>

        <div class="card mt-4">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-info-circle me-2"></i>Upload Instructions</h5>
            </div>
            <div class="card-body">
                <ul>
                    <li>Upload credential files in text format or download from URL</li>
                    <li>Files will be stored securely in your account</li>
                    <li>You can search through your uploaded files after uploading</li>
                    <li><strong>Max upload size:</strong> 80 GB per file</li>
                    <li><strong>Auto-deletion:</strong> All files automatically delete after 24 hours</li>
                    <li>Supports various combo formats including decorated headers</li>
                </ul>
            </div>
        </div>
    </div>
</div>
{% endblock %}
{% block scripts %}
<script>
const uploadForm = document.getElementById('uploadForm');
if (uploadForm) {
    uploadForm.addEventListener('submit', function(event) {
        event.preventDefault();
        const progressCard = document.getElementById('uploadProgressCard');
        const progressBar = document.getElementById('uploadProgressBar');
        const progressText = document.getElementById('uploadProgressText');
        const speedText = document.getElementById('uploadSpeed');
        const statusPill = document.getElementById('uploadStatus');
        const logBox = document.getElementById('uploadLog');
        const startTime = Date.now();

        progressCard.style.display = 'block';
        statusPill.textContent = 'Uploading';
        statusPill.className = 'status-pill status-processing';
        logBox.textContent = 'Upload started...';

        const xhr = new XMLHttpRequest();
        xhr.open('POST', uploadForm.action);
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');

        xhr.upload.onprogress = function(evt) {
            if (!evt.lengthComputable) return;
            const percent = Math.round((evt.loaded / evt.total) * 100);
            progressBar.style.width = percent + '%';
            progressText.textContent = percent + '%';
            const elapsed = (Date.now() - startTime) / 1000;
            const speed = evt.loaded / Math.max(elapsed, 0.01);
            speedText.textContent = (speed / (1024 * 1024)).toFixed(2) + ' MB/s';
        };

        xhr.onload = function() {
            if (xhr.status >= 200 && xhr.status < 300) {
                const data = JSON.parse(xhr.responseText || '{}');
                statusPill.textContent = 'Completed';
                statusPill.className = 'status-pill status-completed';
                progressBar.style.width = '100%';
                progressText.textContent = '100%';
                logBox.textContent = data.message || 'Upload completed.';
                if (data.redirect) {
                    setTimeout(() => { window.location.href = data.redirect; }, 1200);
                }
            } else {
                let message = 'Upload failed. Please try again.';
                try {
                    const data = JSON.parse(xhr.responseText || '{}');
                    if (data.message) message = data.message;
                } catch (e) {}
                statusPill.textContent = 'Error';
                statusPill.className = 'status-pill status-error';
                logBox.textContent = message;
            }
        };

        xhr.onerror = function() {
            statusPill.textContent = 'Error';
            statusPill.className = 'status-pill status-error';
            logBox.textContent = 'Network error during upload.';
        };

        xhr.send(new FormData(uploadForm));
    });
}
</script>
{% endblock %}'''
    
    # Search template
    search_template = '''{% extends "base.html" %}
{% block title %}Search - Credential Manager{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center mb-4">
    <div>
        <h1 class="h2 mb-1">Search</h1>
        <p class="text-muted mb-0">Launch a background search task and track progress in real time.</p>
    </div>
    <a href="{{ url_for('my_searches') }}" class="btn btn-outline-secondary">View Tasks</a>
</div>

<div class="row justify-content-center">
    <div class="col-md-8">
        <div class="card">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-search me-2"></i>Search Options</h5>
            </div>
            <div class="card-body">
                <form method="POST">
                    <div class="mb-3">
                        <label for="search_type" class="form-label">Search Type</label>
                        <select class="form-select" id="search_type" name="search_type" required>
                            <option value="domain">Domain Search</option>
                            <option value="keyword">Keyword Search</option>
                        </select>
                    </div>
                    <div class="mb-3">
                        <label for="query" class="form-label">Search Query</label>
                        <input type="text" class="form-control" id="query" name="query" placeholder="Enter domain or keyword to search" required>
                        <div class="form-text">For domain search: enter domain name (e.g., google.com)</div>
                    </div>
                    <div class="form-check mb-3">
                        <input class="form-check-input" type="checkbox" value="on" id="include_schemes" name="include_schemes">
                        <label class="form-check-label" for="include_schemes">
                            Include non-http schemes (e.g., android://)
                        </label>
                    </div>
                    <button type="submit" class="btn btn-primary">Start Search</button>
                    <a href="{{ url_for('dashboard') }}" class="btn btn-secondary">Cancel</a>
                </form>
            </div>
        </div>
        
        <div class="card mt-4">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-info-circle me-2"></i>Search Tips</h5>
            </div>
            <div class="card-body">
                <ul>
                    <li><strong>Domain Search</strong>: Find credentials containing a specific domain (e.g., google.com)</li>
                    <li><strong>Keyword Search</strong>: Find entries containing specific keywords</li>
                    <li>Searches are case-insensitive</li>
                    <li>Results include username:password, URL:username:password, and URL-only matches</li>
                    <li>Progress updates live in the Tasks screen</li>
                </ul>
            </div>
        </div>
    </div>
</div>
{% endblock %}'''

    my_searches_template = '''{% extends "base.html" %}
{% block title %}Search Tasks - Credential Manager{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center mb-4">
    <div>
        <h1 class="h2 mb-1">Search Tasks</h1>
        <p class="text-muted mb-0">Live status for every search, with progress and logs.</p>
    </div>
    <a href="{{ url_for('search') }}" class="btn btn-outline-secondary">New Search</a>
</div>

<div class="card">
    <div class="card-header">
        <div class="section-title"><i class="fas fa-wave-square text-muted"></i> Active & Recent Tasks</div>
    </div>
    <div class="card-body">
        {% if tasks %}
        <div class="table-responsive">
            <table class="table table-hover align-middle">
                <thead>
                    <tr>
                        <th>Query</th>
                        <th>Type</th>
                        <th>Status</th>
                        <th>Progress</th>
                        <th>Results</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for task in tasks %}
                    <tr data-task-id="{{ task[0] }}">
                        <td>{{ task[1] }}</td>
                        <td>{{ task[2] }}</td>
                        <td>
                            <span class="status-pill status-{{ task[4] }}" data-status>{{ task[4] }}</span>
                        </td>
                        <td style="min-width: 220px;">
                            <div class="progress" style="height: 8px;">
                                <div class="progress-bar" role="progressbar" style="width: {{ task[13] or 0 }}%;" data-progress></div>
                            </div>
                            <small class="text-muted" data-progress-text>{{ (task[13] or 0)|round(1) }}%</small>
                        </td>
                        <td data-results>{{ task[7] }}</td>
                        <td>
                            <div class="btn-group btn-group-sm" role="group">
                                <a href="{{ url_for('view_search_results', task_id=task[0]) }}" class="btn btn-outline-primary {% if task[4] != 'completed' %}disabled{% endif %}" data-view>View</a>
                                <button class="btn btn-outline-danger" data-cancel {% if task[4] in ['completed', 'error', 'cancelled'] %}disabled{% endif %}>Cancel</button>
                                <a href="{{ url_for('download_results', task_id=task[0], search_type='user_pass') }}" class="btn btn-outline-warning {% if task[4] != 'completed' %}disabled{% endif %}" title="Download user:pass">
                                    <i class="fas fa-arrow-down"></i> User:Pass
                                </a>
                                <a href="{{ url_for('download_results', task_id=task[0], search_type='url_user_pass') }}" class="btn btn-outline-info {% if task[4] != 'completed' %}disabled{% endif %}" title="Download url:user:pass">
                                    <i class="fas fa-arrow-down"></i> URL:User:Pass
                                </a>
                                <a href="{{ url_for('download_results', task_id=task[0], search_type='all') }}" class="btn btn-outline-primary {% if task[4] != 'completed' %}disabled{% endif %}" title="Download all results">
                                    <i class="fas fa-arrow-down"></i> All
                                </a>
                                <a href="{{ url_for('download_results_zip', task_id=task[0]) }}" class="btn btn-outline-dark {% if task[4] != 'completed' %}disabled{% endif %}" title="Download zip of all result sets">
                                    <i class="fas fa-file-archive"></i> ZIP
                                </a>
                                <button class="btn btn-outline-secondary" data-delete>Delete</button>
                            </div>
                        </td>
                    </tr>
                    <tr data-task-logs="{{ task[0] }}">
                        <td colspan="6">
                            <div class="code-block small" data-log-box>Loading logs...</div>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        {% else %}
        <p class="text-muted mb-0">No searches yet. Start one to see live progress.</p>
        {% endif %}
    </div>
</div>
{% endblock %}
{% block scripts %}
<script>
const statusClassMap = {
    queued: 'status-pill status-queued',
    processing: 'status-pill status-processing',
    completed: 'status-pill status-completed',
    error: 'status-pill status-error',
    cancelled: 'status-pill status-cancelled'
};

function updateTaskRow(taskId) {
    fetch('/api/search_task_status/' + taskId)
        .then(response => response.json())
        .then(data => {
            const row = document.querySelector(`tr[data-task-id="${taskId}"]`);
            const logRow = document.querySelector(`tr[data-task-logs="${taskId}"]`);
            if (!row) return;
            const statusEl = row.querySelector('[data-status]');
            const progressEl = row.querySelector('[data-progress]');
            const progressText = row.querySelector('[data-progress-text]');
            const resultEl = row.querySelector('[data-results]');
            const viewBtn = row.querySelector('[data-view]');
            const cancelBtn = row.querySelector('[data-cancel]');
            const logBox = logRow ? logRow.querySelector('[data-log-box]') : null;

            statusEl.textContent = data.status;
            statusEl.className = statusClassMap[data.status] || 'status-pill';
            progressEl.style.width = (data.progress || 0) + '%';
            progressText.textContent = (data.progress || 0).toFixed(1) + '%';
            resultEl.textContent = data.result_count || 0;
            if (data.status === 'completed') {
                viewBtn.classList.remove('disabled');
                cancelBtn.disabled = true;
            }
            if (data.status === 'error' || data.status === 'cancelled') {
                cancelBtn.disabled = true;
            }
            if (logBox && data.logs) {
                logBox.textContent = data.logs.slice(-8).join('\\n') || 'No logs yet.';
            }
        });
}

function refreshAll() {
    document.querySelectorAll('tr[data-task-id]').forEach(row => {
        updateTaskRow(row.dataset.taskId);
    });
    setTimeout(refreshAll, 1200);
}

document.querySelectorAll('[data-cancel]').forEach(btn => {
    btn.addEventListener('click', (event) => {
        const row = event.target.closest('tr[data-task-id]');
        if (!row) return;
        fetch('/api/cancel_search/' + row.dataset.taskId, { method: 'POST' })
            .then(() => {
                btn.disabled = true;
            });
    });
});

document.querySelectorAll('[data-delete]').forEach(btn => {
    btn.addEventListener('click', (event) => {
        const row = event.target.closest('tr[data-task-id]');
        if (!row) return;
        fetch('/api/delete_search/' + row.dataset.taskId, { method: 'POST' })
            .then(() => {
                const logRow = document.querySelector(`tr[data-task-logs="${row.dataset.taskId}"]`);
                row.remove();
                if (logRow) logRow.remove();
            });
    });
});

refreshAll();
</script>
{% endblock %}'''
    
    # Search results template
    search_results_template = '''{% extends "base.html" %}
{% block title %}Search Results - Credential Manager{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center mb-4">
    <div>
        <h1 class="h2 mb-1">Search Results</h1>
        <p class="text-muted mb-0">Results for "{{ query }}" ({{ search_type }} search)</p>
    </div>
    <a href="{{ url_for('search') }}" class="btn btn-outline-secondary">New Search</a>
</div>

<div class="row">
    <div class="col-md-12">
        <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center">
                <div class="section-title">
                    <i class="fas fa-layer-group text-muted"></i>
                    Result Sets
                </div>
                <span class="badge bg-primary">{{ results|length }} total</span>
            </div>
            <div class="card-body">
                {% if results %}
                <div class="d-flex align-items-center justify-content-end mb-3 gap-2">
                    <label class="form-label mb-0 me-2"><small>Page size</small></label>
                    <select id="page-size-select" class="form-select form-select-sm" style="width:auto;">
                        <option value="100">100</option>
                        <option value="200" selected>200</option>
                        <option value="500">500</option>
                        <option value="1000">1000</option>
                    </select>
                </div>

                <div class="accordion" id="resultsAccordion">
                    <div class="accordion-item">
                        <h2 class="accordion-header" id="headingAll">
                            <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#collapseAll">
                                <i class="fas fa-list me-2"></i> All Results ({{ results|length }})
                            </button>
                        </h2>
                        <div id="collapseAll" class="accordion-collapse collapse" data-bs-parent="#resultsAccordion">
                            <div class="accordion-body">
                                <div class="d-flex justify-content-between align-items-center mb-2">
                                    <small class="text-muted">Showing <span id="all-shown-count">0</span> of {{ results|length }}</small>
                                    <div class="btn-group">
                                        <button class="btn btn-sm btn-outline-primary" onclick="loadNext('all')">Load next page</button>
                                        <button class="btn btn-sm btn-outline-secondary" onclick="loadAll('all')">Load all</button>
                                        <button class="btn btn-sm btn-outline-dark" onclick="resetSection('all')">Reset</button>
                                    </div>
                                </div>
                                <div class="table-responsive">
                                    <table class="table table-striped">
                                        <thead>
                                            <tr>
                                                <th>#</th>
                                                <th>Credential Entry</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for result in results %}
                                            <tr class="all-row" style="display:none;">
                                                <td>{{ loop.index }}</td>
                                                <td><code>{{ result }}</code></td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="accordion-item">
                        <h2 class="accordion-header" id="headingUserPass">
                            <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#collapseUserPass">
                                <i class="fas fa-user-lock me-2"></i> User:Pass ({{ user_pass_results|length }})
                            </button>
                        </h2>
                        <div id="collapseUserPass" class="accordion-collapse collapse" data-bs-parent="#resultsAccordion">
                            <div class="accordion-body">
                                <div class="d-flex justify-content-between align-items-center mb-2">
                                    <small class="text-muted">Showing <span id="userpass-shown-count">0</span> of {{ user_pass_results|length }}</small>
                                    <div class="btn-group">
                                        <button class="btn btn-sm btn-outline-primary" onclick="loadNext('userpass')">Load next page</button>
                                        <button class="btn btn-sm btn-outline-secondary" onclick="loadAll('userpass')">Load all</button>
                                        <button class="btn btn-sm btn-outline-dark" onclick="resetSection('userpass')">Reset</button>
                                    </div>
                                </div>
                                <div class="table-responsive">
                                    <table class="table table-striped">
                                        <thead>
                                            <tr>
                                                <th>#</th>
                                                <th>User:Pass Entry</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for result in user_pass_results %}
                                            <tr class="userpass-row" style="display:none;">
                                                <td>{{ loop.index }}</td>
                                                <td><code>{{ result }}</code></td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="accordion-item">
                        <h2 class="accordion-header" id="headingUrlUserPass">
                            <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#collapseUrlUserPass">
                                <i class="fas fa-link me-2"></i> URL:User:Pass ({{ url_user_pass_results|length }})
                            </button>
                        </h2>
                        <div id="collapseUrlUserPass" class="accordion-collapse collapse" data-bs-parent="#resultsAccordion">
                            <div class="accordion-body">
                                <div class="d-flex justify-content-between align-items-center mb-2">
                                    <small class="text-muted">Showing <span id="urluserpass-shown-count">0</span> of {{ url_user_pass_results|length }}</small>
                                    <div class="btn-group">
                                        <button class="btn btn-sm btn-outline-primary" onclick="loadNext('urluserpass')">Load next page</button>
                                        <button class="btn btn-sm btn-outline-secondary" onclick="loadAll('urluserpass')">Load all</button>
                                        <button class="btn btn-sm btn-outline-dark" onclick="resetSection('urluserpass')">Reset</button>
                                    </div>
                                </div>
                                <div class="table-responsive">
                                    <table class="table table-striped">
                                        <thead>
                                            <tr>
                                                <th>#</th>
                                                <th>URL:User:Pass Entry</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for result in url_user_pass_results %}
                                            <tr class="urluserpass-row" style="display:none;">
                                                <td>{{ loop.index }}</td>
                                                <td><code>{{ result }}</code></td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="accordion-item">
                        <h2 class="accordion-header" id="headingUrls">
                            <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#collapseUrls">
                                <i class="fas fa-globe me-2"></i> URLs ({{ url_results|length }})
                            </button>
                        </h2>
                        <div id="collapseUrls" class="accordion-collapse collapse" data-bs-parent="#resultsAccordion">
                            <div class="accordion-body">
                                <div class="d-flex justify-content-between align-items-center mb-2">
                                    <small class="text-muted">Showing <span id="urls-shown-count">0</span> of {{ url_results|length }}</small>
                                    <div class="btn-group">
                                        <button class="btn btn-sm btn-outline-primary" onclick="loadNext('urls')">Load next page</button>
                                        <button class="btn btn-sm btn-outline-secondary" onclick="loadAll('urls')">Load all</button>
                                        <button class="btn btn-sm btn-outline-dark" onclick="resetSection('urls')">Reset</button>
                                    </div>
                                </div>
                                <div class="table-responsive">
                                    <table class="table table-striped">
                                        <thead>
                                            <tr>
                                                <th>#</th>
                                                <th>URL</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for result in url_results %}
                                            <tr class="urls-row" style="display:none;">
                                                <td>{{ loop.index }}</td>
                                                <td><code>{{ result }}</code></td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="accordion-item">
                        <h2 class="accordion-header" id="headingErc">
                            <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#collapseErc">
                                <i class="fas fa-coins me-2"></i> ERC Tokens ({{ erc_token_results|length }})
                            </button>
                        </h2>
                        <div id="collapseErc" class="accordion-collapse collapse" data-bs-parent="#resultsAccordion">
                            <div class="accordion-body">
                                <div class="d-flex justify-content-between align-items-center mb-2">
                                    <small class="text-muted">Showing <span id="erc-shown-count">0</span> of {{ erc_token_results|length }}</small>
                                    <div class="btn-group">
                                        <button class="btn btn-sm btn-outline-primary" onclick="loadNext('erc')">Load next page</button>
                                        <button class="btn btn-sm btn-outline-secondary" onclick="loadAll('erc')">Load all</button>
                                        <button class="btn btn-sm btn-outline-dark" onclick="resetSection('erc')">Reset</button>
                                    </div>
                                </div>
                                <div class="table-responsive">
                                    <table class="table table-striped">
                                        <thead>
                                            <tr>
                                                <th>#</th>
                                                <th>Token</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {% for result in erc_token_results %}
                                            <tr class="erc-row" style="display:none;">
                                                <td>{{ loop.index }}</td>
                                                <td><code>{{ result }}</code></td>
                                            </tr>
                                            {% endfor %}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="mt-4">
                    <a href="{{ url_for('download_results', task_id=task_id, search_type='user_pass') }}" class="btn btn-sm btn-warning">
                        <i class="fas fa-download me-1"></i>User:Pass
                    </a>
                    <a href="{{ url_for('download_results', task_id=task_id, search_type='url_user_pass') }}" class="btn btn-sm btn-info">
                        <i class="fas fa-download me-1"></i>URL:User:Pass
                    </a>
                    <a href="{{ url_for('download_results', task_id=task_id, search_type='urls') }}" class="btn btn-sm btn-outline-primary">
                        <i class="fas fa-download me-1"></i>URLs
                    </a>
                    <a href="{{ url_for('download_results', task_id=task_id, search_type='erc_tokens') }}" class="btn btn-sm btn-outline-dark">
                        <i class="fas fa-download me-1"></i>ERC Tokens
                    </a>
                    <a href="{{ url_for('download_results', task_id=task_id, search_type='all') }}" class="btn btn-sm btn-primary">
                        <i class="fas fa-download me-1"></i>All Results
                    </a>
                </div>
                {% else %}
                <div class="text-center py-5">
                    <i class="fas fa-search fa-3x text-muted mb-3"></i>
                    <p class="lead">No results found for "{{ query }}"</p>
                    <a href="{{ url_for('search') }}" class="btn btn-primary">Try Another Search</a>
                </div>
                {% endif %}
            </div>
        </div>
    </div>
</div>
{% endblock %}
{% block scripts %}
<script>
const DEFAULT_PAGE_SIZE = 200;
const sections = ['all','userpass','urluserpass','urls','erc'];

function getPageSize() {
  const sel = document.getElementById('page-size-select');
  const val = sel ? parseInt(sel.value, 10) : DEFAULT_PAGE_SIZE;
  return isNaN(val) ? DEFAULT_PAGE_SIZE : val;
}

function loadMore(kind, count) {
  const rows = document.querySelectorAll(`.${kind}-row`);
  let shown = 0;
  rows.forEach(r => { if (r.style.display !== 'none') shown++; });
  let added = 0;
  for (let i = shown; i < rows.length && added < count; i++) {
    rows[i].style.display = '';
    added++;
  }
  updateShownCounts();
}

function loadNext(kind) {
  loadMore(kind, getPageSize());
}

function loadAll(kind) {
  const rows = document.querySelectorAll(`.${kind}-row`);
  rows.forEach(r => r.style.display = '');
  updateShownCounts();
}

function resetSection(kind) {
  const rows = document.querySelectorAll(`.${kind}-row`);
  rows.forEach(r => r.style.display = 'none');
  loadMore(kind, getPageSize());
}

function updateShownCounts() {
  const mapping = {
    all: 'all-shown-count',
    userpass: 'userpass-shown-count',
    urluserpass: 'urluserpass-shown-count',
    urls: 'urls-shown-count',
    erc: 'erc-shown-count'
  };
  Object.keys(mapping).forEach(kind => {
    const rows = document.querySelectorAll(`.${kind}-row`);
    let shown = 0;
    rows.forEach(r => { if (r.style.display !== 'none') shown++; });
    const el = document.getElementById(mapping[kind]);
    if (el) el.innerText = shown;
  });
}

document.addEventListener('DOMContentLoaded', () => {
  sections.forEach(kind => loadMore(kind, getPageSize()));
  const sel = document.getElementById('page-size-select');
  if (sel) sel.addEventListener('change', () => {
    sections.forEach(kind => resetSection(kind));
  });
});
</script>
{% endblock %}'''
    
    # Files template
    files_template = '''{% extends "base.html" %}
{% block title %}My Files - Credential Manager{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center pt-3 pb-2 mb-3 border-bottom">
    <h1 class="h2">My Files</h1>
</div>

<div class="row mb-3">
    <div class="col-md-12">
        <form method="POST" action="{{ url_for('toggle_all_files') }}" class="d-inline me-2">
            <input type="hidden" name="action" value="select_all">
            <button type="submit" class="btn btn-sm btn-outline-success" title="Select all files for search">
                <i class="fas fa-check-square me-1"></i>Select All
            </button>
        </form>
        <form method="POST" action="{{ url_for('toggle_all_files') }}" class="d-inline">
            <input type="hidden" name="action" value="deselect_all">
            <button type="submit" class="btn btn-sm btn-outline-secondary" title="Deselect all files from search">
                <i class="fas fa-square me-1"></i>Deselect All
            </button>
        </form>
    </div>
</div>

<div class="row">
    <div class="col-md-12">
        {% if files %}
        <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center">
                <h5 class="card-title mb-0"><i class="fas fa-folder me-2"></i>Your Files in Local Storage</h5>
                <span class="badge bg-primary">{{ files|length }} files</span>
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-hover">
                        <thead>
                            <tr>
                                <th>Selected</th>
                                <th>Filename</th>
                                <th>Size</th>
                                <th>Uploaded</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for file_id, file_name, file_size, file_path, upload_date in files %}
                            <tr class="file-list-item">
                                <td>
                                    <form method="POST" action="{{ url_for('toggle_file_selection', file_id=file_id) }}" style="display: inline;">
                                        <input type="checkbox" 
                                               onchange="this.form.submit()" 
                                               {% if file_id in selected_file_ids %}checked{% endif %}>
                                    </form>
                                </td>
                                <td>
                                    <i class="fas fa-file-alt me-2 text-info"></i>
                                    <a href="{{ url_for('view_file', file_id=file_id) }}">{{ file_name }}</a>
                                </td>
                                <td>{{ file_size }}</td>
                                <td>{{ upload_date[:10] }}</td>
                                <td>
                                    <div class="btn-group" role="group">
                                        <a href="{{ url_for('download_file', file_id=file_id) }}" class="btn btn-sm btn-outline-primary" title="Download">
                                            <i class="fas fa-download"></i>
                                        </a>
                                        <a href="{{ url_for('view_file', file_id=file_id) }}" class="btn btn-sm btn-outline-info" title="View">
                                            <i class="fas fa-eye"></i>
                                        </a>
                                        <form method="POST" action="{{ url_for('delete_file', file_id=file_id) }}" style="display: inline;" onsubmit="return confirm('Are you sure you want to delete this file?')">
                                            <button type="submit" class="btn btn-sm btn-outline-danger" title="Delete">
                                                <i class="fas fa-trash"></i>
                                            </button>
                                        </form>
                                    </div>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        {% else %}
        <div class="text-center py-5">
            <i class="fas fa-folder-open fa-3x text-muted mb-3"></i>
            <p class="lead">You haven't uploaded any files yet</p>
            <a href="{{ url_for('upload') }}" class="btn btn-primary">Upload Your First File</a>
        </div>
        {% endif %}
    </div>
</div>
{% endblock %}'''
    
    # View file template
    view_file_template = '''{% extends "base.html" %}
{% block title %}View File - {{ file_name }}{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center pt-3 pb-2 mb-3 border-bottom">
    <h1 class="h2">{{ file_name }}</h1>
    <div>
        <a href="{{ url_for('download_file', file_id=file_id) }}" class="btn btn-success">
            <i class="fas fa-download me-1"></i>Download
        </a>
        <a href="{{ url_for('files') }}" class="btn btn-secondary">
            <i class="fas fa-arrow-left me-1"></i>Back to Files
        </a>
    </div>
</div>

<div class="row">
    <div class="col-md-12">
        <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center">
                <h5 class="card-title mb-0"><i class="fas fa-file-alt me-2"></i>File Content</h5>
                <span class="text-muted">Showing content of {{ file_name }}</span>
            </div>
            <div class="card-body">
                <pre class="p-3 bg-light rounded" style="white-space: pre-wrap; word-break: break-word; max-height: 60vh; overflow-y: auto;">{{ content }}</pre>
            </div>
        </div>
    </div>
</div>
{% endblock %}'''
    
    # Download progress template
    download_progress_template = '''{% extends "base.html" %}
{% block title %}Download Progress{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center mb-4">
    <div>
        <h1 class="h2 mb-1">Download Progress</h1>
        <p class="text-muted mb-0">Tracking live download status.</p>
    </div>
    <button class="btn btn-outline-danger" id="cancelDownloadBtn">Cancel Task</button>
</div>

<div class="row justify-content-center">
    <div class="col-md-8">
        <div class="card">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-download me-2"></i>Downloading File</h5>
            </div>
            <div class="card-body">
                <div class="mb-3">
                    <h6>File: <span id="filename">Loading...</span></h6>
                    <h6>Status: <span id="status" class="badge bg-info">Downloading</span></h6>
                </div>
                
                <div class="progress mb-3" style="height: 30px;">
                    <div id="progressBar" class="progress-bar progress-bar-striped progress-bar-animated" 
                         role="progressbar" style="width: 0%;" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100">
                        <span id="progressText">0%</span>
                    </div>
                </div>
                
                <div class="row text-center">
                    <div class="col-md-3">
                        <div class="card bg-light">
                            <div class="card-body">
                                <h6 class="card-title">Downloaded</h6>
                                <p class="card-text" id="downloaded">0 MB</p>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="card bg-light">
                            <div class="card-body">
                                <h6 class="card-title">Total Size</h6>
                                <p class="card-text" id="totalSize">0 MB</p>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="card bg-light">
                            <div class="card-body">
                                <h6 class="card-title">Speed</h6>
                                <p class="card-text" id="speed">0 MB/s</p>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="card bg-light">
                            <div class="card-body">
                                <h6 class="card-title">ETA</h6>
                                <p class="card-text" id="eta">Calculating...</p>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="mt-3 text-center" id="completeSection" style="display: none;">
                    <div class="alert alert-success">
                        <i class="fas fa-check-circle me-2"></i>Download completed successfully!
                    </div>
                    <a href="{{ url_for('files') }}" class="btn btn-primary">View My Files</a>
                </div>
                
                <div class="mt-3 text-center" id="errorSection" style="display: none;">
                    <div class="alert alert-danger">
                        <i class="fas fa-exclamation-circle me-2"></i><span id="errorMessage">An error occurred</span>
                    </div>
                    <a href="{{ url_for('upload') }}" class="btn btn-primary">Try Again</a>
                </div>
                <div class="mt-3">
                    <div class="section-title"><i class="fas fa-terminal text-muted"></i> Logs</div>
                    <div class="code-block mt-2 small" id="downloadLogs">Waiting for updates...</div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
const sessionId = '{{ session_id }}';

function formatBytes(bytes) {
    if (bytes === 0) return '0 MB';
    const mb = bytes / (1024 * 1024);
    return mb.toFixed(2) + ' MB';
}

function formatSpeed(bytesPerSecond) {
    const mbps = bytesPerSecond / (1024 * 1024);
    return mbps.toFixed(2) + ' MB/s';
}

function formatTime(seconds) {
    if (seconds === 0 || !isFinite(seconds)) return 'Calculating...';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return mins + 'm ' + secs + 's';
}

function updateProgress() {
    fetch('/api/download_progress/' + sessionId)
        .then(response => response.json())
        .then(data => {
            if (data.logs && data.logs.length) {
                document.getElementById('downloadLogs').textContent = data.logs.slice(-8).join('\\n');
            }
            if (data.status === 'downloading') {
                document.getElementById('filename').textContent = data.filename || 'Unknown';
                document.getElementById('status').textContent = 'Downloading';
                document.getElementById('status').className = 'badge bg-info';
                
                const progress = Math.round(data.progress);
                document.getElementById('progressBar').style.width = progress + '%';
                document.getElementById('progressBar').setAttribute('aria-valuenow', progress);
                document.getElementById('progressText').textContent = progress + '%';
                
                document.getElementById('downloaded').textContent = formatBytes(data.downloaded);
                document.getElementById('totalSize').textContent = formatBytes(data.total_size);
                document.getElementById('speed').textContent = formatSpeed(data.speed);
                document.getElementById('eta').textContent = formatTime(data.eta);
                
                setTimeout(updateProgress, 500);
            } else if (data.status === 'complete') {
                document.getElementById('status').textContent = 'Complete';
                document.getElementById('status').className = 'badge bg-success';
                document.getElementById('progressBar').style.width = '100%';
                document.getElementById('progressText').textContent = '100%';
                document.getElementById('progressBar').classList.remove('progress-bar-animated');
                document.getElementById('completeSection').style.display = 'block';
                document.getElementById('cancelDownloadBtn').disabled = true;
            } else if (data.status === 'error') {
                document.getElementById('status').textContent = 'Error';
                document.getElementById('status').className = 'badge bg-danger';
                document.getElementById('errorMessage').textContent = data.error || 'An error occurred';
                document.getElementById('errorSection').style.display = 'block';
                document.getElementById('progressBar').classList.remove('progress-bar-animated');
                document.getElementById('cancelDownloadBtn').disabled = true;
            } else if (data.status === 'cancelled') {
                document.getElementById('status').textContent = 'Cancelled';
                document.getElementById('status').className = 'badge bg-secondary';
                document.getElementById('errorMessage').textContent = 'Download cancelled.';
                document.getElementById('errorSection').style.display = 'block';
                document.getElementById('progressBar').classList.remove('progress-bar-animated');
                document.getElementById('cancelDownloadBtn').disabled = true;
            }
        })
        .catch(error => {
            console.error('Error fetching progress:', error);
            setTimeout(updateProgress, 1000);
        });
}

// Start updating progress
updateProgress();

document.getElementById('cancelDownloadBtn').addEventListener('click', () => {
    fetch('/api/cancel_download/' + sessionId, { method: 'POST' })
        .then(() => {
            document.getElementById('cancelDownloadBtn').disabled = true;
        });
});
</script>
{% endblock %}'''

    batch_download_progress_template = '''{% extends "base.html" %}
{% block title %}Batch Download Progress{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center mb-4">
    <div>
        <h1 class="h2 mb-1">Batch Download</h1>
        <p class="text-muted mb-0">Tracking multiple downloads in one task.</p>
    </div>
    <button class="btn btn-outline-danger" id="cancelBatchBtn">Cancel Task</button>
</div>

<div class="row justify-content-center">
    <div class="col-md-9">
        <div class="card">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-layer-group me-2"></i>Batch Progress</h5>
            </div>
            <div class="card-body">
                <div class="mb-3">
                    <h6>Current: <span id="currentFile">Loading...</span></h6>
                    <h6>Status: <span id="status" class="badge bg-info">Downloading</span></h6>
                </div>
                <div class="progress mb-3" style="height: 18px;">
                    <div id="progressBar" class="progress-bar progress-bar-striped progress-bar-animated" role="progressbar" style="width: 0%;"></div>
                </div>
                <div class="row text-center">
                    <div class="col-md-3">
                        <div class="card bg-light">
                            <div class="card-body">
                                <h6 class="card-title">Completed</h6>
                                <p class="card-text" id="completedCount">0</p>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="card bg-light">
                            <div class="card-body">
                                <h6 class="card-title">Failed</h6>
                                <p class="card-text" id="failedCount">0</p>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="card bg-light">
                            <div class="card-body">
                                <h6 class="card-title">Total URLs</h6>
                                <p class="card-text" id="totalCount">0</p>
                            </div>
                        </div>
                    </div>
                    <div class="col-md-3">
                        <div class="card bg-light">
                            <div class="card-body">
                                <h6 class="card-title">Progress</h6>
                                <p class="card-text" id="progressText">0%</p>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="mt-4">
                    <div class="section-title"><i class="fas fa-list text-muted"></i> Download Log</div>
                    <div class="code-block mt-2 small" id="batchLogs">Waiting for updates...</div>
                </div>
            </div>
        </div>
    </div>
</div>
{% endblock %}
{% block scripts %}
<script>
const sessionId = '{{ session_id }}';

function updateBatch() {
    fetch('/api/batch_download_progress/' + sessionId)
        .then(response => response.json())
        .then(data => {
            if (data.logs && data.logs.length) {
                document.getElementById('batchLogs').textContent = data.logs.slice(-10).join('\\n');
            }
            if (data.status === 'downloading') {
                document.getElementById('status').textContent = 'Downloading';
                document.getElementById('status').className = 'badge bg-info';
            } else if (data.status === 'complete') {
                document.getElementById('status').textContent = 'Complete';
                document.getElementById('status').className = 'badge bg-success';
                document.getElementById('cancelBatchBtn').disabled = true;
            } else if (data.status === 'error') {
                document.getElementById('status').textContent = 'Error';
                document.getElementById('status').className = 'badge bg-danger';
                document.getElementById('cancelBatchBtn').disabled = true;
            } else if (data.status === 'cancelled') {
                document.getElementById('status').textContent = 'Cancelled';
                document.getElementById('status').className = 'badge bg-secondary';
                document.getElementById('cancelBatchBtn').disabled = true;
            }

            document.getElementById('currentFile').textContent = data.current_file || '-';
            document.getElementById('completedCount').textContent = data.completed || 0;
            document.getElementById('failedCount').textContent = data.failed || 0;
            document.getElementById('totalCount').textContent = data.total_urls || 0;
            const progress = Math.round(data.progress || 0);
            document.getElementById('progressText').textContent = progress + '%';
            document.getElementById('progressBar').style.width = progress + '%';

            if (data.status === 'downloading') {
                setTimeout(updateBatch, 700);
            }
        })
        .catch(() => {
            setTimeout(updateBatch, 1200);
        });
}

updateBatch();

document.getElementById('cancelBatchBtn').addEventListener('click', () => {
    fetch('/api/cancel_download/' + sessionId, { method: 'POST' })
        .then(() => {
            document.getElementById('cancelBatchBtn').disabled = true;
        });
});
</script>
{% endblock %}'''
    
    # Settings template
    settings_template = '''{% extends "base.html" %}
{% block title %}Settings - Credential Manager{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center pt-3 pb-2 mb-3 border-bottom">
    <h1 class="h2">Settings</h1>
</div>

<div class="row justify-content-center">
    <div class="col-md-8">
        <div class="card">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-cog me-2"></i>Account Settings</h5>
            </div>
            <div class="card-body">
                <form method="POST" action="{{ url_for('update_settings') }}">
                    <div class="mb-3">
                        <label for="active_base" class="form-label">Active Base</label>
                        <select class="form-select" id="active_base" name="active_base">
                            <option value="all" {% if settings['active_base'] == 'all' %}selected{% endif %}>All Bases</option>
                            {% for file_id, file_name, file_size, file_path, upload_date in user_files %}
                            <option value="{{ file_name }}" {% if settings['active_base'] == file_name %}selected{% endif %}>{{ file_name }}</option>
                            {% endfor %}
                        </select>
                        <div class="form-text">Select which file(s) to use for searches</div>
                    </div>
                    
                    <div class="mb-3">
                        <label for="search_mode" class="form-label">Search Mode</label>
                        <select class="form-select" id="search_mode" name="search_mode">
                            <option value="standard" {% if settings['search_mode'] == 'standard' %}selected{% endif %}>Standard</option>
                            <option value="advanced" {% if settings['search_mode'] == 'advanced' %}selected{% endif %}>Advanced</option>
                        </select>
                        <div class="form-text">Choose the search algorithm complexity</div>
                    </div>
                    
                    <button type="submit" class="btn btn-primary">Save Settings</button>
                    <a href="{{ url_for('dashboard') }}" class="btn btn-secondary">Cancel</a>
                </form>
            </div>
        </div>
        
        <div class="card mt-4">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-info-circle me-2"></i>About Settings</h5>
            </div>
            <div class="card-body">
                <ul>
                    <li><strong>Active Base</strong>: Choose which files to search through</li>
                    <li><strong>Search Mode</strong>: Standard mode for basic searches, Advanced mode for complex pattern matching</li>
                </ul>
            </div>
        </div>
    </div>
</div>
{% endblock %}'''
    
    # Access key template
    access_key_template = '''{% extends "base.html" %}
{% block title %}Access Key Required{% endblock %}
{% block content %}
<div class="container d-flex align-items-center justify-content-center" style="min-height: 70vh;">
    <div class="col-md-6 col-lg-4">
        <div class="card shadow">
            <div class="card-header text-center">
                <h3><i class="fas fa-key me-2"></i>Access Key Required</h3>
            </div>
            <div class="card-body">
                <p class="text-center">This application requires an access key to continue.</p>
                <form method="POST">
                    <div class="mb-3">
                        <label for="access_key" class="form-label">Access Key</label>
                        <input type="password" class="form-control" id="access_key" name="access_key" placeholder="Enter access key" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">Submit Access Key</button>
                </form>
            </div>
        </div>
    </div>
</div>
{% endblock %}'''
    
    # Write all templates
    templates = {
        'base.html': base_template,
        'dashboard.html': dashboard_template,
        'upload.html': upload_template,
        'search.html': search_template,
        'my_searches.html': my_searches_template,
        'search_results.html': search_results_template,
        'files.html': files_template,
        'view_file.html': view_file_template,
        'download_progress.html': download_progress_template,
        'batch_download_progress.html': batch_download_progress_template,
        'access_key.html': access_key_template
    }
    
    for filename, content in templates.items():
        with open(f'templates/{filename}', 'w') as f:
            f.write(content)


# Initialize templates
write_templates()


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    # Set debug mode based on environment
    debug_mode = os.environ.get('FLASK_ENV', 'production') == 'development'
    app.run(debug=debug_mode, host='0.0.0.0', port=port)
