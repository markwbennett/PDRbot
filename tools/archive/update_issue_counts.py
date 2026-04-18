#!/./.venv/bin/python
"""
Update issue counts for all existing analyses using improved pattern matching
"""

import sys
import os
sys.path.insert(0, '/home/mb/github/mwb_common')

from pdrbot import PDRBot
import logging
import sqlite3
import re

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    """Update issue counts for all analyses"""

    bot = PDRBot()

    conn = sqlite3.connect(bot.db_path)
    cursor = conn.cursor()

    # Get all analyses
    cursor.execute('SELECT opinion_id, case_number, analysis_text, issue_count FROM analysis')
    all_analyses = cursor.fetchall()

    logger.info(f"Updating issue counts for {len(all_analyses)} analyses")

    updated = 0
    for opinion_id, case_number, analysis_text, old_count in all_analyses:
        # Count issues using improved pattern matching
        issue_patterns = [
            r'▪\s*Issue Description:',
            r'\*\*Issue Description:\*\*',
            r'\*\*Issue \d+:',
            r'Issue \d+:',
            r'▪\s*Headline:',
            r'\*\*Headline:\*\*',
        ]
        new_count = 0
        for pattern in issue_patterns:
            count = len(re.findall(pattern, analysis_text))
            new_count = max(new_count, count)

        # Update if count changed
        if new_count != old_count:
            cursor.execute('UPDATE analysis SET issue_count = ? WHERE opinion_id = ?',
                          (new_count, opinion_id))
            logger.info(f"Updated {case_number}: {old_count} -> {new_count}")
            updated += 1

    conn.commit()
    conn.close()

    logger.info(f"Updated {updated} analyses")
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
