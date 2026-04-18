#!/usr/bin/env python3
"""
Fix analysis for 06-25-00068-CR specifically
"""
import sys
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

# Add paths
sys.path.insert(0, '/home/mb/github/mwb_common')

# Import PDRBot
from pdrbot import PDRBot
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def fix_case():
    """Fix analysis for 06-25-00068-CR"""

    bot = PDRBot()
    case_number = "06-25-00068-CR"

    # Get case info
    conn = sqlite3.connect(bot.db_path)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, file_path, court, opinion_date
        FROM opinions
        WHERE case_number = ?
    ''', (case_number,))

    result = cursor.fetchone()
    conn.close()

    if not result:
        logger.error(f"Case not found: {case_number}")
        return False

    opinion_id, file_path, court, opinion_date = result

    logger.info(f"Found case: {case_number}")
    logger.info(f"File path: {file_path}")

    # Check if file exists
    if not Path(file_path).exists():
        logger.error(f"File not found: {file_path}")
        return False

    try:
        # Extract text
        logger.info("Extracting text...")
        text_content = bot.extract_text_from_pdf(file_path)
        if not text_content:
            logger.error("Failed to extract text")
            return False

        logger.info(f"Extracted {len(text_content)} characters")

        # Analyze with Claude
        logger.info("Analyzing with Claude...")
        analysis_result = bot.analyze_opinion_with_claude(text_content, case_number)

        if not analysis_result:
            logger.error("Analysis returned None")
            return False

        logger.info(f"Analysis result length: {len(analysis_result)} characters")
        logger.info(f"Analysis result:\n{analysis_result}")

        if "execution error" in analysis_result.lower():
            logger.warning("Still getting execution error")
            return False

        # Save the analysis
        logger.info("Saving analysis...")
        bot.save_analysis_to_db(opinion_id, case_number, court, opinion_date, analysis_result)

        logger.info("Successfully saved analysis")
        return True

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = fix_case()
    sys.exit(0 if success else 1)
