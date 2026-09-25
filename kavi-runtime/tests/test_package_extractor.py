"""Fast, deterministic unit tests for package_extractor + lifecycle merchant
fallback. No live LLM, no network, no SDK.

Covers two bugs fixed 2026-06-22:
  1. _merchant_from_sender parsed the email DOMAIN and returned the
     email-infrastructure label ("shopifyemail", "gmail") instead of the store.
  2. Shopify-style order numbers (#2121341) matched no carrier regex, so
     package_id was always None and tier-1 exact-id matching never fired.
"""

from __future__ import annotations

from kavi_runtime import lifecycle, package_extractor


# --- Bug 1: ESP / freemail sender domains must not become the merchant -------

def test_shopifyemail_sender_not_merchant():
    out = package_extractor.extract(
        subject="Your order #2121341 has shipped",
        body="...order #2121341... tracking TBA332153979959",
        sender="store+57687900323@t.shopifyemail.com",
    )
    assert out["merchant"] != "shopifyemail"
    # No display name / subject brand available to the pure extractor, so the
    # honest answer is None (the title layer falls back).
    assert out["merchant"] is None


def test_shopify_order_number_extracted_as_package_id():
    out = package_extractor.extract(
        subject="Your order #2121341 has shipped",
        body="...order #2121341... tracking TBA332153979959",
        sender="store+57687900323@t.shopifyemail.com",
    )
    assert out["package_id"] == "2121341"
    assert out["carrier"] == "shopify"


def test_freemail_gmail_sender_rejected():
    out = package_extractor.extract(
        subject="Fwd: Your order is confirmed",
        body="Forwarding this order confirmation.",
        sender="jalpa.b.thakkar@gmail.com",
    )
    assert out["merchant"] is None


def test_other_freemail_and_esp_domains_rejected():
    for sender in (
        "someone@icloud.com",
        "person@me.com",
        "buyer@yahoo.com",
        "user@outlook.com",
        "user@hotmail.com",
        "user@live.com",
        "user@aol.com",
        "user@proton.me",
        "user@protonmail.com",
        "user@googlemail.com",
        "store@shopifyemail.com",
        "store+1@em.shopifyemail.com",  # subdomain of shopifyemail.com
    ):
        out = package_extractor.extract(subject="", body="", sender=sender)
        assert out["merchant"] is None, sender


# --- Real merchants still resolve -------------------------------------------

def test_real_merchant_domain_still_resolves():
    out = package_extractor.extract(
        subject="Your order shipped",
        body="",
        sender="ship-confirm@hannaandersson.com",
    )
    assert out["merchant"] == "hannaandersson"


# --- Order-number scoping: no bare-digit false positives --------------------

def test_no_order_cue_means_no_shopify_id():
    # A bare 7-digit run with no "order #" cue must NOT be captured.
    out = package_extractor.extract(
        subject="Big sale 2121341 reasons to shop",
        body="Call 5551234 for details.",
        sender="news@store.com",
    )
    assert out["package_id"] is None


def test_order_number_word_cue_variants():
    for subject in (
        "Order number 1955038 confirmed",
        "Your order #1955038 is on the way",
        "order no. 1955038",
    ):
        out = package_extractor.extract(subject=subject, body="", sender="x@store.com")
        assert out["package_id"] == "1955038", subject


def test_carrier_id_wins_over_shopify_cue():
    # When a real Amazon order id is present, it must win; shopify is fallback only.
    out = package_extractor.extract(
        subject="Your order #2121341 shipped",
        body="Amazon order 112-1234567-7654321 tracking",
        sender="ship@amazon.com",
    )
    assert out["package_id"] == "112-1234567-7654321"
    assert out["carrier"] == "amazon"


# --- Bug 2 / 3: lifecycle title fallback never renders garbage --------------

def _payload(**kw):
    base = {
        "merchant": None,
        "from_name": "",
        "from_address": "jalpa.b.thakkar@gmail.com",
        "subject": "Your order is confirmed",
        "package_id": None,
        "carrier": None,
    }
    base.update(kw)
    return base


def test_neutral_title_no_gmail_none_shopifyemail():
    for transition in (
        lifecycle.to_ordered,
        lifecycle.to_shipped,
        lifecycle.to_out_for_delivery,
        lifecycle.to_delivered,
        lifecycle.to_cancelled,
    ):
        result = transition("", _payload(), "MJ")
        title = result["new_title"]
        assert "Gmail" not in title, title
        assert "gmail" not in title, title
        assert "None" not in title, title
        assert "Shopifyemail" not in title, title
        assert "shopifyemail" not in title, title


def test_neutral_title_uses_subject_brand():
    result = lifecycle.to_ordered("", _payload(subject="EllaOla bundle on the way"), "MJ")
    assert "EllaOla bundle on the way" in result["new_title"]


def test_real_merchant_still_titled():
    payload = _payload(merchant="ellaola")
    result = lifecycle.to_ordered("", payload, "MJ")
    assert "Ordered: Ellaola" in result["new_title"]


def test_esp_domain_not_leaked_into_title_when_merchant_none():
    # merchant None + from_address is an ESP domain -> must not become the label.
    payload = _payload(merchant=None, from_address="store+1@t.shopifyemail.com")
    result = lifecycle.to_ordered("", payload, "MJ")
    assert "shopifyemail" not in result["new_title"].lower()
