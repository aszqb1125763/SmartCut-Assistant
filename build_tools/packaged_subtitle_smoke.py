# -*- coding: utf-8 -*-
"""Minimal packaged-runtime smoke check for the subtitle delivery modules."""
from __future__ import annotations

import json
import os
import sys

import index
import pr_subtitles
import pr_workflow
import subtitle_quality
import subtitles


def main(marker: str) -> int:
    chunks = subtitles.semantic_caption_chunks(
        "这一句没有标点但仍然需要按语义和可读长度拆分")
    if not chunks or max(len(value.replace(" ", "")) for value in chunks) > 15:
        raise RuntimeError("semantic caption smoke check failed")
    if not callable(index.repair_low_confidence_before_delivery):
        raise RuntimeError("low-confidence re-listen is unavailable")
    if not callable(pr_workflow.write_editable_subtitle_tracks):
        raise RuntimeError("Premiere subtitle workflow is unavailable")
    payload = {
        "chunks": chunks,
        "max_chars": max(len(value.replace(" ", "")) for value in chunks),
        "modules": [
            pr_subtitles.__name__, pr_workflow.__name__,
            subtitle_quality.__name__, subtitles.__name__,
        ],
    }
    with open(os.path.abspath(marker), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: packaged_subtitle_smoke.py MARKER")
    raise SystemExit(main(sys.argv[1]))
