"""Shared Pydantic v2 data contracts across Paper Lanterns v2 stages."""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

BlockType = Literal[
    "running_header",
    "folio",
    "heading",
    "letter_header",
    "dateline",
    "salutation",
    "body",
    "blockquote",
    "verse",
    "valediction",
    "signature",
    "postscript",
    "address_block",
    "editorial",
    "ornamental_break",
    "list_item",
    "toc_entry",
    "index_entry",
    "table_caption",
    "table_row",
    "footnote",
    "margin_note",
    "illustration",
    "facsimile_plate",
    "caption",
    "epigraph",
    "imprint",
    "scanner_residue",
    "noise",
]

BlockAlignment = Literal[
    "left",
    "center",
    "right",
    "indented",
    "cascading",
    "inline",
    "two_column_left",
    "two_column_right",
]

EpistolaryTransition = Literal[
    "none",
    "starts_new_letter",
    "starts_enclosure",
    "ends_enclosure",
]

StyleTag = Literal["i", "b", "sc", "sup", "sub", "unclear", "fnref"]
DatelinePosition = Literal["top", "bottom", "inline", "absent"]
BoundaryConfidence = Literal["high", "adjudicated", "human_mapped", "ambiguous"]
SectionKind = Literal["frontmatter", "chapter", "epistolary_part", "appendix", "index", "backmatter"]
FindingSeverity = Literal["blocker", "warning", "info"]
ProvenanceTier = Literal["blocked", "community", "machine_checked", "source_certified"]

BLOCK_TYPES: tuple[str, ...] = get_args(BlockType)
BLOCK_ALIGNMENTS: tuple[str, ...] = get_args(BlockAlignment)
EPISTOLARY_TRANSITIONS: tuple[str, ...] = get_args(EpistolaryTransition)
STYLE_TAGS: tuple[str, ...] = get_args(StyleTag)
FURNITURE_BLOCK_TYPES: frozenset[str] = frozenset({"running_header", "folio", "scanner_residue", "noise"})


class TableCell(BaseModel):
    text: str
    colspan: int = 1
    rowspan: int = 1


class WireBlock(BaseModel):
    type: BlockType
    bbox: list[int]
    text: str
    alignment: BlockAlignment = "left"
    epistolary_transition: EpistolaryTransition = "none"
    line_breaks_preserved: bool = False
    heading_level: int | None = None
    note_label: str | None = None
    continues_from_prev: bool = False
    continues_to_next: bool = False
    table_role: str | None = None
    cells: list[TableCell] | None = None


class PageExtractWire(BaseModel):
    printed_folio: str | None = None
    page_orientation: str = "upright"
    warning_codes: list[str] = Field(default_factory=list)
    blocks: list[WireBlock] = Field(default_factory=list)


class StyleSpan(BaseModel):
    tag: StyleTag
    start: int
    end: int


class BlockIR(BaseModel):
    block_id: str
    page_number: int
    reading_order: int = 0
    printed_folio: str | None = None
    type: BlockType
    bbox: list[int] = Field(default_factory=lambda: [0, 0, 1000, 1000])
    tagged_text: str
    plain_text: str
    spans: list[StyleSpan] = Field(default_factory=list)
    alignment: BlockAlignment = "left"
    epistolary_transition: EpistolaryTransition = "none"
    line_breaks_preserved: bool = False
    heading_level: int | None = None
    note_label: str | None = None
    continues_from_prev: bool = False
    continues_to_next: bool = False
    table_role: str | None = None
    cells: list[TableCell] | None = None
    provenance_provider: str = ""
    provenance_model: str = ""
    provenance_harness: str = "sdk"
    source_page_sha256: str = ""


class PageIR(BaseModel):
    page_number: int
    printed_folio: str | None = None
    page_orientation: str = "upright"
    warning_codes: list[str] = Field(default_factory=list)
    failed_closed: bool = False
    failure_reason: str | None = None
    source_page_sha256: str = ""
    blocks: list[BlockIR] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    harness: str = "sdk"
    rescued: bool = False
    cost_usd: float = 0.0


