#!/usr/bin/env python3
"""
Send corrected today's report via email
"""
import sys
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

def send_report():
    """Send corrected report via email"""

    bot = PDRBot()
    target_date = "2025-11-10"
    report_path = "data/pdrbot_report_20251110-2.pdf"

    logger.info(f"Sending CORRECTED report for {target_date}")
    logger.info(f"Report path: {report_path}")

    # Send email
    success = bot.send_email_report(report_path, target_date)

    if success:
        logger.info("Corrected email sent successfully")
        return True
    else:
        logger.error("Failed to send email")
        return False

if __name__ == "__main__":
    success = send_report()
    sys.exit(0 if success else 1)
