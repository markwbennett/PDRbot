#!/usr/bin/env python3
"""
Test re-analyzing a case that previously got execution error
"""
import sys
import os
from pathlib import Path

# Load environment
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

def test_reanalyze_case():
    """Test re-analyzing case 06-25-00068-CR that got execution error"""

    case_number = "06-25-00068-CR"
    pdf_path = "data/20251110/06-25-00068-CR.pdf"

    # Check if file exists
    if not Path(pdf_path).exists():
        logger.error(f"PDF file not found: {pdf_path}")
        return False

    try:
        # Create PDRBot instance
        bot = PDRBot()

        # Extract text from PDF
        logger.info(f"Extracting text from {pdf_path}...")
        text_content = bot.extract_text_from_pdf(pdf_path)

        if not text_content:
            logger.error("Failed to extract text from PDF")
            return False

        logger.info(f"Extracted {len(text_content)} characters")

        # Analyze with Claude
        logger.info(f"Analyzing {case_number}...")
        analysis_result = bot.analyze_opinion_with_claude(text_content, case_number)

        if not analysis_result:
            logger.error("Analysis returned None")
            return False

        # Check result
        logger.info(f"Analysis length: {len(analysis_result)} characters")
        logger.info(f"First 200 chars: {analysis_result[:200]}")

        if "execution error" in analysis_result.lower():
            logger.error("Still getting execution error!")
            logger.info("This means SDK fallback may have also failed, or SDK also returned execution error")
            return False
        else:
            logger.info("SUCCESS: Got valid analysis (no execution error)")
            return True

    except Exception as e:
        logger.error(f"Error during analysis: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_reanalyze_case()
    sys.exit(0 if success else 1)
