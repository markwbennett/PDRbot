#!/./.venv/bin/python
"""
PDRBot - Daily Criminal Opinions Scraper

Downloads criminal law opinions from all 14 Texas Courts of Appeals
for the previous business day and stores them in data/ with date-based organization.
Runs Tuesday-Saturday at 12:01 AM to collect opinions from the previous day.
"""

import requests
from bs4 import BeautifulSoup
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin
import time
import logging
import sqlite3
import json
from PyPDF2 import PdfReader, PdfWriter
import io
from pathlib import Path
from dotenv import load_dotenv
import anthropic
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from urllib.parse import quote
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import imaplib
import email
import json

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class PDRBot:
    def __init__(self, data_dir="data"):
        # Load environment variables
        load_dotenv()
        
        self.base_url = os.getenv('BASE_URL', "https://search.txcourts.gov/")
        self.data_dir = data_dir
        self.db_path = os.path.join(data_dir, os.getenv('DB_NAME', "pdrbot.db"))
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': os.getenv('USER_AGENT', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')
        })
        
        # Claude API configuration
        self.claude_api_key = os.getenv('CLAUDE_API_KEY')
        self.claude_model = os.getenv('CLAUDE_MODEL', 'claude-3-5-sonnet-20250107')
        self.claude_max_tokens = int(os.getenv('CLAUDE_MAX_TOKENS', '64000'))
        self.analysis_enabled = os.getenv('ANALYSIS_ENABLED', 'true').lower() == 'true'
        
        # Initialize Claude client if API key is provided
        self.claude_client = None
        if self.claude_api_key and self.analysis_enabled:
            self.claude_client = anthropic.Anthropic(api_key=self.claude_api_key)
        
        # Load analysis prompt
        self.analysis_prompt = self.load_analysis_prompt()
        
        # Configuration from environment
        self.request_timeout = int(os.getenv('REQUEST_TIMEOUT', '30'))
        self.max_retries = int(os.getenv('MAX_RETRIES', '3'))
        self.download_delay = int(os.getenv('DOWNLOAD_DELAY', '1'))
        
        # Email configuration
        self.email_enabled = os.getenv('EMAIL_ENABLED', 'false').lower() == 'true'
        self.email_smtp_host = os.getenv('EMAIL_SMTP_HOST', 'smtp.gmail.com')
        self.email_smtp_port = int(os.getenv('EMAIL_SMTP_PORT', '587'))
        self.email_from = os.getenv('EMAIL_FROM')
        self.email_auth_user = os.getenv('EMAIL_AUTH_USER', self.email_from)  # Default to FROM if not specified
        self.email_password = os.getenv('EMAIL_PASSWORD')
        # Support multiple email recipients (comma-separated)
        email_to_raw = os.getenv('EMAIL_TO')
        if email_to_raw:
            self.email_to = [email.strip() for email in email_to_raw.split(',')]
        else:
            self.email_to = []
        self.email_subject_prefix = os.getenv('EMAIL_SUBJECT_PREFIX', 'PDRBot Daily Report')
        
        # Subscription management configuration
        self.subscription_email = os.getenv('SUBSCRIPTION_EMAIL')
        self.subscription_auth_user = os.getenv('SUBSCRIPTION_AUTH_USER', self.subscription_email)
        self.subscription_password = os.getenv('SUBSCRIPTION_PASSWORD')
        self.subscription_imap_host = os.getenv('SUBSCRIPTION_IMAP_HOST', 'imap.fastmail.com')
        self.subscription_imap_port = int(os.getenv('SUBSCRIPTION_IMAP_PORT', '993'))
        self.members_file = os.getenv('MEMBERS_FILE', 'data/members.json')
        
        # Ensure data directory exists
        os.makedirs(data_dir, exist_ok=True)
        
        # Initialize database
        self.init_database()
    
    def init_database(self):
        """Initialize SQLite database with required tables"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create opinions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS opinions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_number TEXT NOT NULL,
                court TEXT NOT NULL,
                opinion_date DATE NOT NULL,
                opinion_type TEXT NOT NULL,
                justice_name TEXT,
                filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                case_url TEXT NOT NULL,
                pdf_url TEXT,
                download_timestamp TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                UNIQUE(case_number, opinion_type, justice_name)
            )
        ''')
        
        # Create daily_runs table to track execution
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date DATE NOT NULL,
                target_date DATE NOT NULL,
                total_courts_checked INTEGER DEFAULT 0,
                total_cases_found INTEGER DEFAULT 0,
                total_files_downloaded INTEGER DEFAULT 0,
                run_timestamp TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                status TEXT DEFAULT 'running',
                error_message TEXT
            )
        ''')
        
        # Create analysis table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opinion_id INTEGER NOT NULL,
                case_number TEXT NOT NULL,
                court TEXT NOT NULL,
                opinion_date DATE NOT NULL,
                analysis_text TEXT NOT NULL,
                has_interesting_issues BOOLEAN NOT NULL DEFAULT 0,
                issue_count INTEGER DEFAULT 0,
                analysis_timestamp TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                claude_model TEXT NOT NULL,
                FOREIGN KEY (opinion_id) REFERENCES opinions (id),
                UNIQUE(opinion_id)
            )
        ''')
        
        # Create representatives table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS representatives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_number TEXT NOT NULL,
                court TEXT NOT NULL,
                opinion_date DATE NOT NULL,
                party_name TEXT NOT NULL,
                party_type TEXT NOT NULL,
                representative_names TEXT NOT NULL,
                scrape_timestamp TIMESTAMP DEFAULT (datetime('now', 'localtime')),
                UNIQUE(case_number, court, party_name)
            )
        ''')

        # Add pdf_url column if it doesn't exist (for existing databases)
        try:
            cursor.execute('ALTER TABLE opinions ADD COLUMN pdf_url TEXT')
            logger.info("Added pdf_url column to existing opinions table")
        except sqlite3.OperationalError:
            # Column already exists
            pass
        
        conn.commit()
        conn.close()
    
    def get_previous_business_day(self):
        """Get the previous business day (skip weekends)"""
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        
        # If yesterday is weekend, go back to Friday
        while yesterday.weekday() >= 5:  # Saturday=5, Sunday=6
            yesterday -= timedelta(days=1)
        
        return yesterday
    
    def load_analysis_prompt(self):
        """Load the analysis prompt from the pdrbot-prompt file"""
        prompt_file = Path("pdrbot-prompt")
        if prompt_file.exists():
            return prompt_file.read_text().strip()
        else:
            logger.warning("pdrbot-prompt file not found, using default prompt")
            return "Analyze this legal opinion for interesting legal issues."
    
    def extract_text_from_pdf(self, file_path):
        """Extract text content from a PDF file"""
        try:
            with open(file_path, 'rb') as file:
                pdf_reader = PdfReader(file)
                text = ""
                for page in pdf_reader.pages:
                    text += page.extract_text() + "\n"
                return text.strip()
        except Exception as e:
            logger.error(f"Failed to extract text from {file_path}: {e}")
            return None
    
    def analyze_opinion_with_claude(self, text_content, case_number):
        """Send opinion text to Claude for analysis"""
        if not self.claude_client:
            logger.warning("Claude client not initialized - skipping analysis")
            return None
        
        max_retries = 3
        base_delay = 5  # Start with 5 seconds
        
        for attempt in range(max_retries + 1):
            try:
                # Truncate extremely long texts to avoid timeout issues
                max_content_length = 150000  # Approximately 150k characters
                if len(text_content) > max_content_length:
                    logger.warning(f"Truncating large opinion {case_number} from {len(text_content)} to {max_content_length} characters")
                    text_content = text_content[:max_content_length] + "\n\n[CONTENT TRUNCATED DUE TO LENGTH]"
                
                # Use streaming for potentially long requests
                with self.claude_client.messages.stream(
                    model=self.claude_model,
                    max_tokens=self.claude_max_tokens,
                    messages=[{
                        "role": "user",
                        "content": f"{self.analysis_prompt}\n\n--- OPINION TEXT ---\n{text_content}"
                    }]
                ) as stream:
                    analysis_text = ""
                    for text in stream.text_stream:
                        analysis_text += text
                
                logger.info(f"Completed analysis for {case_number}")
                return analysis_text
                
            except Exception as e:
                error_str = str(e)
                
                # Check if this is an overloaded error that we should retry
                is_overloaded = (
                    "overloaded_error" in error_str or 
                    "Overloaded" in error_str or
                    "rate_limit" in error_str.lower() or
                    "too_many_requests" in error_str.lower()
                )
                
                if is_overloaded and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)  # Exponential backoff: 5s, 10s, 20s
                    logger.warning(f"Claude API overloaded for {case_number}, retrying in {delay} seconds (attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"Failed to analyze {case_number} with Claude: {e}")
                    return None
        
        logger.error(f"Failed to analyze {case_number} after {max_retries} retries")
        return None
    
    def save_analysis_to_db(self, opinion_id, case_number, court, opinion_date, analysis_text):
        """Save analysis results to database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # Check if analysis contains interesting issues
            has_interesting_issues = "no interesting issues" not in analysis_text.lower()
            
            # Count issues (rough estimate based on bullet points)
            issue_count = analysis_text.count("▪ Issue Description:")
            
            cursor.execute('''
                INSERT OR REPLACE INTO analysis 
                (opinion_id, case_number, court, opinion_date, analysis_text, 
                 has_interesting_issues, issue_count, claude_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (opinion_id, case_number, court, opinion_date, analysis_text, 
                  has_interesting_issues, issue_count, self.claude_model))
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error saving analysis to database: {e}")
            return False
        finally:
            conn.close()
    
    def get_unanalyzed_opinions(self):
        """Get opinions that haven't been analyzed yet"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT o.id, o.case_number, o.court, o.opinion_date, o.file_path
                FROM opinions o
                LEFT JOIN analysis a ON o.id = a.opinion_id
                WHERE a.opinion_id IS NULL
                ORDER BY o.opinion_date DESC, o.case_number
            ''')
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching unanalyzed opinions: {e}")
            return []
        finally:
            conn.close()
    
    def process_opinion_analysis(self, opinion_id, case_number, court, opinion_date, file_path):
        """Process a single opinion for analysis"""
        logger.info(f"Analyzing opinion: {case_number}")
        
        # Extract text from PDF
        text_content = self.extract_text_from_pdf(file_path)
        if not text_content:
            logger.error(f"Could not extract text from {file_path}")
            return False
        
        # Analyze with Claude
        analysis_result = self.analyze_opinion_with_claude(text_content, case_number)
        if not analysis_result:
            logger.error(f"Could not analyze {case_number}")
            return False
        
        # Save analysis to database
        success = self.save_analysis_to_db(opinion_id, case_number, court, opinion_date, analysis_result)
        if success:
            logger.info(f"Saved analysis for {case_number}")
            
            # If case has interesting issues, scrape representative information
            # Check the analysis text for interesting issues
            has_interesting_issues = "no interesting issues" not in analysis_result.lower()
            if has_interesting_issues:
                case_url = self.generate_case_url(case_number, court)
                self.scrape_case_representatives(case_url, case_number, court, opinion_date)
                # Add small delay to be respectful to the website
                time.sleep(1)
        
        return success
    
    def run_analysis_batch(self, limit=None):
        """Process unanalyzed opinions in batches"""
        if not self.analysis_enabled or not self.claude_client:
            logger.info("Analysis is disabled or Claude client not available")
            return
        
        unanalyzed = self.get_unanalyzed_opinions()
        if limit:
            unanalyzed = unanalyzed[:limit]
        
        if not unanalyzed:
            logger.info("No unanalyzed opinions found")
            return
        
        logger.info(f"Processing {len(unanalyzed)} unanalyzed opinions")
        processed = 0
        failed = 0
        
        for opinion_id, case_number, court, opinion_date, file_path in unanalyzed:
            try:
                if self.process_opinion_analysis(opinion_id, case_number, court, opinion_date, file_path):
                    processed += 1
                else:
                    failed += 1
                
                # Rate limiting - be respectful to Claude API
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"Error processing {case_number}: {e}")
                failed += 1
        
        logger.info(f"Analysis batch complete: {processed} processed, {failed} failed")
        
        # Don't generate intermediate reports - only generate after all analysis is complete
    
    def analyze_directory_pdfs(self, directory_path):
        """Analyze all PDF files in a specific directory"""
        if not os.path.exists(directory_path):
            logger.error(f"Directory not found: {directory_path}")
            return
        
        # Find all PDF files in the directory
        pdf_files = []
        for filename in os.listdir(directory_path):
            if filename.endswith('.pdf') and not filename.startswith('analysis_report'):
                pdf_path = os.path.join(directory_path, filename)
                # Extract case number from filename
                case_number = filename.replace('.pdf', '')
                pdf_files.append((case_number, pdf_path))
        
        if not pdf_files:
            logger.info(f"No PDF files found in {directory_path}")
            return
        
        logger.info(f"Found {len(pdf_files)} PDF files in {directory_path}")
        processed = 0
        failed = 0
        
        for case_number, pdf_path in pdf_files:
            try:
                # Check if this case is already analyzed
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT COUNT(*) FROM opinions o
                    JOIN analysis a ON o.id = a.opinion_id
                    WHERE o.case_number = ?
                ''', (case_number,))
                already_analyzed = cursor.fetchone()[0] > 0
                conn.close()
                
                if already_analyzed:
                    logger.info(f"Skipping {case_number} - already analyzed")
                    continue
                
                # Find or create opinion record
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute('SELECT id FROM opinions WHERE case_number = ?', (case_number,))
                result = cursor.fetchone()
                
                if result:
                    opinion_id = result[0]
                else:
                    # Create a basic opinion record for this PDF
                    # Extract court and date from case number (e.g., "01-23-00771-CR")
                    parts = case_number.split('-')
                    if len(parts) >= 3:
                        court = f"COA{parts[0]}"
                        year = f"20{parts[1]}"
                        # Use a default date based on directory name
                        dir_name = os.path.basename(directory_path)
                        if len(dir_name) == 8 and dir_name.isdigit():
                            opinion_date = f"{dir_name[:4]}-{dir_name[4:6]}-{dir_name[6:8]}"
                        else:
                            opinion_date = f"{year}-01-01"  # Default date
                        
                        cursor.execute('''
                            INSERT INTO opinions 
                            (case_number, court, opinion_date, opinion_type, filename, file_path, case_url)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (case_number, court, opinion_date, "combined", filename, pdf_path, ""))
                        opinion_id = cursor.lastrowid
                        conn.commit()
                    else:
                        logger.warning(f"Could not parse case number format: {case_number}")
                        conn.close()
                        continue
                
                conn.close()
                
                # Process the analysis
                if self.process_opinion_analysis(opinion_id, case_number, court, opinion_date, pdf_path):
                    processed += 1
                else:
                    failed += 1
                
                # Rate limiting
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"Error processing {case_number}: {e}")
                failed += 1
        
        logger.info(f"Directory analysis complete: {processed} processed, {failed} failed")
        
        # Don't generate intermediate reports - only generate after all analysis is complete
    
    def backfill_pdf_urls(self):
        """Backfill PDF URLs for existing records that don't have them"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # Find records without PDF URLs
            cursor.execute('''
                SELECT id, case_number, court, opinion_date 
                FROM opinions 
                WHERE pdf_url IS NULL OR pdf_url = ''
            ''')
            records = cursor.fetchall()
            
            if not records:
                logger.info("No records need PDF URL backfill")
                return
            
            logger.info(f"Backfilling PDF URLs for {len(records)} records")
            
            for opinion_id, case_number, court, opinion_date in records:
                # Try to reconstruct the PDF URLs by re-scraping the case
                try:
                    coa_num = int(court.replace("COA", ""))
                    date_obj = datetime.strptime(str(opinion_date), '%Y-%m-%d').date()
                    
                    # Get the docket page for this case
                    url = self.get_docket_url(coa_num, date_obj)
                    response = self.get_with_retry(url)
                    
                    if response:
                        soup = BeautifulSoup(response.content, 'html.parser')
                        criminal_cases = self.parse_criminal_causes(soup)
                        
                        # Find the matching case
                        for case in criminal_cases:
                            if case['case_number'] == case_number:
                                pdf_urls = [link['url'] for link in case['pdf_links']]
                                pdf_urls_string = ';'.join(pdf_urls) if pdf_urls else None
                                
                                # Update the record
                                cursor.execute('''
                                    UPDATE opinions 
                                    SET pdf_url = ?
                                    WHERE id = ?
                                ''', (pdf_urls_string, opinion_id))
                                
                                logger.info(f"Updated PDF URLs for {case_number}")
                                break
                    
                    # Small delay to be respectful
                    time.sleep(1)
                    
                except Exception as e:
                    logger.warning(f"Could not backfill PDF URL for {case_number}: {e}")
            
            conn.commit()
            logger.info("PDF URL backfill complete")
            
        except Exception as e:
            logger.error(f"Error during PDF URL backfill: {e}")
        finally:
            conn.close()
    
    def get_analysis_results(self, date_filter=None, interesting_only=True):
        """Get analysis results for report generation"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            query = '''
                SELECT a.case_number, a.court, a.opinion_date, a.analysis_text, 
                       a.has_interesting_issues, a.issue_count, a.analysis_timestamp,
                       o.file_path, o.case_url, o.pdf_url
                FROM analysis a
                JOIN opinions o ON a.opinion_id = o.id
            '''
            params = []
            
            conditions = []
            
            if interesting_only:
                conditions.append("a.has_interesting_issues = 1")
            
            if date_filter:
                # For daily reports, we want exact date matching
                conditions.append("a.opinion_date = ?")
                params.append(date_filter)
            
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            
            # Order by interesting issues first, then by date and case number
            query += " ORDER BY a.has_interesting_issues DESC, a.opinion_date DESC, a.case_number"
            
            cursor.execute(query, params)
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching analysis results: {e}")
            return []
        finally:
            conn.close()
    
    def generate_case_url(self, case_number, court):
        """Generate the online case URL"""
        base_url = "https://search.txcourts.gov/Case.aspx?cn="
        return f"{base_url}{case_number}"
    
    def scrape_case_representatives(self, case_url, case_number, court, opinion_date):
        """Scrape representative information from case URL"""
        try:
            logger.info(f"Scraping representatives for case {case_number}")
            response = self.get_with_retry(case_url)
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Find the parties panel - search for panel-heading containing "Parties"
            parties_panel = soup.find('div', class_=['panel-heading', 'panel-heading-content'], string=lambda text: text and 'Parties' in text)
            if not parties_panel:
                logger.warning(f"No parties panel found for case {case_number}")
                return []
            
            # Navigate to the table containing party information
            panel_content = parties_panel.find_next_sibling('div', class_='panel-content')
            if not panel_content:
                logger.warning(f"No panel content found for case {case_number}")
                return []
            
            # Find the grid table
            party_table = panel_content.find('table', class_='rgMasterTable')
            if not party_table:
                logger.warning(f"No party table found for case {case_number}")
                return []
            
            representatives = []
            tbody = party_table.find('tbody')
            if tbody:
                for row in tbody.find_all('tr'):
                    cells = row.find_all('td')
                    if len(cells) >= 3:
                        party_name = cells[0].get_text(strip=True)
                        party_type = cells[1].get_text(strip=True)
                        representative_cell = cells[2]
                        
                        # Skip "State of Texas" entries
                        if "State of Texas" in party_name or "Criminal - State of Texas" in party_type:
                            continue
                        
                        # Extract representative names (they may be separated by <br> tags)
                        rep_names = []
                        for br in representative_cell.find_all('br'):
                            br.replace_with('\n')
                        rep_text = representative_cell.get_text()
                        
                        # Clean and split representative names
                        for name in rep_text.split('\n'):
                            name = name.strip()
                            if name and name not in rep_names:
                                rep_names.append(name)
                        
                        if rep_names:
                            representatives.append({
                                'party_name': party_name,
                                'party_type': party_type,
                                'representative_names': ', '.join(rep_names)
                            })
            
            # Save to database
            self.save_representatives_to_db(case_number, court, opinion_date, representatives)
            
            return representatives
            
        except Exception as e:
            logger.error(f"Error scraping representatives for case {case_number}: {e}")
            return []
    
    def save_representatives_to_db(self, case_number, court, opinion_date, representatives):
        """Save representative information to database"""
        if not representatives:
            return
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            for rep in representatives:
                cursor.execute('''
                    INSERT OR REPLACE INTO representatives 
                    (case_number, court, opinion_date, party_name, party_type, representative_names)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (case_number, court, opinion_date, rep['party_name'], 
                      rep['party_type'], rep['representative_names']))
            
            conn.commit()
            logger.info(f"Saved {len(representatives)} representative entries for case {case_number}")
            
        except Exception as e:
            logger.error(f"Error saving representatives to database: {e}")
        finally:
            conn.close()
    
    def get_case_representatives(self, case_number, court):
        """Get representative information for a specific case"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT party_name, party_type, representative_names
                FROM representatives 
                WHERE case_number = ? AND court = ?
                ORDER BY party_type
            ''', (case_number, court))
            
            results = cursor.fetchall()
            return [{'party_name': row[0], 'party_type': row[1], 'representative_names': row[2]} 
                    for row in results]
        except Exception as e:
            logger.error(f"Error getting representatives for case {case_number}: {e}")
            return []
        finally:
            conn.close()

    def get_opinion_pdf_urls(self, case_number, court):
        """Get the original PDF URLs for a case by reconstructing from case page structure"""
        # This is a simplified approach - ideally we'd store these during download
        # For now, we'll link to the case page where users can access the PDFs
        court_num = court.replace("COA", "").zfill(2)
        case_encoded = quote(case_number)
        
        # The actual PDF URLs follow a pattern but are complex to reconstruct
        # So we'll provide the case page URL where PDFs can be accessed
        return [f"https://search.txcourts.gov/Case.aspx?cn={case_encoded}&coa={court_num}"]
    
    def generate_pdf_path(self, file_path):
        """Generate a relative path to the PDF file for links"""
        # Convert absolute path to relative from report location
        return os.path.relpath(file_path, self.data_dir)
    
    def create_pdf_styles(self):
        """Create custom styles for the PDF report"""
        styles = getSampleStyleSheet()
        
        # Custom styles
        styles.add(ParagraphStyle(
            name='CustomTitle',
            parent=styles['Heading1'],
            fontSize=18,
            spaceAfter=30,
            alignment=TA_CENTER,
            textColor=colors.darkblue
        ))
        
        styles.add(ParagraphStyle(
            name='CaseTitle',
            parent=styles['Heading2'],
            fontSize=14,
            spaceAfter=12,
            spaceBefore=20,
            textColor=colors.darkred
        ))
        
        styles.add(ParagraphStyle(
            name='CaseInfo',
            parent=styles['Normal'],
            fontSize=10,
            spaceAfter=8,
            textColor=colors.grey
        ))
        
        styles.add(ParagraphStyle(
            name='Analysis',
            parent=styles['Normal'],
            fontSize=11,
            spaceAfter=12,
            alignment=TA_JUSTIFY
        ))
        
        return styles
    
    def get_unique_filename(self, base_filename):
        """Generate a unique filename by appending -1, -2, etc. if file exists"""
        output_path = os.path.join(self.data_dir, base_filename)
        if not os.path.exists(output_path):
            return base_filename
        
        name, ext = os.path.splitext(base_filename)
        counter = 1
        while True:
            new_filename = f"{name}-{counter}{ext}"
            new_path = os.path.join(self.data_dir, new_filename)
            if not os.path.exists(new_path):
                return new_filename
            counter += 1
    
    def generate_analysis_report(self, date_filter=None, output_filename=None):
        """Generate a comprehensive PDF report of analysis results"""
        logger.info("Generating analysis PDF report...")
        
        # Get analysis results (include all cases, interesting first)
        results = self.get_analysis_results(date_filter=date_filter, interesting_only=False)
        
        if not results:
            logger.info("No analyzed cases found for report generation")
            return None
        
        # Determine the opinion date for the report title
        opinion_dates = [row[2] for row in results]  # opinion_date column
        if date_filter:
            # Handle both datetime objects and strings
            if hasattr(date_filter, 'strftime'):
                report_date = date_filter.strftime("%Y-%m-%d")
            else:
                report_date = str(date_filter)
        elif opinion_dates:
            # Use the most common date or latest date
            from collections import Counter
            date_counter = Counter(opinion_dates)
            most_common_date = date_counter.most_common(1)[0][0]
            report_date = str(most_common_date)
        else:
            report_date = datetime.now().strftime("%Y-%m-%d")
        
        # Create output filename if not provided
        if not output_filename:
            base_filename = f"pdrbot_report_{report_date.replace('-', '')}.pdf"
            output_filename = self.get_unique_filename(base_filename)
        
        output_path = os.path.join(self.data_dir, output_filename)
        
        # Create PDF document
        doc = SimpleDocTemplate(
            output_path,
            pagesize=letter,
            rightMargin=72,
            leftMargin=72,
            topMargin=72,
            bottomMargin=18
        )
        
        # Get styles
        styles = self.create_pdf_styles()
        story = []
        
        # Title with opinion date
        title = Paragraph(f"PDRBot Report<br/>Interesting Legal Issues<br/>{report_date} Handdowns", styles['CustomTitle'])
        story.append(title)
        story.append(Spacer(1, 20))
        
        # AI Disclaimer
        disclaimer_text = f"This report is AI-generated by {self.claude_model} for the sole purpose of assisting experts in finding cases that might be PDR worthy. Do not depend on it."
        disclaimer = Paragraph(disclaimer_text, styles['CaseInfo'])
        story.append(disclaimer)
        story.append(Spacer(1, 20))
        
        # Summary statistics
        total_cases = len(results)
        interesting_cases = sum(1 for row in results if row[4])  # has_interesting_issues column
        total_issues = sum(row[5] for row in results)  # issue_count column
        
        summary_data = [
            ['Total Cases Analyzed:', str(total_cases)],
            ['Cases with Interesting Issues:', str(interesting_cases)],
            ['Report Generated:', datetime.now().strftime("%B %d, %Y at %I:%M %p")],
        ]
        
        summary_table = Table(summary_data, colWidths=[3*inch, 2*inch])
        summary_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        
        story.append(summary_table)
        story.append(Spacer(1, 30))
        
        # Track when we switch from interesting to non-interesting cases
        added_non_interesting_header = False
        
        # Process each case
        for i, (case_number, court, opinion_date, analysis_text, has_interesting, 
                issue_count, analysis_timestamp, file_path, case_url, pdf_url) in enumerate(results):
            
            # Add section header when we reach non-interesting cases
            if not has_interesting and not added_non_interesting_header:
                # Only add the header if we actually have non-interesting cases to show
                remaining_cases = results[i:]
                non_interesting_remaining = [r for r in remaining_cases if not r[4]]  # has_interesting column
                if non_interesting_remaining:
                    section_header = Paragraph("Cases with No Interesting Legal Issues", styles['CustomTitle'])
                    story.append(PageBreak())
                    story.append(section_header)
                    story.append(Spacer(1, 20))
                    added_non_interesting_header = True
            
            # Case title with links
            case_status = "⭐ " if has_interesting else ""
            case_title = f"{case_status}Case {i+1}: {case_number} ({court})"
            story.append(Paragraph(case_title, styles['CaseTitle']))
            
            # Case information with clickable links
            case_url_link = self.generate_case_url(case_number, court)
            case_info_parts = [
                f"<b>Date:</b> {opinion_date}",
                f"<b>Issues Found:</b> {issue_count}",
                f"<b>Case Page:</b> <link href='{case_url_link}' color='blue'>View Case Details</link>",
            ]
            
            # Add direct links to PDF opinions if available
            if pdf_url and pdf_url.strip():
                pdf_urls = pdf_url.split(';')
                if len(pdf_urls) == 1:
                    case_info_parts.append(f"<b>Opinion PDF:</b> <link href='{pdf_urls[0]}' color='blue'>Direct PDF Link</link>")
                else:
                    # Multiple PDFs - list them
                    for i, url in enumerate(pdf_urls, 1):
                        case_info_parts.append(f"<b>Opinion {i} PDF:</b> <link href='{url}' color='blue'>Direct PDF Link {i}</link>")
            else:
                # Fallback to case page
                case_info_parts.append(f"<b>Online Opinions:</b> <link href='{case_url_link}' color='blue'>View on Court Website</link>")
            
            # Optionally include local file link as well
            if file_path and os.path.exists(file_path):
                local_link = f"file://{os.path.abspath(file_path)}"
                case_info_parts.append(f"<b>Local PDF:</b> <link href='{local_link}' color='grey'>{os.path.basename(file_path)}</link>")
            
            case_info = "<br/>".join(case_info_parts)
            story.append(Paragraph(case_info, styles['CaseInfo']))
            story.append(Spacer(1, 10))
            
            # Add representative information for interesting cases
            if has_interesting:
                representatives = self.get_case_representatives(case_number, court)
                if representatives:
                    rep_info_parts = ["<b>Defense Representatives:</b>"]
                    for rep in representatives:
                        party_info = f"• <i>{rep['party_name']}</i> ({rep['party_type']}): {rep['representative_names']}"
                        rep_info_parts.append(party_info)
                    
                    rep_info = "<br/>".join(rep_info_parts)
                    story.append(Paragraph(rep_info, styles['CaseInfo']))
                    story.append(Spacer(1, 10))
            
            # Analysis text (clean up formatting for PDF)
            analysis_clean = analysis_text.replace('▪', '•').replace('◦', '○')
            
            # Convert markdown formatting to HTML for PDF
            import re
            # Remove markdown headers (# and ##)
            analysis_clean = re.sub(r'^#+\s*', '', analysis_clean, flags=re.MULTILINE)
            # Remove Priority Level sections completely
            analysis_clean = re.sub(r'\*\*▪ Priority Level:\*\*[^\n]*\n?', '', analysis_clean)
            analysis_clean = re.sub(r'\*\*Priority Level:\*\*[^\n]*\n?', '', analysis_clean)
            # Convert **bold** to <b>bold</b>
            analysis_clean = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', analysis_clean)
            # Convert *italic* to <i>italic</i> (but not when part of **)
            analysis_clean = re.sub(r'(?<!\*)\*([^*]+?)\*(?!\*)', r'<i>\1</i>', analysis_clean)
            
            # Split analysis into paragraphs for better formatting
            analysis_paragraphs = analysis_clean.split('\n\n')
            
            # Filter out empty paragraphs and very short ones that could cause blank pages
            valid_paragraphs = []
            for para in analysis_paragraphs:
                para_content = para.strip()
                # Skip empty paragraphs and very short lines that are just formatting artifacts
                if len(para_content) > 5 and not para_content.isspace():
                    valid_paragraphs.append(para_content)
            
            # Only add paragraphs if we have substantial content
            if valid_paragraphs:
                for para_content in valid_paragraphs:
                    story.append(Paragraph(para_content, styles['Analysis']))
                    story.append(Spacer(1, 6))  # Small spacing between paragraphs
            
            # Add page break except for last case, and only if we added content
            if i < len(results) - 1:
                # Add some spacing before page break to avoid orphaned content
                story.append(Spacer(1, 12))
                story.append(PageBreak())
        
        # Build PDF
        try:
            doc.build(story)
            logger.info(f"Analysis report generated: {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"Error generating PDF report: {e}")
            return None
    
    def generate_daily_report(self, target_date=None):
        """Generate a report for a specific date"""
        if target_date is None:
            target_date = self.get_previous_business_day()
        
        # Convert datetime to string format for database comparison
        if hasattr(target_date, 'strftime'):
            date_filter = target_date.strftime('%Y-%m-%d')
        else:
            date_filter = target_date
        
        return self.generate_analysis_report(date_filter=date_filter)
    
    def get_docket_url(self, coa_num, date):
        """Generate the docket URL for a specific court and date"""
        date_str = date.strftime("%m/%d/%Y")
        return f"{self.base_url}Docket.aspx?coa=coa{coa_num:02d}&FullDate={date_str}"
    
    def get_abbreviation_and_justice(self, description, disposition=""):
        """Get abbreviation for opinion type and justice name based on description and disposition"""
        description_lower = description.lower()
        disposition_lower = disposition.lower()
        
        # Check disposition ONLY if it contains concurring or dissenting
        if "concurring" in disposition_lower:
            # Look for justice name in description
            justice_match = re.search(r'(?:concurring )?opinion by (?:chief )?justice (\w+)', description_lower)
            justice_name = justice_match.group(1) if justice_match else None
            return "con", justice_name
        elif "dissenting" in disposition_lower:
            # Look for justice name in description
            justice_match = re.search(r'(?:dissenting )?opinion by (?:chief )?justice (\w+)', description_lower)
            justice_name = justice_match.group(1) if justice_match else None
            return "dis", justice_name
        
        # Otherwise, use description to determine type
        if "memorandum" in description_lower:
            return "mem", None
        elif "dissenting" in description_lower:
            justice_match = re.search(r'dissenting opinion by (?:chief )?justice (\w+)', description_lower)
            justice_name = justice_match.group(1) if justice_match else None
            return "dis", justice_name
        elif "concurring" in description_lower:
            justice_match = re.search(r'concurring opinion by (?:chief )?justice (\w+)', description_lower)
            justice_name = justice_match.group(1) if justice_match else None
            return "con", justice_name
        elif "opinion" in description_lower:
            return "op", None
        
        return "", None
    
    def extract_case_number(self, case_link_text):
        """Extract case number from link text"""
        match = re.search(r'\d{2}-\d{2}-\d{5}-CR', case_link_text)
        return match.group(0) if match else None
    
    def download_pdf(self, pdf_url, filepath, max_retries=None):
        """Download PDF file to specified path with retry logic"""
        if max_retries is None:
            max_retries = self.max_retries
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                response = self.session.get(pdf_url, timeout=self.request_timeout)
                response.raise_for_status()
                
                # Validate response is actually a PDF
                if response.headers.get('content-type', '').lower() != 'application/pdf':
                    logger.warning(f"Response is not a PDF (attempt {attempt + 1}): {response.headers.get('content-type', 'unknown')}")
                    if attempt == max_retries - 1:
                        logger.error(f"Not a PDF after {max_retries} attempts: {os.path.basename(filepath)}")
                        return False
                    time.sleep(2 ** attempt)  # Exponential backoff
                    continue
                
                # Ensure directory exists
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                
                with open(filepath, 'wb') as f:
                    f.write(response.content)
                
                # Verify file was written and has content
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    # Basic PDF validation - check for PDF magic bytes
                    with open(filepath, 'rb') as f:
                        header = f.read(4)
                        if not header.startswith(b'%PDF'):
                            raise Exception("Downloaded file is not a valid PDF")
                    
                    logger.info(f"Downloaded: {os.path.basename(filepath)}")
                    return True
                else:
                    raise Exception("File was not written or is empty")
                    
            except (requests.exceptions.RequestException, requests.exceptions.Timeout, 
                    requests.exceptions.ConnectionError, Exception) as e:
                last_exception = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                    logger.warning(f"Download failed (attempt {attempt + 1}/{max_retries}): {os.path.basename(filepath)} - {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Failed to download {os.path.basename(filepath)} after {max_retries} attempts: {e}")
        
        return False
    
    def get_with_retry(self, url, max_retries=None):
        """Make HTTP request with retry logic"""
        if max_retries is None:
            max_retries = self.max_retries
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=self.request_timeout)
                response.raise_for_status()
                return response
                
            except (requests.exceptions.RequestException, requests.exceptions.Timeout, 
                    requests.exceptions.ConnectionError) as e:
                last_exception = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                    logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries}): {url} - {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Failed to fetch {url} after {max_retries} attempts: {e}")
                    raise last_exception
    
    def parse_criminal_causes(self, soup):
        """Parse the Criminal Causes Decided section"""
        criminal_cases = []
        
        # Look for the "Criminal Causes Decided" heading
        criminal_heading = None
        h3_elements = soup.find_all('h3')
        for h3 in h3_elements:
            if 'Criminal Causes Decided' in h3.get_text():
                criminal_heading = h3
                break
        
        if not criminal_heading:
            return criminal_cases
        
        # Find the table that follows the heading
        table = criminal_heading.find_next('table', class_='rgMasterTable')
        if not table:
            return criminal_cases
        
        # Find all rows in the table body
        tbody = table.find('tbody')
        if not tbody:
            return criminal_cases
        
        rows = tbody.find_all('tr', class_=['rgRow', 'rgAltRow'])
        
        for row in rows:
            case_data = self.parse_case_row(row)
            if case_data:
                criminal_cases.append(case_data)
        
        return criminal_cases
    
    def parse_case_row(self, row):
        """Parse a single case row"""
        try:
            # Find the case number link
            case_link = row.find('a', href=re.compile(r'Case\.aspx\?cn=.*-CR'))
            if not case_link:
                return None
            
            case_number = self.extract_case_number(case_link.text.strip())
            if not case_number:
                return None
            
            # Find the disposition column - look for td with class "caseDisp"
            disposition = ""
            disposition_cell = row.find('td', class_='caseDisp')
            if disposition_cell:
                disposition = disposition_cell.text.strip()
            else:
                # Fallback: try 3rd column if caseDisp class not found
                cells = row.find_all('td')
                if len(cells) >= 3:
                    disposition = cells[2].text.strip()
            
            # Find all PDF links in this row
            pdf_links = []
            doc_tables = row.find_all('table', class_='docGrid')
            
            for doc_table in doc_tables:
                pdf_link = doc_table.find('a', href=re.compile(r'SearchMedia\.aspx'))
                if pdf_link:
                    # Get the description (opinion type)
                    desc_cell = pdf_link.find_parent('td').find_previous_sibling('td')
                    description = desc_cell.text.strip() if desc_cell else ""
                    
                    # Clean up the PDF URL
                    href = pdf_link['href']
                    href = href.replace('" + this.CurrentWebState.CurrentCourt + @"', 'coa01')
                    pdf_url = urljoin(self.base_url, href)
                    pdf_links.append({
                        'url': pdf_url,
                        'description': description
                    })
            
            if pdf_links:
                return {
                    'case_number': case_number,
                    'pdf_links': pdf_links,
                    'disposition': disposition
                }
        
        except Exception as e:
            logger.error(f"Error parsing case row: {e}")
        
        return None
    
    def get_opinion_sort_order(self, opinion_type):
        """Get sort order for opinion types: main opinions first, then concurring, then dissenting"""
        order_map = {
            'mem': 1,  # Memorandum opinions first
            'op': 1,   # Regular opinions first
            'con': 2,  # Concurring opinions second
            'dis': 3,  # Dissenting opinions last
            'opinion': 1  # Default opinions first
        }
        return order_map.get(opinion_type, 4)  # Unknown types go last
    
    def concatenate_pdfs(self, pdf_paths, output_path):
        """Concatenate multiple PDF files into one"""
        writer = PdfWriter()
        
        for pdf_path in pdf_paths:
            if os.path.exists(pdf_path):
                try:
                    reader = PdfReader(pdf_path)
                    for page in reader.pages:
                        writer.add_page(page)
                except Exception as e:
                    logger.error(f"Error reading PDF {pdf_path}: {e}")
        
        with open(output_path, 'wb') as output_file:
            writer.write(output_file)
    
    def process_case_opinions(self, case_number, case_info, court_name, date, date_folder):
        """Download and concatenate all opinions for a single case"""
        opinions = case_info['opinions']
        temp_files = []
        downloaded_count = 0
        
        # Final combined filename
        final_filename = f"{case_number}.pdf"
        final_filepath = os.path.join(date_folder, final_filename)
        
        # Check if final file already exists
        if os.path.exists(final_filepath):
            logger.info(f"Combined file already exists: {final_filename}")
            return 0
        
        # Sort opinions: main (mem/op) first, then concurring, then dissenting
        sorted_opinions = sorted(opinions, key=lambda x: (x['sort_order'], x['justice_name'] or ''))
        
        # Download individual opinion PDFs to temp files
        failed_opinions = []
        for opinion in sorted_opinions:
            temp_path = os.path.join(date_folder, opinion['temp_filename'])
            
            if self.download_pdf(opinion['url'], temp_path):
                temp_files.append(temp_path)
                downloaded_count += 1
            else:
                failed_opinions.append(opinion)
            
            time.sleep(self.download_delay)  # Delay between downloads
        
        # Log failed downloads for visibility
        if failed_opinions:
            failed_descriptions = [op['description'] for op in failed_opinions]
            logger.warning(f"Failed to download {len(failed_opinions)} opinions for case {case_number}: {', '.join(failed_descriptions)}")
        
        # Concatenate PDFs if we have multiple opinions, otherwise just rename
        if len(temp_files) > 1:
            logger.info(f"Concatenating {len(temp_files)} opinions for case {case_number}")
            self.concatenate_pdfs(temp_files, final_filepath)
        elif len(temp_files) == 1:
            # Single opinion - just rename the temp file
            os.rename(temp_files[0], final_filepath)
            temp_files = []  # Don't delete the file we just renamed
        else:
            logger.warning(f"No PDFs downloaded for case {case_number}")
            return 0
        
        # Clean up temp files
        for temp_file in temp_files:
            try:
                os.remove(temp_file)
            except Exception as e:
                logger.warning(f"Could not delete temp file {temp_file}: {e}")
        
        # Generate case URL and save to database
        case_url = f"https://search.txcourts.gov/Case.aspx?cn={case_number}"
        
        # Create summary of opinion types for database
        opinion_types = []
        justices = []
        for opinion in sorted_opinions:
            opinion_types.append(opinion['abbrev'])
            if opinion['justice_name']:
                justices.append(f"{opinion['abbrev']}_{opinion['justice_name']}")
        
        opinion_type_summary = '+'.join(opinion_types)
        justice_summary = ';'.join(justices) if justices else None
        
        # Collect all PDF URLs for this case
        pdf_urls = [opinion['url'] for opinion in sorted_opinions]
        pdf_urls_string = ';'.join(pdf_urls) if pdf_urls else None
        
        # Save to database
        self.save_opinion_to_db(
            case_number=case_number,
            court=court_name,
            opinion_date=date,
            opinion_type=opinion_type_summary,
            justice_name=justice_summary,
            filename=final_filename,
            file_path=final_filepath,
            case_url=case_url,
            pdf_url=pdf_urls_string
        )
        
        logger.info(f"Created combined opinion file: {final_filename}")
        return 1  # Return 1 for the combined file
    
    def save_opinion_to_db(self, case_number, court, opinion_date, opinion_type, justice_name, filename, file_path, case_url, pdf_url=None):
        """Save opinion information to database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO opinions 
                (case_number, court, opinion_date, opinion_type, justice_name, filename, file_path, case_url, pdf_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (case_number, court, opinion_date, opinion_type, justice_name, filename, file_path, case_url, pdf_url))
            
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error saving to database: {e}")
            return False
        finally:
            conn.close()
    
    def scrape_court_date(self, coa_num, date, date_folder):
        """Scrape opinions for a specific court and date"""
        url = self.get_docket_url(coa_num, date)
        date_str = date.strftime("%Y-%m-%d")
        court_name = f"COA{coa_num:02d}"
        
        logger.info(f"Scraping {court_name} for {date_str}")
        
        try:
            response = self.get_with_retry(url)
            soup = BeautifulSoup(response.content, 'html.parser')
            criminal_cases = self.parse_criminal_causes(soup)
            
            if not criminal_cases:
                logger.debug(f"No criminal cases found for {court_name} on {date_str}")
                return 0, 0
            
            downloaded_count = 0
            total_cases = len(criminal_cases)
            total_opinions_attempted = 0
            
            # Group opinions by case for concatenation
            case_opinions = {}
            for case in criminal_cases:
                case_number = case['case_number']
                disposition = case.get('disposition', '')
                
                if case_number not in case_opinions:
                    case_opinions[case_number] = {
                        'case_data': case,
                        'opinions': []
                    }
                
                for i, pdf_link in enumerate(case['pdf_links']):
                    description = pdf_link['description']
                    pdf_url = pdf_link['url']
                    
                    # Generate filename
                    abbrev, justice_name = self.get_abbreviation_and_justice(description, disposition)
                    if abbrev:
                        if justice_name and abbrev in ['con', 'dis']:
                            temp_filename = f"{case_number}_{abbrev}_{justice_name}_temp.pdf"
                        else:
                            temp_filename = f"{case_number}_{abbrev}_temp.pdf"
                    else:
                        suffix = f"_{i+1}" if len(case['pdf_links']) > 1 else ""
                        temp_filename = f"{case_number}{suffix}_temp.pdf"
                    
                    case_opinions[case_number]['opinions'].append({
                        'url': pdf_url,
                        'description': description,
                        'disposition': disposition,
                        'abbrev': abbrev or "opinion",
                        'justice_name': justice_name,
                        'temp_filename': temp_filename,
                        'sort_order': self.get_opinion_sort_order(abbrev or "opinion")
                    })
                    total_opinions_attempted += 1
            
            # Process each case: download individual PDFs and concatenate
            for case_number, case_info in case_opinions.items():
                try:
                    downloaded_count += self.process_case_opinions(
                        case_number, case_info, court_name, date, date_folder
                    )
                except Exception as e:
                    logger.error(f"Error processing case {case_number}: {e}")
                
                # Be respectful with delays
                time.sleep(self.download_delay)
            
            logger.info(f"Downloaded {downloaded_count} files from {court_name} on {date_str}")
            if total_opinions_attempted > downloaded_count:
                failed_count = total_opinions_attempted - downloaded_count
                logger.warning(f"Failed to download {failed_count} out of {total_opinions_attempted} opinions for {court_name} on {date_str}")
            return total_cases, downloaded_count
            
        except Exception as e:
            logger.error(f"Error scraping {court_name} on {date_str}: {e}")
            return 0, 0
    
    def run_daily_scrape(self):
        """Run daily scrape for the previous business day"""
        target_date = self.get_previous_business_day()
        run_date = datetime.now().date()
        
        logger.info(f"Starting daily scrape for {target_date.strftime('%Y-%m-%d')}")
        
        # Create date-based folder
        date_folder = os.path.join(self.data_dir, target_date.strftime('%Y%m%d'))
        os.makedirs(date_folder, exist_ok=True)
        
        # Initialize run record
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO daily_runs (run_date, target_date, status)
            VALUES (?, ?, 'running')
        ''', (run_date, target_date))
        run_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        total_cases = 0
        total_downloaded = 0
        courts_checked = 0
        
        try:
            # Scrape all 14 courts
            for coa_num in range(1, 15):
                cases_found, files_downloaded = self.scrape_court_date(coa_num, target_date, date_folder)
                total_cases += cases_found
                total_downloaded += files_downloaded
                courts_checked += 1
                
                # Small delay between courts
                time.sleep(int(os.getenv('COURT_DELAY', '2')))
            
            # Update run record as completed
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE daily_runs 
                SET total_courts_checked = ?, total_cases_found = ?, 
                    total_files_downloaded = ?, status = 'completed'
                WHERE id = ?
            ''', (courts_checked, total_cases, total_downloaded, run_id))
            conn.commit()
            conn.close()
            
            logger.info(f"Daily scrape completed!")
            logger.info(f"Courts checked: {courts_checked}")
            logger.info(f"Total cases found: {total_cases}")
            logger.info(f"Total files downloaded: {total_downloaded}")
            logger.info(f"Files saved to: {date_folder}")
            
            # Run analysis if enabled and new files were downloaded
            if self.analysis_enabled and total_downloaded > 0:
                logger.info("Starting analysis of newly downloaded opinions...")
                self.run_analysis_batch(limit=total_downloaded)
            
        except Exception as e:
            # Update run record with error
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE daily_runs 
                SET total_courts_checked = ?, total_cases_found = ?, 
                    total_files_downloaded = ?, status = 'error', error_message = ?
                WHERE id = ?
            ''', (courts_checked, total_cases, total_downloaded, str(e), run_id))
            conn.commit()
            conn.close()
            
            logger.error(f"Daily scrape failed: {e}")
            raise

    def send_email_report(self, report_path, target_date):
        """Send email with PDF report attachment"""
        if not self.email_enabled:
            logger.info("Email sending is disabled")
            return False
        
        # Get all recipients (static + dynamic members)
        all_recipients = self.get_all_recipients()
        
        if not all([self.email_from, self.email_auth_user, self.email_password, report_path]) or not all_recipients:
            logger.error("Email configuration incomplete or no recipients")
            return False
            
        if not os.path.exists(report_path):
            logger.error(f"Report file not found: {report_path}")
            return False
        
        try:
            # Create message
            msg = MIMEMultipart()
            msg['From'] = self.email_from
            msg['To'] = ', '.join(all_recipients)  # Join all recipients
            msg['Subject'] = f"{self.email_subject_prefix} - {target_date}"
            
            # Email body
            body = f"""Daily PDRBot Report

Attached is the criminal law opinion analysis for {target_date}.

This report contains AI-generated analysis of Texas Courts of Appeals criminal opinions for potential PDR worthiness.

Report generated: {datetime.now().strftime("%B %d, %Y at %I:%M %p")}
"""
            
            msg.attach(MIMEText(body, 'plain'))
            
            # Attach PDF
            with open(report_path, "rb") as attachment:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(attachment.read())
                encoders.encode_base64(part)
                filename = os.path.basename(report_path)
                part.add_header(
                    'Content-Disposition',
                    f'attachment; filename= {filename}'
                )
                msg.attach(part)
            
            # Send email
            if self.email_smtp_port == 465:
                # Use SSL for port 465
                server = smtplib.SMTP_SSL(self.email_smtp_host, self.email_smtp_port)
            else:
                # Use STARTTLS for port 587
                server = smtplib.SMTP(self.email_smtp_host, self.email_smtp_port)
                server.starttls()
            server.login(self.email_auth_user, self.email_password)
            text = msg.as_string()
            server.sendmail(self.email_from, all_recipients, text)  # sendmail accepts list of recipients
            server.quit()
            
            logger.info(f"Email sent successfully to {', '.join(all_recipients)}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False
    
    def run_daily_automation(self, resume_run_id=None):
        """Run complete daily automation with resumption support"""
        target_date = self.get_previous_business_day()
        date_str = target_date.strftime('%Y-%m-%d')
        
        # Check for incomplete runs first
        if resume_run_id is None:
            incomplete_runs = self.find_incomplete_runs(date_str)
            if incomplete_runs:
                latest_run = incomplete_runs[0]
                run_id = latest_run[0]
                logger.info(f"Found incomplete run {run_id} for {date_str}. Resuming...")
                return self.resume_incomplete_run(run_id)
        else:
            logger.info(f"Resuming specific run {resume_run_id}")
            return self.resume_incomplete_run(resume_run_id)
        
        # Start new run
        logger.info(f"Starting new daily automation for {date_str}")
        
        # Create run record
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO daily_runs (run_date, target_date, status)
            VALUES (?, ?, ?)
        ''', (datetime.now().date(), target_date, 'running'))
        run_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        try:
            # Step 1: Scrape opinions
            logger.info("Step 1: Scraping opinions...")
            self.update_run_state(run_id, status='scraping')
            self.resume_daily_scrape(run_id, target_date)
            
            # Step 2: Run analysis
            if self.analysis_enabled and self.claude_client:
                unanalyzed_count = len(self.get_unanalyzed_opinions_for_date(date_str))
                if unanalyzed_count > 0:
                    logger.info(f"Step 2: Running analysis on {unanalyzed_count} cases...")
                    self.update_run_state(run_id, status='analyzing')
                    self.run_analysis_batch()
                else:
                    logger.info("Step 2: No cases need analysis")
            else:
                logger.warning("Analysis is disabled or Claude client not available")
            
            # Step 3: Generate report
            logger.info("Step 3: Generating report...")
            self.update_run_state(run_id, status='reporting')
            report_path = self.generate_daily_report(target_date)
            
            if not report_path:
                self.update_run_state(run_id, status='no_cases', 
                                    error_message="No cases found for report")
                logger.warning("No report generated (no cases found)")
                return False
            
            # Step 4: Check subscription emails
            logger.info("Step 4: Checking subscription emails...")
            self.check_subscription_emails()
            
            # Step 5: Send email
            if self.email_enabled:
                logger.info("Step 5: Sending email...")
                self.update_run_state(run_id, status='emailing')
                email_success = self.send_email_report(report_path, date_str)
                
                if email_success:
                    self.update_run_state(run_id, status='completed')
                    logger.info("Daily automation completed successfully")
                    return True
                else:
                    self.update_run_state(run_id, status='email_failed', 
                                        error_message="Email sending failed")
                    logger.error("Daily automation completed but email failed")
                    return False
            else:
                self.update_run_state(run_id, status='completed')
                logger.info("Daily automation completed (email disabled)")
                return True
                
        except Exception as e:
            error_msg = f"Daily automation failed: {str(e)}"
            self.update_run_state(run_id, status='failed', error_message=error_msg)
            logger.error(error_msg)
            return False

    def get_run_state(self, run_id):
        """Get the current state of a run"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT status, total_courts_checked, total_cases_found, 
                   total_files_downloaded, target_date, error_message
            FROM daily_runs 
            WHERE id = ?
        ''', (run_id,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                'status': result[0],
                'courts_checked': result[1],
                'cases_found': result[2],
                'files_downloaded': result[3],
                'target_date': result[4],
                'error_message': result[5]
            }
        return None
    
    def update_run_state(self, run_id, status=None, courts_checked=None, 
                        cases_found=None, files_downloaded=None, error_message=None):
        """Update the state of a run"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        updates = []
        params = []
        
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if courts_checked is not None:
            updates.append("total_courts_checked = ?")
            params.append(courts_checked)
        if cases_found is not None:
            updates.append("total_cases_found = ?")
            params.append(cases_found)
        if files_downloaded is not None:
            updates.append("total_files_downloaded = ?")
            params.append(files_downloaded)
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)
        
        if updates:
            params.append(run_id)
            cursor.execute(f'''
                UPDATE daily_runs 
                SET {", ".join(updates)}
                WHERE id = ?
            ''', params)
            conn.commit()
        
        conn.close()
    
    def find_incomplete_runs(self, target_date=None):
        """Find runs that were interrupted and can be resumed"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if target_date:
            cursor.execute('''
                SELECT id, run_date, target_date, status, total_courts_checked,
                       total_cases_found, total_files_downloaded
                FROM daily_runs 
                WHERE target_date = ? AND status IN ('running', 'scraping', 'analyzing', 'reporting')
                ORDER BY run_timestamp DESC
            ''', (target_date,))
        else:
            cursor.execute('''
                SELECT id, run_date, target_date, status, total_courts_checked,
                       total_cases_found, total_files_downloaded
                FROM daily_runs 
                WHERE status IN ('running', 'scraping', 'analyzing', 'reporting')
                ORDER BY run_timestamp DESC
            ''')
        
        results = cursor.fetchall()
        conn.close()
        
        return results
    
    def resume_incomplete_run(self, run_id):
        """Resume an incomplete run from where it left off"""
        state = self.get_run_state(run_id)
        if not state:
            logger.error(f"Run {run_id} not found")
            return False
        
        target_date_str = state['target_date']
        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
        
        logger.info(f"Resuming run {run_id} for {target_date_str} from status: {state['status']}")
        
        try:
            # Update status to indicate we're resuming
            self.update_run_state(run_id, status='resuming')
            
            # Determine what steps need to be completed
            if state['status'] in ['running', 'scraping']:
                # Resume scraping
                logger.info("Resuming scraping phase...")
                self.update_run_state(run_id, status='scraping')
                self.resume_daily_scrape(run_id, target_date)
            
            # Check if analysis is needed
            unanalyzed_count = len(self.get_unanalyzed_opinions_for_date(target_date_str))
            if unanalyzed_count > 0 and self.analysis_enabled:
                logger.info(f"Resuming analysis phase... {unanalyzed_count} cases to analyze")
                self.update_run_state(run_id, status='analyzing')
                self.run_analysis_batch()
            
            # Generate report if needed
            if state['status'] in ['running', 'scraping', 'analyzing', 'reporting']:
                logger.info("Generating report...")
                self.update_run_state(run_id, status='reporting')
                report_path = self.generate_daily_report(target_date)
                
                if report_path and self.email_enabled:
                    logger.info("Sending email...")
                    self.update_run_state(run_id, status='emailing')
                    email_success = self.send_email_report(report_path, target_date_str)
                    
                    if email_success:
                        self.update_run_state(run_id, status='completed')
                        logger.info(f"Run {run_id} resumed and completed successfully")
                        return True
                    else:
                        self.update_run_state(run_id, status='email_failed', 
                                            error_message="Email sending failed")
                        logger.error(f"Run {run_id} completed but email failed")
                        return False
                else:
                    self.update_run_state(run_id, status='completed')
                    logger.info(f"Run {run_id} resumed and completed successfully (no email)")
                    return True
            
        except Exception as e:
            error_msg = f"Resume failed: {str(e)}"
            self.update_run_state(run_id, status='failed', error_message=error_msg)
            logger.error(f"Failed to resume run {run_id}: {e}")
            return False
    
    def get_unanalyzed_opinions_for_date(self, target_date):
        """Get unanalyzed opinions for a specific date"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT o.id, o.case_number, o.court, o.opinion_date, o.file_path
            FROM opinions o
            LEFT JOIN analysis a ON o.id = a.opinion_id
            WHERE o.opinion_date = ? AND a.opinion_id IS NULL AND o.consolidated_with IS NULL
            ORDER BY o.case_number
        ''', (target_date,))
        
        results = cursor.fetchall()
        conn.close()
        return results
    
    def resume_daily_scrape(self, run_id, target_date):
        """Resume daily scrape with progress tracking"""
        date_folder = os.path.join(self.data_dir, target_date.strftime('%Y%m%d'))
        os.makedirs(date_folder, exist_ok=True)
        
        courts_checked = 0
        total_cases = 0
        total_downloaded = 0
        
        try:
            for coa_num in range(1, 15):  # Courts 1-14
                try:
                    courts_checked += 1
                    logger.info(f"Scraping COA{coa_num:02d} for {target_date.strftime('%Y-%m-%d')}")
                    
                    # Update progress
                    self.update_run_state(run_id, courts_checked=courts_checked)
                    
                    # Scrape this court
                    cases_found, files_downloaded = self.scrape_court_date(coa_num, target_date, date_folder)
                    
                    total_cases += cases_found
                    total_downloaded += files_downloaded
                    
                    # Update progress
                    self.update_run_state(run_id, cases_found=total_cases, 
                                        files_downloaded=total_downloaded)
                    
                    if files_downloaded > 0:
                        logger.info(f"Downloaded {files_downloaded} files from COA{coa_num:02d} on {target_date.strftime('%Y-%m-%d')}")
                    elif cases_found > 0:
                        logger.warning(f"Failed to download {cases_found} out of {cases_found} opinions for COA{coa_num:02d} on {target_date.strftime('%Y-%m-%d')}")
                    
                    # Small delay between courts
                    time.sleep(self.download_delay)
                    
                except Exception as e:
                    logger.error(f"Error scraping COA{coa_num:02d}: {e}")
                    continue
            
            # Final update
            self.update_run_state(run_id, status='scrape_completed', 
                                courts_checked=courts_checked,
                                cases_found=total_cases,
                                files_downloaded=total_downloaded)
            
            logger.info(f"Resume scrape completed!")
            logger.info(f"Courts checked: {courts_checked}")
            logger.info(f"Total cases found: {total_cases}")
            logger.info(f"Total files downloaded: {total_downloaded}")
            
        except Exception as e:
            self.update_run_state(run_id, status='scrape_failed', 
                                error_message=f"Scraping failed: {str(e)}")
            raise

    def load_members(self):
        """Load members from the members file"""
        try:
            if os.path.exists(self.members_file):
                with open(self.members_file, 'r') as f:
                    data = json.load(f)
                    return data.get('members', [])
            return []
        except Exception as e:
            logger.error(f"Error loading members file: {e}")
            return []
    
    def save_members(self, members):
        """Save members to the members file"""
        try:
            # Ensure data directory exists
            os.makedirs(os.path.dirname(self.members_file), exist_ok=True)
            
            data = {
                'members': members,
                'last_updated': datetime.now().isoformat(),
                'total_members': len(members)
            }
            
            with open(self.members_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.info(f"Saved {len(members)} members to {self.members_file}")
            return True
        except Exception as e:
            logger.error(f"Error saving members file: {e}")
            return False
    
    def add_member(self, email_address):
        """Add a new member to the subscription list"""
        members = self.load_members()
        email_address = email_address.lower().strip()
        
        if email_address not in members:
            members.append(email_address)
            if self.save_members(members):
                logger.info(f"Added new member: {email_address}")
                return True
        else:
            logger.info(f"Member already exists: {email_address}")
            return True
        
        return False
    
    def remove_member(self, email_address):
        """Remove a member from the subscription list"""
        members = self.load_members()
        email_address = email_address.lower().strip()
        
        if email_address in members:
            members.remove(email_address)
            if self.save_members(members):
                logger.info(f"Removed member: {email_address}")
                return True
        else:
            logger.info(f"Member not found: {email_address}")
            return True
        
        return False
    
    def get_all_recipients(self):
        """Get all email recipients (static + members)"""
        # Start with configured static recipients
        static_recipients = self.email_to.copy() if self.email_to else []
        
        # Add dynamic members
        members = self.load_members()
        
        # Combine and deduplicate
        all_recipients = list(set(static_recipients + members))
        
        return all_recipients
    
    def check_subscription_emails(self):
        """Check the subscription mailbox for subscribe/unsubscribe requests"""
        if not all([self.subscription_email, self.subscription_auth_user, self.subscription_password]):
            logger.warning("Subscription email configuration incomplete")
            return False
        
        try:
            # Connect to IMAP server
            mail = imaplib.IMAP4_SSL(self.subscription_imap_host, self.subscription_imap_port)
            mail.login(self.subscription_auth_user, self.subscription_password)
            
            # Try PDRbot folder first, then fallback to main INBOX
            try:
                status, data = mail.select('INBOX/PDRbot')
                if status != 'OK':
                    mail.select('INBOX')
                    logger.info("Using main INBOX folder for subscription emails")
                else:
                    logger.info("Using INBOX/PDRbot folder for subscription emails")
            except:
                mail.select('INBOX')
                logger.info("Fallback to main INBOX folder for subscription emails")
            
            # Search for unread emails
            status, message_ids = mail.search(None, 'UNSEEN')
            
            if status != 'OK':
                logger.error("Failed to search for emails")
                return False
            
            message_ids = message_ids[0].split()
            processed_count = 0
            
            for msg_id in message_ids:
                try:
                    # Fetch email
                    status, msg_data = mail.fetch(msg_id, '(RFC822)')
                    if status != 'OK':
                        continue
                    
                    # Parse email
                    email_message = email.message_from_bytes(msg_data[0][1])
                    sender = email_message.get('From')
                    subject = email_message.get('Subject', '')
                    
                    # Extract sender email address
                    if '<' in sender and '>' in sender:
                        sender_email = sender.split('<')[1].split('>')[0]
                    else:
                        sender_email = sender
                    
                    # Get email body and subject
                    body = self.get_email_body(email_message)
                    subject = email_message.get('Subject', '').lower().strip()
                    
                    # Check both body and subject for subscription keywords
                    body_lower = body.lower().strip() if body else ""
                    is_subscribe = body_lower.startswith('subscribe') or subject == 'subscribe'
                    is_unsubscribe = body_lower.startswith('unsubscribe') or subject == 'unsubscribe'
                    
                    if is_subscribe:
                        if self.add_member(sender_email):
                            self.send_confirmation_email(sender_email, 'subscribed')
                            source = 'subject' if subject == 'subscribe' else 'body'
                            logger.info(f"Processed subscription request from {sender_email} (via {source})")
                            processed_count += 1
                    
                    elif is_unsubscribe:
                        if self.remove_member(sender_email):
                            self.send_confirmation_email(sender_email, 'unsubscribed')
                            source = 'subject' if subject == 'unsubscribe' else 'body'
                            logger.info(f"Processed unsubscription request from {sender_email} (via {source})")
                            processed_count += 1
                    
                    # Mark as read
                    mail.store(msg_id, '+FLAGS', '\\Seen')
                    
                except Exception as e:
                    logger.error(f"Error processing email {msg_id}: {e}")
                    continue
            
            mail.close()
            mail.logout()
            
            if processed_count > 0:
                logger.info(f"Processed {processed_count} subscription requests")
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking subscription emails: {e}")
            return False
    
    def get_email_body(self, email_message):
        """Extract text body from email message"""
        try:
            if email_message.is_multipart():
                for part in email_message.walk():
                    if part.get_content_type() == "text/plain":
                        return part.get_payload(decode=True).decode('utf-8', errors='ignore')
            else:
                return email_message.get_payload(decode=True).decode('utf-8', errors='ignore')
        except Exception as e:
            logger.error(f"Error extracting email body: {e}")
            return None
    
    def send_confirmation_email(self, recipient, action):
        """Send confirmation email for subscription changes"""
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email_from
            msg['To'] = recipient
            msg['Subject'] = f"PDRBot Subscription {action.title()}"
            
            if action == 'subscribed':
                body = f"""Thank you for subscribing to PDRBot daily reports!

You will now receive daily criminal law opinion analysis reports from the Texas Courts of Appeals.

To unsubscribe at any time, simply send an email to {self.subscription_email} with "unsubscribe" in the message body.

PDRBot Team"""
            else:  # unsubscribed
                body = f"""You have been successfully unsubscribed from PDRBot daily reports.

You will no longer receive daily criminal law opinion analysis reports.

To resubscribe at any time, simply send an email to {self.subscription_email} with "subscribe" in the message body.

PDRBot Team"""
            
            msg.attach(MIMEText(body, 'plain'))
            
            # Send confirmation
            if self.email_smtp_port == 465:
                server = smtplib.SMTP_SSL(self.email_smtp_host, self.email_smtp_port)
            else:
                server = smtplib.SMTP(self.email_smtp_host, self.email_smtp_port)
                server.starttls()
            
            server.login(self.email_auth_user, self.email_password)
            server.sendmail(self.email_from, [recipient], msg.as_string())
            server.quit()
            
            logger.info(f"Sent {action} confirmation to {recipient}")
            return True
            
        except Exception as e:
            logger.error(f"Error sending confirmation email to {recipient}: {e}")
            return False

def main():
    """Main entry point for PDRBot"""
    import sys
    import sqlite3
    
    bot = PDRBot()
    
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        
        if command == "analyze":
            # Run analysis on unanalyzed opinions
            limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
            bot.run_analysis_batch(limit=limit)
        elif command == "scrape":
            # Run daily scrape only
            bot.run_daily_scrape()
        elif command == "both":
            # Run scrape then analysis
            bot.run_daily_scrape()
            if bot.analysis_enabled:
                bot.run_analysis_batch()
        elif command == "report":
            # Generate PDF report from existing analyses
            if len(sys.argv) > 2:
                try:
                    date_str = sys.argv[2]
                    target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                    report_path = bot.generate_daily_report(target_date)
                except ValueError:
                    print("Invalid date format. Use YYYY-MM-DD")
                    sys.exit(1)
            else:
                report_path = bot.generate_analysis_report()
            
            if report_path:
                print(f"Report generated: {report_path}")
            else:
                print("No report generated (no interesting cases found)")
        elif command == "daily-report":
            # Generate report for yesterday's analyses or specified date
            target_date = None
            if len(sys.argv) > 2:
                date_str = sys.argv[2]
                try:
                    target_date = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    print(f"Error: Invalid date format '{date_str}'. Use YYYY-MM-DD format.")
                    sys.exit(1)
            
            report_path = bot.generate_daily_report(target_date)
            if report_path:
                print(f"Daily report generated: {report_path}")
            else:
                print("No daily report generated (no interesting cases found)")
        elif command == "backfill-urls":
            # Backfill PDF URLs for existing records
            bot.backfill_pdf_urls()
        elif command == "analyze-dir":
            # Analyze all PDFs in a specific directory
            if len(sys.argv) > 2:
                directory_path = sys.argv[2]
                bot.analyze_directory_pdfs(directory_path)
            else:
                print("Usage: python pdrbot.py analyze-dir <directory_path>")
                sys.exit(1)
        elif command == "auto" or command == "automation":
            # Run full daily automation: scrape, analyze, report, and email
            success = bot.run_daily_automation()
            if success:
                print("Daily automation completed successfully")
            else:
                print("Daily automation failed")
                sys.exit(1)
        elif command == "resume":
            # Resume an incomplete run
            if len(sys.argv) > 2:
                try:
                    run_id = int(sys.argv[2])
                    success = bot.resume_incomplete_run(run_id)
                    if success:
                        print(f"Run {run_id} resumed and completed successfully")
                    else:
                        print(f"Failed to resume run {run_id}")
                        sys.exit(1)
                except ValueError:
                    print("Invalid run ID. Must be a number.")
                    sys.exit(1)
            else:
                # Show incomplete runs
                incomplete = bot.find_incomplete_runs()
                if incomplete:
                    print("Incomplete runs found:")
                    for run_id, run_date, target_date, status, courts, cases, files in incomplete:
                        print(f"  Run {run_id}: {target_date} (status: {status}, courts: {courts}, cases: {cases}, files: {files})")
                    print("\nUse 'python pdrbot.py resume <run_id>' to resume a specific run")
                else:
                    print("No incomplete runs found")
        elif command == "status":
            # Show recent run status
            incomplete = bot.find_incomplete_runs()
            if incomplete:
                print("Incomplete runs:")
                for run_id, run_date, target_date, status, courts, cases, files in incomplete:
                    print(f"  Run {run_id}: {target_date} (status: {status})")
            else:
                print("No incomplete runs")
            
            # Show recent completed runs
            conn = sqlite3.connect(bot.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, target_date, status, total_files_downloaded, run_timestamp
                FROM daily_runs 
                WHERE status IN ('completed', 'failed', 'email_failed')
                ORDER BY run_timestamp DESC 
                LIMIT 5
            ''')
            recent = cursor.fetchall()
            conn.close()
            
            if recent:
                print("\nRecent completed runs:")
                for run_id, target_date, status, files, timestamp in recent:
                    print(f"  Run {run_id}: {target_date} ({status}, {files} files) - {timestamp}")
            else:
                print("No recent completed runs")
        elif command == "members":
            # Show subscription members
            members = bot.load_members()
            static_recipients = bot.email_to if bot.email_to else []
            
            print(f"Static recipients ({len(static_recipients)}):")
            for email in static_recipients:
                print(f"  - {email}")
            
            print(f"\nDynamic members ({len(members)}):")
            for email in members:
                print(f"  - {email}")
            
            all_recipients = bot.get_all_recipients()
            print(f"\nTotal unique recipients: {len(all_recipients)}")
        elif command == "check-subscriptions":
            # Manually check subscription emails
            print("Checking subscription emails...")
            success = bot.check_subscription_emails()
            if success:
                print("✅ Subscription check completed")
            else:
                print("❌ Subscription check failed")
        else:
            print("Usage: python pdrbot.py [scrape|analyze|both|report|daily-report|auto|resume|status|members|check-subscriptions|backfill-urls|analyze-dir] [options]")
            print("  scrape       - Download new opinions only")
            print("  analyze      - Analyze unanalyzed opinions only")
            print("  both         - Download and analyze (default)")
            print("  report       - Generate PDF report from all analyses")
            print("  report YYYY-MM-DD - Generate report for specific date")
            print("  daily-report [YYYY-MM-DD] - Generate report for specified date or yesterday's analyses")
            print("  auto         - Full automation: scrape, analyze, report, and email")
            print("  resume [run_id] - Resume incomplete run or list incomplete runs")
            print("  status       - Show status of recent and incomplete runs")
            print("  members      - Show subscription members and recipients")
            print("  check-subscriptions - Manually check for subscription emails")
            print("  backfill-urls - Update existing records with direct PDF URLs")
            print("  analyze-dir <path> - Analyze all PDFs in specific directory")
            print("")
            print("Options:")
            print("  limit        - Maximum number of opinions to analyze (analyze mode only)")
            sys.exit(1)
    else:
        # Default: run daily scrape with analysis
        bot.run_daily_scrape()

if __name__ == "__main__":
    main() 