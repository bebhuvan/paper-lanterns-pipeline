---
trigger: always_on
description: Enforces conveyor-belt compiler discipline, AST anti-bloat laws, 5-plane isolation, and zero-innerHTML security.
---

# Epistolary Compiler & Anti-Bloat Law

1. **Zero Content-Word Regexes or Book Branching:**
   - Never match natural-language prose words (`"Dear"`, `"Yours"`, `"Sincerely"`, month names, or correspondent names) in `src/lanterns/epistolary/` or `src/lanterns/renderers/`.
   - Never write `if book_id == ...` or author-specific conditionals anywhere in `src/lanterns/`.
   - Route strictly on `BlockIR.type`, `epistolary_transition`, `alignment`, `bbox`, `continues_from_prev`/`continues_to_next`, `heading_level`, `note_label`, and `books/<book_id>/letter_map.json`.
2. **Acyclic Layer DAG & Complexity Budgets:**
   - Respect `core -> source -> providers -> extraction -> epistolary -> validation -> renderers -> cli`.
   - Keep per-function cyclomatic complexity `<= 18` and stay within subsystem branch budgets enforced by `lanterns.validation.architecture_guard`.
   - Never swallow exceptions silently (`except Exception: pass` is banned).
3. **Five-Plane Isolation:**
   - Run exploratory/canary tests ONLY with `--kind experiment` (`workspace/experiments/`).
   - Run production editions ONLY with `--kind canonical` (`workspace/runs/` -> `library/`).
4. **Zero-`innerHTML` Security & Documentation Cap:**
   - Never use `innerHTML`, `outerHTML`, or `insertAdjacentHTML` in `renderers/` or `proofing_studio.py`; use `document.createElement` and `textContent` with strict CSP.
   - Never add `.md` files in repo root or `docs/` outside the allowlist checked by `uv run lanterns doctor`.
