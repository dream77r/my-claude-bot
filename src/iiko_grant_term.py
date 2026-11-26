"""Срок подключения BB. Старые документы с числовым expires остаются срочными."""
from datetime import datetime, timezone


def valid_term(grant, now):
    if grant.get("revoked"):
        return False
    if grant.get("expiryMode", "fixed") == "fixed":
        expires = grant.get("expires")
        return type(expires) is int and expires > now
    confirmed = grant.get("termConfirmedAt")
    return (grant.get("expiryMode") == "until_revoked" and "expires" in grant
            and grant["expires"] is None and type(confirmed) is int and 0 < confirmed <= now
            and type(grant.get("owner")) is int and grant["owner"] > 0
            and grant.get("termConfirmedBy") == grant["owner"])


def term_label(grant):
    if grant.get("expiryMode") == "until_revoked":
        return "до отзыва"
    end = datetime.fromtimestamp(grant["expires"], timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    return f"до {end}"
