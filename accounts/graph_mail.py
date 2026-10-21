"""Django email backend that sends through Microsoft 365 (Graph sendMail) from
the mailbox in MS_MAIL_SENDER. Needs the app registration to have the
application permission Mail.Send (ideally limited to that one mailbox)."""
import logging

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

from .microsoft import MicrosoftError, graph_post

log = logging.getLogger(__name__)


class GraphEmailBackend(BaseEmailBackend):
    def send_messages(self, messages):
        sent = 0
        for m in messages:
            html = next((c for c, mime in getattr(m, "alternatives", []) if mime == "text/html"), None)
            payload = {
                "message": {
                    "subject": m.subject,
                    "body": {"contentType": "HTML" if html else "Text", "content": html or m.body},
                    "toRecipients": [{"emailAddress": {"address": a}} for a in m.to],
                    "ccRecipients": [{"emailAddress": {"address": a}} for a in m.cc],
                    "bccRecipients": [{"emailAddress": {"address": a}} for a in m.bcc],
                },
                "saveToSentItems": False,
            }
            if m.reply_to:
                payload["message"]["replyTo"] = [{"emailAddress": {"address": a}} for a in m.reply_to]
            try:
                graph_post(f"/users/{settings.MS_MAIL_SENDER}/sendMail", payload)
                sent += 1
            except MicrosoftError:
                if not self.fail_silently:
                    raise
        return sent
