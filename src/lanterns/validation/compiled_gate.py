"""ROLE: Post-compile verification gate auditing all multi-format artifacts on disk.
INPUTS: Compiled edition directory path and optional CollectionAST.
OUTPUTS: Structured audit dictionary with ok/valid flags, error records, and ValidationFinding list.
INVARIANT: Strictly inspects emitted Web, Letters, EPUB 3, Markdown, TEI P5, and Proofing Studio files.
"""

from __future__ import annotations

import json
import posixpath
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from lanterns.core.models import CollectionAST, ValidationFinding

_LEAKED_PLACEHOLDER_RE = re.compile(r"\[\[(?:FOLIO|FNFOLIO|NOTE):|&lt;/?(?:i|b|sc|fnref|sup|sub|unclear)&gt;")
_UNSAFE_DOM_SINK_RE = re.compile(r"\b(?:innerHTML|outerHTML|insertAdjacentHTML)\b")
_PROOFING_HASH_RE = re.compile(r"^(?:page|leaf|p)-?\d+$")
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:")
_XML_ID_ATTR = "{http://www.w3.org/XML/1998/namespace}id"
_EPUB_TYPE_ATTR = "{http://www.idpf.org/2007/ops}type"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_TEI_RNG = _REPO_ROOT / "workspace" / "tools" / "tei_all.rng"
_DEFAULT_EPUBCHECK_JAR = _REPO_ROOT / "workspace" / "tools" / "epubcheck-5.1.0" / "epubcheck.jar"
_REQUIRED_OPF_A11Y_PROPS = frozenset(
    {
        "schema:accessMode",
        "schema:accessModeSufficient",
        "schema:accessibilityFeature",
        "schema:accessibilityHazard",
        "schema:accessibilitySummary",
    }
)
_TEI_BODY_CHUNK_TAGS = frozenset({"p", "ab", "lg", "list", "table", "quote", "floatingText"})
_TEI_NON_TESSELLATED_TAGS = _TEI_BODY_CHUNK_TAGS | frozenset({"opener", "closer", "postscript"})


def _local_xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _HtmlInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.links: list[tuple[str, str, str]] = []
        self.headings: list[int] = []
        self.has_li_doc_footnote = False
        self.has_csp = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t_low = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if attr_map.get("id"):
            self.ids.append(attr_map["id"])
        if len(t_low) == 2 and t_low[0] == "h" and t_low[1] in "123456":
            self.headings.append(int(t_low[1]))
        if t_low == "li" and attr_map.get("role", "").strip() == "doc-footnote":
            self.has_li_doc_footnote = True
        if t_low == "meta" and attr_map.get("http-equiv", "").lower() == "content-security-policy":
            if attr_map.get("content", "").strip():
                self.has_csp = True
        for attr in ("href", "src"):
            val = attr_map.get(attr, "").strip()
            if val:
                self.links.append((tag, attr, val))


def _record_err(
    errors: list[dict[str, Any]],
    surface: str,
    code: str,
    message: str,
    file_rel: str | None = None,
) -> None:
    errors.append({"surface": surface, "code": code, "file": file_rel, "message": message})


def _parse_html_file(path: Path) -> tuple[set[str], list[str], list[tuple[str, str, str]], bool, str, _HtmlInspector]:
    text = path.read_text(encoding="utf-8")
    inspector = _HtmlInspector()
    inspector.feed(text)
    dups = sorted(k for k, cnt in Counter(inspector.ids).items() if cnt > 1)
    return set(inspector.ids), dups, inspector.links, inspector.has_csp, text, inspector


def _check_heading_hierarchy(
    headings: list[int],
    surface: str,
    rel_file: str,
    errors: list[dict[str, Any]],
) -> None:
    for prev_h, cur_h in zip(headings, headings[1:], strict=False):
        if cur_h > prev_h + 1:
            _record_err(
                errors,
                surface,
                "heading_level_skip",
                f"Skipped heading level h{prev_h} -> h{cur_h} in {rel_file}",
                rel_file,
            )
            return


