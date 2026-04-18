#!/./.venv/bin/python
"""
Generate and send updated catch-up report to mb@ivi3.com only
"""

import sys
import os
from datetime import datetime
sys.path.insert(0, '/home/mb/github/mwb_common')

from pdrbot import PDRBot
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import smtplib

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    """Generate and send updated catch-up report to mb@ivi3.com only"""

    logger.info("Generating updated catch-up report after re-analysis...")

    # Initialize PDRBot
    bot = PDRBot()

    # Date range for missed reports
    start_date = "2025-10-31"
    end_date = "2025-11-07"

    # Generate combined report
    logger.info(f"Generating report for {start_date} through {end_date}")
    report_path = bot.generate_analysis_report(
        date_range=(start_date, end_date),
        custom_title=f"Oct 31 through Nov 7, 2025 Combined Report (Updated)"
    )

    if not report_path:
        logger.error("Failed to generate report")
        return False

    # Get interesting issues count
    results = bot.get_analysis_results(date_range=(start_date, end_date), interesting_only=True)
    interesting_count = len(results)

    logger.info(f"Report generated: {report_path}")
    logger.info(f"Found {interesting_count} interesting issues across the date range")

    # Get total count
    all_results = bot.get_analysis_results(date_range=(start_date, end_date), interesting_only=False)
    total_count = len(all_results)

    # Send to mb@ivi3.com only
    recipient = "mb@ivi3.com"

    # Generate prompt PDF
    prompt_pdf_path = bot.generate_prompt_pdf("2025-11-08")

    try:
        msg = MIMEMultipart()
        msg['From'] = bot.email_from
        msg['To'] = recipient
        msg['Subject'] = f"{bot.email_subject_prefix}—Oct 31-Nov 7 Updated Report—{interesting_count} interesting issues"

        # Custom email body explaining the updated report
        body = f"""PDRBot Updated Report - Oct 31 through Nov 7, 2025

This is an UPDATED version of the catch-up report sent earlier today.

Previous issue: The original report included 69 cases that had "Execution error" due to a transient Claude API issue during Oct 31 - Nov 7. These cases have now been successfully re-analyzed.

Summary for this period:
- {total_count} total opinions analyzed (after filtering out execution errors and incomplete analyses)
- {interesting_count} cases with interesting legal issues identified
- All 69 previously failed cases have been successfully re-analyzed

This report now contains the complete analysis for the missed week.

Normal daily reporting has resumed as of November 8, 2025.

If you see an error in this report—especially if it misses what you think is an interesting issue—please email mb@ivi3.com.

PDRBot source code: https://github.com/markwbennett/PDRbot

The prompt used to produce this report is attached as a separate PDF.

Report generated: {datetime.now().strftime("%B %d, %Y at %I:%M %p")}
"""

        msg.attach(MIMEText(body, 'plain'))

        # Attach main report PDF
        with open(report_path, "rb") as attachment:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            filename = os.path.basename(report_path)
            part.add_header(
                'Content-Disposition',
                f'attachment; filename= {filename}'
            )
            msg.attach(part)

        # Attach prompt PDF if available
        if prompt_pdf_path and os.path.exists(prompt_pdf_path):
            with open(prompt_pdf_path, "rb") as attachment:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(attachment.read())
                encoders.encode_base64(part)
                filename = os.path.basename(prompt_pdf_path)
                part.add_header(
                    'Content-Disposition',
                    f'attachment; filename= {filename}'
                )
                msg.attach(part)

        # Send email
        if bot.email_smtp_port == 465:
            server = smtplib.SMTP_SSL(bot.email_smtp_host, bot.email_smtp_port)
        else:
            server = smtplib.SMTP(bot.email_smtp_host, bot.email_smtp_port)
            server.starttls()
        server.login(bot.email_auth_user, bot.email_password)
        text = msg.as_string()
        server.sendmail(bot.email_from, [recipient], text)
        server.quit()

        logger.info(f"Email sent successfully to {recipient}")

        # Clean up prompt PDF
        if prompt_pdf_path and os.path.exists(prompt_pdf_path):
            try:
                os.remove(prompt_pdf_path)
            except:
                pass

        print(f"\nReport path: {report_path}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
