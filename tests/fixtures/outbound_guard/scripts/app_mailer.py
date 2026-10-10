# test fixture for outbound-guard: an app emailing its own users, which the operator allows
import smtplib
from email.message import EmailMessage


def send_welcome(address):
    msg = EmailMessage()
    msg["To"] = address
    msg["Subject"] = "Welcome"
    with smtplib.SMTP("localhost") as s:
        s.send_message(msg)
