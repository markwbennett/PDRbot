#!/./.venv/bin/python
"""
One-time script to send catch-up report for missed days (Oct 31 - Nov 7)
"""

import sys
import os
from datetime import datetime
sys.path.insert(0, '/home/mb/github/mwb_common')

# Import PDRBot
from pdrbot import PDRBot
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    """Generate and send catch-up report for Oct 31 - Nov 7"""

    logger.info("Starting catch-up report generation...")

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
        return False

    logger.info(f"Report generated: {report_path}")

    # Get interesting issues count
    results = bot.get_analysis_results(date_range=(start_date, end_date), interesting_only=True)
    interesting_count = len(results)

    # Get all recipients
    all_recipients = bot.get_all_recipients()

    if not all_recipients:
        logger.error("No recipients found")
        return False

    logger.info(f"Found {interesting_count} interesting issues across the date range")
    logger.info(f"Sending to {len(all_recipients)} recipients")

    # Send custom emails
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    import smtplib

    # Generate prompt PDF
    prompt_pdf_path = bot.generate_prompt_pdf("2025-11-08")

    successful_sends = 0

    for recipient in all_recipients:
        try:
            msg = MIMEMultipart()
            msg['From'] = bot.email_from
            msg['To'] = recipient
            msg['Subject'] = f"{bot.email_subject_prefix}—Oct 31-Nov 7 Combined Report—{interesting_count} interesting issues"

            # Custom email body explaining the situation
            body = f"""PDRBot produces a report at 9:10 a.m., at which time all of the day's opinions will likely have been released. If a court releases opinions after 9:10 a.m., they will be in the next day's report.

Daily PDRBot Report - COMBINED REPORT FOR OCT 31 - NOV 7

Due to a technical issue that occurred on October 31 and was resolved on November 8, PDRBot was unable to send daily reports for seven days. This combined report includes all criminal law opinions from October 31 through November 7, 2025.

This report contains AI-generated analysis of Texas Courts of Appeals criminal opinions for potential PDR worthiness.

Summary for this period:
- 70 total opinions analyzed
- {interesting_count} cases with interesting issues identified

Normal daily reporting has resumed as of November 8, 2025.

If you see an error in this report—especially if it misses what you think is an interesting issue—please email mb@ivi3.com.

PDRBot source code: https://github.com/markwbennett/PDRbot

The prompt used to produce this report is attached as a separate PDF.

Report generated: {datetime.now().strftime("%B %d, %Y at %I:%M %p")}

To unsubscribe, reply with 'unsubscribe' as the first word of the subject or body.
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
            successful_sends += 1

        except Exception as e:
            logger.error(f"Failed to send email to {recipient}: {e}")
            continue

    # Clean up prompt PDF
    if prompt_pdf_path and os.path.exists(prompt_pdf_path):
        try:
            os.remove(prompt_pdf_path)
        except:
            pass

    logger.info(f"Successfully sent emails to {successful_sends} out of {len(all_recipients)} recipients")
    return successful_sends > 0

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
