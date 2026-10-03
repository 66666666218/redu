"""消息触达单测(2026-10-03 补 —— 这个模块此前**没有测试**)。

**为什么值得测**:它是**告警的出口**。今天一整轮都在修"告警发不出去/发不出去没人知道"
(看门狗失联、备份失败、静默失败 A 类 7 例),而"出口本身"没有任何测试 ——
这里把几条**刻意的设计决定**钉住(它们都写在源码注释里,但注释拦不住回归)。
"""
import smtplib
from types import SimpleNamespace

from app.services import notifier as nf


def _settings(**kw):
    """用**真的 Settings**(不是 SimpleNamespace) —— `get_user_notifier` 里用了 pydantic 的
    `model_copy` 派生"用户自建 SMTP"的配置,假对象撑不住(实测:第一版就是这么挂的)。
    """
    from config.settings import Settings

    base = dict(_env_file=None, is_dev=False, smtp_host="smtp.example.com", smtp_port=465,
                smtp_user="u@example.com", smtp_pass="pw", smtp_from="监控",
                notify_to="a@b.c, d@e.f")
    base.update(kw)
    return Settings(**base)


# ---------------------------------------------------------------- 选哪个实现

def test_dev_mode_uses_null_notifier() -> None:
    assert isinstance(nf.get_notifier(_settings(is_dev=True)), nf.NullNotifier)


def test_no_smtp_host_uses_null_notifier() -> None:
    """**没配 SMTP → 空实现**,而不是"配了但发不出去"。"""
    assert isinstance(nf.get_notifier(_settings(smtp_host="")), nf.NullNotifier)


def test_configured_uses_email_notifier() -> None:
    assert isinstance(nf.get_notifier(_settings()), nf.EmailNotifier)


# ---------------------------------------------------------------- 发送

def test_send_returns_false_when_unconfigured(monkeypatch) -> None:
    """⚠️ 缺配置时**返回 False,不是 True** —— 静默"成功"会让上层以为已送达。"""
    assert nf.EmailNotifier(_settings(notify_to="")).send("s", "b") is False
    assert nf.EmailNotifier(_settings(smtp_host="")).send("s", "b") is False


def test_send_success(monkeypatch) -> None:
    sent = {}

    class _SMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"], sent["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            sent["login"] = (u, p)

        def sendmail(self, frm, to, msg):
            sent["to"] = to
            sent["msg_has_subject"] = "Subject:" in msg

    monkeypatch.setattr(nf.smtplib, "SMTP_SSL", _SMTP)
    assert nf.EmailNotifier(_settings()).send("主题", "正文") is True
    assert sent["host"] == "smtp.example.com" and sent["to"] == ["a@b.c", "d@e.f"]
    assert sent["msg_has_subject"]


def test_send_swallows_smtp_errors(monkeypatch) -> None:
    """⚠️ SMTP 异常要**接住并返回 False,不抛**。

    注释里写明原因:不接住会把**采集任务错标 failed**、定时摘要整批回滚重发 ——
    即"邮件服务器抖一下"会被误报成"采集挂了"。
    """
    def _boom(*a, **k):
        raise smtplib.SMTPException("smtp down")

    monkeypatch.setattr(nf.smtplib, "SMTP_SSL", _boom)
    assert nf.EmailNotifier(_settings()).send("s", "b") is False


def test_send_swallows_oserror(monkeypatch) -> None:
    """连接被拒/超时是 `OSError` 子类 —— 同样不能冒泡(同上)。"""
    def _boom(*a, **k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(nf.smtplib, "SMTP_SSL", _boom)
    assert nf.EmailNotifier(_settings()).send("s", "b") is False


# ---------------------------------------------------------------- 密码兼容读

def test_decrypt_secret_plaintext_passthrough() -> None:
    """没有 `enc:` 前缀 = 历史明文,原样返回(别把老配置读成空)。"""
    assert nf._decrypt_secret("plainpw") == "plainpw"
    assert nf._decrypt_secret("") == "" and nf._decrypt_secret(None) == ""


def test_decrypt_secret_bad_ciphertext_is_empty_not_raise() -> None:
    """密文解不开(密钥轮换过)→ **返回空串,不抛** —— 抛会把告警链路一起带走。"""
    assert nf._decrypt_secret("enc:not-a-valid-token") == ""


# ---------------------------------------------------------------- 用户级下发

def test_user_notifier_respects_dev_gate(monkeypatch) -> None:
    """⚠️ **dev 门控同样管住"用户自建 SMTP"**。

    注释写明:全局走 NullNotifier 时,若用户分支绕过该门,会得到
    "dev 环境不发全局邮件,但配了自建 SMTP 的用户仍收预警/日报"的**自相矛盾**语义。
    """
    user = SimpleNamespace(smtp_user="me@x.com", smtp_host="smtp.x.com", smtp_port=465,
                           smtp_pass="", smtp_from="", email="me@x.com")
    assert isinstance(nf.get_user_notifier(user, _settings(is_dev=True)), nf.NullNotifier)


def test_user_notifier_uses_user_smtp_when_configured() -> None:
    user = SimpleNamespace(smtp_user="me@x.com", smtp_host="smtp.x.com", smtp_port=465,
                           smtp_pass="plain", smtp_from="我", email="me@x.com")
    got = nf.get_user_notifier(user, _settings())
    assert isinstance(got, nf.EmailNotifier)
    assert got._recipients() == ["me@x.com"]        # 收件人是**他自己的邮箱**
    assert got._settings.smtp_host == "smtp.x.com"  # 用**他自己的** SMTP


def test_user_notifier_falls_back_to_global() -> None:
    """用户没配自建 SMTP → 回退全局(收件人取全局 NOTIFY_TO)。"""
    user = SimpleNamespace(smtp_user="", smtp_host="", smtp_port=None,
                           smtp_pass="", smtp_from="", email="me@x.com")
    got = nf.get_user_notifier(user, _settings())
    assert isinstance(got, nf.EmailNotifier)
    assert got._recipients() == ["a@b.c", "d@e.f"]
