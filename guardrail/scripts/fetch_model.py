"""Downloads a classifier model at a pinned revision into the local models folder.

    python scripts/fetch_model.py protectai/deberta-v3-base-prompt-injection-v2 90c9989b1a342275dd0d1a95aad283c04e075671

The guardrail never downloads anything while it is serving: it loads models from a folder on disk
and nowhere else. This script is the one place a download happens, at build time or on a developer
machine. A revision is REQUIRED and must be a full commit hash, so what is loaded tomorrow is what
was evaluated today.

Only the files needed for inference are fetched (config, tokenizer, safetensors weights); ONNX
exports, training logs and pickled weights are skipped. A gated model needs HF_TOKEN in the
environment, from an account whose access request has been approved.

The folder it prints is what goes in the policy as `input.injection_classifier.model`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "models"
FILES = ["config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "added_tokens.json",
         "spm.model", "sentencepiece.bpe.model", "vocab.txt", "model.safetensors", "LICENSE", "USE_POLICY.md", "README.md"]


def folder_for(model: str, revision: str, base: Path = DEFAULT_DIR) -> Path:
    return base / f"{model.replace('/', '--')}@{revision[:12]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model", help="Hugging Face model id, e.g. protectai/deberta-v3-base-prompt-injection-v2")
    parser.add_argument("revision", help="full 40-character commit hash")
    parser.add_argument("--out", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("revision must be a full 40-character commit hash, not a branch or tag")

    from huggingface_hub import snapshot_download

    target = folder_for(args.model, args.revision, args.out)
    snapshot_download(args.model, revision=args.revision, local_dir=target, allow_patterns=FILES)
    if not (target / "model.safetensors").exists():
        sys.exit(f"{args.model}@{args.revision} has no model.safetensors; refusing to load pickled weights")
    labels = json.loads((target / "config.json").read_text())["id2label"]
    (target / "PINNED.json").write_text(json.dumps({"model": args.model, "revision": args.revision}, indent=1) + "\n")
    print(f"{target}\nlabels: {labels}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
