"""ROLE: Dual `StructuralUnit` + `LetterUnit` materialization from `structure_auto` / `structure_overrides` or `$0` offline items.

INPUTS: Joined non-furniture/non-footnote `BlockIR` stream, optional `structure_plan` & `structure_overrides`, or `BookSection`s.
OUTPUTS: Source-ordered `BookSection` (`items` + conserved `units`) and `LetterUnit` projections.
INVARIANT: Zero VLM imports; zero content-word heuristics; 100% block conservation across `sec.units` and `sec.items`.
"""

from __future__ import annotations

from typing import Any, cast

from lanterns.core.models import (
    BlockIR,
    BookSection,
    BoundaryAnchor,
    BoundaryConfidence,
    ChildSpan,
    ChildSpanKind,
    CorrespondentRef,
    EpistolaryMetadata,
    LetterUnit,
    SectionItem,
    SectionKind,
    StructuralUnit,
    StructuralUnitKind,
    make_boundary_anchor,
)
from lanterns.epistolary.corrections import rebase_boundary_anchor
from lanterns.epistolary.metadata import build_letter_slug, extract_epistolary_metadata
from lanterns.epistolary.segmentation import _assign_unavailable_pages, _link_editorial_framing

_UNIT_KIND_BY_TYPE: dict[str, StructuralUnitKind] = {
    "heading": "apparatus",
    "toc_entry": "apparatus",
    "index_entry": "apparatus",
    "imprint": "apparatus",
    "facsimile_plate": "apparatus",
    "caption": "apparatus",
    "illustration": "apparatus",
    "editorial": "editorial_bridge",
}


def _block_unit_kind(btype: str, sec_kind: str) -> StructuralUnitKind:
    return "apparatus" if sec_kind in {"frontmatter", "index", "backmatter"} else _UNIT_KIND_BY_TYPE.get(btype, "narrative")


def _derive_enclosure_children(blocks: list[BlockIR], child_seq: int) -> tuple[list[ChildSpan], int]:
    children: list[ChildSpan] = []
    enc_buf: list[BlockIR] = []
    for blk in blocks:
        if blk.epistolary_transition == "starts_enclosure" and not enc_buf:
            enc_buf = [blk]
        elif enc_buf:
            enc_buf.append(blk)
            if blk.epistolary_transition == "ends_enclosure":
                child_seq += 1
                children.append(
                    ChildSpan(
                        child_id=f"child-{child_seq:04d}",
                        kind="enclosure",
                        start_anchor=make_boundary_anchor(enc_buf[0]),
                        end_anchor=make_boundary_anchor(enc_buf[-1]),
                        block_ids=[b.block_id for b in enc_buf],
                    )
                )
                enc_buf = []
    return children, child_seq


def _flush_block_run_units(
    sec_kind: str,
    blocks: list[BlockIR],
    u_seq: int,
) -> tuple[list[StructuralUnit], int]:
    groups: list[tuple[StructuralUnitKind, list[BlockIR]]] = []
    for blk in blocks:
        ukind = _block_unit_kind(blk.type, sec_kind)
        if groups and (
            groups[-1][0] == ukind
            or (blk.continues_from_prev and {groups[-1][0], ukind} <= {"narrative", "editorial_bridge"})
        ):
            groups[-1][1].append(blk)
        else:
            groups.append((ukind, [blk]))
    units: list[StructuralUnit] = []
    for ukind, gblks in groups:
        u_seq += 1
        units.append(
            StructuralUnit(
                unit_id=f"unit-{u_seq:04d}",
                kind=ukind,
                start_anchor=make_boundary_anchor(gblks[0]),
                end_anchor=make_boundary_anchor(gblks[-1]),
                block_ids=[b.block_id for b in gblks],
            )
        )
    return units, u_seq