def _check_single_html_links(
    hp: Path,
    links: list[tuple[str, str, str]],
    root: Path,
    surface: str,
    require_proofing: bool,
    id_cache: dict[Path, set[str]],
    errors: list[dict[str, Any]],
) -> None:
    rel_hp = str(hp.relative_to(root))
    for tag, attr, raw_url in links:
        if raw_url.startswith(_EXTERNAL_SCHEMES):
            continue
        if raw_url.lower().startswith("javascript:"):
            _record_err(errors, surface, "javascript_url", f"Disallowed javascript: URL in {rel_hp}", rel_hp)
            continue
        path_part, sep, frag = raw_url.partition("#")
        target = (hp.parent / unquote(path_part)).resolve() if path_part else hp.resolve()
        frag_clean = unquote(frag) if sep else ""

        if not require_proofing and target == (root / "proofing" / "index.html").resolve() and not target.exists():
            continue
        if not target.exists():
            _record_err(errors, surface, "broken_relative_link", f"Missing <{tag} {attr}={raw_url!r}> in {rel_hp}", rel_hp)
            continue
        if not frag_clean or target.suffix.lower() not in {".html", ".htm"}:
            continue
        if target not in id_cache:
            t_ids, _, _, _, _, _ = _parse_html_file(target)
            id_cache[target] = t_ids
        is_proof_hash = target.parent.name == "proofing" and bool(_PROOFING_HASH_RE.match(frag_clean))
        if frag_clean not in id_cache[target] and not is_proof_hash:
            _record_err(errors, surface, "broken_anchor", f"Broken #{frag_clean} in {raw_url!r} from {rel_hp}", rel_hp)


def _validate_html_group(
    root: Path,
    html_files: list[Path],
    surface: str,
    require_proofing: bool,
    strict_standards: bool,
    id_cache: dict[Path, set[str]],
    errors: list[dict[str, Any]],
) -> None:
    parsed_by_path: dict[Path, tuple[list[str], list[tuple[str, str, str]], str, _HtmlInspector]] = {}
    for hp in html_files:
        resolved = hp.resolve()
        ids, dups, links, _, text, insp = _parse_html_file(resolved)
        id_cache[resolved] = ids
        parsed_by_path[resolved] = (dups, links, text, insp)

    for hp in html_files:
        resolved = hp.resolve()
        rel_hp = str(resolved.relative_to(root))
        dups, links, text, insp = parsed_by_path[resolved]
        if _LEAKED_PLACEHOLDER_RE.search(text) or "\ufffd" in text:
            _record_err(errors, surface, "leaked_placeholder_or_mojibake", f"Corrupted token in {rel_hp}", rel_hp)
        for dup in dups:
            _record_err(errors, surface, "duplicate_html_id", f"Duplicate id={dup!r} in {rel_hp}", rel_hp)
        if strict_standards:
            _check_heading_hierarchy(insp.headings, surface, rel_hp, errors)
            if insp.has_li_doc_footnote:
                _record_err(errors, surface, "invalid_li_doc_footnote", f"Disallowed <li role='doc-footnote'> in {rel_hp}", rel_hp)
        _check_single_html_links(resolved, links, root, surface, require_proofing, id_cache, errors)


def _validate_web_edition(
    root: Path,
    require_proofing: bool,
    strict_standards: bool,
    id_cache: dict[Path, set[str]],
    errors: list[dict[str, Any]],
) -> None:
    web_dir = root / "web"
    index_path = web_dir / "index.html"
    if not index_path.is_file():
        _record_err(errors, "web", "missing_web_index", "Missing web/index.html", "web/index.html")
        return
    section_htmls = sorted((web_dir / "sections").glob("*.html")) if (web_dir / "sections").is_dir() else []
    chapter_htmls = sorted((web_dir / "chapters").glob("*.html")) if (web_dir / "chapters").is_dir() else []
    _validate_html_group(
        root, [index_path, *section_htmls, *chapter_htmls], "web", require_proofing, strict_standards, id_cache, errors
    )


def _extract_expected_slugs(index_payload: Any, ast: CollectionAST | None) -> list[str]:
    if ast is not None:
        return [lu.slug for lu in ast.letters]
    entries = index_payload.get("letters") if isinstance(index_payload, dict) else index_payload
    if not isinstance(entries, list):
        return []
    return [str(item["slug"]) for item in entries if isinstance(item, dict) and item.get("slug")]


