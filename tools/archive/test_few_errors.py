#!/./.venv/bin/python
"""
Test a few execution error cases
"""

import sys
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
    """Test 5 execution error cases"""

    bot = PDRBot()

    # Get 5 execution error cases from different dates
    import sqlite3
    conn = sqlite3.connect(bot.db_path)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT o.id, o.case_number, o.court, o.opinion_date, o.file_path, a.analysis_timestamp
        FROM opinions o
        JOIN analysis a ON o.id = a.opinion_id
        WHERE a.analysis_text = 'Execution error'
        ORDER BY a.analysis_timestamp ASC
        LIMIT 5
    ''')

    cases = cursor.fetchall()
    conn.close()

    logger.info(f"Testing {len(cases)} cases with execution errors\n")

    for opinion_id, case_number, court, opinion_date, file_path, analysis_timestamp in cases:
        logger.info(f"Case: {case_number} (analyzed {analysis_timestamp})")
        logger.info(f"File: {file_path}")

        # Extract text
        text_content = bot.extract_text_from_pdf(file_path)
        if not text_content:
            logger.error(f"  ✗ Failed to extract text\n")
            continue

        text_len = len(text_content)
        logger.info(f"  Extracted {text_len} characters")

        # Analyze
        analysis_result = bot.analyze_opinion_with_claude(text_content, case_number)

        if not analysis_result:
            logger.error(f"  ✗ Analysis returned None\n")
            continue

        if "execution error" in analysis_result.lower():
            logger.warning(f"  ✗ Still getting execution error")
            logger.info(f"  Response: {analysis_result[:200]}\n")
        else:
            logger.info(f"  ✓ SUCCESS - Analysis completed ({len(analysis_result)} chars)")
            # Show has_interesting_issues
            has_interesting = "no interesting issues" not in analysis_result.lower()
            issue_count = analysis_result.count("▪ Issue Description:")
            logger.info(f"  Interesting: {has_interesting}, Issue count: {issue_count}\n")

    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