def populate_section_units_from_items(
    sections: list[BookSection],
    letters_by_id: dict[str, LetterUnit],
    all_letters: list[LetterUnit] | None = None,
) -> None:
    """Populate 100%-conserving `sec.units` in source order from legacy `sec.items` (`$0` offline path)."""
    by_first_bid = {
        lu.blocks[0].block_id: lu for lu in (all_letters or letters_by_id.values()) if lu.blocks
    }
    u_seq, c_seq = 0, 0
    for sec in sections:
        sec_units: list[StructuralUnit] = []
        for item in sec.items:
            blks = item.blocks
            lu = (blks and by_first_bid.get(blks[0].block_id)) or letters_by_id.get(str(item.letter_id or ""))
            if item.kind == "letter" and blks:
                u_seq += 1
                meta = (lu and lu.metadata) or EpistolaryMetadata()
                children, c_seq = (
                    (lu.children, c_seq) if (lu is not None and lu.children) else _derive_enclosure_children(blks, c_seq)
                )
                if lu is not None:
                    lu.children = children
                sec_units.append(
                    StructuralUnit(
                        unit_id=f"unit-{u_seq:04d}",
                        kind="letter",
                        start_anchor=make_boundary_anchor(blks[0]),
                        end_anchor=make_boundary_anchor(blks[-1]),
                        block_ids=[b.block_id for b in blks],
                        children=children,
                        label=meta.letter_number_label,
                        senders=list(meta.senders),
                        recipients=list(meta.recipients),
                        place=meta.place,
                        date_raw=meta.dateline_plain,
                        date_iso=meta.date_iso,
                        footnote_ids=list((lu and lu.footnote_ids) or ()),
                    )
                )
            elif blks:
                run_units, u_seq = _flush_block_run_units(sec.kind, blks, u_seq)
                sec_units.extend(run_units)
        sec.units = sec_units


def _rebase_spec_anchor(raw_anchor: Any, fallback_bid: str, blocks: list[BlockIR], id_to_idx: dict[str, int]) -> int | None:
    if isinstance(raw_anchor, dict) and raw_anchor.get("block_id"):
        rebased = rebase_boundary_anchor(BoundaryAnchor.model_validate(raw_anchor), blocks)
        if rebased in id_to_idx:
            return id_to_idx[rebased]
    return id_to_idx.get(fallback_bid)


def _clamp_indexed_spans(
    raw_specs: list[dict[str, Any]],
    total_len: int,
    default_kind: str,
    default_title: str,
) -> list[dict[str, Any]]:
    ordered = sorted(raw_specs, key=lambda s: int(s["start_idx"]))
    if not ordered:
        return [{"kind": default_kind, "title_plain": default_title, "start_idx": 0, "end_idx": total_len - 1}]
    out: list[dict[str, Any]] = []
    if int(ordered[0]["start_idx"]) > 0:
        out.append({"kind": default_kind, "title_plain": default_title, "start_idx": 0, "end_idx": int(ordered[0]["start_idx"]) - 1})
    for spec in ordered:
        s_i = int(spec["start_idx"])
        if out and s_i <= int(out[-1]["start_idx"]):
            continue
        if out:
            out[-1]["end_idx"] = s_i - 1
        out.append({**spec, "start_idx": s_i, "end_idx": total_len - 1})
    return out


