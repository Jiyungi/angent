"""Angent email sender — Gmail via SMTP with an App Password.

This is the reliable, Windows-friendly send path (no Google OAuth app needed).
Requires GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")


def send_email(to_address: str, subject: str, body: str, from_name: str = "Angent") -> dict:
    """Send a plain-text email via Gmail SMTP.

    Returns a dict describing the result (used by the Sender agent + UI log).
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return {"ok": False, "error": "GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env"}

    msg = EmailMessage()
    msg["From"] = f"{from_name} <{GMAIL_ADDRESS}>"
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        return {"ok": True, "to": to_address, "subject": subject}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    # Quick self-test: sends a test email to yourself.
    result = send_email(
        to_address=GMAIL_ADDRESS or "you@example.com",
        subject="Angent SMTP test",
        body="If you're reading this, Angent can send email. \u2705",
    )
    print(result)
