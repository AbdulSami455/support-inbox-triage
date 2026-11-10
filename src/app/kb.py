"""A small help-center knowledge base and a keyword retriever (no AI cost)."""

import re

ARTICLES = [
    {
        "id": "KB-101",
        "title": "Resetting your password",
        "keywords": ["password", "reset", "login", "log in", "sign in", "locked", "2fa", "two-factor"],
        "body": "Use 'Forgot password' on the sign-in page; the reset link is valid for 1 hour. "
        "If the account is locked after 5 failed attempts, it unlocks automatically after 30 minutes. "
        "Support can resend the link but will never ask for your password.",
    },
    {
        "id": "KB-102",
        "title": "Understanding your invoice and charges",
        "keywords": ["invoice", "charge", "charged", "billing", "bill", "payment", "receipt", "twice", "double"],
        "body": "Invoices are under Settings > Billing. A duplicate charge is usually a temporary card "
        "authorization that drops off in 3-5 business days. If it remains, the billing team reviews it; "
        "refunds are decided by billing, not by support agents.",
    },
    {
        "id": "KB-103",
        "title": "Cancelling or changing your plan",
        "keywords": ["cancel", "cancellation", "downgrade", "upgrade", "plan", "subscription", "renew"],
        "body": "Plans can be changed or cancelled under Settings > Billing > Plan. Changes apply at the end "
        "of the current billing period. Data is kept for 30 days after cancellation.",
    },
    {
        "id": "KB-104",
        "title": "Tracking your order and delivery times",
        "keywords": ["order", "shipping", "shipped", "delivery", "deliver", "tracking", "package", "arrive", "late"],
        "body": "A tracking link is emailed when the order ships. Standard delivery takes 3-7 business days. "
        "If tracking has not updated for 5 business days, support opens a carrier investigation.",
    },
    {
        "id": "KB-105",
        "title": "Returns and damaged items",
        "keywords": ["return", "refund", "damaged", "broken", "wrong item", "exchange", "defective"],
        "body": "Items can be returned within 30 days. For damaged or wrong items, send the order number and "
        "a photo; support will arrange a replacement or pass the case to billing for a refund decision.",
    },
    {
        "id": "KB-106",
        "title": "App errors and troubleshooting",
        "keywords": ["error", "bug", "crash", "crashes", "not working", "broken", "slow", "500", "loading", "app"],
        "body": "Try signing out and back in, updating to the latest app version and clearing the cache. "
        "If it continues, send the device/browser, app version, steps to reproduce and a screenshot.",
    },
    {
        "id": "KB-107",
        "title": "Updating account details and email",
        "keywords": ["email address", "change email", "account", "profile", "name", "delete account", "data"],
        "body": "Name and email can be changed under Settings > Profile; a new email must be confirmed. "
        "Account deletion requests are processed within 7 days after identity confirmation.",
    },
    {
        "id": "KB-108",
        "title": "Requesting a feature",
        "keywords": ["feature", "request", "suggestion", "would be nice", "add support", "roadmap", "integration"],
        "body": "Feature requests are logged for the product team and reviewed monthly. "
        "Support cannot commit to dates but will share the request with product.",
    },
]

_BY_ID = {a["id"]: a for a in ARTICLES}


def get_article(article_id: str | None) -> dict | None:
    return _BY_ID.get(article_id) if article_id else None


def search(text: str, limit: int = 3) -> list[dict]:
    """Rank articles by how many of their keywords appear in the text."""
    text = text.lower()
    scored = []
    for art in ARTICLES:
        score = sum(1 for kw in art["keywords"] if re.search(r"\b" + re.escape(kw) + r"\b", text))
        if score:
            scored.append((score, art))
    scored.sort(key=lambda s: -s[0])
    return [art for _, art in scored[:limit]]
