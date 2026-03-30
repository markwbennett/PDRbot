#!/./.venv/bin/python
"""
Diagnose execution error cases
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
    """Test analysis of an execution error case"""

    bot = PDRBot()

    # Get one execution error case
    import sqlite3
    conn = sqlite3.connect(bot.db_path)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT o.id, o.case_number, o.court, o.opinion_date, o.file_path
        FROM opinions o
        JOIN analysis a ON o.id = a.opinion_id
        WHERE a.analysis_text = 'Execution error'
        ORDER BY o.opinion_date DESC
        LIMIT 1
    ''')

    result = cursor.fetchone()
    conn.close()

    if not result:
        logger.error("No execution error cases found")
        return False

    opinion_id, case_number, court, opinion_date, file_path = result

    logger.info(f"Testing case: {case_number}")
    logger.info(f"File path: {file_path}")

    # Extract text from PDF
    logger.info("Extracting text from PDF...")
    text_content = bot.extract_text_from_pdf(file_path)

    if not text_content:
        logger.error("Failed to extract text")
        return False

    text_length = len(text_content)
    logger.info(f"Extracted {text_length} characters")
    logger.info(f"First 500 chars: {text_content[:500]}")

    # Try analyzing with Claude
    logger.info("\nAttempting analysis with Claude...")
    analysis_result = bot.analyze_opinion_with_claude(text_content, case_number)

    if analysis_result:
        logger.info(f"\nAnalysis result ({len(analysis_result)} chars):")
        logger.info(f"First 1000 chars:\n{analysis_result[:1000]}")
        logger.info(f"\nLast 500 chars:\n{analysis_result[-500:]}")

        if "execution error" in analysis_result.lower():
            logger.error("\n*** EXECUTION ERROR REPRODUCED ***")
        else:
            logger.info("\n*** ANALYSIS SUCCEEDED ***")
    else:
        logger.error("Analysis returned None")

    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
