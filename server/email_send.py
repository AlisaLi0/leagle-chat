"""Transactional email for JuriCodex — used to verify a user's email address.

Sign-in is OAuth-only, but the account email doubles as the billing email and
the key we merge duplicate sign-ins on. So when a user adds or changes an email
from Settings we must prove they control it before trusting it: we mail a short
code and only set ``users.email`` once they enter it back.

Sending goes through Aliyun Direct Mail (smtpdm) over implicit TLS on 465 —
mirroring the other juricodex.online mailers. Config is env-only; if SMTP is not
configured the module degrades to "log the code" so local/dev still works and
the endpoint can surface the code to the caller instead of failing.

Env:
  SMTP_HOST       e.g. smtpdm-ap-southeast-1.aliyun.com
  SMTP_PORT       default 465
  SMTP_USER       full from-address, e.g. noreply@mail.juricodex.online
  SMTP_PASS       SMTP password (the smtpdm sending password)
  SMTP_FROM       from-address (defaults to SMTP_USER)
  SMTP_FROM_NAME  display name (default "JuriCodex")
  SMTP_SSL        "1"/"true" for implicit TLS (default), else STARTTLS
"""
from __future__ import annotations

import logging
import os
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("leagle.email")

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465") or "465")
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = (os.getenv("SMTP_FROM", "") or SMTP_USER).strip()
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "JuriCodex").strip()
_SMTP_SSL = (os.getenv("SMTP_SSL", "1").strip().lower() not in ("0", "false", "no"))


def enabled() -> bool:
    """True only when enough is configured to actually send mail."""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM)


def gen_code(digits: int = 6) -> str:
    """A numeric verification code that avoids modulo bias."""
    lo, hi = 10 ** (digits - 1), 10 ** digits
    return str(secrets.randbelow(hi - lo) + lo)


def send_email(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Send one email. Returns True on success, False if not configured/failed.

    Never raises to the caller — email is best-effort and must not 500 an API.
    """
    to = (to or "").strip()
    if not to:
        return False
    if not enabled():
        log.warning("SMTP not configured; would send to %s: %s", to, subject)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM))
    msg["To"] = to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        if _SMTP_SSL:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=15) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:  # network/auth/timeouts
        log.warning("SMTP send to %s failed: %s", to, exc)
        return False


# Light localization for the verification email. Falls back to English; the
# product UI is fully translated, but the mail only needs the code to be clear.
_SUBJECTS = {
    "en": "Your JuriCodex verification code",
    "es": "Tu código de verificación de JuriCodex",
    "zh": "你的 JuriCodex 验证码",
    "zh-TW": "你的 JuriCodex 驗證碼",
    "fr": "Votre code de vérification JuriCodex",
    "pt": "Seu código de verificação JuriCodex",
    "ko": "JuriCodex 인증 코드",
    "ja": "JuriCodex の確認コード",
    "vi": "Mã xác minh JuriCodex của bạn",
}
_INTROS = {
    "en": "Use this code to verify your email for JuriCodex:",
    "es": "Usa este código para verificar tu correo en JuriCodex:",
    "zh": "请使用以下验证码验证你在 JuriCodex 的邮箱：",
    "zh-TW": "請使用以下驗證碼驗證你在 JuriCodex 的電子郵件：",
    "fr": "Utilisez ce code pour vérifier votre e-mail JuriCodex :",
    "pt": "Use este código para verificar seu e-mail no JuriCodex:",
    "ko": "JuriCodex 이메일을 확인하려면 이 코드를 입력하세요:",
    "ja": "JuriCodex のメールアドレスを確認するには、このコードを入力してください：",
    "vi": "Dùng mã này để xác minh email của bạn trên JuriCodex:",
}
_EXPIRES = {
    "en": "The code expires in 15 minutes. If you didn't request it, ignore this email.",
    "es": "El código caduca en 15 minutos. Si no lo solicitaste, ignora este correo.",
    "zh": "验证码 15 分钟内有效。如果不是你本人操作，请忽略此邮件。",
    "zh-TW": "驗證碼 15 分鐘內有效。如果不是你本人操作，請忽略此郵件。",
    "fr": "Le code expire dans 15 minutes. Si vous ne l'avez pas demandé, ignorez cet e-mail.",
    "pt": "O código expira em 15 minutos. Se você não solicitou, ignore este e-mail.",
    "ko": "코드는 15분 후 만료됩니다. 요청하지 않았다면 이 이메일을 무시하세요.",
    "ja": "コードは15分で期限切れになります。心当たりがない場合は、このメールを無視してください。",
    "vi": "Mã sẽ hết hạn sau 15 phút. Nếu bạn không yêu cầu, hãy bỏ qua email này.",
}


def send_verification(to: str, code: str, lang: str = "en") -> bool:
    """Send a verification code email in the user's language (English fallback)."""
    subject = _SUBJECTS.get(lang, _SUBJECTS["en"])
    intro = _INTROS.get(lang, _INTROS["en"])
    expires = _EXPIRES.get(lang, _EXPIRES["en"])
    text = f"{intro}\n\n    {code}\n\n{expires}\n\n— JuriCodex"
    html = (
        '<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:480px;'
        'margin:0 auto;padding:24px;color:#1a1a2e">'
        '<div style="font-size:20px;font-weight:700;margin-bottom:16px">⚖ JuriCodex</div>'
        f'<p style="font-size:15px;line-height:1.5;margin:0 0 16px">{intro}</p>'
        f'<div style="font-size:32px;font-weight:700;letter-spacing:8px;background:#f3f4f8;'
        f'border-radius:10px;padding:16px;text-align:center;margin:0 0 16px">{code}</div>'
        f'<p style="font-size:13px;color:#666;line-height:1.5;margin:0">{expires}</p>'
        '</div>'
    )
    return send_email(to, subject, text, html)
