"""Views of a piece of text, for the detectors.

An attacker who knows a detector reads text will disguise the text: letters from another alphabet,
invisible characters, an encoding, digits for letters, a line break in the middle of a phrase. OCR
does some of the same by accident. Detectors therefore never look at one string. They look at:

    visible    what a person sees: invisible and control characters removed, nothing else changed
    plain      `visible` with runs of spaces collapsed (the text as a person reads it)
    unmasked   disguises undone: escapes decoded, look-alike letters and digits folded, accents
               dropped, spaced-out and split words joined, reversed runs put back in order
    ocr        `unmasked` with OCR misreadings of watch-list words corrected ("systern" -> "system")
    joined     `unmasked` on one line, page-break lines removed, so a phrase broken across lines
               or pages is whole again
    decoded    text recovered from base64, hex and ROT13 inside it, each decoded twice at most

ONLY `visible` IS EVER RETURNED TO A CALLER. Everything else exists to be matched against and is
thrown away. The guardrail removes what cannot be seen; it never rewrites what can.
"""
from __future__ import annotations

import base64
import binascii
import codecs
import html
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote

# Characters that render as nothing but can hide text from a person reading the email.
# Zero-width joiner and non-joiner are left alone: Indian scripts use them legitimately.
INVISIBLE = re.compile("[͏᠎​‎‏‪-‮⁠-⁤⁦-⁩ㅤ﻿ﾠ]")
# Unicode tag characters and the variation-selector supplement: invisible carriers used to smuggle
# instructions to models. Never legitimate in a business email.
SMUGGLING = re.compile("[\U000e0000-\U000e007f\U000e0100-\U000e01ef]")
SOFT_HYPHEN = "­"           # PDF extraction leaves these; removed silently
ZERO_WIDTH_SPACE = "\u200b"
# A PDF or chat exporter leaves a zero-width space where a line may break: at a line's end, beside a
# space or a punctuation mark, alone or in a pair. On the first real mail read (Q65) that was every
# one of 1,639, in four documents of fourteen. It is not a finding there. Anywhere else it is.
LONGEST_BREAK_MARK = 2
# Any run of characters that cannot be seen: the invisible ones above with the joiners they leave
# alone, tag characters, the soft hyphen and control characters. Used to see what a zero-width space
# stands next to; it does not decide what is a finding.
_UNSEEN_RUN = re.compile("[\u034f\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\u3164\ufeff\uffa0\u00ad"
                         "\U000e0000-\U000e007f\U000e0100-\U000e01ef\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]+")
# C0 and C1 control characters other than tab, newline and carriage return.
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"[ \t ]+")

# Letters from other alphabets that look Latin, small capitals, and digits used as letters.
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h", "ӏ": "l", 
    "к": "k", "м": "m", "т": "t", "ё": "e",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    "α": "a", "ο": "o", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z",
    "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P",
    "Τ": "T", "Υ": "Y", "Χ": "X",
    # Small capitals, which NFKC leaves alone.
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g",
    "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n",
    "ᴏ": "o", "ᴘ": "p", "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v",
    "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_MIXED_TOKEN = re.compile(r"\S*[A-Za-z]\S*")
