"""ROLE: AST-based structural anti-bloat guard and tuning-vs-held-out epistolary exam evaluator.
INPUTS: Repository source tree (`src/lanterns`, `prompts/`, `benchmarks/epistolary_exam/`).
OUTPUTS: Deterministic violation report (`run_architecture_guard`) and split exam score (`evaluate_epistolary_exam`).
INVARIANT: Enforces 7 structural laws via Python `ast` parsing rather than superficial line-count caps.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from lanterns.core.models import PageIR
from lanterns.core.paths import RepoPaths
from lanterns.epistolary.compiler import compile_collection_ast

LAYER_RANKS: dict[str, int] = {
    "core": 0,
    "source": 1,
    "providers": 2,
    "extraction": 3,
    "epistolary": 4,
    "validation": 5,
    "renderers": 6,
    "cli": 7,
}

MAX_FUNCTION_COMPLEXITY = 18
MAX_SUBSYSTEM_MODULES = {"epistolary": 8, "renderers": 12}
MAX_SUBSYSTEM_BRANCHES = {"epistolary": 320, "renderers": 340}
MAX_PROMPT_WORDS_PER_FILE = 2000
MAX_PROMPT_WORDS_TOTAL = 4500

BANNED_PROSE_WORDS = frozenset(
    {
        "dear",
        "sincerely",
        "faithfully",
        "affectionately",
        "obedient",
        "servant",
        "yours",
        "regards",
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "gandhi",
        "vallabhbhai",
        "goethe",
        "zelter",
        "erasmus",
        "newton",
        "bentley",
        "faraday",
        "napoleon",
        "nietzsche",
        "wagner",
        "tchaikovsky",
        "vivekananda",
        "tagore",
        "dostoevsky",
    }
)

EPISTOLARY_EXTRA_BANNED_WORDS = frozenset({"chapter", "preface", "foreword", "appendix"})
PROMPT_BANNED_PROPER_NOUNS = frozenset(
    {
        "gandhi",
        "vallabhbhai",
        "zelter",
        "bentley",
        "faraday",
        "tchaikovsky",
        "vivekananda",
        "dostoevsky",
        "nietzsche",
        "wagner",
    }
)

FORBIDDEN_RENDERER_DEFS = frozenset(
    {
        "segment_epistolary_collection",
        "segment_letters",
        "stitch_footnotes",
        "dehyphenate_line_wraps",
    }
)

_UNSAFE_DOM_SINKS = ("inner" + "HTML", "outer" + "HTML", "insertAdjacent" + "HTML")
_REGEX_CALL_NAMES = frozenset({"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "split"})
_WORD_EXTRACT_RE = re.compile(r"[A-Za-z]{4,}")
ALLOWED_REGEX_TOKENS_STRICT = frozenset({"ivxlcdm", "unclear", "fnref", "folio", "fnfolio", "note", "pages"})
ALLOWED_METADATA_HEADER_TOKENS = ALLOWED_REGEX_TOKENS_STRICT | frozenset(
    {
        "letter",
        "lettre",
        "epistle",
        "epistola",
        "brief",
        "carta",
        "dispatch",
        "number",
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "dated",
        "written",
        "from",
        "addressed",
        "copy",
        "following",
        "foregoing",
        "above",
        "same",
        "king",
        "firmand",
        "reply",
        "answer",
        "articles",
        "agreement",
        "objections",
        "consultations",
        "notes",
        "account",
        "overland",
        "extract",
        "minutes",
        "declaration",
        "proceedings",
        "commission",
        "instructions",
        "memorandum",
        "sept",
        "rich",
        "capt",
        "thos",
        "will",
        "lieut",
        "honble",
    }
)


def _subpackage_of(rel_parts: tuple[str, ...]) -> str:
    if len(rel_parts) == 1 and rel_parts[0] == "cli.py":
        return "cli"
    if len(rel_parts) >= 2:
        return rel_parts[0]
    return "root"


def _add_violation(
    violations: list[dict[str, Any]],
    rule: str,
    file_rel: str,
    message: str,
    lineno: int | None = None,
) -> None:
    violations.append({"rule": rule, "file": file_rel, "line": lineno, "message": message})


def _iter_child_nodes_outside_nested_defs(func_node: ast.AST) -> list[ast.AST]:
    collected: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(func_node))
    while stack:
        curr = stack.pop()
        collected.append(curr)
        if not isinstance(curr, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(curr))
    return collected


def compute_function_complexity(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Compute AST McCabe cyclomatic complexity of a single function (excluding nested defs)."""
    complexity = 1
    for node in _iter_child_nodes_outside_nested_defs(func_node):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp)):
            complexity += 1
        elif isinstance(node, ast.Match):
            complexity += max(1, len(node.cases))
        elif isinstance(node, ast.comprehension):
            complexity += len(node.ifs)
    return complexity