class FootnoteItem(BaseModel):
    footnote_id: str
    label: str
    origin_page: int
    origin_folio: str | None = None
    block_ids: list[str] = Field(default_factory=list)
    tagged_text: str
    plain_text: str
    referenced_in_block_ids: list[str] = Field(default_factory=list)
    referenced_in_letter_ids: list[str] = Field(default_factory=list)
    section_id: str | None = None
    is_orphan: bool = False


class EpistolaryMetadata(BaseModel):
    letter_number_label: str | None = None
    header_tagged: str | None = None
    sender_display: str | None = None
    recipient_display: str | None = None
    dateline_tagged: str | None = None
    dateline_plain: str | None = None
    dateline_position: Literal["top", "bottom", "inline", "absent"] = "absent"
    salutation_tagged: str | None = None
    valediction_tagged: str | None = None
    signature_tagged: str | None = None
    address_block_tagged: str | None = None
    has_postscript: bool = False
    has_enclosure: bool = False
    languages: list[str] = Field(default_factory=list)
    incipit: str = ""


class LetterUnit(BaseModel):
    letter_id: str
    slug: str
    sequence_index: int
    section_id: str
    start_page: int
    end_page: int
    start_folio: str | None = None
    end_folio: str | None = None
    block_ids: list[str] = Field(default_factory=list)
    blocks: list[BlockIR] = Field(default_factory=list)
    metadata: EpistolaryMetadata = Field(default_factory=EpistolaryMetadata)
    footnote_ids: list[str] = Field(default_factory=list)
    preceding_editorial_block_ids: list[str] = Field(default_factory=list)
    following_editorial_block_ids: list[str] = Field(default_factory=list)
    boundary_confidence: Literal["high", "adjudicated", "human_mapped", "ambiguous"] = "high"


class SectionItem(BaseModel):
    kind: Literal["letter", "blocks"]
    letter_id: str | None = None
    blocks: list[BlockIR] = Field(default_factory=list)


class BookSection(BaseModel):
    section_id: str
    kind: Literal["frontmatter", "chapter", "epistolary_part", "appendix", "index", "backmatter"]
    title_tagged: str
    title_plain: str
    start_page: int
    end_page: int
    items: list[SectionItem] = Field(default_factory=list)
    footnote_ids: list[str] = Field(default_factory=list)
    unavailable_pages: list[int] = Field(default_factory=list)


class ValidationFinding(BaseModel):
    code: str
    severity: Literal["blocker", "warning", "info"]
    page_number: int | None = None
    block_id: str | None = None
    letter_id: str | None = None
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class RubricBreakdown(BaseModel):
    total_score: float
    text_and_diacritic_fidelity: float
    epistolary_structure_and_layout: float
    apparatus_and_footnote_integrity: float
    completeness_and_page_coverage: float
    compiled_output_validity: float
    provenance_tier: Literal["blocked", "community", "machine_checked", "source_certified"]
    blocker_count: int
    hotspot_count: int
    notes: list[str] = Field(default_factory=list)


class CollectionAST(BaseModel):
    schema_version: str = "1.0.0"
    book_id: str
    collection: str = "world-correspondence"
    title: str
    author_or_editor: str = ""
    year: int | None = None
    archive_id: str | None = None
    active_run_id: str
    total_pages: int
    pages: list[PageIR] = Field(default_factory=list)
    sections: list[BookSection] = Field(default_factory=list)
    letters: list[LetterUnit] = Field(default_factory=list)
    footnotes: dict[str, FootnoteItem] = Field(default_factory=dict)
    findings: list[ValidationFinding] = Field(default_factory=list)
    rubric: RubricBreakdown | None = None
    proof_summary: dict[str, Any] = Field(default_factory=dict)
    model_breakdown: dict[str, int] = Field(default_factory=dict)
    total_spend_usd: float = 0.0