_SPACED_LETTERS = re.compile(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b")
_BETWEEN_LETTERS = re.compile(r"(?<=[A-Za-z])[.\-_*~|]+(?=[A-Za-z])")
# A run of text displayed right to left by an override character: written backwards so that it
# reads forwards on screen.
_REVERSED_RUN = re.compile("‮([^‬\n]*)‬?")
_PAGE_BREAK = re.compile(r"^[ \t]*[-=_*]{2,}[ \t]*page[ \t]+\d+([ \t]+of[ \t]+\d+)?[ \t]*[-=_*]{2,}[ \t]*$",
                         re.IGNORECASE | re.MULTILINE)


def break_marks(text: str) -> int:
    """How many zero-width spaces in `text` only mark where a line may break (Q65). Such a one is
    alone or in a pair, has no other unseen character beside it, and does not sit between two
    letters or digits: "ig" + it + "nore" is a disguise, "Rotterdam." + it + a line break is not."""
    allowed = 0
    for run in _UNSEEN_RUN.finditer(text):
        marks = run.group()
        if len(marks) > LONGEST_BREAK_MARK or marks.strip(ZERO_WIDTH_SPACE):
            continue                # a long run, or something else unseen is part of it
        before = text[run.start() - 1] if run.start() else ""
        after = text[run.end()] if run.end() < len(text) else ""
        if not (before.isalnum() and after.isalnum()):
            allowed += len(marks)
    return allowed


def strip_hidden(text: str) -> str:
    return SMUGGLING.sub("", INVISIBLE.sub("", text)).replace(SOFT_HYPHEN, "")


def visible(text: str) -> str:
    """The text with what cannot be seen removed, and nothing that can be seen changed."""
    return _CONTROL.sub("", strip_hidden(text))


def plain_view(text: str) -> str:
    """The text as a person reads it: hidden characters removed, spaces collapsed."""
    return _SPACES.sub(" ", strip_hidden(text))


def _drop_accents_on_latin(text: str) -> str:
    """ïgnörë -> ignore. Marks are only dropped after a Latin letter: in Devanagari and other
    scripts the same class of character is part of the word."""
    out, previous_is_latin = [], False
    for character in unicodedata.normalize("NFD", text):
        if unicodedata.category(character) == "Mn":
            if previous_is_latin:
                continue
        else:
            previous_is_latin = "a" <= character.lower() <= "z"
        out.append(character)
    return unicodedata.normalize("NFC", "".join(out))


@lru_cache(maxsize=64)
def unmasked_view(text: str) -> str:
    """The text with common disguises undone, for matching only."""
    text = _REVERSED_RUN.sub(lambda m: m.group(1)[::-1], text)
    text = unquote(html.unescape(unquote(html.unescape(strip_hidden(text)))))      # twice: double encoding
    text = _drop_accents_on_latin(unicodedata.normalize("NFKC", text).translate(_CONFUSABLES))
    text = _SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), text)
    text = _BETWEEN_LETTERS.sub("", text)
    text = _MIXED_TOKEN.sub(lambda m: m.group(0).translate(_LEET) if re.search(r"[0-9@$]", m.group(0))
                            else m.group(0), text)
    return _SPACES.sub(" ", text)


@lru_cache(maxsize=64)
def readable_view(text: str) -> str:
    """The text with character-level disguises undone and everything else left alone: reversed runs
    put back, hidden characters removed, HTML and URL escapes decoded, look-alike letters replaced
    with the letters they imitate, s p a c e d letters joined. Unlike unmasked_view it does not
    touch digits or punctuation, so a container number or a GSTIN stays what it is. For a reader
    that takes in meaning (the classifier), which mangled numbers would only confuse."""
    text = _REVERSED_RUN.sub(lambda m: m.group(1)[::-1], text)
    text = unquote(html.unescape(unquote(html.unescape(strip_hidden(text)))))
    text = _drop_accents_on_latin(unicodedata.normalize("NFKC", text).translate(_CONFUSABLES))
    return _SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), text)


_ESCAPE = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});|(?:%[0-9A-Fa-f]{2})+")