def _validate_letter_sidecars(
    letters_dir: Path,
    slug: str,
    root: Path,
    errors: list[dict[str, Any]],
) -> None:
    md_path = letters_dir / f"{slug}.md"
    json_path = letters_dir / f"{slug}.json"
    if not md_path.is_file():
        _record_err(errors, "letters", "missing_letter_markdown", f"Missing letters/{slug}.md", f"letters/{slug}.md")
    else:
        md_txt = md_path.read_text(encoding="utf-8")
        if not md_txt.startswith("---\n") or "\n---\n" not in md_txt[4:]:
            _record_err(errors, "letters", "invalid_letter_frontmatter", f"Missing YAML frontmatter in letters/{slug}.md", f"letters/{slug}.md")
    if not json_path.is_file():
        _record_err(errors, "letters", "missing_letter_json", f"Missing letters/{slug}.json", f"letters/{slug}.json")
    else:
        try:
            json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            rel_j = str(json_path.relative_to(root))
            _record_err(errors, "letters", "invalid_letter_json", f"Invalid JSON in {rel_j}: {exc}", rel_j)


def _validate_letters_edition(
    root: Path,
    ast: CollectionAST | None,
    require_proofing: bool,
    strict_standards: bool,
    id_cache: dict[Path, set[str]],
    errors: list[dict[str, Any]],
) -> None:
    letters_dir = root / "letters"
    idx_json = letters_dir / "index.json"
    idx_html = letters_dir / "index.html"
    if not idx_json.is_file():
        _record_err(errors, "letters", "missing_letters_index_json", "Missing letters/index.json", "letters/index.json")
        return
    try:
        payload = json.loads(idx_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _record_err(errors, "letters", "invalid_letters_index_json", f"Malformed letters/index.json: {exc}", "letters/index.json")
        return

    entries = payload.get("letters") if isinstance(payload, dict) else payload
    if ast is not None and isinstance(entries, list) and len(entries) != len(ast.letters):
        _record_err(
            errors,
            "letters",
            "letter_count_mismatch",
            f"letters/index.json has {len(entries)} entries vs {len(ast.letters)} AST letters",
            "letters/index.json",
        )

    expected_slugs = _extract_expected_slugs(payload, ast)
    html_targets: list[Path] = []
    if idx_html.is_file():
        html_targets.append(idx_html)
    elif expected_slugs:
        _record_err(errors, "letters", "missing_letters_index_html", "Missing letters/index.html", "letters/index.html")

    for slug in expected_slugs:
        l_html = letters_dir / f"{slug}.html"
        if not l_html.is_file():
            _record_err(errors, "letters", "missing_letter_html", f"Missing letters/{slug}.html", f"letters/{slug}.html")
        else:
            html_targets.append(l_html)
        _validate_letter_sidecars(letters_dir, slug, root, errors)

    if html_targets:
        _validate_html_group(root, html_targets, "letters", require_proofing, strict_standards, id_cache, errors)

    for js_file in sorted(letters_dir.glob("*.js")):
        if _UNSAFE_DOM_SINK_RE.search(js_file.read_text(encoding="utf-8")):
            rel_js = str(js_file.relative_to(root))
            _record_err(errors, "letters", "unsafe_dom_sink", f"Disallowed innerHTML/outerHTML in {rel_js}", rel_js)


def _inspect_epub_xhtml(
    zf: zipfile.ZipFile,
    name: str,
    rel_epub: str,
    strict_standards: bool,
    errors: list[dict[str, Any]],
) -> tuple[set[str], list[tuple[str, str]], ET.Element] | None:
    raw = zf.read(name).decode("utf-8")
    if _LEAKED_PLACEHOLDER_RE.search(raw):
        _record_err(errors, "epub", "leaked_placeholder", f"Leaked placeholder in {rel_epub}:{name}", rel_epub)
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError as exc:
        _record_err(errors, "epub", "malformed_xhtml", f"XML error in {rel_epub}:{name}: {exc}", rel_epub)
        return None

    ids_list: list[str] = []
    links: list[tuple[str, str]] = []
    headings: list[int] = []
    for elem in tree.iter():
        lname = _local_xml_name(elem.tag)
        if len(lname) == 2 and lname[0] == "h" and lname[1] in "123456":
            headings.append(int(lname[1]))
        if strict_standards and lname == "li" and elem.attrib.get("role", "").strip() == "doc-footnote":
            _record_err(errors, "epub", "invalid_li_doc_footnote", f"Disallowed <li role='doc-footnote'> in {name}", rel_epub)
        eid = elem.attrib.get("id") or elem.attrib.get(_XML_ID_ATTR)
        if eid:
            ids_list.append(eid)
        for attr in ("href", "src"):
            val = elem.attrib.get(attr, "").strip()
            if val and not val.startswith(_EXTERNAL_SCHEMES):
                links.append((attr, val))
    if strict_standards:
        _check_heading_hierarchy(headings, "epub", f"{rel_epub}:{name}", errors)
    for dup, cnt in Counter(ids_list).items():
        if cnt > 1:
            _record_err(errors, "epub", "duplicate_xhtml_id", f"Duplicate id={dup!r} in {rel_epub}:{name}", rel_epub)
    return set(ids_list), links, tree


def _check_epub_opf_and_container(
    zf: zipfile.ZipFile,
    names_set: set[str],
    rel_epub: str,
    strict_standards: bool,
    errors: list[dict[str, Any]],
) -> tuple[dict[str, str], list[str]] | None:
    if "META-INF/container.xml" not in names_set:
        _record_err(errors, "epub", "missing_container_xml", f"Missing META-INF/container.xml in {rel_epub}", rel_epub)
        return None
    try:
        c_tree = ET.fromstring(zf.read("META-INF/container.xml").decode("utf-8"))
    except ET.ParseError as exc:
        _record_err(errors, "epub", "malformed_container_xml", f"Malformed container.xml: {exc}", rel_epub)
        return None
    opf_paths = [el.attrib["full-path"] for el in c_tree.iter() if el.tag.endswith("rootfile") and "full-path" in el.attrib]
    if not opf_paths or opf_paths[0] not in names_set:
        _record_err(errors, "epub", "missing_content_opf", f"Missing OPF package in {rel_epub}", rel_epub)
        return None
    opf_entry = opf_paths[0]
    try:
        opf_tree = ET.fromstring(zf.read(opf_entry).decode("utf-8"))
    except ET.ParseError as exc:
        _record_err(errors, "epub", "malformed_opf", f"Malformed {opf_entry}: {exc}", rel_epub)
        return None
    opf_dir = posixpath.dirname(opf_entry)
    manifest_by_id: dict[str, str] = {}
    meta_props: set[str] = set()
    spine_idrefs: list[str] = []
    for item in opf_tree.iter():
        lname = _local_xml_name(item.tag)
        if lname == "meta" and item.attrib.get("property"):
            meta_props.add(item.attrib["property"].strip())
        elif lname == "item" and "href" in item.attrib:
            href_entry = posixpath.normpath(posixpath.join(opf_dir, unquote(item.attrib["href"])))
            if item.attrib.get("id"):
                manifest_by_id[item.attrib["id"]] = href_entry
            if href_entry not in names_set:
                _record_err(errors, "epub", "missing_manifest_item", f"Missing manifest item {href_entry} in {rel_epub}", rel_epub)
        elif lname == "itemref" and item.attrib.get("idref"):
            spine_idrefs.append(item.attrib["idref"])
    if strict_standards:
        missing_a11y = sorted(_REQUIRED_OPF_A11Y_PROPS - meta_props)
        if missing_a11y:
            _record_err(errors, "epub", "missing_epub_a11y_meta", f"Missing OPF metadata {missing_a11y} in {rel_epub}", rel_epub)
    return manifest_by_id, spine_idrefs


def _extract_nav_pagelist_refs(nav_entry: str, nav_tree: ET.Element, rel_epub: str, errors: list[dict[str, Any]]) -> list[tuple[str, int]]:
    nav_dir = posixpath.dirname(nav_entry)
    toc_ok = False
    page_refs: list[tuple[str, int]] = []
    for nav_el in nav_tree.iter():
        if _local_xml_name(nav_el.tag) != "nav":
            continue
        etype = nav_el.attrib.get(_EPUB_TYPE_ATTR, "")
        role = nav_el.attrib.get("role", "").strip()
        if "toc" in etype.split():
            toc_ok = role == "doc-toc"
        elif "page-list" in etype.split():
            if role != "doc-pagelist":
                _record_err(errors, "epub", "missing_nav_doc_pagelist", f"Missing role='doc-pagelist' in {rel_epub}:{nav_entry}", rel_epub)
            for a_el in nav_el.iter():
                href = a_el.attrib.get("href", "").strip()
                if _local_xml_name(a_el.tag) == "a" and "#page-" in href:
                    p_part, _, frag = href.partition("#")
                    if frag[5:].isdigit():
                        tgt = posixpath.normpath(posixpath.join(nav_dir, unquote(p_part))) if p_part else nav_entry
                        page_refs.append((tgt, int(frag[5:])))
    if not toc_ok:
        _record_err(errors, "epub", "missing_nav_doc_toc", f"Missing role='doc-toc' in {rel_epub}:{nav_entry}", rel_epub)
    return page_refs


def _check_epub_strict_standards(
    zf: zipfile.ZipFile,
    names: list[str],
    manifest_by_id: dict[str, str],
    spine_idrefs: list[str],
    xhtml_trees: dict[str, ET.Element],
    rel_epub: str,
    errors: list[dict[str, Any]],
) -> None:
    for css_name in (n for n in names if n.endswith(".css")):
        css_raw = zf.read(css_name).decode("utf-8").lower()
        if "#faf7f2" in css_raw or "#1a1816" in css_raw:
            _record_err(errors, "epub", "epub_hardcoded_paper_theme", f"Hardcoded day theme in {rel_epub}:{css_name}", rel_epub)

    nav_entries = [n for n in names if n.endswith("nav.xhtml") and n in xhtml_trees]
    if not nav_entries:
        return
    nav_refs = _extract_nav_pagelist_refs(nav_entries[0], xhtml_trees[nav_entries[0]], rel_epub, errors)
    spine_refs: list[tuple[str, int]] = []
    for sid in spine_idrefs:
        entry = manifest_by_id.get(sid, "")
        tree = xhtml_trees.get(entry)
        if tree is None:
            continue
        for el in tree.iter():
            eid = el.attrib.get("id", "")
            if eid.startswith("page-") and eid[5:].isdigit():
                spine_refs.append((entry, int(eid[5:])))

    for (_, prev_p), (_, cur_p) in zip(spine_refs, spine_refs[1:], strict=False):
        if cur_p <= prev_p:
            _record_err(
                errors,
                "epub",
                "epub_non_monotone_page_order",
                f"Non-monotone spine page anchor #page-{prev_p} -> #page-{cur_p} in {rel_epub} (NAV-011)",
                rel_epub,
            )
            break
    if spine_refs and nav_refs != spine_refs:
        _record_err(
            errors,
            "epub",
            "epub_pagelist_spine_mismatch",
            f"nav.xhtml page-list ({len(nav_refs)} items) does not match spine page order ({len(spine_refs)} items) in {rel_epub}",
            rel_epub,
        )


def _run_external_epubcheck(epub_path: Path, rel_epub: str, errors: list[dict[str, Any]]) -> None:
    if not _DEFAULT_EPUBCHECK_JAR.is_file():
        _record_err(errors, "epub", "missing_epubcheck_jar", f"EPUBCheck jar missing at {_DEFAULT_EPUBCHECK_JAR}", rel_epub)
        return
    cmd: list[str] | None = None
    if shutil.which("java") and subprocess.run(["java", "-version"], capture_output=True, check=False).returncode == 0:
        cmd = ["java", "-jar", str(_DEFAULT_EPUBCHECK_JAR), str(epub_path)]
    elif shutil.which("mise"):
        cmd = ["mise", "x", "java@temurin-21.0.13+9.0.LTS", "--", "java", "-jar", str(_DEFAULT_EPUBCHECK_JAR), str(epub_path)]
    if cmd is None:
        _record_err(errors, "epub", "missing_java_runtime", "No Java runtime available for EPUBCheck", rel_epub)
        return
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=240)
    combined = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0 or "No errors or warnings detected" not in combined:
        _record_err(errors, "epub", "epubcheck_failed", f"EPUBCheck failure on {rel_epub}: {combined[:400]}", rel_epub)


