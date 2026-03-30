#!/./.venv/bin/python
"""
Re-analyze all cases that have "Execution error"
"""

import sys
sys.path.insert(0, '/home/mb/github/mwb_common')

from pdrbot import PDRBot
import logging
import time

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    """Re-analyze all execution error cases"""

    bot = PDRBot()

    # Get all execution error cases
    import sqlite3
    conn = sqlite3.connect(bot.db_path)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT o.id, o.case_number, o.court, o.opinion_date, o.file_path
        FROM opinions o
        JOIN analysis a ON o.id = a.opinion_id
        WHERE a.analysis_text = 'Execution error'
        ORDER BY o.opinion_date DESC
    ''')

    cases = cursor.fetchall()
    conn.close()

    total = len(cases)
    logger.info(f"Found {total} cases with execution errors")

    if total == 0:
        return True

    # Ask for confirmation
    print(f"\nThis will re-analyze {total} cases. Continue? (y/n): ", end='')
    response = input().strip().lower()
    if response != 'y':
        logger.info("Aborted")
        return False

    successful = 0
    still_failing = 0
    extraction_failed = 0

    for i, (opinion_id, case_number, court, opinion_date, file_path) in enumerate(cases, 1):
        logger.info(f"\n[{i}/{total}] Re-analyzing {case_number}...")

        # Extract text
        text_content = bot.extract_text_from_pdf(file_path)
        if not text_content:
            logger.error(f"Failed to extract text from {file_path}")
            extraction_failed += 1
            continue

        # Analyze
        analysis_result = bot.analyze_opinion_with_claude(text_content, case_number)

        if not analysis_result:
            logger.error(f"Analysis returned None for {case_number}")
            still_failing += 1
            continue

        if "execution error" in analysis_result.lower():
            logger.warning(f"Still getting execution error for {case_number}")
            still_failing += 1
            continue

        # Success! Save the new analysis
        success = bot.save_analysis_to_db(opinion_id, case_number, court, opinion_date, analysis_result)
        if success:
            logger.info(f"✓ Successfully re-analyzed {case_number}")
            successful += 1
        else:
            logger.error(f"Failed to save analysis for {case_number}")
            still_failing += 1

        # Rate limiting - wait 3 seconds between requests
        if i < total:
            time.sleep(3)

    logger.info(f"\n=== Summary ===")
    logger.info(f"Total cases: {total}")
    logger.info(f"Successfully re-analyzed: {successful}")
    logger.info(f"Extraction failed: {extraction_failed}")
    logger.info(f"Still failing: {still_failing}")

    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