def _index_overrides(
    overrides: dict[str, Any] | None,
    content_blocks: list[BlockIR],
) -> tuple[set[str], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    ovr = overrides or {}
    forbid_ids = {
        bid
        for anc in (ovr.get("forbid_unit_splits") or [])
        if (bid := rebase_boundary_anchor(BoundaryAnchor.model_validate(anc), content_blocks))
    }
    force_by_bid: dict[str, dict[str, Any]] = {}
    for fs in ovr.get("force_unit_starts") or []:
        bid = rebase_boundary_anchor(BoundaryAnchor.model_validate(fs["anchor"]), content_blocks)
        if bid:
            force_by_bid[bid] = {**fs, "boundary_confidence": "human_mapped"}
    encs_by_start_bid: dict[str, list[dict[str, Any]]] = {}
    for enc in ovr.get("enclosure_spans") or []:
        s_bid = rebase_boundary_anchor(BoundaryAnchor.model_validate(enc["start_anchor"]), content_blocks)
        e_bid = rebase_boundary_anchor(BoundaryAnchor.model_validate(enc["end_anchor"]), content_blocks)
        if s_bid:
            encs_by_start_bid.setdefault(s_bid, []).append(
                {**enc, "start_block_id": s_bid, "end_block_id": e_bid or s_bid}
            )
    return forbid_ids, force_by_bid, encs_by_start_bid


def _apply_override_directives(
    unit_specs: list[dict[str, Any]],
    sec_id_to_idx: dict[str, int],
    forbid_ids: set[str],
    force_by_bid: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    forbid_starts = {sec_id_to_idx[bid] for bid in forbid_ids if bid in sec_id_to_idx}
    filtered = [sp for sp in unit_specs if sp["start_idx"] == 0 or int(sp["start_idx"]) not in forbid_starts]
    for bid, fs in force_by_bid.items():
        if bid in sec_id_to_idx:
            filtered.append({**fs, "start_idx": sec_id_to_idx[bid]})
    return filtered


def _build_rebased_children(
    raw_children: list[dict[str, Any]],
    unit_blocks: list[BlockIR],
    child_seq: int,
) -> tuple[list[ChildSpan], int]:
    u_idx = {b.block_id: i for i, b in enumerate(unit_blocks)}
    spans: list[tuple[int, int, dict[str, Any]]] = []
    for rc in raw_children:
        s_i = _rebase_spec_anchor(rc.get("start_anchor"), str(rc.get("start_block_id") or ""), unit_blocks, u_idx)
        e_i = _rebase_spec_anchor(rc.get("end_anchor"), str(rc.get("end_block_id") or ""), unit_blocks, u_idx)
        if s_i is not None:
            spans.append((s_i, max(s_i, e_i if e_i is not None else s_i), rc))
    spans.sort(key=lambda t: (t[0], t[1]))
    out: list[ChildSpan] = []
    last_end = -1
    for s_i, e_i, rc in spans:
        if s_i <= last_end:
            continue
        last_end = e_i
        span = unit_blocks[s_i : e_i + 1]
        child_seq += 1
        out.append(
            ChildSpan(
                child_id=f"child-{child_seq:04d}",
                kind=cast(ChildSpanKind, str(rc.get("kind") or "enclosure")),
                start_anchor=make_boundary_anchor(span[0]),
                end_anchor=make_boundary_anchor(span[-1]),
                block_ids=[b.block_id for b in span],
                title=rc.get("title"),
                senders=[CorrespondentRef.model_validate(c) for c in (rc.get("senders") or [])],
                recipients=[CorrespondentRef.model_validate(c) for c in (rc.get("recipients") or [])],
                place=rc.get("place"),
                date_raw=rc.get("date_raw"),
                date_iso=rc.get("date_iso"),
            )
        )
    return out, child_seq


def compile_from_structure_plan(
    content_blocks: list[BlockIR],
    *,
    book_title: str,
    all_page_numbers: list[int],
    failed_pages: set[int],
    structure_plan: dict[str, Any],
    structure_overrides: dict[str, Any] | None = None,
) -> tuple[list[BookSection], list[LetterUnit]]:
    """Materialize conserved `BookSection`s (`items` + `units`) and `LetterUnit`s from `structure_plan`."""
    if not content_blocks:
        return [], []
    id_to_idx = {b.block_id: i for i, b in enumerate(content_blocks)}
    ovr = structure_overrides or {}
    forbid_ids, force_by_bid, encs_by_start_bid = _index_overrides(ovr, content_blocks)
    raw_secs = list(ovr.get("section_overrides") or structure_plan.get("sections") or [])
    sec_candidates = [
        {**rs, "start_idx": s_i}
        for rs in raw_secs
        if (s_i := _rebase_spec_anchor(rs.get("start_anchor"), str(rs.get("start_block_id") or ""), content_blocks, id_to_idx)) is not None
    ]
    sec_spans = _clamp_indexed_spans(sec_candidates, len(content_blocks), "epistolary_part", book_title or "Correspondence")

    sections: list[BookSection] = []
    all_letters: list[LetterUnit] = []
    used_slugs: set[str] = set()
    l_seq, u_seq, c_seq = 0, 0, 0

    plan_units = [u for psec in (structure_plan.get("sections") or []) for u in (psec.get("units") or [])]
    indexed_plan_units = [
        (g_i, ru)
        for ru in plan_units
        if (g_i := _rebase_spec_anchor(ru.get("start_anchor"), str(ru.get("start_block_id") or ""), content_blocks, id_to_idx)) is not None
    ]

    for sec_i, sp in enumerate(sec_spans, start=1):
        sec_id = f"sec-{sec_i:02d}"
        s_start, s_end = int(sp["start_idx"]), int(sp["end_idx"])
        sec_blocks = content_blocks[s_start : s_end + 1]
        sec_idx = {b.block_id: i for i, b in enumerate(sec_blocks)}
        sec_kind = cast(SectionKind, str(sp.get("kind") or "chapter"))
        if sp.get("units"):
            u_cands = [
                {**ru, "start_idx": u_si}
                for ru in sp["units"]
                if (u_si := _rebase_spec_anchor(ru.get("start_anchor"), str(ru.get("start_block_id") or ""), sec_blocks, sec_idx)) is not None
            ]
        else:
            u_cands = [{**ru, "start_idx": g_i - s_start} for g_i, ru in indexed_plan_units if s_start <= g_i <= s_end]
        u_filtered = _apply_override_directives(u_cands, sec_idx, forbid_ids, force_by_bid)
        u_spans = _clamp_indexed_spans(u_filtered, len(sec_blocks), "narrative", "")

        sec_items: list[SectionItem] = []
        sec_units: list[StructuralUnit] = []
        sec_letters: dict[str, LetterUnit] = {}
        for usp in u_spans:
            u_seq += 1
            ublks = sec_blocks[int(usp["start_idx"]) : int(usp["end_idx"]) + 1]
            ubids = {b.block_id for b in ublks}
            ukind = cast(StructuralUnitKind, str(usp.get("kind") or "narrative"))
            unit_extra_encs = [e for bid in ubids for e in encs_by_start_bid.get(bid, [])]
            children, c_seq = _build_rebased_children([*(usp.get("children") or []), *unit_extra_encs], ublks, c_seq)
            senders = [CorrespondentRef.model_validate(c) for c in (usp.get("senders") or [])]
            recipients = [CorrespondentRef.model_validate(c) for c in (usp.get("recipients") or [])]
            unit_obj = StructuralUnit(
                unit_id=f"unit-{u_seq:04d}",
                kind=ukind,
                start_anchor=make_boundary_anchor(ublks[0]),
                end_anchor=make_boundary_anchor(ublks[-1]),
                block_ids=[b.block_id for b in ublks],
                children=children,
                label=usp.get("label"),
                title=usp.get("title"),
                senders=senders,
                recipients=recipients,
                place=usp.get("place"),
                date_raw=usp.get("date_raw"),
                date_iso=usp.get("date_iso"),
            )
            sec_units.append(unit_obj)

            if ukind in {"letter", "letter_excerpt"}:
                l_seq += 1
                lid = f"letter-{l_seq:04d}"
                meta = extract_epistolary_metadata(ublks, enclosure_block_ids={bid for ch in children for bid in ch.block_ids})
                meta = meta.model_copy(
                    update={
                        "letter_number_label": usp.get("label") or meta.letter_number_label,
                        "sender_display": " / ".join(c.name for c in senders) or meta.sender_display,
                        "recipient_display": " / ".join(c.name for c in recipients) or meta.recipient_display,
                        "senders": senders or meta.senders,
                        "recipients": recipients or meta.recipients,
                        "place": usp.get("place") or meta.place,
                        "date_iso": usp.get("date_iso") or meta.date_iso,
                        "dateline_plain": usp.get("date_raw") or meta.dateline_plain,
                        "has_enclosure": meta.has_enclosure or bool(children),
                    }
                )
                lu = LetterUnit(
                    letter_id=lid,
                    slug=build_letter_slug(l_seq, meta, used_slugs=used_slugs),
                    sequence_index=l_seq,
                    section_id=sec_id,
                    start_page=ublks[0].page_number,
                    end_page=ublks[-1].page_number,
                    start_folio=ublks[0].printed_folio,
                    end_folio=ublks[-1].printed_folio,
                    block_ids=[b.block_id for b in ublks],
                    blocks=ublks,
                    children=children,
                    metadata=meta,
                    boundary_confidence=cast(BoundaryConfidence, str(usp.get("boundary_confidence") or "adjudicated")),
                )
                all_letters.append(lu)
                sec_letters[lid] = lu
                sec_items.append(SectionItem(kind="letter", letter_id=lid, blocks=ublks))
            else:
                sec_items.append(SectionItem(kind="blocks", blocks=ublks))

        _link_editorial_framing(sec_items, sec_letters)
        pnums = [b.page_number for b in sec_blocks]
        title_p = str(sp.get("title_plain") or book_title or f"Section {sec_i}")
        sections.append(
            BookSection(
                section_id=sec_id,
                kind=sec_kind,
                title_tagged=str(sp.get("title_tagged") or title_p),
                title_plain=title_p,
                start_page=min(pnums),
                end_page=max(pnums),
                items=sec_items,
                units=sec_units,
            )
        )

    _assign_unavailable_pages(sections, failed_pages, all_page_numbers or [1])
    return sections, all_letters
