"""
通知モジュール: メール（Gmail/SMTP）と LINE Notify への送信
"""

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

log = logging.getLogger(__name__)


def send_email(config: dict, subject: str, body: str) -> bool:
    n = config.get("notifications", {})
    if not n.get("email_enabled"):
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = n["email_user"]
        msg["To"] = n["email_to"]
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        ctx = ssl.create_default_context()
        with smtplib.SMTP(n["email_smtp"], int(n["email_port"])) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(n["email_user"], n["email_password"])
            server.sendmail(n["email_user"], n["email_to"], msg.as_string())

        log.info(f"メール送信成功 → {n['email_to']}")
        return True
    except Exception as e:
        log.error(f"メール送信失敗: {e}")
        return False


def send_line(config: dict, message: str) -> bool:
    n = config.get("notifications", {})
    if not n.get("line_enabled") or not n.get("line_token"):
        return False
    try:
        resp = requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {n['line_token']}"},
            data={"message": message},
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("LINE通知送信成功")
            return True
        log.error(f"LINE通知失敗: {resp.status_code} {resp.text}")
        return False
    except Exception as e:
        log.error(f"LINE通知失敗: {e}")
        return False


def build_message(buy_signals: list, watch_signals: list, run_date: str) -> str:
    lines = [f"📈 スイングセンサー [{run_date}]", ""]

    if buy_signals:
        lines.append(f"✅ 買い推奨: {len(buy_signals)}件")
        for s in buy_signals[:15]:
            code = s.get("ticker", "").replace(".T", "")
            name = s.get("name", "")
            strat = "順張り" if s.get("strategy") == "trend" else "逆張り"
            lines.append(f"  • {code} {name} [{strat}]")
        if len(buy_signals) > 15:
            lines.append(f"  … 他 {len(buy_signals) - 15} 件")
    else:
        lines.append("✅ 買い推奨: 0件")

    lines.append("")
    lines.append(f"👁 監視中（翌日確認）: {len(watch_signals)}件")
    return "\n".join(lines)


def notify(config: dict, buy_signals: list, watch_signals: list, run_date: str) -> dict:
    msg = build_message(buy_signals, watch_signals, run_date)
    subject = f"スイングセンサー 買い推奨{len(buy_signals)}件 [{run_date}]"
    return {
        "email": send_email(config, subject, msg),
        "line": send_line(config, "\n" + msg),
    }