def _validate_epub_edition(
    root: Path,
    strict_standards: bool,
    run_external_tools: bool,
    errors: list[dict[str, Any]],
) -> None:
    epub_files = sorted((root / "epub").glob("*.epub")) if (root / "epub").is_dir() else []
    if not epub_files:
        _record_err(errors, "epub", "missing_epub", "No .epub archive found under epub/", "epub")
        return

    for epub_path in epub_files:
        rel_epub = str(epub_path.relative_to(root))
        with zipfile.ZipFile(epub_path, "r") as zf:
            names = zf.namelist()
            names_set = set(names)
            if not names or names[0] != "mimetype":
                _record_err(errors, "epub", "mimetype_not_first", f"'mimetype' must be first in {rel_epub}", rel_epub)
            elif zf.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
                _record_err(errors, "epub", "compressed_mimetype", f"'mimetype' compressed in {rel_epub}", rel_epub)

            opf_info = _check_epub_opf_and_container(zf, names_set, rel_epub, strict_standards, errors)
            if not any(n.endswith("nav.xhtml") for n in names):
                _record_err(errors, "epub", "missing_nav_xhtml", f"Missing nav.xhtml in {rel_epub}", rel_epub)

            xhtml_ids: dict[str, set[str]] = {}
            xhtml_links: dict[str, list[tuple[str, str]]] = {}
            xhtml_trees: dict[str, ET.Element] = {}
            for name in names:
                if name.endswith(".xhtml"):
                    res = _inspect_epub_xhtml(zf, name, rel_epub, strict_standards, errors)
                    if res is not None:
                        xhtml_ids[name], xhtml_links[name], xhtml_trees[name] = res

            for name, links in xhtml_links.items():
                base_dir = posixpath.dirname(name)
                for _attr, raw_url in links:
                    p_part, sep, frag = raw_url.partition("#")
                    target_entry = posixpath.normpath(posixpath.join(base_dir, unquote(p_part))) if p_part else name
                    frag_clean = unquote(frag) if sep else ""
                    if target_entry not in names_set:
                        _record_err(errors, "epub", "broken_epub_href", f"Missing {target_entry} from {name}", rel_epub)
                    elif frag_clean and target_entry in xhtml_ids and frag_clean not in xhtml_ids[target_entry]:
                        _record_err(errors, "epub", "broken_epub_fragment", f"Broken #{frag_clean} in {raw_url!r} from {name}", rel_epub)

            if strict_standards and opf_info is not None:
                _check_epub_strict_standards(zf, names, opf_info[0], opf_info[1], xhtml_trees, rel_epub, errors)
        if run_external_tools:
            _run_external_epubcheck(epub_path, rel_epub, errors)


