"""Golden set for the chunking bake-off: documents of four shapes and 34 question/answer pairs written for them.
Each question names the document that holds the answer, a verbatim `answer` sentence/row, and `terms` that an LLM
would need to see in its context to answer correctly."""

from __future__ import annotations

from dataclasses import dataclass

RETURNS = """# Acme Retail Returns and Refunds Policy

## Overview

This policy explains how customers can send purchases back to Acme Retail and how the money is returned. It applies to all orders placed on the Acme website and in Acme stores. Please read the sections below before starting a return.

## Eligibility

Items can be returned if they are unused, undamaged and still in the original packaging with all tags attached. Proof of purchase is required for every return. Gift cards, downloadable software and perishable goods cannot be returned.

## Return windows

Most items can be returned within 30 days of delivery. Electronics have a shorter window of 15 days from delivery. Clothing and shoes can be returned within 60 days during the winter holiday season.

## How to start a return

Sign in to your account, open the order page and choose Return item. Pick the reason for the return and print the prepaid shipping label. Drop the parcel at any carrier location. Customers without a printer can request a QR code label instead.

## Refund timing

Refunds are issued to the original payment method five business days after the returned parcel reaches our warehouse. Bank transfers can take up to ten additional business days to appear on a statement. Store credit refunds are instant.

## Damaged or wrong items

If an item arrives damaged or is not what you ordered, report it within 7 days of delivery with a photo. We will send a replacement at no cost or refund the full price including shipping. You do not need to return a damaged item worth less than 20 dollars.

## Gift returns

Gift recipients can return an item without the original buyer's details by using the gift receipt number. The refund for a gift return is issued as store credit, never as cash.
"""

SHIPPING = """# Acme Shipping Policy

## Domestic delivery

Domestic orders ship within 1 business day and arrive in 3 to 5 business days. Standard shipping is free for orders over 50 dollars. Orders below that amount pay a flat shipping fee of 6 dollars.

## International delivery

International orders ship within 3 business days and arrive in 7 to 14 business days depending on the destination country. Customers are responsible for import duties and taxes charged by their local customs office. We do not ship to post office boxes abroad.

## Express shipping

Express shipping delivers in 1 to 2 business days and costs 18 dollars. Orders placed before 2 pm local warehouse time leave the same day. Express shipping is not available for oversized items such as furniture.

## Tracking your order

A tracking number is emailed as soon as the parcel leaves the warehouse. You can follow the parcel from the order page. Tracking updates may lag by up to 12 hours after the carrier scans the parcel.

## Delayed or lost orders

If a domestic order has not arrived 10 business days after shipping, contact support and we will open a carrier investigation. Lost parcels are reshipped or refunded in full once the investigation closes, normally within 5 business days.

## Changing your address

The delivery address can be changed free of charge until the order is packed, which usually happens within 4 hours of placing it. After that we cannot redirect the parcel and the customer must arrange redelivery with the carrier.

## Holiday cutoffs

For delivery before the winter holidays, order by 15 December for domestic shipping and by 1 December for international shipping. Orders placed after the cutoff ship on the first business day of January.
"""

BILLING = """# Acme Account and Billing Guide

## Subscription plans

Acme offers three subscription plans. The Starter plan costs 29 dollars a month and includes 5 seats. The Team plan costs 99 dollars a month and includes 25 seats. The Enterprise plan has custom pricing and unlimited seats.

## Billing cycle

Subscriptions renew on the same calendar day each month. If the renewal day does not exist in a month, billing happens on the last day of that month. Annual billing gives a 15 percent discount.

## Payment methods

We accept Visa, Mastercard and American Express cards as well as direct debit in supported countries. Invoices for the Enterprise plan can be paid by bank transfer within 30 days.

## Failed payments

When a renewal payment fails we retry three times over seven days. The account stays active during the retries. If the final retry fails the workspace is switched to read-only mode until a valid payment method is added.

## Cancelling a subscription

Open Billing and choose Cancel plan. Access continues until the end of the paid period and no further charges are made. Cancelling does not delete your data. Partial months are not refunded.

## Invoices and receipts

Invoices are emailed on the renewal date and can be downloaded as PDF files from the Billing page. Admins can add a company name and tax number that appear on every future invoice.

## Security and two factor login

Two factor authentication can be turned on for each user under Security settings. Admins can require it for the whole workspace. Recovery codes are shown once when two factor login is enabled, so store them somewhere safe.

## Closing your account

Closing an account permanently deletes all workspace data after a 30 day grace period. During the grace period an admin can restore the workspace by signing in again. Data exports must be requested before closing.
"""

