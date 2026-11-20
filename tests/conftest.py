"""Shared fixtures and diplomatic synthetic page builders for Paper Lanterns v2 tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from lanterns.core.models import (
    BlockAlignment,
    BlockIR,
    BlockType,
    EpistolaryTransition,
    PageExtractWire,
    PageIR,
    StyleSpan,
    TableCell,
    WireBlock,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


def make_block(
    block_id: str,
    page_number: int,
    block_type: BlockType,
    tagged_text: str,
    *,
    plain_text: str | None = None,
    spans: list[StyleSpan] | None = None,
    reading_order: int = 0,
    printed_folio: str | None = None,
    bbox: list[int] | None = None,
    alignment: BlockAlignment = "left",
    epistolary_transition: EpistolaryTransition = "none",
    line_breaks_preserved: bool = False,
    heading_level: int | None = None,
    note_label: str | None = None,
    continues_from_prev: bool = False,
    continues_to_next: bool = False,
    table_role: str | None = None,
    cells: list[TableCell] | None = None,
) -> BlockIR:
    import re

    derived_plain = plain_text if plain_text is not None else re.sub(r"</?(?:i|b|sc|sup|sub|unclear)>", "", tagged_text)
    return BlockIR(
        block_id=block_id,
        page_number=page_number,
        reading_order=reading_order,
        printed_folio=printed_folio,
        type=block_type,
        bbox=bbox or [100, 100 + reading_order * 40, 900, 130 + reading_order * 40],
        tagged_text=tagged_text,
        plain_text=derived_plain,
        spans=spans or [],
        alignment=alignment,
        epistolary_transition=epistolary_transition,
        line_breaks_preserved=line_breaks_preserved,
        heading_level=heading_level,
        note_label=note_label,
        continues_from_prev=continues_from_prev,
        continues_to_next=continues_to_next,
        table_role=table_role,
        cells=cells,
        provenance_provider="mock",
        provenance_model="mock-v1",
        provenance_harness="sdk",
        source_page_sha256=f"sha256-page-{page_number:04d}",
    )


def make_page(
    page_number: int,
    blocks: list[BlockIR],
    *,
    printed_folio: str | None = None,
    failed_closed: bool = False,
    failure_reason: str | None = None,
    warning_codes: list[str] | None = None,
    provider: str = "mock",
    model: str = "mock-v1",
    harness: str = "sdk",
    cost_usd: float = 0.0,
) -> PageIR:
    resolved_folio = printed_folio if printed_folio is not None else (None if failed_closed else str(page_number))
    ordered: list[BlockIR] = []
    for idx, blk in enumerate(blocks):
        ordered.append(
            blk.model_copy(
                update={
                    "reading_order": idx,
                    "page_number": page_number,
                    "printed_folio": blk.printed_folio if blk.printed_folio is not None else resolved_folio,
                }
            )
        )
    return PageIR(
        page_number=page_number,
        printed_folio=resolved_folio,
        warning_codes=warning_codes or [],
        failed_closed=failed_closed,
        failure_reason=failure_reason,
        source_page_sha256=f"sha256-page-{page_number:04d}",
        blocks=ordered,
        provider=provider,
        model=model,
        harness=harness,
        cost_usd=cost_usd,
    )


def make_wire_page(blocks: list[WireBlock], printed_folio: str | None = "1") -> PageExtractWire:
    return PageExtractWire(printed_folio=printed_folio, blocks=blocks)
