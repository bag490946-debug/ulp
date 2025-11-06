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
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # 1GB max upload size

# Access key
ACCESS_KEY = '@BaignX'

# Constants
TELEGRAM_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB (Telegram limit)
MAX_UPLOAD_SIZE = 1024 * 1024 * 1024  # 1GB max upload size

# Database setup
DB_PATH = "bot_database.db"

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
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Check if is_selected column exists, if not, add it
    cursor.execute("PRAGMA table_info(global_files)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'is_selected' not in columns:
        cursor.execute('ALTER TABLE global_files ADD COLUMN is_selected INTEGER DEFAULT 1')
    
    conn.commit()
    conn.close()

def save_file_record(file_name, file_size, file_path):
    """Save file record to database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO global_files (file_name, file_size, file_path)
        VALUES (?, ?, ?)
    ''', (file_name, file_size, file_path))
    
    conn.commit()
    conn.close()

def get_global_files():
    """Get all global files"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, file_name, file_size, file_path, upload_date 
        FROM global_files 
        ORDER BY upload_date DESC
    ''')
    
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

def get_file_by_id(file_id):
    """Get file record by file_id"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('SELECT id, file_name, file_size, file_path FROM global_files WHERE id = ?', (file_id,))
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
        WHERE is_selected = 1
        ORDER BY upload_date DESC
    ''')
    
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

# Initialize the database
init_db()

# Ensure upload directory exists
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])


# Clean URL for searching
def clean_url(url):
    # Remove protocol
    url = re.sub(r'^(https?://)?(www\.)?', '', url.lower())
    # Remove path, query strings, etc.
    url = url.split('/')[0]
    return url


def extract_credentials(line):
    """
    Extract user:pass combinations from various formats:
    - Format 1: https://example.com/user:pass -> https://example.com:user:pass
    - Format 2: https://example.com:username:password -> https://example.com:username:password
    - Format 3: user@domain:pass -> user@domain:pass
    - Format 4: user:pass -> user:pass
    """
    line = line.strip()
    extracted = []
    
    # Special case: URLs ending with /<something>/signup:email:pass → return email:pass
    # Examples:
    #   https://sms-man.com/de/signup:amer@example.com:Pass123
    #   https://sms-man.com/signup:user@example.com:Pass
    signup_email_pass_pattern = re.compile(
        r'https?://[^\s]*?/signup:([^:\s]+@[^:\s]+):([^\s]+)',
        re.IGNORECASE,
    )
    matches = signup_email_pass_pattern.findall(line)
    for email_part, pass_part in matches:
        extracted.append(f"{email_part}:{pass_part}")

    # Pattern 1: URL with user:pass format after a path segment (keep triple for url_user_pass list)
    url_log_pass_pattern = re.compile(r'(https?://[^\s/]+/[^:\s]+):([^:\s]+):([^:\s]+)', re.IGNORECASE)
    matches = url_log_pass_pattern.findall(line)
    for match in matches:
        url_part = match[0]
        user_part = match[1]
        pass_part = match[2]
        extracted.append(f"{url_part}:{user_part}:{pass_part}")
    
    # Pattern 2: URL:username:password format (keep triple; also emit email:pass if username is an email)
    url_user_pass_pattern = re.compile(r'(https?://[^\s]+):([^:\s]+):([^\s]+)', re.IGNORECASE)
    matches = url_user_pass_pattern.findall(line)
    for url_part, user_part, pass_part in matches:
        combo = f"{url_part}:{user_part}:{pass_part}"
        if combo not in extracted:
            extracted.append(combo)
        # If the username is an email, also add pure email:pass for filtering
        if re.match(r'^[^:\s]+@[^:\s]+$', user_part):
            email_pass_combo = f"{user_part}:{pass_part}"
            if email_pass_combo not in extracted:
                extracted.append(email_pass_combo)
    
    # Pattern 3: URL:pass format (e.g., https://example.com:password)
    url_pass_pattern = re.compile(r'(https?://[^\s]+):([^\s]+)', re.IGNORECASE)
    matches = url_pass_pattern.findall(line)
    for match in matches:
        url_part = match[0]  # URL
        pass_part = match[1]  # pass
        # Create URL:pass format
        url_pass_combo = f"{url_part}:{pass_part}"
        if url_pass_combo not in extracted:
            extracted.append(url_pass_combo)
    
    # Pattern 4: email:pass (preserving email:pass format)
    email_pattern = re.compile(r'([^:\s]+@[^:\s]+):([^:\s]+)')
    matches = email_pattern.findall(line)
    for match in matches:
        email_part = match[0]  # email
        pass_part = match[1]  # pass
        email_pass_combo = f"{email_part}:{pass_part}"
        if email_pass_combo not in extracted:
            extracted.append(email_pass_combo)
    
    # Pattern 5: general user:pass but filter out invalid formats
    general_pattern = re.compile(r'([^:\s]+):([^:\s]+)', re.IGNORECASE)
    matches = general_pattern.findall(line)
    for match in matches:
        user_part = match[0]  # user
        pass_part = match[1]  # pass
        
        # Skip entries that look like file extensions, paths, or non-credentials
        if (len(user_part) <= 100 and len(pass_part) <= 100 and  # Reasonable length
            not any(skip in user_part.lower() for skip in ['.com', '.org', '.net', '.php', '.html', '.js', '.css', '.txt']) and
            not any(skip in pass_part.lower() for skip in ['.com', '.org', '.net', '.php', '.html', '.js', '.css', '.txt']) and
            not user_part.isdigit() and  # Skip if just numbers
            not (user_part.startswith('/') or user_part.startswith('\\'))):  # Skip paths
            
            user_pass_combo = f"{user_part}:{pass_part}"
            if user_pass_combo not in extracted:
                extracted.append(user_pass_combo)
    
    return list(set(extracted))  # Remove duplicates while returning a list


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