def _count_module_branch_nodes(tree: ast.AST) -> int:
    """Count conditional decision branches (`If`, `IfExp`, `Match`) across a module."""
    branch_types = (ast.If, ast.IfExp, ast.Match)
    return sum(1 for node in ast.walk(tree) if isinstance(node, branch_types))


def _check_content_heuristics(
    tree: ast.AST,
    subpkg: str,
    file_rel: str,
    violations: list[dict[str, Any]],
) -> None:
    if subpkg not in {"epistolary", "renderers"} or file_rel.endswith("doctor.py"):
        return
    banned = BANNED_PROSE_WORDS | (EPISTOLARY_EXTRA_BANNED_WORDS if subpkg == "epistolary" else frozenset())

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_re_call = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in _REGEX_CALL_NAMES
        )
        is_prefix_call = isinstance(node.func, ast.Attribute) and node.func.attr in {"startswith", "endswith"}
        if not (is_re_call or is_prefix_call) or not node.args:
            continue

        first_arg = node.args[0]
        str_vals: list[str] = []
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            str_vals.append(first_arg.value)
        elif isinstance(first_arg, ast.Tuple):
            str_vals.extend(el.value for el in first_arg.elts if isinstance(el, ast.Constant) and isinstance(el.value, str))

        allowed_tokens = (
            ALLOWED_METADATA_HEADER_TOKENS
            if file_rel.endswith("epistolary/metadata.py")
            else ALLOWED_REGEX_TOKENS_STRICT
        )
        for pat in str_vals:
            tokens = {w.lower() for w in _WORD_EXTRACT_RE.findall(pat)}
            hits = sorted(tokens & banned)
            disallowed = sorted(tokens - allowed_tokens) if is_re_call else []
            if hits or disallowed:
                bad = hits or disallowed
                _add_violation(
                    violations,
                    "content_word_heuristic",
                    file_rel,
                    f"Forbidden content-word/non-structural regex pattern {bad} in `{pat}` (Conveyor-Belt Law violation).",
                    getattr(node, "lineno", None),
                )


def _extract_target_var_name(expr: ast.AST) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Constant) and isinstance(expr.slice.value, str):
        return expr.slice.value
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "get"
        and expr.args
        and isinstance(expr.args[0], ast.Constant)
        and isinstance(expr.args[0].value, str)
    ):
        return expr.args[0].value
    return None


def _has_nonempty_str_literal(expr: ast.AST) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str) and expr.value.strip():
        return expr.value
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        for elt in expr.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str) and elt.value.strip():
                return elt.value
    return None


def _check_book_id_branching(tree: ast.AST, file_rel: str, violations: list[dict[str, Any]]) -> None:
    suspect_identifiers = {"book_id", "author_or_editor", "archive_id"}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.IfExp)):
            continue
        for sub in ast.walk(node.test):
            if not isinstance(sub, ast.Compare):
                continue
            sides = [sub.left, *sub.comparators]
            names = {_extract_target_var_name(s) for s in sides}
            matched_vars = names & suspect_identifiers
            if not matched_vars:
                continue
            lit = next((val for s in sides if (val := _has_nonempty_str_literal(s)) is not None), None)
            if lit is not None and lit not in {"book"}:
                _add_violation(
                    violations,
                    "book_id_branching",
                    file_rel,
                    f"Forbidden hardcoded conditional on `{next(iter(matched_vars))}` == {lit!r}.",
                    getattr(sub, "lineno", None),
                )


