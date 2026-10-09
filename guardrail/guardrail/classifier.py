"""The injection classifier: a learned detector, for what the rules cannot enumerate.

Model-agnostic on purpose. Anything that is a Hugging Face sequence classifier works: the policy
names the model folder, says which of the model's labels mean "attack", and sets the window size
and thresholds. Swapping one model for another is a policy change and a folder on disk, not a code
change.

  * The model is loaded from a FOLDER ON DISK and nowhere else. Nothing is downloaded while
    serving (see scripts/fetch_model.py for the one place a download happens).
  * Loaded once, then reused. Loading happens at startup when the check is on, so the first
    request does not pay for it.
  * Long text is scored in overlapping windows and the highest window score is the text's score.
    An instruction is a few sentences; averaging would let ten pages of invoice wash it out.
  * If the text needs more windows than the policy allows, that is an error, not a pass: the
    engine turns it into a review.

torch and transformers are imported only when a model is actually loaded, so the service runs
without them as long as the check is off.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict

from . import deadline
from .errors import NotReady
from pathlib import Path
from typing import Protocol

log = logging.getLogger("guardrail")

MODELS_DIR = Path(os.environ.get("GUARDRAIL_MODELS_DIR", Path(__file__).resolve().parents[1] / "models"))


class TooLong(RuntimeError):
    """The text needs more windows than the policy allows."""


class Scorer(Protocol):
    def score(self, text: str) -> tuple[float, tuple[int, int] | None]:
        """(probability the text contains an attack, character span of the highest-scoring window)."""


def cpu_limit() -> int | None:
    """How many CPUs this process may actually use, when that is fewer than the machine has:
    GUARDRAIL_TORCH_THREADS if set, otherwise the container's CPU quota. None: no limit found."""
    configured = os.environ.get("GUARDRAIL_TORCH_THREADS")
    if configured:
        return max(1, int(configured))
    for quota_file, period_file in ((Path("/sys/fs/cgroup/cpu.max"), None),
                                    (Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))):
        try:
            parts = quota_file.read_text().split()
            quota, period = (parts[0], parts[1]) if period_file is None else (parts[0], period_file.read_text().split()[0])
            if quota in ("max", "-1"):
                return None
            limit = max(1, -(-int(quota) // int(period)))           # rounded up
            return limit if limit < (os.cpu_count() or limit + 1) else None
        except (OSError, ValueError, IndexError):
            continue
    return None


class HuggingFaceScorer:
    """A sequence classifier from a local folder, scored over overlapping windows."""

    def __init__(self, folder: Path, positive_labels: list[str], window_tokens: int, overlap_tokens: int,
                 batch_size: int, max_windows: int) -> None:
        # The folder first: "the model is not there" is the same answer with or without the
        # classifier's libraries installed, and it is the one an operator can act on.
        if not (folder / "model.safetensors").exists():
            raise FileNotFoundError(f"no model.safetensors in {folder.name}; run scripts/fetch_model.py")

        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        from transformers.utils import logging as transformers_logging

        transformers_logging.disable_progress_bar()
        threads = cpu_limit()
        if threads:
            # torch sizes its thread pool from the machine's cores, not the container's share of
            # them. More threads than the quota allows only buys throttling.
            torch.set_num_threads(threads)
        self._torch = torch
        self._tokenizing = threading.Lock()
        self.tokenizer = AutoTokenizer.from_pretrained(folder, local_files_only=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            folder, local_files_only=True, use_safetensors=True).eval()
        labels = {str(name): int(index) for index, name in self.model.config.id2label.items()}
        unknown = [name for name in positive_labels if name not in labels]
        if unknown:
            raise ValueError(f"the model has no label(s) {unknown}; it has {sorted(labels)}")
        self.positive = [labels[name] for name in positive_labels]
        self.before, self.after = _special_tokens(self.tokenizer)
        self.pad = self.tokenizer.pad_token_id or 0     # padded positions are masked out either way
        self.body = window_tokens - len(self.before) - len(self.after)
        if self.body < 1:
            raise ValueError("window_tokens leaves no room for text")
        self.step = max(1, self.body - overlap_tokens)
        self.batch_size, self.max_windows = batch_size, max_windows
        self.score("warm-up")       # the first forward pass is slow; pay for it now

    def score(self, text: str) -> tuple[float, tuple[int, int] | None]:
        torch = self._torch
        with self._tokenizing:      # a fast tokenizer must not be used from two threads at once
            encoded = self.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
        ids, offsets = encoded["input_ids"], encoded["offset_mapping"]
        if not ids:
            return 0.0, None
        starts = window_starts(len(ids), self.body, self.step)
        if len(starts) > self.max_windows:
            raise TooLong(f"{len(starts)} windows needed, {self.max_windows} allowed")
        windows = [self.before + ids[s:s + self.body] + self.after for s in starts]
        best, best_window = 0.0, 0
        with torch.inference_mode():
            for batch_start in range(0, len(windows), self.batch_size):
                deadline.check()        # a long document is many batches; stop between them when out of time
                batch = windows[batch_start:batch_start + self.batch_size]
                width = max(len(w) for w in batch)
                input_ids = torch.full((len(batch), width), self.pad, dtype=torch.long)
                mask = torch.zeros((len(batch), width), dtype=torch.long)
                for row, window in enumerate(batch):
                    input_ids[row, :len(window)] = torch.tensor(window)
                    mask[row, :len(window)] = 1
                probabilities = torch.softmax(self.model(input_ids=input_ids, attention_mask=mask).logits, dim=-1)
                scores = probabilities[:, self.positive].sum(dim=-1)
                top = int(torch.argmax(scores))
                if float(scores[top]) > best:
                    best, best_window = float(scores[top]), batch_start + top
        start = starts[best_window]
        end = min(len(ids), start + self.body) - 1
        return min(best, 1.0), (offsets[start][0], offsets[end][1])


def window_starts(tokens: int, body: int, step: int) -> list[int]:
    """Where each window begins. Windows are `body` tokens long and `step` apart, and the last one
    always reaches the end of the text, so every token is read and any run of up to `body - step`
    tokens falls whole inside at least one window."""
    return list(range(0, max(1, tokens - body + step), step))


def _special_tokens(tokenizer) -> tuple[list[int], list[int]]:
    """What this tokenizer puts before and after a text ([CLS] ... [SEP] for most), found by asking
    it rather than assuming, so the windows we build look like the ones the model was trained on."""
    plain = tokenizer("freight", add_special_tokens=False)["input_ids"]
    full = tokenizer("freight")["input_ids"]
    for at in range(len(full) - len(plain) + 1):
        if full[at:at + len(plain)] == plain:
            return full[:at], full[at + len(plain):]
    raise ValueError("cannot tell which special tokens this tokenizer adds")


class CachedScorer:
    """Remembers the last few scores, so text seen again is not scored again. The backend sends the
    same task text as the question of every email; without this each one paid a forward pass for
    it. In memory only, bounded, and never logged."""

    def __init__(self, scorer: Scorer, size: int = 256) -> None:
        self.scorer, self.size = scorer, size
        self._recent: OrderedDict[str, tuple[float, tuple[int, int] | None]] = OrderedDict()
        self._lock = threading.Lock()

    def score(self, text: str) -> tuple[float, tuple[int, int] | None]:
        with self._lock:
            if text in self._recent:
                self._recent.move_to_end(text)
                return self._recent[text]
        result = self.scorer.score(text)
        with self._lock:
            self._recent[text] = result
            while len(self._recent) > self.size:
                self._recent.popitem(last=False)
        return result

    def __getattr__(self, name):           # tokenizer, model, max_windows ... of the scorer it wraps
        return getattr(self.scorer, name)


# --- one scorer per model, loaded once ---------------------------------------------------------------

_lock = threading.Lock()
_scorers: dict[tuple, Scorer] = {}
_errors: dict[str, str] = {}


def _key(params) -> tuple:
    return (params.model, tuple(params.positive_labels), params.window_tokens, params.overlap_tokens,
            params.batch_size, params.max_windows)


def get_scorer(params) -> Scorer:
    """The scorer for these settings, loading the model on first use. Raises if it cannot be loaded;
    what the engine makes of that depends on the check's mode (a review if enforced, `not_observed`
    in shadow), on every request until it can."""
    key = _key(params)
    with _lock:
        if key not in _scorers:
            folder = Path(params.model) if Path(params.model).is_absolute() else MODELS_DIR / params.model
            try:
                _scorers[key] = CachedScorer(HuggingFaceScorer(folder, list(params.positive_labels),
                                                               params.window_tokens, params.overlap_tokens,
                                                               params.batch_size, params.max_windows))
                _errors.pop(params.model, None)
            except Exception as exc:
                _errors[params.model] = type(exc).__name__
                raise
        return _scorers[key]


def use_scorer(params, scorer: Scorer) -> None:
    """Puts a scorer in place for these settings without loading a model. For tests."""
    with _lock:
        _scorers[_key(params)] = scorer


def forget_scorers() -> None:
    with _lock:
        _scorers.clear()
        _errors.clear()


def status(rules) -> dict[str, str]:
    """For /health: whether the classifier is off, loaded, not loaded yet, or failed to load.
    Names the model folder and the kind of error, never a path or a message."""
    config = rules.input
    params = config.injection_classifier
    mode = config.modes.get("injection_classifier", "enforce") if "injection_classifier" in config.checks else "off"
    if params is None or mode == "off":
        return {"injection_classifier": "off"}
    if params.model in _errors:
        return {"injection_classifier": f"error: {_errors[params.model]}", "model": params.model}
    loaded = _key(params) in _scorers
    return {"injection_classifier": "loaded" if loaded else "not loaded", "model": params.model, "mode": mode}


def prepare(rules) -> None:
    """Called with a policy before it is put in force: a version being activated, or a replica
    about to swap one in. Loads the classifier's model if the policy uses it.

    The owner's decision of 3 Oct 2026 (open question Q53). If the classifier is ENFORCED and its
    model fails to load, this raises `NotReady`: the activation fails and the last good policy
    stays. If it is in SHADOW, a load failure blocks nothing: the check is recorded as
    `not_observed` on each request, and /health says why."""
    config = rules.input
    if status(rules)["injection_classifier"] == "off":
        return
    mode = config.modes.get("injection_classifier", "enforce")
    try:
        get_scorer(config.injection_classifier)
    except Exception as exc:        # noqa: BLE001 -- recorded in _errors for /health
        if mode == "enforce":
            raise NotReady("injection_classifier is enforced and its model could not be loaded "
                           f"({type(exc).__name__})") from None
        log.warning("policy %s: the shadow classifier's model could not be loaded (%s); it will be recorded as "
                    "not observed", rules.version, type(exc).__name__)


def warm_up(rules) -> None:
    """Loads the model at startup when the check is on. A failure here does not stop the service,
    and /health says why. What the check then does on each request depends on its mode: enforced,
    it fails closed (review); in shadow, it is recorded as not observed and changes nothing."""
    if status(rules)["injection_classifier"] == "off":
        return
    try:
        get_scorer(rules.input.injection_classifier)
    except Exception:       # noqa: BLE001 -- recorded in _errors; the engine deals with it per request
        pass