FAQ = """# Acme Support FAQ

Q: How can I reset my password?
A: Choose Forgot password on the sign in page and enter your email. A reset link valid for 60 minutes is sent to you.

Q: Does Acme have a mobile app?
A: Yes. The Acme mobile app is available for iOS and Android and supports order tracking and push notifications.

Q: Can I change the email address on my account?
A: Yes. Open Profile settings and edit the email field. You must confirm the new address with a code before it is used.

Q: Which languages does support speak?
A: Support is available in English, Hindi, Spanish and German from 8 am to 8 pm on weekdays.

Q: Is there a student discount?
A: Students get 20 percent off the Starter plan after verifying a school email address.

Q: How do I contact support outside business hours?
A: Send an email to the support address and we will reply on the next business day. Urgent outages are monitored around the clock.

Q: Can I use Acme on more than one device?
A: Yes. There is no device limit and you stay signed in on every device until you sign out.
"""

DELIVERY_ROWS = [
    ("city", "delivery_days", "delivery_fee", "cod_available"),
    ("Mumbai", "2", "40", "yes"), ("Delhi", "2", "40", "yes"), ("Bengaluru", "3", "45", "yes"),
    ("Chennai", "3", "45", "yes"), ("Kolkata", "4", "55", "yes"), ("Hyderabad", "3", "45", "yes"),
    ("Pune", "3", "50", "yes"), ("Jaipur", "4", "60", "no"), ("Lucknow", "5", "65", "no"),
    ("Guwahati", "6", "90", "no"), ("Kochi", "4", "55", "yes"), ("Indore", "4", "60", "no"),
    ("Nagpur", "5", "65", "no"), ("Patna", "6", "80", "no"),
]
DELIVERY_CSV = "\n".join(",".join(r) for r in DELIVERY_ROWS)

HISTORY = """Acme Retail began in 2009 as a single shop in Pune selling refurbished phones to university students. The founders, Meera Nair and Karan Shah, financed the first inventory with a small family loan and repaid it within eighteen months.

In 2013 Acme opened its online store and moved the warehouse to a larger building on the outskirts of Pune. The website was built by a team of four engineers who later became the core of the company's technology group.

By 2017 the company employed more than 400 people and had opened stores in eleven cities. It introduced the mobile app in the same year, which quickly accounted for forty percent of all orders.

In 2021 Acme committed to carbon neutral deliveries. All delivery vans in Mumbai and Delhi were replaced with electric vehicles, and the packaging was redesigned to use recycled cardboard only.

Today Acme serves customers in twenty countries and runs three fulfilment centres. Its largest centre, in Hyderabad, processes about 30 thousand parcels each day and operates around the clock.
"""

# Shapes that broke or degraded the first chunker: text before the first heading, headings with no body, no headings.
WARRANTY = """This handbook describes how Acme Retail handles warranty claims for hardware sold in its stores and online. It is maintained by the service team and applies to every region unless a local addendum says otherwise.

# Coverage

Acme hardware carries a warranty of 24 months from the delivery date. Batteries are covered for 12 months. Accidental damage and liquid damage are not covered by the standard warranty.

# Making a claim

Start a claim from the order page and upload a photo of the serial number. A service agent replies within 2 business days with a prepaid courier label.

## Repairs

Repairs take 10 business days from the day the device reaches the service centre. A replacement is shipped instead when the repair is not possible.
"""

OUTLINE = "# Acme Employee Handbook\n\n## Onboarding\n\n## Payroll\n\n## Leave policy\n\n## Expenses\n\n## Security training\n"

NOTES = """Platform team meeting notes. The team agreed to freeze deployments on 20 December until the new year. The on-call rotation for the holidays is Meera, then Karan, then Isha. The database migration moves to the first week of February because the vendor delayed the replica upgrade.
"""

DOCS = {
    "warranty": ("policy", WARRANTY), "outline": ("outline", OUTLINE), "notes": ("prose", NOTES),
    "returns": ("policy", RETURNS), "shipping": ("policy", SHIPPING), "billing": ("policy", BILLING),
    "faq": ("faq", FAQ), "delivery_rates": ("table", DELIVERY_CSV), "history": ("prose", HISTORY),
}


@dataclass(frozen=True)
class Question:
    doc: str
    question: str
    answer: str            # verbatim span of the document that answers it
    terms: tuple[str, ...]  # facts the LLM's context must contain