def _validate_markdown_edition(root: Path, errors: list[dict[str, Any]]) -> None:
    md_files = sorted((root / "markdown").glob("*.md")) if (root / "markdown").is_dir() else []
    if not md_files:
        _record_err(errors, "markdown", "missing_markdown", "No scholarly Markdown found under markdown/", "markdown")
        return
    for md_path in md_files:
        rel_md = str(md_path.relative_to(root))
        txt = md_path.read_text(encoding="utf-8")
        if not txt.strip():
            _record_err(errors, "markdown", "empty_markdown", f"Empty Markdown file {rel_md}", rel_md)
        if _LEAKED_PLACEHOLDER_RE.search(txt) or "\ufffd" in txt or "\x00" in txt:
            _record_err(errors, "markdown", "corrupted_markdown", f"Placeholder or mojibake in {rel_md}", rel_md)


def _check_single_tei_div(div_el: ET.Element, rel_tei: str, errors: list[dict[str, Any]]) -> None:
    child_tags = [_local_xml_name(c.tag) for c in list(div_el)]
    div_id = div_el.attrib.get(_XML_ID_ATTR, "<div?>")
    if "div" in child_tags and (set(child_tags) & _TEI_NON_TESSELLATED_TAGS):
        _record_err(
            errors,
            "tei",
            "tei_div_tessellation_violation",
            f"Mixed prose chunks and child <div> in {div_id} of {rel_tei}",
            rel_tei,
        )
    seen_body = False
    seen_bottom = False
    for ct in child_tags:
        if ct in {"head", "opener"} and (seen_body or seen_bottom):
            _record_err(errors, "tei", "tei_late_divtop_element", f"<{ct}> after body/closer in {div_id} of {rel_tei}", rel_tei)
            return
        if ct in _TEI_BODY_CHUNK_TAGS or ct == "div":
            if seen_bottom:
                _record_err(errors, "tei", "tei_body_after_divbottom", f"<{ct}> after closer/postscript in {div_id} of {rel_tei}", rel_tei)
                return
            seen_body = True
        elif ct in {"closer", "postscript"}:
            seen_bottom = True


