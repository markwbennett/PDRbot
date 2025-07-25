#!/./.venv/bin/python
"""
Texas Court of Appeals Criminal Opinions Scraper

Scrapes criminal law opinions from Texas Courts of Appeals (COA01-COA14)
for dates from 01/01/2025 to present.
"""

import requests
from bs4 import BeautifulSoup
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, parse_qs, urlparse
import time
import logging
import csv
import json

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class COAOpinionScraper:
    def __init__(self, output_dir="opinions", status_file="scraper_status.json", log_file="scrape_log.csv"):
        self.base_url = "https://search.txcourts.gov/"
        self.output_dir = output_dir
        self.status_file = status_file
        self.log_file = log_file
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
        # Create output directory
        os.makedirs(output_dir, exist_ok=True)
        
        # Initialize CSV log file
        self.init_csv_log()
        
        # Load or initialize status
        self.status = self.load_status()
    
    def init_csv_log(self):
        """Initialize CSV log file with headers if it doesn't exist"""
        if not os.path.exists(self.log_file):
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp', 'court', 'date', 'criminal_cases_found', 'files_downloaded', 'case_numbers', 'status'])
    
    def load_status(self):
        """Load scraper status from file"""
        if os.path.exists(self.status_file):
            try:
                with open(self.status_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load status file: {e}")
        
        return {
            'last_completed_date': None,
            'last_completed_court': None,
            'total_files_downloaded': 0,
            'total_requests': 0,
            'start_time': None,
            'completed_combinations': []  # List of "YYYY-MM-DD_COA##" strings
        }
    
    def save_status(self):
        """Save current status to file"""
        try:
            with open(self.status_file, 'w') as f:
                json.dump(self.status, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save status: {e}")
    
    def log_scrape_result(self, court, date, criminal_cases_found, files_downloaded, case_numbers, status):
        """Log scrape result to CSV"""
        try:
            with open(self.log_file, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().isoformat(),
                    f"COA{court:02d}",
                    date.strftime('%Y-%m-%d'),
                    criminal_cases_found,
                    files_downloaded,
                    ';'.join(case_numbers) if case_numbers else '',
                    status
                ])
        except Exception as e:
            logger.error(f"Could not log to CSV: {e}")
    
    def is_combination_completed(self, court, date):
        """Check if court/date combination has already been completed"""
        combo_str = f"{date.strftime('%Y-%m-%d')}_COA{court:02d}"
        return combo_str in self.status.get('completed_combinations', [])
    
    def mark_combination_completed(self, court, date):
        """Mark court/date combination as completed"""
        combo_str = f"{date.strftime('%Y-%m-%d')}_COA{court:02d}"
        if combo_str not in self.status.get('completed_combinations', []):
            self.status['completed_combinations'].append(combo_str)
            self.status['last_completed_date'] = date.strftime('%Y-%m-%d')
            self.status['last_completed_court'] = court
            self.save_status()

    def generate_date_range(self, start_date, end_date, skip_weekends=True):
        """Generate all dates between start_date and end_date, optionally skipping weekends"""
        current = start_date
        while current <= end_date:
            if skip_weekends and current.weekday() >= 5:  # Saturday=5, Sunday=6
                current += timedelta(days=1)
                continue
            yield current
            current += timedelta(days=1)
    
    def get_docket_url(self, coa_num, date):
        """Generate the docket URL for a specific court and date"""
        date_str = date.strftime("%m/%d/%Y")
        return f"{self.base_url}Docket.aspx?coa=coa{coa_num:02d}&FullDate={date_str}"
    
    def get_abbreviation_and_justice(self, description):
        """Get abbreviation for opinion type and justice name based on description"""
        description_lower = description.lower()
        
        if "memorandum" in description_lower:
            return "mem", None
        elif "dissenting" in description_lower:
            # Extract justice name from "Dissenting Opinion by Justice [Name]"
            justice_match = re.search(r'dissenting opinion by (?:chief )?justice (\w+)', description_lower)
            justice_name = justice_match.group(1) if justice_match else None
            return "dis", justice_name
        elif "concurring" in description_lower:
            # Extract justice name from "Concurring Opinion by Justice [Name]"
            justice_match = re.search(r'concurring opinion by (?:chief )?justice (\w+)', description_lower)
            justice_name = justice_match.group(1) if justice_match else None
            return "con", justice_name
        elif "opinion" in description_lower:
            return "op", None
        
        return "", None
    
    def extract_case_number(self, case_link_text):
        """Extract case number from link text"""
        # Should match pattern like 01-23-00751-CR (5 digits, not 4)
        match = re.search(r'\d{2}-\d{2}-\d{5}-CR', case_link_text)
        return match.group(0) if match else None
    
    def download_pdf(self, pdf_url, filename, max_retries=3):
        """Download PDF file with retry logic"""
        last_exception = None
        filepath = os.path.join(self.output_dir, filename)
        
        for attempt in range(max_retries):
            try:
                response = self.session.get(pdf_url, timeout=30)
                response.raise_for_status()
                
                # Validate response is actually a PDF
                if response.headers.get('content-type', '').lower() != 'application/pdf':
                    logger.warning(f"Response is not a PDF (attempt {attempt + 1}): {response.headers.get('content-type', 'unknown')}")
                    if attempt == max_retries - 1:
                        logger.error(f"Not a PDF after {max_retries} attempts: {filename}")
                        return False
                    time.sleep(2 ** attempt)  # Exponential backoff
                    continue
                
                with open(filepath, 'wb') as f:
                    f.write(response.content)
                
                # Verify file was written and has content
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    # Basic PDF validation - check for PDF magic bytes
                    with open(filepath, 'rb') as f:
                        header = f.read(4)
                        if not header.startswith(b'%PDF'):
                            raise Exception("Downloaded file is not a valid PDF")
                    
                    logger.info(f"Downloaded: {filename}")
                    return True
                else:
                    raise Exception("File was not written or is empty")
                    
            except (requests.exceptions.RequestException, requests.exceptions.Timeout, 
                    requests.exceptions.ConnectionError, Exception) as e:
                last_exception = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                    logger.warning(f"Download failed (attempt {attempt + 1}/{max_retries}): {filename} - {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Failed to download {filename} after {max_retries} attempts: {e}")
        
        return False
    
    def get_with_retry(self, url, max_retries=3):
        """Make HTTP request with retry logic"""
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=30)
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
        
        # Look for the "Criminal Causes Decided" heading - use text contains approach
        criminal_heading = None
        h3_elements = soup.find_all('h3')
        for h3 in h3_elements:
            if 'Criminal Causes Decided' in h3.get_text():
                criminal_heading = h3
                break
        
        if not criminal_heading:
            logger.debug("No Criminal Causes Decided section found")
            return criminal_cases
        
        # Find the table that follows the heading
        table = criminal_heading.find_next('table', class_='rgMasterTable')
        if not table:
            logger.debug("No table found after Criminal Causes Decided heading")
            return criminal_cases
        
        # Find all rows in the table body
        tbody = table.find('tbody')
        if not tbody:
            logger.debug("No tbody found in criminal causes table")
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
            
            # Find all PDF links in this row
            pdf_links = []
            doc_tables = row.find_all('table', class_='docGrid')
            
            for doc_table in doc_tables:
                pdf_link = doc_table.find('a', href=re.compile(r'SearchMedia\.aspx'))
                if pdf_link:
                    # Get the description (opinion type)
                    desc_cell = pdf_link.find_parent('td').find_previous_sibling('td')
                    description = desc_cell.text.strip() if desc_cell else ""
                    
                    # Clean up the PDF URL (remove JavaScript template syntax)
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
                    'pdf_links': pdf_links
                }
        
        except Exception as e:
            logger.error(f"Error parsing case row: {e}")
        
        return None
    
    def scrape_court_date(self, coa_num, date):
        """Scrape opinions for a specific court and date"""
        # Check if already completed
        if self.is_combination_completed(coa_num, date):
            logger.debug(f"Skipping COA{coa_num:02d} for {date.strftime('%Y-%m-%d')} - already completed")
            return 0
        
        url = self.get_docket_url(coa_num, date)
        date_str = date.strftime("%Y-%m-%d")
        
        logger.info(f"Scraping COA{coa_num:02d} for {date_str}")
        
        try:
            response = self.get_with_retry(url)
            soup = BeautifulSoup(response.content, 'html.parser')
            criminal_cases = self.parse_criminal_causes(soup)
            
            case_numbers = [case['case_number'] for case in criminal_cases]
            
            if not criminal_cases:
                logger.debug(f"No criminal cases found for COA{coa_num:02d} on {date_str}")
                self.log_scrape_result(coa_num, date, 0, 0, [], "no_cases")
                self.mark_combination_completed(coa_num, date)
                return 0
            
            downloaded_count = 0
            
            for case in criminal_cases:
                case_number = case['case_number']
                
                for i, pdf_link in enumerate(case['pdf_links']):
                    description = pdf_link['description']
                    pdf_url = pdf_link['url']
                    
                    # Generate filename
                    abbrev, justice_name = self.get_abbreviation_and_justice(description)
                    if abbrev:
                        if justice_name and abbrev in ['con', 'dis']:
                            # For concurring/dissenting opinions, include justice name
                            filename = f"{case_number}_{abbrev}_{justice_name}.pdf"
                        else:
                            filename = f"{case_number}_{abbrev}.pdf"
                    else:
                        # If multiple PDFs without clear type, number them
                        suffix = f"_{i+1}" if len(case['pdf_links']) > 1 else ""
                        filename = f"{case_number}{suffix}.pdf"
                    
                    # Check if file already exists
                    filepath = os.path.join(self.output_dir, filename)
                    if os.path.exists(filepath):
                        logger.info(f"File already exists: {filename}")
                        continue
                    
                    if self.download_pdf(pdf_url, filename):
                        downloaded_count += 1
                        self.status['total_files_downloaded'] += 1
                    
                    # Be respectful with delays
                    time.sleep(1)
            
            logger.info(f"Downloaded {downloaded_count} files for COA{coa_num:02d} on {date_str}")
            self.log_scrape_result(coa_num, date, len(criminal_cases), downloaded_count, case_numbers, "completed")
            self.mark_combination_completed(coa_num, date)
            self.status['total_requests'] += 1
            
            return downloaded_count
            
        except Exception as e:
            logger.error(f"Error scraping COA{coa_num:02d} on {date_str}: {e}")
            self.log_scrape_result(coa_num, date, 0, 0, [], f"error: {str(e)}")
            return 0
    
    def run_development_test(self):
        """Run development test for COA01-02 for January 2025"""
        start_date = datetime(2025, 1, 1)
        end_date = datetime(2025, 1, 31)
        courts = [1, 2]  # COA01 and COA02
        
        total_downloaded = 0
        
        for date in self.generate_date_range(start_date, end_date):
            for coa_num in courts:
                count = self.scrape_court_date(coa_num, date)
                total_downloaded += count
                
                # Small delay between requests
                time.sleep(2)
        
        logger.info(f"Development test completed. Total files downloaded: {total_downloaded}")
        return total_downloaded
    
    def run_full_production(self):
        """Run full production scrape for all 14 courts from January 2025 to present"""
        start_date = datetime(2025, 1, 1)
        end_date = datetime.now()  # Today
        courts = list(range(1, 15))  # COA01 through COA14
        
        # Set start time if not already set
        if not self.status.get('start_time'):
            self.status['start_time'] = datetime.now().isoformat()
            self.save_status()
        
        # Calculate date statistics
        total_days = (end_date - start_date).days + 1
        weekdays_only = sum(1 for _ in self.generate_date_range(start_date, end_date, skip_weekends=True))
        weekend_days = total_days - weekdays_only
        time_saved_pct = (weekend_days / total_days) * 100
        
        logger.info(f"Starting full production run from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        logger.info(f"Scraping {len(courts)} courts: COA01-COA14")
        logger.info(f"Skipping weekends: {weekdays_only} weekdays vs {total_days} total days ({time_saved_pct:.1f}% time saved)")
        
        # Calculate resume point
        resume_date = start_date
        resume_court = 1
        
        if self.status.get('last_completed_date'):
            last_date = datetime.strptime(self.status['last_completed_date'], '%Y-%m-%d')
            last_court = self.status.get('last_completed_court', 1)
            
            if last_court < 14:
                # Resume with next court on same date
                resume_date = last_date
                resume_court = last_court + 1
            else:
                # Resume with next weekday and court 1
                resume_date = last_date + timedelta(days=1)
                # Skip to next weekday if needed
                while resume_date.weekday() >= 5:  # Skip weekends
                    resume_date += timedelta(days=1)
                resume_court = 1
            
            logger.info(f"Last completed: {self.status['last_completed_date']} COA{last_court:02d}")
            logger.info(f"Resuming from: {resume_date.strftime('%Y-%m-%d')} COA{resume_court:02d}")
        
        total_downloaded = self.status.get('total_files_downloaded', 0)
        total_requests = self.status.get('total_requests', 0)
        
        # Start from resume point
        started_processing = False
        
        for date in self.generate_date_range(start_date, end_date):
            for coa_num in courts:
                # Skip until we reach resume point
                if not started_processing:
                    if date < resume_date or (date == resume_date and coa_num < resume_court):
                        continue
                    started_processing = True
                count = self.scrape_court_date(coa_num, date)
                total_downloaded += count
                total_requests += 1
                
                # Small delay between requests
                time.sleep(2)
                
                # Progress logging every 50 requests
                if total_requests % 50 == 0:
                    logger.info(f"Progress: {total_requests} requests completed, {total_downloaded} files downloaded so far")
                    logger.info(f"Currently processing: {date.strftime('%Y-%m-%d')} COA{coa_num:02d}")
        
        logger.info(f"Full production run completed!")
        logger.info(f"Total requests: {total_requests}")
        logger.info(f"Total files downloaded: {total_downloaded}")
        return total_downloaded

def main():
    scraper = COAOpinionScraper()
    
    # Run full production instead of development test
    scraper.run_full_production()

if __name__ == "__main__":
    main() 