def traced_view(text: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    """The text with its character-level disguises undone, and for each character of the result the
    span of `text` it came from, so that a caller can change the text it was given (leakage.py masks
    a number found here). Hidden and control characters are dropped; HTML and URL escapes,
    compatibility forms (full-width digits and letters) and look-alike letters become what they
    stand for, and accents on Latin letters are dropped, as in `unmasked_view`. Digits, spaces and
    punctuation are otherwise left as they are."""
    out: list[str] = []
    spans: list[tuple[int, int]] = []

    def put(characters: str, start: int, end: int) -> None:
        for character in unicodedata.normalize("NFKC", characters).translate(_CONFUSABLES):
            for part in unicodedata.normalize("NFD", character):
                if unicodedata.category(part) == "Mn" and out and "a" <= out[-1].lower() <= "z":
                    continue                    # an accent on a Latin letter
                out.append(part)
                spans.append((start, end))

    index = 0
    while index < len(text):
        escape = _ESCAPE.match(text, index)
        if escape:
            written = escape.group()
            decoded = html.unescape(written) if written.startswith("&") else unquote(written)
            if decoded != written:
                put(_CONTROL.sub("", strip_hidden(decoded)), index, escape.end())
                index = escape.end()
                continue
        character = text[index]
        if not (INVISIBLE.match(character) or SMUGGLING.match(character) or character == SOFT_HYPHEN
                or _CONTROL.match(character)):
            put(character, index, index + 1)
        index += 1
    return "".join(out), tuple(spans)


# --- OCR misreadings -------------------------------------------------------------------------------

# Words worth recovering when OCR has mangled them. Four letters or more, so that a correction
# cannot turn one short ordinary word into another.
OCR_WATCH_WORDS = (
    "ignore disregard forget override bypass skip discard dismiss abandon disable instructions instruction "
    "previous prior earlier above system prompt assistant model language developer mode rules guidelines "
    "reveal print repeat pretend unrestricted jailbreak approve refund cleared released delivered consignee "
    "notify bank account beneficiary payment remit customer customers client rates delete mark shipment "
    "consignment programming safety filters restrictions without original release cargo container invoice "
    "status paid your from classify extract output record field fields automated parser engine summary "
    "summarise summarize confidence priority review task credit details changed change address deliver"
).split()
_TOKEN = re.compile(r"[A-Za-z0-9|]{4,}")


def _skeleton(word: str) -> str:
    """One spelling for every way OCR confuses a word: rn/m, cl/d, vv/w, and i/l/1/I/| alike."""
    # Single characters first, so "c1" and "ci" have become "cl" by the time cl/d is folded.
    word = word.lower().translate(str.maketrans({"i": "l", "1": "l", "|": "l", "0": "o", "5": "s", "8": "b"}))
    return word.replace("rn", "m").replace("cl", "d").replace("vv", "w")


@lru_cache(maxsize=8)
def _watch_list(words: tuple[str, ...]) -> tuple[dict[str, str], frozenset[str]]:
    return {_skeleton(word): word for word in words}, frozenset(words)


@lru_cache(maxsize=64)
def ocr_view(text: str, watch_words: tuple[str, ...] = tuple(OCR_WATCH_WORDS)) -> str:
    """`unmasked` with OCR misreadings of watch-list words corrected. Only those words change:
    "systern prornpt" becomes "system prompt", and "modern" stays "modern" because no watch word
    was misread to produce it... unless one could have been, which is why the list is short."""
    by_skeleton, known = _watch_list(watch_words)

    def correct(match: re.Match) -> str:
        token = match.group(0)
        if token.lower() in known:
            return token
        return by_skeleton.get(_skeleton(token), token)

    return _TOKEN.sub(correct, unmasked_view(text))


def joined_view(text: str) -> str:
    """`unmasked` on a single line with page-break lines taken out, so an instruction broken
    across lines or pages can be matched whole."""
    return re.sub(r"\s+", " ", _PAGE_BREAK.sub(" ", unmasked_view(text))).strip()


# --- encoded text ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Decoded:
    kind: str      # base64 | hex | rot13
    text: str


_B64_TOKEN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_HEX_RUN = re.compile(r"(?<![0-9A-Za-z])(?:[0-9a-fA-F]{2}[ :]?){12,}(?![0-9A-Za-z])")
# Enough ordinary English to tell prose from noise.
_COMMON_WORDS = frozenset(
    "the and you your this that with from for all are not have will please any out into about before after "
    "ignore instructions system prompt previous rules above reveal print repeat forget disregard mark approve "
    "send list give show tell what were given told only reply output".split())
_WORD = re.compile(r"[A-Za-z]{2,}")


def _readable(decoded: str) -> bool:
    printable = sum(ch.isprintable() or ch in "\n\t" for ch in decoded)
    return len(decoded) >= 8 and " " in decoded and printable / len(decoded) >= 0.9


def _common_words(text: str) -> int:
    return sum(word.lower() in _COMMON_WORDS for word in _WORD.findall(text))


def _decode_once(text: str) -> list[Decoded]:
    out = []
    # base64, including blocks wrapped over several lines
    joined = re.sub(r"(?<=[A-Za-z0-9+/])[ \t]*\r?\n[ \t]*(?=[A-Za-z0-9+/])", "", text)
    for token in _B64_TOKEN.findall(joined):
        try:
            decoded = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            continue
        if _readable(decoded):
            out.append(Decoded("base64", decoded))
    # hex
    for run in _HEX_RUN.findall(text):
        digits = re.sub(r"[ :]", "", run)
        if len(digits) % 2:
            continue
        try:
            decoded = bytes.fromhex(digits).decode("utf-8")
        except ValueError:
            continue
        if _readable(decoded):
            out.append(Decoded("hex", decoded))
    # ROT13: a line that is noise as written and English once rotated
    for line in text.splitlines():
        if len(line) < 12:
            continue
        rotated = codecs.encode(line, "rot13")
        if _common_words(rotated) >= 3 and _common_words(rotated) >= 2 * max(1, _common_words(line)):
            out.append(Decoded("rot13", rotated))
    return out


@lru_cache(maxsize=64)
def decoded_views(text: str) -> tuple[Decoded, ...]:
    """Text hidden inside `text` by an encoding, decoded. One level of nesting is followed."""
    first = _decode_once(text)
    nested = [Decoded(f"{outer.kind}+{inner.kind}", inner.text) for outer in first for inner in _decode_once(outer.text)]
    return tuple(first + nested)


def decoded_segments(text: str) -> list[str]:
    """Base64 blocks that decode to readable text. Kept for callers that only want those."""
    return [d.text for d in decoded_views(text) if d.kind == "base64"]


def encoded_runs(text: str) -> list[tuple[int, int, str]]:
    """(start, end, decoded) for each base64 token and hex run in `text` that decodes to readable
    text. Unlike decoded_views it says where each one is, so that a caller can act on the text it
    came from (leakage.py). A base64 block wrapped over several lines is read line by line here."""
    runs = []
    for token in _B64_TOKEN.finditer(text):
        try:
            decoded = base64.b64decode(token.group() + "=" * (-len(token.group()) % 4), validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            continue
        if _readable(decoded):
            runs.append((token.start(), token.end(), decoded))
    for run in _HEX_RUN.finditer(text):
        digits = re.sub(r"[ :]", "", run.group())
        try:
            decoded = bytes.fromhex(digits).decode("utf-8") if len(digits) % 2 == 0 else ""
        except ValueError:
            continue
        if _readable(decoded):
            runs.append((run.start(), run.end(), decoded))
    return runs


_BASE64_RUN = re.compile(r"(?:[A-Za-z0-9+/]{40,}={0,2}\s*)+")
_LONG_HEX_RUN = re.compile(r"(?<![0-9A-Za-z])(?:[0-9a-fA-F]{2}[ :]?){40,}(?![0-9A-Za-z])")


def longest_encoded_block(text: str) -> int:
    """Length of the longest base64- or hex-looking block, ignoring whitespace."""
    runs = _BASE64_RUN.findall(text) + _LONG_HEX_RUN.findall(text)
    return max((len(re.sub(r"[\s:]", "", run)) for run in runs), default=0)
