"""消息触达/告警(见 doc/dev.md §5.6)。

定义抽象接口 `Notifier`;MVP 提供 `EmailNotifier`(SMTP SSL);
开发模式(`IS_DEV=true`)使用 `NullNotifier` 仅打日志、不外发。
发信失败必须 try/except 并返回标准化结果,不阻塞主流程。
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
    """告警通道接口。"""

    def notify(self, alert: Alert, context: str = "") -> bool:
        """发送一条告警,返回是否成功。"""
        raise NotImplementedError


class NullNotifier(Notifier):
    """开发/未配置时的空实现:只写日志,不外发。"""

    def __init__(self) -> None:
        self._sent: list[Alert] = []

    def notify(self, alert: Alert, context: str = "") -> bool:
        logger.info("[DEV] 跳过外发告警 keyword=%s reason=%s", alert.keyword, alert.reason)
        self._sent.append(alert)
        return True


class EmailNotifier(Notifier):
    """基于 SMTP(SSL)的邮件告警实现。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def notify(self, alert: Alert, context: str = "") -> bool:
        settings = self._settings
        recipients = settings.notify_to_list
        if not recipients or not settings.smtp_host:
            logger.warning("未配置 SMTP 或收件人,告警已忽略 keyword=%s", alert.keyword)
            return False

        subject = f"[热点预警] {alert.keyword} {alert.reason}"
        body = self._build_body(alert, context)
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = subject
        message["From"] = formataddr(("热点监控", settings.smtp_user))
        message["To"] = ", ".join(recipients)

        try:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                server.login(settings.smtp_user, settings.smtp_pass) if settings.smtp_user else None
                server.sendmail(settings.smtp_user, recipients, message.as_string())
            logger.info("邮件告警已发送 keyword=%s recipients=%s", alert.keyword, recipients)
            return True
        except smtplib.SMTPException as exc:
            logger.error("邮件告警发送失败:%s", exc)
            return False

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
