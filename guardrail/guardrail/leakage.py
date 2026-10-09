"""What must not leave in a reply or an extraction: secrets, card numbers, Aadhaar numbers.

Decided by the owner on 3 Oct 2026. Deliberately narrow:

  * No general PII detection. GSTIN, IEC, PAN, phone numbers, addresses and names are legitimate
    answers in freight, grounding already withholds what is not in the excerpts, and logs carry no
    text at all.
  * Secrets are found by FORMAT only: a prefix, a key name, a structure that nothing else has. No
    entropy-based detection, because container, bill-of-lading and booking numbers are exactly
    what a high-entropy detector flags.
  * A card number must have a real issuer prefix, be 15 or 16 digits long, pass the Luhn check,
    and not be introduced as a bank account. An Aadhaar number must be twelve digits, not start
    with 0 or 1, pass the Verhoeff check, and either be called one nearby or be written 4-4-4 and
    not be introduced as some other reference.

The last condition of each is there because of what freight mail is full of. A third of a percent
of random 16-digit numbers pass an issuer prefix and Luhn by chance, and one in ten 12-digit
numbers passes Verhoeff: bank account numbers on invoices and e-way bill numbers were being masked
as cards and Aadhaar numbers on the evaluation set before these conditions were added. The price:
an Aadhaar number written without separators and without the word near it is not caught.

`find()` returns what was found and where; `mask()` returns the text with each find replaced: a
secret by a placeholder, a card or Aadhaar number by its last four digits. Nothing here logs, and a
finding never carries the text it matched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Found:
    kind: str           # e.g. secret.private_key, secret.jwt, card_number, aadhaar_number
    start: int
    end: int
    replacement: str


# --- secrets: format only ---------------------------------------------------------------------------

_SECRETS: list[tuple[str, re.Pattern]] = [(kind, re.compile(pattern)) for kind, pattern in [
    ("secret.private_key", r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----(?:[\s\S]*?-----END (?:[A-Z]+ )?PRIVATE KEY-----)?"),
    ("secret.jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # Azure Storage: an account key inside or outside a connection string.
    ("secret.azure_storage_key", r"\bAccountKey=[A-Za-z0-9+/]{40,}={0,2}"),
    # Azure Service Bus, Event Hubs, IoT: a shared access key.
    ("secret.azure_shared_access_key", r"\bSharedAccessKey=[A-Za-z0-9+/]{20,}={0,2}"),
    # A shared access signature: the signature of a URL or token that also names a service version.
    ("secret.azure_sas_token", r"(?<![\w-])(?:sv=\d{4}-\d{2}-\d{2}[^\s\"'<>]*[&?]sig=[A-Za-z0-9%+/]{20,}={0,2}"
                               r"|sig=[A-Za-z0-9%+/]{20,}={0,2}[^\s\"'<>]*&sv=\d{4}-\d{2}-\d{2})"),
    ("secret.azure_sas_token", r"\bSharedAccessSignature sr=[^\s\"']+&sig=[A-Za-z0-9%+/]{20,}={0,2}[^\s\"']*"),
    # An Entra ID client secret: three characters, a digit, "Q~", then the rest.
    ("secret.azure_client_secret", r"(?<![\w~.-])[A-Za-z0-9_.~-]{3}\dQ~[A-Za-z0-9_.~-]{31,34}(?![\w~.-])"),
    # An Azure key given with its name: Cognitive Services, OpenAI, API Management.
    ("secret.azure_api_key", r"(?i)\b(?:api[-_]?key|ocp-apim-subscription-key|subscription[-_]?key)\b[\"']?\s*[:=]\s*[\"']?"
                             r"[0-9a-f]{32}\b"),
    ("secret.azure_devops_token", r"\b[A-Za-z0-9]{76}AZDO[A-Za-z0-9]{4}\b"),
    ("secret.aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("secret.aws_secret_key", r"(?i)\baws_secret_access_key\b[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}\b"),
    ("secret.gcp_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("secret.github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ("secret.gitlab_token", r"\bglpat-[A-Za-z0-9_-]{20,}"),
    ("secret.slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("secret.api_key", r"\bsk-(?:proj-|ant-|lf-)?[A-Za-z0-9_-]{20,}"),
    ("secret.stripe_key", r"\b[sr]k_live_[A-Za-z0-9]{20,}"),
    # A password inside a URL: scheme://user:password@host
    ("secret.url_credentials", r"\b[a-zA-Z][a-zA-Z0-9+.-]{1,20}://[^\s:/@\"'<>]{1,64}:[^\s@/\"'<>]{3,}@[^\s/\"'<>]+"),
]]

# --- card numbers: issuer prefix, length, Luhn --------------------------------------------------------

_CARD_CANDIDATE = re.compile(r"(?<![\dA-Za-z])(?:\d[ -]?){14,15}\d(?![\dA-Za-z])")
# (prefix test, length): Visa, Mastercard, American Express, RuPay, Discover, JCB, Maestro. The
# 13-, 14- and 19-digit forms are left out: rare as cards, common as account and reference numbers.
_ISSUERS = [
    (re.compile(r"4"), 16),
    (re.compile(r"5[1-5]|2(?:2[2-9][1-9]|2[3-9]\d|[3-6]\d\d|7[01]\d|720)"), 16),
    (re.compile(r"3[47]"), 15),
    (re.compile(r"60|65|81|82|508|353|356"), 16),
    (re.compile(r"6011|64[4-9]"), 16),
    (re.compile(r"35(?:2[89]|[3-8]\d)"), 16),
    (re.compile(r"5[06-9]|6[37]"), 16),
]
# What introduces a bank account number rather than a card.
_ACCOUNT_WORD = re.compile(r"(?i)\b(?:a/?c|acc(?:oun)?t|account|iban|beneficiary|bank|ifsc|swift|neft|rtgs)\b[^\n]{0,24}$")


def luhn_valid(digits: str) -> bool:
    total = 0
    for index, character in enumerate(reversed(digits)):
        digit = int(character)
        if index % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _is_card(digits: str, before: str = "") -> bool:
    if len(set(digits)) < 2:                    # 0000 0000 0000 0000 and the like
        return False
    if not (any(prefix.match(digits) and len(digits) == length for prefix, length in _ISSUERS) and luhn_valid(digits)):
        return False
    return not _ACCOUNT_WORD.search(before[-40:])


# --- Aadhaar: twelve digits, Verhoeff ------------------------------------------------------------------

_AADHAAR_CANDIDATE = re.compile(r"(?<![\dA-Za-z+])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\dA-Za-z])")
_AADHAAR_WORD = re.compile(r"(?i)aadha?ar|\buid(?:ai)?\b|आधार")
# What introduces some other twelve-digit number: an e-way bill, an air waybill, an invoice, a reference.
_REFERENCE_WORD = re.compile(r"(?i)(?:\b(?:e-?way|ewb|bill|awb|invoice|inv|ref(?:erence)?|booking|tracking|docket|lr|order|"
                             r"shipment|consignment|a/?c|account|no|number)\b|#)[^\n]{0,20}$")
_VERHOEFF_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
               [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
               [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
               [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_VERHOEFF_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
               [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
               [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_valid(digits: str) -> bool:
    check = 0
    for index, character in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[index % 8][int(character)]]
    return check == 0


def _is_aadhaar(text: str, match: re.Match) -> bool:
    digits = re.sub(r"\D", "", match.group(0))
    if not verhoeff_valid(digits) or len(set(digits)) < 2:
        return False
    # Called an Aadhaar number nearby: it is one.
    if _AADHAAR_WORD.search(text[max(0, match.start() - 60):match.end() + 40]):
        return True
    # Otherwise only in its printed form, 4-4-4, and not introduced as some other reference. One in
    # ten twelve-digit numbers passes the check by chance: e-way bills, waybills, mobile numbers
    # with their country code.
    grouped = match.group(0) != digits
    return grouped and not _REFERENCE_WORD.search(text[max(0, match.start() - 30):match.start()])


def _last_four(original: str) -> str:
    digits = re.sub(r"\D", "", original)
    return "X" * (len(digits) - 4) + digits[-4:]


# --- finding and masking ------------------------------------------------------------------------------

def find(text: str) -> list[Found]:
    """Everything found, in order, with no overlaps (the earliest, then the longest, wins)."""
    found: list[Found] = []
    for kind, pattern in _SECRETS:
        found += [Found(kind, m.start(), m.end(), "[secret removed]") for m in pattern.finditer(text)]
    for match in _CARD_CANDIDATE.finditer(text):
        if _is_card(re.sub(r"\D", "", match.group(0)), text[max(0, match.start() - 40):match.start()]):
            found.append(Found("card_number", match.start(), match.end(), _last_four(match.group(0))))
    for match in _AADHAAR_CANDIDATE.finditer(text):
        if _is_aadhaar(text, match):
            found.append(Found("aadhaar_number", match.start(), match.end(), _last_four(match.group(0))))
    kept: list[Found] = []
    for item in sorted(found, key=lambda f: (f.start, -(f.end - f.start))):
        if not kept or item.start >= kept[-1].end:
            kept.append(item)
    return kept


def mask(text: str) -> tuple[str, list[str]]:
    """(the text with every find replaced, the kinds found in order). Unchanged text and an empty
    list when there is nothing."""
    found = find(text)
    for item in reversed(found):
        text = text[:item.start] + item.replacement + text[item.end:]
    return text, [item.kind for item in found]


def kinds_in(value) -> list[str]:
    """The kinds found anywhere in a value: a string, or strings inside lists and objects."""
    if isinstance(value, str):
        return [item.kind for item in find(value)]
    if isinstance(value, dict):
        return [kind for inner in value.values() for kind in kinds_in(inner)]
    if isinstance(value, (list, tuple)):
        return [kind for inner in value for kind in kinds_in(inner)]
    return []