Q = Question
QUESTIONS: list[Question] = [
    # returns (policy)
    Q("returns", "How many days do I have to return electronics?", "Electronics have a shorter window of 15 days from delivery.", ("15 days",)),
    Q("returns", "Can I send back a gift card?", "Gift cards, downloadable software and perishable goods cannot be returned.", ("Gift cards", "cannot be returned")),
    Q("returns", "How long until the refund reaches my original payment method?", "Refunds are issued to the original payment method five business days after the returned parcel reaches our warehouse.", ("five business days",)),
    Q("returns", "What do I do if my item arrives damaged?", "report it within 7 days of delivery with a photo", ("7 days", "photo")),
    Q("returns", "Do I need a printer to print the return label?", "Customers without a printer can request a QR code label instead.", ("QR code",)),
    Q("returns", "How is a gift return refunded?", "The refund for a gift return is issued as store credit, never as cash.", ("store credit",)),
    Q("returns", "What is the clothing return window during the holidays?", "Clothing and shoes can be returned within 60 days during the winter holiday season.", ("60 days",)),
    Q("returns", "Do I have to return a cheap damaged item?", "You do not need to return a damaged item worth less than 20 dollars.", ("20 dollars",)),
    # shipping (policy)
    Q("shipping", "What does express shipping cost?", "Express shipping delivers in 1 to 2 business days and costs 18 dollars.", ("18 dollars",)),
    Q("shipping", "Who pays import duties on international orders?", "Customers are responsible for import duties and taxes charged by their local customs office.", ("import duties", "customers")),
    Q("shipping", "When is the cutoff to order for domestic delivery before the winter holidays?", "order by 15 December for domestic shipping", ("15 December",)),
    Q("shipping", "How soon can I change my delivery address?", "The delivery address can be changed free of charge until the order is packed, which usually happens within 4 hours of placing it.", ("4 hours",)),
    Q("shipping", "Is shipping free?", "Standard shipping is free for orders over 50 dollars.", ("50 dollars",)),
    Q("shipping", "My domestic parcel has not arrived, when should I contact support?", "If a domestic order has not arrived 10 business days after shipping, contact support", ("10 business days",)),
    Q("shipping", "Can furniture be sent with express delivery?", "Express shipping is not available for oversized items such as furniture.", ("not available", "furniture")),
    # billing (policy)
    Q("billing", "How many seats does the Team plan include?", "The Team plan costs 99 dollars a month and includes 25 seats.", ("25 seats",)),
    Q("billing", "What happens if my renewal payment keeps failing?", "If the final retry fails the workspace is switched to read-only mode until a valid payment method is added.", ("read-only",)),
    Q("billing", "Will I be refunded for part of a month if I cancel?", "Partial months are not refunded.", ("not refunded",)),
    Q("billing", "How long can I restore a closed account?", "During the grace period an admin can restore the workspace by signing in again.", ("grace period", "30 day")),
    Q("billing", "Is there a discount for paying yearly?", "Annual billing gives a 15 percent discount.", ("15 percent",)),
    Q("billing", "Can two factor login be required for everyone?", "Admins can require it for the whole workspace.", ("require it for the whole workspace",)),
    # faq
    Q("faq", "How long is the password reset link valid?", "A reset link valid for 60 minutes is sent to you.", ("60 minutes",)),
    Q("faq", "Is the Acme app available on Android?", "The Acme mobile app is available for iOS and Android", ("iOS and Android",)),
    Q("faq", "Do students get a discount?", "Students get 20 percent off the Starter plan after verifying a school email address.", ("20 percent", "Starter")),
    Q("faq", "Which languages can support help me in?", "Support is available in English, Hindi, Spanish and German from 8 am to 8 pm on weekdays.", ("Hindi", "8 am")),
    Q("faq", "Is there a limit on devices?", "There is no device limit and you stay signed in on every device until you sign out.", ("no device limit",)),
    # delivery rates (table)
    Q("delivery_rates", "What is the delivery fee to Guwahati?", "Guwahati,6,90,no", ("Guwahati", "90")),
    Q("delivery_rates", "Is cash on delivery available in Jaipur?", "Jaipur,4,60,no", ("Jaipur", "no")),
    Q("delivery_rates", "How many days does delivery to Kochi take?", "Kochi,4,55,yes", ("Kochi", "4")),
    Q("delivery_rates", "What does shipping to Patna cost?", "Patna,6,80,no", ("Patna", "80")),
    # intro-before-heading, headings-only and heading-less shapes
    Q("warranty", "How long is the hardware warranty?", "Acme hardware carries a warranty of 24 months from the delivery date.", ("24 months",)),
    Q("warranty", "How long do repairs take?", "Repairs take 10 business days from the day the device reaches the service centre.", ("10 business days",)),
    Q("warranty", "Who maintains the warranty handbook?", "It is maintained by the service team", ("service team",)),
    Q("outline", "Which handbook section covers leave?", "Leave policy", ("Leave policy",)),
    Q("notes", "When is the deployment freeze?", "freeze deployments on 20 December", ("20 December",)),
    # history (prose)
    Q("history", "Who founded Acme Retail?", "The founders, Meera Nair and Karan Shah", ("Meera Nair", "Karan Shah")),
    Q("history", "Where is the largest fulfilment centre?", "Its largest centre, in Hyderabad, processes about 30 thousand parcels each day", ("Hyderabad",)),
    Q("history", "When did the company go carbon neutral for deliveries?", "In 2021 Acme committed to carbon neutral deliveries.", ("2021",)),
    Q("history", "What share of orders came from the mobile app?", "which quickly accounted for forty percent of all orders", ("forty percent",)),
]


