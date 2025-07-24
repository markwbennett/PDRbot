#!/./.venv/bin/python
"""
Check scraper status and progress
"""

import json
import os
import csv
from datetime import datetime
from collections import defaultdict

def check_status():
    """Check and display scraper status"""
    
    # Check status file
    if os.path.exists('scraper_status.json'):
        with open('scraper_status.json', 'r') as f:
            status = json.load(f)
        
        print("=== SCRAPER STATUS ===")
        print(f"Start time: {status.get('start_time', 'Not started')}")
        print(f"Last completed: {status.get('last_completed_date', 'None')} COA{status.get('last_completed_court', 0):02d}")
        print(f"Total requests: {status.get('total_requests', 0)}")
        print(f"Total files downloaded: {status.get('total_files_downloaded', 0)}")
        print(f"Completed combinations: {len(status.get('completed_combinations', []))}")
    else:
        print("No status file found - scraper hasn't started yet")
    
    # Check log file for patterns
    if os.path.exists('scrape_log.csv'):
        print("\n=== LOG ANALYSIS ===")
        
        court_stats = defaultdict(lambda: {'total': 0, 'with_cases': 0, 'files': 0})
        date_stats = defaultdict(lambda: {'total': 0, 'with_cases': 0, 'files': 0})
        
        with open('scrape_log.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                court = row['court']
                date = row['date']
                cases = int(row['criminal_cases_found'])
                files = int(row['files_downloaded'])
                
                court_stats[court]['total'] += 1
                court_stats[court]['files'] += files
                if cases > 0:
                    court_stats[court]['with_cases'] += 1
                
                date_stats[date]['total'] += 1
                date_stats[date]['files'] += files
                if cases > 0:
                    date_stats[date]['with_cases'] += 1
        
        print(f"Courts processed: {len(court_stats)}")
        print(f"Dates processed: {len(date_stats)}")
        
        # Show court summary
        print("\n=== BY COURT ===")
        for court in sorted(court_stats.keys()):
            stats = court_stats[court]
            print(f"{court}: {stats['with_cases']}/{stats['total']} dates with cases, {stats['files']} files")
        
        # Show recent dates with opinions
        print("\n=== RECENT DATES WITH CRIMINAL OPINIONS ===")
        dates_with_cases = [(date, stats) for date, stats in date_stats.items() if stats['with_cases'] > 0]
        dates_with_cases.sort(key=lambda x: x[0], reverse=True)
        
        for date, stats in dates_with_cases[:10]:  # Last 10 dates with cases
            print(f"{date}: {stats['with_cases']}/{stats['total']} courts had cases, {stats['files']} files")
    
    else:
        print("No log file found")

if __name__ == "__main__":
    check_status() 