def _check_tei_strict_structure(tree: ET.Element, raw: str, rel_tei: str, errors: list[dict[str, Any]]) -> None:
    if "<persName>Unknown</persName>" in raw:
        _record_err(errors, "tei", "tei_placeholder_unknown_correspondent", f"Placeholder 'Unknown' in {rel_tei}", rel_tei)
    for el in tree.iter():
        lname = _local_xml_name(el.tag)
        if lname == "l" and any(_local_xml_name(sub.tag) == "lb" for sub in el.iter()):
            _record_err(errors, "tei", "tei_lb_inside_verse_line", f"<lb/> inside <l> in {rel_tei}", rel_tei)
            break
    for el in tree.iter():
        if _local_xml_name(el.tag) == "div":
            _check_single_tei_div(el, rel_tei, errors)


def _run_external_tei_relaxng(tei_path: Path, rel_tei: str, errors: list[dict[str, Any]]) -> None:
    if not _DEFAULT_TEI_RNG.is_file() or not shutil.which("xmllint"):
        _record_err(errors, "tei", "missing_tei_rng_validator", "xmllint or tei_all.rng missing", rel_tei)
        return
    proc = subprocess.run(
        ["xmllint", "--noout", "--relaxng", str(_DEFAULT_TEI_RNG), str(tei_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        _record_err(errors, "tei", "tei_relaxng_failed", f"tei_all.rng failure on {rel_tei}: {proc.stderr[:400]}", rel_tei)


def _check_tei_ids_and_pointers(
    tree: ET.Element,
    ast: CollectionAST | None,
    rel_tei: str,
    errors: list[dict[str, Any]],
) -> None:
    xml_ids = [el.attrib[_XML_ID_ATTR] for el in tree.iter() if _XML_ID_ATTR in el.attrib]
    for dup_id, cnt in Counter(xml_ids).items():
        if cnt > 1:
            _record_err(errors, "tei", "duplicate_xml_id", f"Duplicate xml:id={dup_id!r} in {rel_tei}", rel_tei)

    corresp_descs = [el for el in tree.iter() if el.tag.endswith("correspDesc")]
    letter_divs = [el for el in tree.iter() if el.tag.endswith("div") and el.attrib.get("type") == "letter"]
    expected_letters = len(ast.letters) if ast is not None else len(letter_divs)
    if expected_letters > 0 and not corresp_descs:
        _record_err(errors, "tei", "missing_corresp_desc", f"Missing <correspDesc> in {rel_tei}", rel_tei)
    elif ast is not None and len(corresp_descs) != len(ast.letters):
        _record_err(
            errors,
            "tei",
            "corresp_desc_count_mismatch",
            f"Found {len(corresp_descs)} <correspDesc> vs {len(ast.letters)} AST letters in {rel_tei}",
            rel_tei,
        )

    id_set = set(xml_ids)
    for el in tree.iter():
        for ref_attr in ("corresp", "target"):
            val = el.attrib.get(ref_attr, "").strip()
            if val.startswith("#") and val[1:] not in id_set:
                _record_err(errors, "tei", "broken_tei_pointer", f"Unresolved {ref_attr}={val!r} in {rel_tei}", rel_tei)


def _validate_tei_edition(
    root: Path,
    ast: CollectionAST | None,
    strict_standards: bool,
    run_external_tools: bool,
    errors: list[dict[str, Any]],
) -> None:
    tei_files = sorted((root / "tei").glob("*.xml")) if (root / "tei").is_dir() else []
    if not tei_files:
        _record_err(errors, "tei", "missing_tei_xml", "No TEI P5 XML found under tei/", "tei")
        return
    for tei_path in tei_files:
        rel_tei = str(tei_path.relative_to(root))
        raw = tei_path.read_text(encoding="utf-8")
        if _LEAKED_PLACEHOLDER_RE.search(raw):
            _record_err(errors, "tei", "leaked_placeholder", f"Leaked placeholder in {rel_tei}", rel_tei)
        try:
            tree = ET.fromstring(raw)
        except ET.ParseError as exc:
            _record_err(errors, "tei", "malformed_tei_xml", f"TEI XML parse error in {rel_tei}: {exc}", rel_tei)
            continue

        _check_tei_ids_and_pointers(tree, ast, rel_tei, errors)
        if strict_standards:
            _check_tei_strict_structure(tree, raw, rel_tei, errors)
        if run_external_tools:
            _run_external_tei_relaxng(tei_path, rel_tei, errors)


def _validate_proofing_studio(
    root: Path,
    ast: CollectionAST | None,
    errors: list[dict[str, Any]],
) -> None:
    proof_dir = root / "proofing"
    idx_path = proof_dir / "index.html"
    if not idx_path.is_file():
        _record_err(errors, "proofing", "missing_proofing_studio", "Missing proofing/index.html", "proofing/index.html")
        return

    _, dups, _, has_csp, _, _ = _parse_html_file(idx_path)
    if not has_csp:
        _record_err(errors, "proofing", "missing_csp", "Missing Content-Security-Policy meta tag in proofing/index.html", "proofing/index.html")
    for dup in dups:
        _record_err(errors, "proofing", "duplicate_html_id", f"Duplicate id={dup!r} in proofing/index.html", "proofing/index.html")

    code_files = [*sorted(proof_dir.rglob("*.html")), *sorted(proof_dir.rglob("*.js"))]
    for cf in code_files:
        rel_cf = str(cf.relative_to(root))
        src_txt = cf.read_text(encoding="utf-8")
        if _UNSAFE_DOM_SINK_RE.search(src_txt):
            _record_err(errors, "proofing", "unsafe_dom_sink", f"Forbidden innerHTML/outerHTML in {rel_cf}", rel_cf)

    pages_dir = proof_dir / "pages"
    shards = sorted(pages_dir.glob("*.json")) if pages_dir.is_dir() else []
    expected_pages = len(ast.pages) if ast is not None else 1
    if expected_pages > 0 and not shards:
        _record_err(errors, "proofing", "missing_proofing_shards", "No per-page JSON shards in proofing/pages/", "proofing/pages")
    elif ast is not None and len(shards) < len(ast.pages):
        _record_err(
            errors,
            "proofing",
            "incomplete_proofing_shards",
            f"Found {len(shards)} proofing page shards vs {len(ast.pages)} AST pages",
            "proofing/pages",
        )
    for shard_p in shards:
        shard_doc = json.loads(shard_p.read_text(encoding="utf-8"))
        scan_url = str(shard_doc.get("scan_url") or "")
        if scan_url.lower().endswith(".json"):
            rel_sp = str(shard_p.relative_to(root))
            _record_err(errors, "proofing", "non_image_scan_url", f"Non-image scan_url={scan_url!r} in {rel_sp}", rel_sp)


def _resolve_strict_standards_flag(
    root: Path,
    strict_standards: bool | None,
    ast_arg_provided: bool,
    require_proofing: bool,
) -> bool:
    if strict_standards is not None:
        return strict_standards
    if ast_arg_provided or require_proofing:
        return True
    manifest_p = root / "edition_manifest.json"
    if manifest_p.is_file():
        try:
            doc = json.loads(manifest_p.read_text(encoding="utf-8"))
            return doc.get("renderer_version") == "3.0.0"
        except json.JSONDecodeError:
            return False
    return False


def validate_compiled_edition(
    library_book_dir: str | Path,
    ast: CollectionAST | None = None,
    *,
    require_proofing: bool = True,
    strict_standards: bool | None = None,
    run_external_tools: bool = False,
) -> dict[str, Any]:
    """Verify all compiled multi-format surfaces on disk for a published or preview edition."""
    root = Path(library_book_dir).resolve()
    errors: list[dict[str, Any]] = []
    id_cache: dict[Path, set[str]] = {}
    strict_active = _resolve_strict_standards_flag(root, strict_standards, ast is not None, require_proofing)

    if ast is None and (root / "collection_ast.json").is_file():
        ast = CollectionAST.model_validate_json((root / "collection_ast.json").read_text(encoding="utf-8"))

    _validate_web_edition(root, require_proofing, strict_active, id_cache, errors)
    _validate_letters_edition(root, ast, require_proofing, strict_active, id_cache, errors)
    _validate_epub_edition(root, strict_active, run_external_tools, errors)
    _validate_markdown_edition(root, errors)
    _validate_tei_edition(root, ast, strict_active, run_external_tools, errors)
    if require_proofing:
        _validate_proofing_studio(root, ast, errors)

    findings = [
        ValidationFinding(
            code=f"compiled_gate_{err['code']}",
            severity="blocker",
            message=f"[{err['surface']}] {err['message']}",
            evidence={"surface": err["surface"], "file": err.get("file")},
        )
        for err in errors
    ]
    is_ok = len(errors) == 0
    return {
        "ok": is_ok,
        "valid": is_ok,
        "edition_dir": str(root),
        "strict_standards": strict_active,
        "error_count": len(errors),
        "errors": errors,
        "findings": findings,
    }
