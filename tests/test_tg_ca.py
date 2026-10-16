"""Telegram за SSL-фильтром антивируса: сертификат ESET подключается только к клиенту Telegram через TELEGRAM_CA_FILE (база не нужна)."""
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "fastapi-api"))

import tgnotify  # noqa: E402

# Самоподписанный сертификат только для проверки загрузки (не доверенный ни для чего)
PEM = """-----BEGIN CERTIFICATE-----
MIIBtjCCAVugAwIBAgITBmyf1XSXNmY/Owua2eaTJCnQmzAKBggqhkjOPQQDAjA5
MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6b24g
Um9vdCBDQSAzMB4XDTE1MDUyNjAwMDAwMFoXDTQwMDUyNjAwMDAwMFowOTELMAkG
A1UEBhMCVVMxDzANBgNVBAoTBkFtYXpvbjEZMBcGA1UEAxMQQW1hem9uIFJvb3Qg
Q0EgMzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABCmXp8ZBf8ANm+gBG1bG8lKl
ui2yEujSLtf6ycXYqm0fc4E7O5hrOXwzpcVOho6AF2hiRVd9RFgdszflZwjrZt6j
QjBAMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgGGMB0GA1UdDgQWBBSr
ttvXBp43rDCGB5Fwx5zEGbF4wDAKBggqhkjOPQQDAgNJADBGAiEA4IWSoxe3jfkr
BqWTrBqYaGFy+uGh0PsceGCmQ5nFuMQCIQCcAu/xlJyzlvnrxir4tiz+OpAUFteM
YyRIHN8wfdVoOw==
-----END CERTIFICATE-----
"""


def test_default_verify_without_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CA_FILE", raising=False)
    assert tgnotify.tg_verify() is True


def test_missing_file_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_CA_FILE", str(tmp_path / "нет.pem"))
    assert tgnotify.tg_verify() is True


def test_extra_ca_file_builds_context_with_system_roots(monkeypatch, tmp_path):
    f = tmp_path / "ca.pem"
    f.write_text(PEM, encoding="ascii")
    monkeypatch.setenv("TELEGRAM_CA_FILE", str(f))
    ctx = tgnotify.tg_verify()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    # системные корни остались: сертификатов больше одного (дополнительный + обычные)
    assert len(ctx.get_ca_certs()) > 1


def test_broken_file_falls_back_to_default(monkeypatch, tmp_path):
    f = tmp_path / "bad.pem"
    f.write_text("не сертификат", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_CA_FILE", str(f))
    assert tgnotify.tg_verify() is True