# Search by domain - optimized for large files
def search_domain(query):
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
                        # Extract credentials from the line
                        credentials = extract_credentials(line)
                        
                        for cred in credentials:
                            # Classify results strictly by format
                            # 1) URL:USER:PASS (keep only if domain present in the URL)
                            if re.match(r'^https?://[^\s]+:[^:\s]+:[^\s]+$', cred, re.IGNORECASE):
                                if domain in cred.lower():
                                    if cred not in url_user_pass_results:
                                        url_user_pass_results.append(cred)
                                continue

                            # 2) Pure email:pass (no URL prefix)
                            if re.match(r'^[^:\s]+@[^:\s]+:[^\s]+$', cred):
                                # We allow email:pass even though it doesn't include domain itself,
                                # because it was extracted from a line that contains the domain
                                if cred not in user_pass_results:
                                    user_pass_results.append(cred)
                                continue
                        
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
def search_keywords(keywords):
    if not keywords:
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
            # Process file in chunks
            with open(file_path, 'r', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    # Case-insensitive search
                    if keywords.lower() in line.lower():
                        # Extract all possible credentials from line
                        credentials = extract_credentials(line)
                        
                        for cred in credentials:
                            # Check if the credential contains the keyword
                            if keywords.lower() in cred.lower():
                                # If it's an email format
                                if '@' in cred:
                                    if cred not in user_pass_results:
                                        user_pass_results.append(cred)
                                else:
                                    # For general user:pass formats
                                    if cred not in user_pass_results and cred not in url_user_pass_results:
                                        user_pass_results.append(cred)
                        
                        # Extract URLs from the line if it matches the keyword
                        urls = extract_urls(line)
                        for url in urls:
                            if keywords.lower() in url.lower():
                                if url not in url_results:
                                    url_results.append(url)
                        
                        # Extract ERC tokens from the line if it matches the keyword
                        erc_tokens = extract_erc_tokens(line)
                        for token in erc_tokens:
                            if keywords.lower() in token.lower():
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
        if 'file' not in request.files:
            flash('No file selected', 'error')
            return redirect(request.url)
        
        file = request.files['file']
        if file.filename == '':
            flash('No file selected', 'error')
            return redirect(request.url)
        
        if file:
            filename = secure_filename(file.filename)
            if not filename.lower().endswith('.txt'):
                flash('Only text (.txt) files are supported', 'error')
                return redirect(request.url)
            
            # Save to global upload directory
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            
            # Get file size
            file_size = os.path.getsize(file_path)
            
            # Check if file is too large
            if file_size > MAX_UPLOAD_SIZE:
                flash(f'File is too large. Maximum size is {MAX_UPLOAD_SIZE / (1024*1024*1024):.0f}GB', 'error')
                os.remove(file_path)
                return redirect(request.url)
            
            # Save to database
            save_file_record(filename, file_size, file_path)
            
            flash(f'File "{filename}" uploaded successfully!', 'success')
            return redirect(url_for('files'))
    
    return render_template('upload.html')


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
        
        if not query:
            flash('Please enter a search query', 'error')
            return redirect(request.url)
        
        results = {}
        if search_type == 'domain':
            results = search_domain(query)
        elif search_type == 'keyword':
            results = search_keywords(query)
        
        # Store query and type in session for download functionality
        session['search_query'] = query
        session['search_type'] = search_type
        
        return render_template(
            'search_results.html',
            results=results['all'],
            user_pass_results=results.get('user_pass', []),
            url_user_pass_results=results.get('url_user_pass', []),
            query=query,
            search_type=search_type,
        )
    
    return render_template('search.html')


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


@app.route('/download_results/<search_type>')
def download_results(search_type):
    if not is_authenticated():
        return redirect(url_for('access_key'))
    
    # Get the most recent search from session
    search_query = session.get('search_query', 'recent')
    search_type_session = session.get('search_type', 'domain')
    
    # Perform the search again to get the results
    results = {}
    if search_type_session == 'domain':
        results = search_domain(search_query)
    elif search_type_session == 'keyword':
        results = search_keywords(search_query)
    
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
        filename = f"user_pass_results_{search_query.replace(' ', '_')}_{timestamp}.txt"
    elif search_type == 'url_user_pass':
        filename = f"url_user_pass_results_{search_query.replace(' ', '_')}_{timestamp}.txt"
    elif search_type == 'urls':
        filename = f"url_results_{search_query.replace(' ', '_')}_{timestamp}.txt"
    elif search_type == 'erc_tokens':
        filename = f"erc_token_results_{search_query.replace(' ', '_')}_{timestamp}.txt"
    else:
        filename = f"search_results_{search_query.replace(' ', '_')}_{timestamp}.txt"
    
    # Create temporary file
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as temp_file:
        for result in result_data:
            temp_file.write(result + '\n')
        temp_file_path = temp_file.name
    
    # Return the temporary file for download
    return send_file(temp_file_path, as_attachment=True, download_name=filename, 
                     mimetype='text/plain', conditional=True)


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
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        body {
            background-color: #f8f9fa;
        }
        .sidebar {
            min-height: 100vh;
            background: linear-gradient(180deg, #6f42c1, #5a32a3);
            color: white;
        }
        .sidebar .nav-link {
            color: rgba(255,255,255,0.8);
            padding: 0.75rem 1.5rem;
        }
        .sidebar .nav-link:hover, .sidebar .nav-link.active {
            color: white;
            background-color: rgba(255,255,255,0.1);
        }
        .main-content {
            padding: 2rem 0;
        }
        .card {
            box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.075);
            border: 1px solid rgba(0, 0, 0, 0.125);
        }
        .card-header {
            background-color: #f8f9fa;
            border-bottom: 1px solid rgba(0, 0, 0, 0.125);
        }
        .navbar-brand {
            font-weight: bold;
        }
        .btn-primary {
            background-color: #6f42c1;
            border-color: #6f42c1;
        }
        .btn-primary:hover {
            background-color: #5a32a3;
            border-color: #5a32a3;
        }
        .feature-card {
            transition: transform 0.2s;
        }
        .feature-card:hover {
            transform: translateY(-5px);
        }
        .file-list-item {
            border-left: 4px solid #6f42c1;
        }
    </style>
</head>
<body>
    <div class="container-fluid">
        <div class="row">
            <!-- Sidebar -->
            <nav class="col-md-3 col-lg-2 d-md-block sidebar collapse">
                <div class="position-sticky pt-3">
                    <h5 class="text-white text-center mb-4">Credential Manager</h5>
                    <ul class="nav flex-column">
                        <li class="nav-item">
                            <a class="nav-link {% if request.endpoint == 'dashboard' %}active{% endif %}" href="{{ url_for('dashboard') }}">
                                <i class="fas fa-tachometer-alt me-2"></i>Dashboard
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link {% if request.endpoint == 'upload' %}active{% endif %}" href="{{ url_for('upload') }}">
                                <i class="fas fa-upload me-2"></i>Upload File
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link {% if request.endpoint == 'search' %}active{% endif %}" href="{{ url_for('search') }}">
                                <i class="fas fa-search me-2"></i>Search
                            </a>
                        </li>
                        <li class="nav-item">
                            <a class="nav-link {% if request.endpoint == 'files' %}active{% endif %}" href="{{ url_for('files') }}">
                                <i class="fas fa-folder me-2"></i>My Files
                            </a>
                        </li>
                
                    </ul>
                </div>
            </nav>

            <!-- Main content -->
            <main class="col-md-9 ms-sm-auto col-lg-10 px-md-4">
                <!-- Top navigation -->
                <nav class="navbar navbar-expand-lg navbar-light bg-light mb-4">
                    <div class="container-fluid">
                        <a class="navbar-brand" href="{{ url_for('dashboard') }}">
                            <i class="fas fa-lock me-2"></i>Credential Manager
                        </a>
                        <div class="d-flex align-items-center">
                            {% if logged_in_user %}
                            <span class="me-3">Hello, <strong>{{ logged_in_user }}</strong></span>
                            {% endif %}
                            <a href="{{ url_for('logout') }}" class="btn btn-outline-secondary btn-sm">Logout</a>
                        </div>
                    </div>
                </nav>

                <!-- Alerts -->
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

                <!-- Main content block -->
                {% block content %}{% endblock %}
            </main>
        </div>
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
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center pt-3 pb-2 mb-3 border-bottom">
    <h1 class="h2">Dashboard</h1>
</div>

<div class="row">
    <div class="col-md-6 mb-4">
        <div class="card feature-card">
            <div class="card-body text-center">
                <div class="bg-primary text-white rounded-circle d-inline-flex align-items-center justify-content-center" style="width: 60px; height: 60px;">
                    <i class="fas fa-upload fa-2x"></i>
                </div>
                <h5 class="card-title mt-3">Upload Files</h5>
                <p class="card-text">Upload credential files up to 1GB to local storage for searching and management</p>
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
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center pt-3 pb-2 mb-3 border-bottom">
    <h1 class="h2">Upload File</h1>
</div>

<div class="row justify-content-center">
    <div class="col-md-8">
        <div class="card">
            <div class="card-header">
                <h5 class="card-title mb-0"><i class="fas fa-file-upload me-2"></i>Upload Credentials File</h5>
            </div>
            <div class="card-body">
                <form method="POST" enctype="multipart/form-data" id="uploadForm">
                    <div class="mb-3">
                        <label for="file" class="form-label">Choose a text file to upload</label>
                        <input type="file" class="form-control" id="file" name="file" accept=".txt" required>
                        <div class="form-text">Only text files (.txt) up to 1GB are supported</div>
                    </div>
                    <button type="submit" class="btn btn-primary" id="uploadBtn">Upload File to Local Storage</button>
                    <a href="{{ url_for('dashboard') }}" class="btn btn-secondary">Cancel</a>
                </form>
                
                <!-- Progress Bar -->
                <div class="mt-3" id="progressContainer" style="display: none;">
                    <div class="progress">
                        <div id="progressBar" class="progress-bar progress-bar-striped progress-bar-animated" 
                             role="progressbar" style="width: 0%;"></div>
                    </div>
                    <div class="mt-1 text-center">
                        <small id="progressText">0% Complete</small>
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
                    <li>Upload credential files in text format to local storage</li>
                    <li>Files will be stored securely in your account</li>
                    <li>You can search through your uploaded files after uploading</li>
                    <li>Maximum file size is 1GB</li>
                </ul>
            </div>
        </div>
    </div>
</div>

<script>
document.getElementById('uploadForm').addEventListener('submit', function(e) {
    const fileInput = document.getElementById('file');
    if (fileInput.files.length === 0) {
        alert('Please select a file to upload.');
        e.preventDefault();
        return;
    }
    
    // Show progress bar
    document.getElementById('progressContainer').style.display = 'block';
    document.getElementById('uploadBtn').disabled = true;
    
    // Simulate upload progress (in a real scenario, this would be handled via server-side progress tracking)
    let progress = 0;
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');
    
    const interval = setInterval(function() {
        progress += Math.random() * 15; // Random increment to simulate progress
        if (progress >= 100) {
            progress = 100;
            clearInterval(interval);
        }
        
        progressBar.style.width = progress + '%';
        progressText.textContent = Math.round(progress) + '% Complete';
        
        if (progress === 100) {
            // Reset after completion
            setTimeout(function() {
                document.getElementById('progressContainer').style.display = 'none';
                document.getElementById('uploadBtn').disabled = false;
            }, 1000);
        }
    }, 200);
});
</script>
{% endblock %}'''
    
    # Search template
    search_template = '''{% extends "base.html" %}
{% block title %}Search - Credential Manager{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center pt-3 pb-2 mb-3 border-bottom">
    <h1 class="h2">Search Credentials</h1>
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
                    <button type="submit" class="btn btn-primary">Search</button>
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
                    <li>Results will include both username:password and URL:username:password formats</li>
                </ul>
            </div>
        </div>
    </div>
</div>
{% endblock %}'''
    
    # Search results template
    search_results_template = '''{% extends "base.html" %}
{% block title %}Search Results - Credential Manager{% endblock %}
{% block content %}
<div class="d-flex justify-content-between flex-wrap flex-md-nowrap align-items-center pt-3 pb-2 mb-3 border-bottom">
    <h1 class="h2">Search Results</h1>
</div>

<div class="row">
    <div class="col-md-12">
        <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center">
                <h5 class="card-title mb-0">
                    <i class="fas fa-search me-2"></i>
                    Results for "{{ query }}" ({{ search_type }} search)
                </h5>
                <span class="badge bg-primary">{{ results|length }} results</span>
            </div>
            <div class="card-body">
                {% if results %}
                <!-- Buttons to show/hide different result types -->
                <div class="mb-3">
                    <button type="button" class="btn btn-sm btn-outline-primary" onclick="toggleResults('all')">Show All Results</button>
                    <button type="button" class="btn btn-sm btn-outline-secondary" onclick="toggleResults('user_pass')">Show User:Pass Only</button>
                    <button type="button" class="btn btn-sm btn-outline-success" onclick="toggleResults('url_user_pass')">Show URL:User:Pass Only</button>
                </div>
                
                <!-- Download buttons -->
                <div class="mb-3">
                    <a href="{{ url_for('download_results', search_type='user_pass') }}" class="btn btn-sm btn-warning">
                        <i class="fas fa-download me-1"></i>Download User:Pass File
                    </a>
                    <a href="{{ url_for('download_results', search_type='url_user_pass') }}" class="btn btn-sm btn-info">
                        <i class="fas fa-download me-1"></i>Download URL:User:Pass File
                    </a>
                    <a href="{{ url_for('download_results', search_type='all') }}" class="btn btn-sm btn-primary">
                        <i class="fas fa-download me-1"></i>Download All Results
                    </a>
                </div>
                
                <div class="table-responsive">
                    <table class="table table-striped" id="all-results">
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>Credential Entry</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for result in results %}
                            <tr>
                                <td>{{ loop.index }}</td>
                                <td><code>{{ result }}</code></td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    
                    <table class="table table-striped" id="user-pass-results" style="display:none;">
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>User:Pass Entry</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for result in user_pass_results %}
                            <tr>
                                <td>{{ loop.index }}</td>
                                <td><code>{{ result }}</code></td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    
                    <table class="table table-striped" id="url-user-pass-results" style="display:none;">
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>URL:User:Pass Entry</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for result in url_user_pass_results %}
                            <tr>
                                <td>{{ loop.index }}</td>
                                <td><code>{{ result }}</code></td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                
                <div class="mt-3">
                    <a href="{{ url_for('search') }}" class="btn btn-primary">New Search</a>
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

<script>
function toggleResults(type) {
    // Hide all tables
    document.getElementById('all-results').style.display = 'none';
    document.getElementById('user-pass-results').style.display = 'none';
    document.getElementById('url-user-pass-results').style.display = 'none';
    
    // Show selected table
    if (type === 'all') {
        document.getElementById('all-results').style.display = 'table';
    } else if (type === 'user_pass') {
        document.getElementById('user-pass-results').style.display = 'table';
    } else if (type === 'url_user_pass') {
        document.getElementById('url-user-pass-results').style.display = 'table';
    }
}
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
        'search_results.html': search_results_template,
        'files.html': files_template,
        'view_file.html': view_file_template,
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