def _is_broad_exception_type(exc_type: ast.AST | None) -> bool:
    if exc_type is None:
        return True
    if isinstance(exc_type, ast.Name):
        return exc_type.id in {"Exception", "BaseException"}
    if isinstance(exc_type, ast.Tuple):
        return any(_is_broad_exception_type(el) for el in exc_type.elts)
    return False


def _is_noop_body(stmts: list[ast.stmt]) -> bool:
    if not stmts:
        return True
    return all(
        isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        for s in stmts
    )


def _check_silent_exceptions(tree: ast.AST, file_rel: str, violations: list[dict[str, Any]]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            _add_violation(
                violations,
                "silent_exception_swallow",
                file_rel,
                "Bare `except:` handler is forbidden; catch specific typed exceptions.",
                node.lineno,
            )
        elif _is_broad_exception_type(node.type) and _is_noop_body(node.body):
            _add_violation(
                violations,
                "silent_exception_swallow",
                file_rel,
                "Silent `except Exception: pass` is forbidden; record a typed finding/error or propagate.",
                node.lineno,
            )


def _extract_imports(tree: ast.AST, current_mod: str) -> list[tuple[str, int]]:
    imports: list[tuple[str, int]] = []
    curr_pkg_parts = current_mod.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level > 0:
                prefix = curr_pkg_parts[: max(0, len(curr_pkg_parts) - node.level + 1)]
                base = ".".join([*prefix, base]).strip(".")
            if base:
                imports.append((base, node.lineno))
                for alias in node.names:
                    if alias.name != "*":
                        imports.append((f"{base}.{alias.name}", node.lineno))
    return imports


def _check_imports_and_boundaries(
    tree: ast.AST,
    mod_name: str,
    subpkg: str,
    file_rel: str,
    known_modules: set[str],
    dep_graph: dict[str, set[str]],
    violations: list[dict[str, Any]],
) -> None:
    src_rank = LAYER_RANKS.get(subpkg, -1)
    for target, lineno in _extract_imports(tree, mod_name):
        top_pkg = target.split(".")[0]
        if top_pkg in {"google", "genai", "openai", "anthropic"} and subpkg != "providers":
            _add_violation(
                violations,
                "forbidden_sdk_import",
                file_rel,
                f"Only `lanterns.providers` may import VLM SDK `{target}` (found in `{subpkg}`).",
                lineno,
            )
        if top_pkg == "httpx" and subpkg not in {"providers", "source"}:
            _add_violation(
                violations,
                "forbidden_sdk_import",
                file_rel,
                f"HTTP client `httpx` is restricted to `providers` and `source` (found in `{subpkg}`).",
                lineno,
            )
        if not target.startswith("lanterns."):
            continue
        parts = target.split(".")
        if len(parts) < 2:
            continue
        dst_subpkg = "cli" if parts[1] == "cli" else parts[1]
        dst_rank = LAYER_RANKS.get(dst_subpkg, -1)
        if subpkg == "core" and dst_subpkg != "core":
            _add_violation(
                violations,
                "layer_import_violation",
                file_rel,
                f"`lanterns.core` must remain a leaf package; cannot import `{target}`.",
                lineno,
            )
        elif src_rank >= 0 and dst_rank >= 0 and dst_rank > src_rank:
            _add_violation(
                violations,
                "layer_import_violation",
                file_rel,
                f"Layer DAG violation: `{subpkg}` (rank {src_rank}) cannot import `{dst_subpkg}` (rank {dst_rank}) via `{target}`.",
                lineno,
            )
        for cand_len in range(len(parts), 1, -1):
            cand_mod = ".".join(parts[:cand_len])
            if cand_mod in known_modules and cand_mod != mod_name:
                dep_graph.setdefault(mod_name, set()).add(cand_mod)
                break


def _detect_graph_cycles(dep_graph: dict[str, set[str]]) -> list[list[str]]:
    visited: set[str] = set()
    active_stack: list[str] = []
    active_set: set[str] = set()
    cycles: list[list[str]] = []

    def _dfs(node: str) -> None:
        visited.add(node)
        active_stack.append(node)
        active_set.add(node)
        for nxt in sorted(dep_graph.get(node, set())):
            if nxt not in visited:
                _dfs(nxt)
            elif nxt in active_set:
                idx = active_stack.index(nxt)
                cycles.append([*active_stack[idx:], nxt])
        active_stack.pop()
        active_set.remove(node)

    for mod in sorted(dep_graph):
        if mod not in visited:
            _dfs(mod)
    return cycles


def _check_prompts_budget(repo: RepoPaths, violations: list[dict[str, Any]]) -> dict[str, int]:
    if not repo.prompts_dir.is_dir():
        return {"prompt_files": 0, "total_prompt_words": 0}
    total_words = 0
    files = sorted(repo.prompts_dir.glob("*.md"))
    for pfile in files:
        rel = pfile.relative_to(repo.root).as_posix()
        txt = pfile.read_text(encoding="utf-8")
        words = txt.split()
        w_cnt = len(words)
        total_words += w_cnt
        if w_cnt > MAX_PROMPT_WORDS_PER_FILE:
            _add_violation(
                violations,
                "prompt_bloat",
                rel,
                f"Prompt `{rel}` has {w_cnt} words, exceeding single-prompt cap {MAX_PROMPT_WORDS_PER_FILE}.",
            )
        lower_tokens = {w.lower() for w in _WORD_EXTRACT_RE.findall(txt)}
        hits = sorted(lower_tokens & PROMPT_BANNED_PROPER_NOUNS)
        if hits:
            _add_violation(
                violations,
                "prompt_proper_noun_patch",
                rel,
                f"Global prompt `{rel}` contains corpus-specific proper noun(s) {hits}.",
            )
    if total_words > MAX_PROMPT_WORDS_TOTAL:
        _add_violation(
            violations,
            "prompt_bloat",
            "prompts/",
            f"Total prompt budget {total_words} words exceeds ceiling {MAX_PROMPT_WORDS_TOTAL}.",
        )
    return {"prompt_files": len(files), "total_prompt_words": total_words}


def _audit_functions_and_renderers(
    tree: ast.AST,
    raw_src: str,
    pf_name: str,
    subpkg: str,
    rel_repo: str,
    violations: list[dict[str, Any]],
) -> int:
    if subpkg == "renderers" and pf_name != "doctor.py":
        for sink in _UNSAFE_DOM_SINKS:
            if sink in raw_src:
                _add_violation(violations, "unsafe_dom_sink", rel_repo, f"Unsafe DOM sink `{sink}` in `{rel_repo}`.")

    max_comp = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        comp = compute_function_complexity(node)
        max_comp = max(max_comp, comp)
        if comp > MAX_FUNCTION_COMPLEXITY:
            _add_violation(
                violations,
                "cyclomatic_complexity_exceeded",
                rel_repo,
                f"Function `{node.name}` has McCabe complexity {comp} > {MAX_FUNCTION_COMPLEXITY}.",
                node.lineno,
            )
        if subpkg == "renderers" and node.name in FORBIDDEN_RENDERER_DEFS:
            _add_violation(
                violations,
                "renderer_domain_reimplementation",
                rel_repo,
                f"Renderer `{rel_repo}` re-implements compiler logic `{node.name}`.",
                node.lineno,
            )
    return max_comp


def _check_subsystem_budgets(
    subsystem_modules: dict[str, int],
    subsystem_branches: dict[str, int],
    violations: list[dict[str, Any]],
) -> None:
    for sub_name, mod_cap in MAX_SUBSYSTEM_MODULES.items():
        if subsystem_modules.get(sub_name, 0) > mod_cap:
            _add_violation(
                violations,
                "subsystem_module_budget_exceeded",
                f"src/lanterns/{sub_name}",
                f"Subsystem `{sub_name}` has {subsystem_modules[sub_name]} modules > budget {mod_cap}.",
            )
    for sub_name, br_cap in MAX_SUBSYSTEM_BRANCHES.items():
        if subsystem_branches.get(sub_name, 0) > br_cap:
            _add_violation(
                violations,
                "subsystem_branch_budget_exceeded",
                f"src/lanterns/{sub_name}",
                f"Subsystem `{sub_name}` has {subsystem_branches[sub_name]} conditional branches > budget {br_cap}.",
            )


def run_architecture_guard(repo_root: str | Path | None = None) -> dict[str, Any]:
    """Audit `src/lanterns`, `prompts/`, and `benchmarks/` against all 7 structural anti-bloat laws."""
    repo = RepoPaths.from_root(repo_root)
    src_pkg = repo.root / "src" / "lanterns"
    violations: list[dict[str, Any]] = []
    if not src_pkg.is_dir():
        return {"ok": True, "violation_count": 0, "violations": [], "metrics": {}}

    py_files = sorted(src_pkg.rglob("*.py"))
    known_modules: set[str] = set()
    mod_by_path: dict[Path, str] = {}
    for pf in py_files:
        rel_parts = pf.relative_to(src_pkg).parts
        stem_parts = rel_parts[:-1] if rel_parts[-1] == "__init__.py" else (*rel_parts[:-1], pf.stem)
        mod_name = ".".join(("lanterns", *stem_parts))
        known_modules.add(mod_name)
        mod_by_path[pf] = mod_name

    dep_graph: dict[str, set[str]] = {m: set() for m in known_modules}
    subsystem_modules: dict[str, int] = {}
    subsystem_branches: dict[str, int] = {}
    max_complexity_seen = 0

    for pf in py_files:
        rel_repo = pf.relative_to(repo.root).as_posix()
        subpkg = _subpackage_of(pf.relative_to(src_pkg).parts)
        raw_src = pf.read_text(encoding="utf-8")
        tree = ast.parse(raw_src, filename=rel_repo)

        subsystem_modules[subpkg] = subsystem_modules.get(subpkg, 0) + 1
        subsystem_branches[subpkg] = subsystem_branches.get(subpkg, 0) + _count_module_branch_nodes(tree)

        _check_content_heuristics(tree, subpkg, rel_repo, violations)
        _check_book_id_branching(tree, rel_repo, violations)
        _check_silent_exceptions(tree, rel_repo, violations)
        _check_imports_and_boundaries(tree, mod_by_path[pf], subpkg, rel_repo, known_modules, dep_graph, violations)
        comp = _audit_functions_and_renderers(tree, raw_src, pf.name, subpkg, rel_repo, violations)
        max_complexity_seen = max(max_complexity_seen, comp)

    _check_subsystem_budgets(subsystem_modules, subsystem_branches, violations)
    for cyc in _detect_graph_cycles(dep_graph):
        _add_violation(violations, "import_cycle", "src/lanterns", f"Import cycle detected: {' -> '.join(cyc)}")

    prompt_metrics = _check_prompts_budget(repo, violations)
    exam_check = evaluate_epistolary_exam(repo.root)
    if exam_check["manifest_present"] and not exam_check["ok"]:
        for fail in exam_check["failures"]:
            _add_violation(violations, "epistolary_exam_guard", "benchmarks/epistolary_exam", str(fail["message"]))

    return {
        "ok": len(violations) == 0,
        "violation_count": len(violations),
        "violations": violations,
        "metrics": {
            "max_function_complexity": max_complexity_seen,
            "subsystem_modules": subsystem_modules,
            "subsystem_branches": subsystem_branches,
            **prompt_metrics,
        },
    }


def _render_sc_visual_plain(blk: Any) -> str:
    chars = list(blk.plain_text)
    for sp in blk.spans:
        if sp.tag == "sc":
            for idx in range(max(0, sp.start), min(len(chars), sp.end)):
                chars[idx] = chars[idx].upper()
    return "".join(chars)


def _normalize_typographic_spacing(text: str) -> str:
    collapsed = re.sub(r"\[\s+", "[", text)
    collapsed = re.sub(r"\s+\]", "]", collapsed)
    collapsed = re.sub(r"(?<=[a-z])-(?=[a-z])", "", collapsed)
    return re.sub(r"\s+", " ", collapsed)


def _token_present_in_surfaces(token: str, surfaces: tuple[str, str, str, str]) -> bool:
    if token in surfaces[0] or token in surfaces[1]:
        return True
    norm_tok = _normalize_typographic_spacing(token)
    return norm_tok in surfaces[2] or norm_tok in surfaces[3]


def _normalize_exam_folio(val: Any) -> str | None:
    if val is None:
        return None
    txt = re.sub(r"</?unclear>", "", str(val), flags=re.IGNORECASE).strip()
    txt = re.sub(r"^(?:p\.?|page)\s*", "", txt, flags=re.IGNORECASE).strip(" []().—–-")
    return txt or None


def _score_single_exam_case(
    case: dict[str, Any],
    expect_doc: dict[str, Any],
    pred_page: PageIR | None,
    *,
    cid: str = "",
    require_prediction: bool = False,
) -> tuple[bool, list[str]]:
    if pred_page is None:
        return (False, [f"missing prediction {cid}.json"]) if require_prediction else (True, [])
    ast_out = compile_collection_ast([pred_page], book_id=str(case.get("book_id") or "exam"))
    reasons: list[str] = []

    if "expected_letter_count" in expect_doc and len(ast_out.letters) != int(expect_doc["expected_letter_count"]):
        reasons.append(f"letters={len(ast_out.letters)} != expected {expect_doc['expected_letter_count']}")
    if "min_letters" in expect_doc and len(ast_out.letters) < int(expect_doc["min_letters"]):
        reasons.append(f"letters={len(ast_out.letters)} < min_letters {expect_doc['min_letters']}")

    if "acceptable_printed_folios" in expect_doc or "expected_printed_folio" in expect_doc:
        raw_acc = expect_doc.get("acceptable_printed_folios")
        if raw_acc is None:
            raw_acc = [expect_doc.get("expected_printed_folio")]
        acc_norm = {_normalize_exam_folio(v) for v in raw_acc}
        if _normalize_exam_folio(pred_page.printed_folio) not in acc_norm:
            reasons.append(f"printed_folio={pred_page.printed_folio!r} not in acceptable {raw_acc!r}")

    full_plain = "\n".join(b.plain_text for b in pred_page.blocks)
    sc_visual = "\n".join(_render_sc_visual_plain(b) for b in pred_page.blocks)
    surfaces = (
        full_plain,
        sc_visual,
        _normalize_typographic_spacing(full_plain),
        _normalize_typographic_spacing(sc_visual),
    )
    for token in expect_doc.get("must_contain_exact") or expect_doc.get("verbatim_substrings") or []:
        if not _token_present_in_surfaces(str(token), surfaces):
            reasons.append(f"missing verbatim token {token!r}")

    fn_labels = {fn.label for fn in ast_out.footnotes.values()}
    for lbl in expect_doc.get("expected_footnote_labels") or []:
        if str(lbl) not in fn_labels:
            reasons.append(f"missing footnote label {lbl!r}")

    if expect_doc.get("expect_margin_note") and not any(b.type == "margin_note" for b in pred_page.blocks):
        reasons.append("expected margin_note block missing")
    if expect_doc.get("expect_editorial") and not any(b.type == "editorial" for b in pred_page.blocks):
        reasons.append("expected editorial block missing")
    return len(reasons) == 0, reasons


def _evaluate_exam_cases(
    cases: list[dict[str, Any]],
    edir: Path,
    pdir: Path | None,
) -> tuple[dict[str, dict[str, int]], list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    split_ids: dict[str, set[str]] = {"tuning": set(), "held_out": set()}
    split_stats: dict[str, dict[str, int]] = {
        "tuning": {"total": 0, "passed": 0},
        "held_out": {"total": 0, "passed": 0},
    }

    for idx, case in enumerate(cases):
        cid = str(case.get("case_id") or case.get("id") or f"case_{idx + 1}")
        split = str(case.get("split") or "")
        if split not in {"tuning", "held_out"}:
            failures.append({"case_id": cid, "message": f"Invalid split {split!r} on case {cid}"})
            continue
        split_ids[split].add(cid)
        split_stats[split]["total"] += 1

        exp_rel = case.get("expectation_file") or f"expectations/{cid}.json"
        exp_path = edir / str(exp_rel)
        expect_doc = json.loads(exp_path.read_text(encoding="utf-8")) if exp_path.is_file() else dict(case)

        if pdir is None:
            continue
        pred_file = pdir / f"{cid}.json"
        pred_page = PageIR.model_validate_json(pred_file.read_text(encoding="utf-8")) if pred_file.is_file() else None
        passed, reasons = _score_single_exam_case(case, expect_doc, pred_page, cid=cid, require_prediction=True)
        if passed:
            split_stats[split]["passed"] += 1
        else:
            failures.append({"case_id": cid, "split": split, "message": "; ".join(reasons)})

    overlap = sorted(split_ids["tuning"] & split_ids["held_out"])
    if overlap:
        failures.append({"case_id": overlap[0], "message": f"Tuning/held_out split leakage on {overlap}"})
    if not split_ids["tuning"] or not split_ids["held_out"]:
        failures.append({"case_id": "manifest", "message": "Exam manifest must populate both 'tuning' and 'held_out' splits."})
    return split_stats, failures


def evaluate_epistolary_exam(
    repo_root: str | Path | None = None,
    *,
    exam_dir: str | Path | None = None,
    predictions_dir: str | Path | None = None,
    require_predictions: bool = False,
) -> dict[str, Any]:
    """Validate and score `benchmarks/epistolary_exam/` across disjoint `tuning` and `held_out` splits."""
    repo = RepoPaths.from_root(repo_root)
    edir = Path(exam_dir) if exam_dir is not None else (repo.benchmarks_dir / "epistolary_exam")
    manifest_path = edir / "manifest.json"
    pdir = Path(predictions_dir) if predictions_dir is not None else None
    if not manifest_path.is_file():
        def_rate = None if pdir is None else 1.0
        return {
            "ok": not require_predictions,
            "manifest_present": False,
            "vacuous": pdir is None,
            "mode": "manifest_only" if pdir is None else "predictions",
            "tuning": {"total": 0, "passed": 0, "pass_rate": def_rate},
            "held_out": {"total": 0, "passed": 0, "pass_rate": def_rate},
            "generalization_gap": None if pdir is None else 0.0,
            "failures": [],
        }

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = manifest.get("cases") or manifest.get("pages") or []
    split_stats, failures = _evaluate_exam_cases(cases, edir, pdir)

    if pdir is None:
        if require_predictions:
            failures.append(
                {
                    "case_id": "predictions_dir",
                    "message": "Refusing vacuous benchmark run: pass --predictions-dir with PageIR predictions.",
                }
            )
        return {
            "ok": len(failures) == 0,
            "manifest_present": True,
            "vacuous": True,
            "mode": "manifest_only",
            "tuning": {**split_stats["tuning"], "pass_rate": None},
            "held_out": {**split_stats["held_out"], "pass_rate": None},
            "generalization_gap": None,
            "failures": failures,
        }

    t_tot = max(1, split_stats["tuning"]["total"])
    h_tot = max(1, split_stats["held_out"]["total"])
    t_rate = round(split_stats["tuning"]["passed"] / t_tot, 4) if split_stats["tuning"]["total"] else 0.0
    h_rate = round(split_stats["held_out"]["passed"] / h_tot, 4) if split_stats["held_out"]["total"] else 0.0
    gap = round(t_rate - h_rate, 4)
    if gap > 0.25:
        failures.append({"case_id": "generalization_gap", "message": f"Overfitting detected: tuning={t_rate} vs held_out={h_rate} (gap={gap})"})

    return {
        "ok": len(failures) == 0,
        "manifest_present": True,
        "vacuous": False,
        "mode": "predictions",
        "tuning": {**split_stats["tuning"], "pass_rate": t_rate},
        "held_out": {**split_stats["held_out"], "pass_rate": h_rate},
        "generalization_gap": gap,
        "failures": failures,
    }