# ── distractors ─────────────────────────────────────────────────────────
# With only six documents, recall@5 is trivial (a handful of chunks in total). Seeded, template-generated documents of
# the same four shapes put realistic competition in the index. They share vocabulary with the real documents (orders,
# refunds, plans, days, dollars) but never contain a question's answer, and the same seed always gives the same text.

_TOPICS = ["Warranty", "Loyalty rewards", "Privacy", "Recycling", "Corporate orders", "Gift cards", "Price matching",
           "Installation", "Subscriptions", "Marketplace sellers", "Accessibility", "Repairs"]
_SECTIONS = ["Overview", "Eligibility", "Time limits", "How to apply", "Fees and charges", "Exceptions", "Contacting us",
             "Cancellations", "Processing times", "Required documents", "Regional rules", "Limits"]
_TEMPLATES = [
    "Requests for {t} must be made within {n} days of the order date and need the order number.",
    "A fee of {m} dollars applies to {t} requests that are submitted after the deadline.",
    "Customers in {c} receive an answer within {n} business days of submitting the form.",
    "The {t} team reviews every order and may ask for photos, receipts or a signed statement.",
    "Plans, orders and refunds linked to {t} are handled by a separate support queue.",
    "Items bought in {c} follow local rules and may ship from a different warehouse.",
    "If a {t} request is refused you can appeal once, within {n} days, using the appeal form.",
    "Approved {t} requests are confirmed by email and shown on the account page.",
]
_PLACES = ["Canada", "Brazil", "Japan", "Kenya", "Norway", "Chile", "Vietnam", "Poland"]


def _distractors(seed: int = 11) -> dict[str, tuple[str, str]]:
    import random
    rng = random.Random(seed)
    out: dict[str, tuple[str, str]] = {}
    for i in range(24):
        topic = _TOPICS[i % len(_TOPICS)]
        parts = [f"# Contoso {topic} Policy {i}"]
        for section in rng.sample(_SECTIONS, 6):
            body = " ".join(
                rng.choice(_TEMPLATES).format(t=topic.lower(), n=rng.randint(2, 45), m=rng.randint(3, 40), c=rng.choice(_PLACES))
                for _ in range(rng.randint(2, 5))
            )
            parts.append(f"## {section}\n\n{body}")
        out[f"x_policy_{i}"] = ("policy", "\n\n".join(parts) + "\n")
    for i in range(6):
        pairs = [
            f"Q: How do I change the {rng.choice(_TOPICS).lower()} settings in {rng.choice(_PLACES)}?\n"
            f"A: Open the account page, choose {rng.choice(_SECTIONS).lower()} and follow the steps within {rng.randint(2, 30)} days."
            for _ in range(7)
        ]
        out[f"x_faq_{i}"] = ("faq", "# Contoso FAQ\n\n" + "\n\n".join(pairs) + "\n")
    for i in range(6):
        paras = [" ".join(rng.choice(_TEMPLATES).format(t=rng.choice(_TOPICS).lower(), n=rng.randint(2, 45), m=rng.randint(3, 40),
                                                       c=rng.choice(_PLACES)) for _ in range(4)) for _ in range(5)]
        out[f"x_prose_{i}"] = ("prose", "\n\n".join(paras) + "\n")
    for i in range(3):
        rows = ["city,delivery_days,delivery_fee,cod_available"] + [
            f"{rng.choice(_PLACES)}{j},{rng.randint(2, 9)},{rng.randint(30, 120)},{rng.choice(['yes', 'no'])}" for j in range(14)
        ]
        out[f"x_table_{i}"] = ("table", "\n".join(rows))
    return out


ALL_DOCS = {**DOCS, **_distractors()}
