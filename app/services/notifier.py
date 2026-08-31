"""消息触达(见 doc/dev.md §5.6)。

抽象接口 `Notifier`:既用于热点告警(`notify(alert)`),也用于每日总结等
通用邮件(`send(subject, body)`)。开发模式(`IS_DEV=true`)用 `NullNotifier` 仅打日志。
"""
from __future__ import annotations

import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

from config.settings import Settings
from app.models import Alert
from app.utils import get_logger

logger = get_logger(__name__)


class Notifier:
    """消息触达接口。"""

    def send(self, subject: str, body: str, context: str = "", html: bool = False) -> bool:
        """发送一封通用邮件,返回是否成功。"""
        raise NotImplementedError

    def notify(self, alert: Alert, context: str = "") -> bool:
        """发送一条热点告警,返回是否成功。"""
        raise NotImplementedError


class NullNotifier(Notifier):
    """开发/未配置时的空实现:只写日志,不外发。"""

    def __init__(self) -> None:
        self._sent: list[Alert] = []

    def send(self, subject: str, body: str, context: str = "", html: bool = False) -> bool:
        logger.info("[DEV] 跳过外发邮件 subject=%s", subject)
        return True

    def notify(self, alert: Alert, context: str = "") -> bool:
        logger.info("[DEV] 跳过外发告警 keyword=%s reason=%s", alert.keyword, alert.reason)
        self._sent.append(alert)
        return True


class EmailNotifier(Notifier):
    """基于 SMTP(SSL)的邮件实现。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _recipients(self) -> list[str]:
        return self._settings.notify_to_list

    def send(self, subject: str, body: str, context: str = "", html: bool = False) -> bool:
        settings = self._settings
        recipients = self._recipients()
        if not recipients or not settings.smtp_host:
            logger.warning("未配置 SMTP 或收件人,邮件已忽略 subject=%s", subject)
            return False

        message = MIMEText(body, "html" if html else "plain", "utf-8")
        message["Subject"] = subject
        message["From"] = formataddr((settings.smtp_from or "热点监控", settings.smtp_user))
        message["To"] = ", ".join(recipients)
        try:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                server.login(settings.smtp_user, settings.smtp_pass) if settings.smtp_user else None
                server.sendmail(settings.smtp_user, recipients, message.as_string())
            logger.info("邮件已发送 subject=%s recipients=%s", subject, recipients)
            return True
        except smtplib.SMTPException as exc:
            logger.error("邮件发送失败:%s", exc)
            return False

    def notify(self, alert: Alert, context: str = "") -> bool:
        subject = f"[热点预警] {alert.keyword} {alert.reason}"
        return self.send(subject, self._build_body(alert, context), context)

    @staticmethod
    def _build_body(alert: Alert, context: str) -> str:
        lines = [
            f"关键词: {alert.keyword}",
            f"原因: {alert.reason}",
            f"触发时间: {alert.triggered_at.isoformat()}",
        ]
        if alert.sources:
            lines.append("来源指标:")
            for src in alert.sources:
                lines.append(f"  - {src}")
        if context:
            lines.append(f"备注: {context}")
        return "\n".join(lines)


def get_notifier(settings: Settings) -> Notifier:
    """按配置返回对应的 Notifier。

    - `IS_DEV=true` 或无 SMTP 配置:`NullNotifier`(开发模式,仅日志)。
    - 否则:`EmailNotifier`。
    """
    if settings.is_dev or not settings.smtp_host:
        return NullNotifier()
    return EmailNotifier(settings)


def get_user_notifier(user, settings: Settings) -> Notifier:
    """返回发件给某用户的 Notifier。

    - 用户配置了自己的 SMTP → 用其 SMTP、收件人为其邮箱;
    - 否则回退全局 notifier(收件人取全局 NOTIFY_TO)。
    """
    if getattr(user, "smtp_user", None) and getattr(user, "smtp_host", None):
        per = settings.model_copy(
            update={
                "smtp_host": user.smtp_host,
                "smtp_port": user.smtp_port or settings.smtp_port,
                "smtp_user": user.smtp_user,
                "smtp_pass": user.smtp_pass or "",
                "smtp_from": user.smtp_from or settings.smtp_from,
                "notify_to": user.email or "",
            }
        )
        if per.smtp_host and per.smtp_user:
            return EmailNotifier(per)
    return get_notifier(settings)
