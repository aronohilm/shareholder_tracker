"""
notify.py — Email notifications via Gmail or Resend.
"""

import os
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime

log = logging.getLogger(__name__)

CHANGE_LABELS = {
    "new_entry":  ("🆕", "New Shareholder"),
    "dropped_out": ("👋", "Dropped Out"),
    "increased":  ("📈", "Stake Increased"),
    "decreased":  ("📉", "Stake Decreased"),
}


def format_html(changes: list[dict]) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    by_company: dict[str, list] = {}
    for c in changes:
        by_company.setdefault(c["ticker"], []).append(c)

    rows = ""
    for ticker, items in sorted(by_company.items()):
        company = items[0]["company"]
        rows += f"""
        <tr style="background:#f5f5f5;">
          <td colspan="3" style="padding:10px 12px;font-weight:bold;
              font-size:15px;border-top:2px solid #ddd;">
            {company} <span style="color:#888;font-weight:normal;">({ticker})</span>
          </td>
        </tr>"""
        for c in items:
            emoji, label = CHANGE_LABELS.get(c["type"], ("•", c["type"]))
            name = c["name"]
            if c["pct_now"] and c["pct_before"]:
                detail = f"{c['pct_before']:.2f}% → {c['pct_now']:.2f}% ({c['delta']:+.2f}%)"
            elif c["pct_now"]:
                detail = f"{c['pct_now']:.2f}%"
            else:
                detail = f"was {c['pct_before']:.2f}%"

            rows += f"""
        <tr>
          <td style="padding:8px 12px 8px 24px;width:140px;">
            <span style="background:#eef;border-radius:4px;padding:2px 8px;
                font-size:12px;color:#336;">{emoji} {label}</span>
          </td>
          <td style="padding:8px 8px;font-weight:500;">{name}</td>
          <td style="padding:8px 12px;color:#555;font-size:13px;">{detail}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Arial,sans-serif;max-width:700px;margin:0 auto;color:#333;">
  <div style="background:#1a2e1a;padding:20px 24px;border-radius:8px 8px 0 0;">
    <h1 style="margin:0;color:#fff;font-size:20px;">📊 Shareholder Tracker — Changes Detected</h1>
    <p style="margin:4px 0 0;color:#aaa;font-size:13px;">{now}</p>
  </div>
  <div style="border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px;overflow:hidden;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;font-size:14px;">
      {rows}
    </table>
  </div>
  <p style="color:#aaa;font-size:12px;margin-top:16px;text-align:center;">
    {len(changes)} change(s) across {len(by_company)} company/companies
  </p>
</body></html>"""


def format_text(changes: list[dict]) -> str:
    lines = ["SHAREHOLDER TRACKER — Changes Detected", "=" * 40, ""]
    by_company: dict[str, list] = {}
    for c in changes:
        by_company.setdefault(c["ticker"], []).append(c)
    for ticker, items in sorted(by_company.items()):
        lines.append(f"{items[0]['company']} ({ticker})")
        for c in items:
            lines.append(f"  {c['emoji']} {c['summary']}")
        lines.append("")
    return "\n".join(lines)


def notify_email(changes: list[dict]) -> bool:
    # Try Resend first
    api_key = os.environ.get("RESEND_API_KEY")
    to_addr = os.environ.get("NOTIFY_EMAIL_TO")

    if api_key and to_addr:
        tickers = sorted(set(c["ticker"] for c in changes))
        subject = f"[Shareholder Tracker] Changes detected — {', '.join(tickers)}"
        payload = json.dumps({
            "from":    "Shareholder Tracker <onboarding@resend.dev>",
            "to":      [to_addr],
            "subject": subject,
            "html":    format_html(changes),
            "text":    format_text(changes),
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
                log.info(f"Email sent via Resend — id: {result.get('id')}")
                return True
        except urllib.error.HTTPError as e:
            log.error(f"Resend error {e.code}: {e.read().decode()}")

    # Fall back to Gmail
    from_addr = os.environ.get("NOTIFY_EMAIL_FROM")
    password = os.environ.get("NOTIFY_EMAIL_PASS")
    if from_addr and password and to_addr:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        tickers = sorted(set(c["ticker"] for c in changes))
        subject = f"[Shareholder Tracker] Changes — {', '.join(tickers)}"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.attach(MIMEText(format_text(changes), "plain", "utf-8"))
        msg.attach(MIMEText(format_html(changes), "html", "utf-8"))
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(from_addr, password)
                smtp.sendmail(from_addr, to_addr, msg.as_string())
            log.info(f"Email sent via Gmail to {to_addr}")
            return True
        except Exception as e:
            log.error(f"Gmail error: {e}")

    log.warning("No email credentials configured")
    return False


def send_notifications(changes: list[dict]):
    if not changes:
        return
    log.info(f"Sending notification for {len(changes)} change(s)...")
    notify_email(changes)
