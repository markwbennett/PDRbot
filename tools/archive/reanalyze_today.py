#!/usr/bin/env python3
"""
Re-analyze today's cases that have execution errors
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

def reanalyze_execution_errors():
    """Re-analyze cases from today that have execution errors"""

    bot = PDRBot()

    # Get cases with execution errors from today
    conn = sqlite3.connect(bot.db_path)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT o.id, o.case_number, o.file_path, o.court, o.opinion_date
        FROM opinions o
        JOIN analysis a ON o.id = a.opinion_id
        WHERE a.opinion_date = '2025-11-10' AND LOWER(a.analysis_text) = 'execution error'
    ''')

    error_cases = cursor.fetchall()
    conn.close()

    if not error_cases:
        logger.info("No execution error cases found for today")
        return True

    logger.info(f"Found {len(error_cases)} execution error cases to re-analyze")

    success_count = 0
    for opinion_id, case_number, file_path, court, opinion_date in error_cases:
        logger.info(f"Re-analyzing {case_number}...")

        # Check if file exists
        if not Path(file_path).exists():
            logger.error(f"File not found: {file_path}")
            continue

        try:
            # Extract text
            text_content = bot.extract_text_from_pdf(file_path)
            if not text_content:
                logger.error(f"Failed to extract text from {file_path}")
                continue

            # Analyze with Claude (will use CLI first, then SDK if needed)
            analysis_result = bot.analyze_opinion_with_claude(text_content, case_number)

            if not analysis_result:
                logger.error(f"Analysis returned None for {case_number}")
                continue

            if "execution error" in analysis_result.lower():
                logger.warning(f"Still getting execution error for {case_number}")
                continue

            # Save the new analysis
            logger.info(f"Saving successful analysis for {case_number}")
            bot.save_analysis_to_db(opinion_id, case_number, court, opinion_date, analysis_result)

            # Scrape representatives
            logger.info(f"Scraping representatives for {case_number}")
            bot.scrape_and_save_representatives(case_number)

            success_count += 1
            logger.info(f"Successfully re-analyzed {case_number}")

        except Exception as e:
            logger.error(f"Error re-analyzing {case_number}: {e}")
            import traceback
            traceback.print_exc()
            continue

    logger.info(f"Re-analysis complete: {success_count}/{len(error_cases)} successful")
    return success_count == len(error_cases)

if __name__ == "__main__":
    success = reanalyze_execution_errors()
    sys.exit(0 if success else 1)
