#!/./.venv/bin/python
"""
Generate catch-up report for missed days (Oct 31 - Nov 7) without emailing
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
    """Generate catch-up report for Oct 31 - Nov 7"""

    logger.info("Generating catch-up report...")

    # Initialize PDRBot
    bot = PDRBot()

    # Date range for missed reports
    start_date = "2025-10-31"
    end_date = "2025-11-07"

    # Generate combined report
    logger.info(f"Generating report for {start_date} through {end_date}")
    report_path = bot.generate_analysis_report(
        date_range=(start_date, end_date),
        custom_title=f"Oct 31 through Nov 7, 2025 Combined Report"
    )

    if not report_path:
        logger.error("Failed to generate report")
        return None

    # Get interesting issues count
    results = bot.get_analysis_results(date_range=(start_date, end_date), interesting_only=True)
    interesting_count = len(results)

    logger.info(f"Report generated: {report_path}")
    logger.info(f"Found {interesting_count} interesting issues across the date range")

    print(f"\nReport path: {report_path}")

    return report_path

if __name__ == "__main__":
    report_path = main()
    sys.exit(0 if report_path else 1)
