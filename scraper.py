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

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class COAOpinionScraper:
    def __init__(self, output_dir="opinions"):
        self.base_url = "https://search.txcourts.gov/"
        self.output_dir = output_dir
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
        # Create output directory
        os.makedirs(output_dir, exist_ok=True)
    
    def generate_date_range(self, start_date, end_date):
        """Generate all dates between start_date and end_date"""
        current = start_date
        while current <= end_date:
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
    
    def download_pdf(self, pdf_url, filename):
        """Download PDF file"""
        try:
            response = self.session.get(pdf_url, timeout=30)
            response.raise_for_status()
            
            filepath = os.path.join(self.output_dir, filename)
            with open(filepath, 'wb') as f:
                f.write(response.content)
            
            logger.info(f"Downloaded: {filename}")
            return True
        except Exception as e:
            logger.error(f"Failed to download {filename}: {e}")
            return False
    
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
        
        rows = tbody.find_all('tr', class_='rgRow')
        
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
        url = self.get_docket_url(coa_num, date)
        date_str = date.strftime("%Y-%m-%d")
        
        logger.info(f"Scraping COA{coa_num:02d} for {date_str}")
        
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            criminal_cases = self.parse_criminal_causes(soup)
            
            if not criminal_cases:
                logger.debug(f"No criminal cases found for COA{coa_num:02d} on {date_str}")
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
                    
                    # Be respectful with delays
                    time.sleep(1)
            
            logger.info(f"Downloaded {downloaded_count} files for COA{coa_num:02d} on {date_str}")
            return downloaded_count
            
        except Exception as e:
            logger.error(f"Error scraping COA{coa_num:02d} on {date_str}: {e}")
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

def main():
    scraper = COAOpinionScraper()
    
    # Run development test
    scraper.run_development_test()

if __name__ == "__main__":
    main() 