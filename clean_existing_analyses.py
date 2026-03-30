#!/./.venv/bin/python
"""
One-time script to clean existing analyses in database
"""

import sys
import sqlite3
import re
sys.path.insert(0, '/home/mb/github/mwb_common')

from pdrbot import PDRBot
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    """Clean all existing analyses in database"""

    bot = PDRBot()

    conn = sqlite3.connect(bot.db_path)
    cursor = conn.cursor()

    # Get all analyses
    cursor.execute('SELECT id, case_number, analysis_text FROM analysis')
    analyses = cursor.fetchall()

    logger.info(f"Processing {len(analyses)} analyses...")

    updated = 0
    recalculated = 0
    for analysis_id, case_number, analysis_text in analyses:
        # Clean the text using PDRBot's clean method
        cleaned_text = bot.clean_analysis_text(analysis_text)

        # Recalculate flags
        if "execution error" in cleaned_text.lower():
            has_interesting = False
            issue_count = 0
        else:
            has_interesting = "no interesting issues" not in cleaned_text.lower()

            # Count issues using improved pattern matching
            issue_patterns = [
                r'▪\s*Issue Description:',
                r'\*\*Issue Description:\*\*',
                r'\*\*Issue \d+:',
                r'Issue \d+:',
            ]
            issue_count = 0
            for pattern in issue_patterns:
                count = len(re.findall(pattern, cleaned_text))
                issue_count = max(issue_count, count)

        # Always update to ensure flags are correct
        cursor.execute('''
            UPDATE analysis
            SET analysis_text = ?,
                has_interesting_issues = ?,
                issue_count = ?
            WHERE id = ?
        ''', (cleaned_text, has_interesting, issue_count, analysis_id))

        if cleaned_text != analysis_text:
            updated += 1
        recalculated += 1

        if recalculated % 100 == 0:
            logger.info(f"Processed {recalculated} analyses ({updated} cleaned)...")

    conn.commit()
    conn.close()

    logger.info(f"Completed! Updated {updated} out of {len(analyses)} analyses")
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
