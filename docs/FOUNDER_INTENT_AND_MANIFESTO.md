# Founder Intent & Manifesto: Why Paper Lanterns Exists

> *"A book is written for the world, in full dress, before a mirror. A letter is written across a kitchen table at midnight to one human being—under the quiet, perilous presumption that no stranger will ever read it."*

---

## 1. Sister to Project Akshara: Returning Our Inheritance to the Commons

**Paper Lanterns** was born alongside **Project Akshara** (*"Project Gutenberg for India"*) out of the same refusal to watch humanity's printed inheritance decay inside crooked, blot-compressed PDF scans. Where Akshara set out to liberate India's neglected histories, economic ledgers, philological treatises, and vernacular classics into living, verified public-domain editions, Paper Lanterns turns that same archival compiler ethic toward a medium that digitization programs across the world have systematically left behind: **correspondence**.

Inspired in spirit by Shaun Usher's *Letters of Note*—which reminded contemporary readers how electrifying a single preserved letter can be—Paper Lanterns asks a larger structural question: *what happens when we liberate not merely an anthology of famous excerpts, but entire multi-volume public-domain correspondence collections, intact from cover to cover and simultaneously navigable letter by individual letter?*

---

## 2. Why Letters Are Unlike Any Other Medium in Human History

For millennia before the telephone, encrypted messaging, or ephemeral email, **almost all human action at a distance passed through letters**:

1. **The Engine of World Affairs, Commerce & Secrecy:**
   Treaties were negotiated, rebellions plotted, scientific discoveries staked, mercantile voyages financed, East India Company factories instructed, famines reported, and covert intelligence passed in folded sheets of rag paper sealed with wax. Letters are simultaneously instruments of **statecraft, economic life, diplomacy, surreptitious communication, intrigue, and betrayal**.
2. **Intimate Confessions Under the Presumption of Privacy:**
   Unlike formal treatises, novels, or memoirs composed for posterity, letters to friends, siblings, spouses, mentors, and lovers were written for an audience of one. Under that presumption of privacy, the public monument disappears and the living, contradictory human being steps forward.
3. **The Unmasked Writer:**
   To read a thinker's or artist's formal books is to meet the persona they curated for history; to read their correspondence is often to meet a **completely different human being**.
   - In **Dostoevsky's** letters—writing to his brother Mikhail hours after standing before a firing squad in Semyonovsky Square, or pleading from debt and epilepsy—we encounter raw terror, gratitude for existence, and vulnerability that illuminate *The Idiot* and *The Brothers Karamazov* from within.
   - In **Nietzsche's** letters to Franz Overbeck, Peter Gast, and Richard Wagner, the thunderbolt aphorist of *Zarathustra* appears lonely, half-blind in boarding houses, worrying over stove smoke, printer proofs, and broken friendships.
   - In **Napoleon's** correspondence with his brother Joseph, state dispatches ordering troop movements sit beside frantic, unguarded domestic vulnerability.
   - In **Tchaikovsky's** letters to Nadezhda von Meck—his patron whom he agreed never to meet in person—music, self-doubt, financial anxiety, and the inner creative process are laid bare with astonishing candor.
   - In **Tolstoy**, **Freud**, **Goethe and Zelter**, **Erasmus**, **Newton and Bentley**, and **Faraday**, we watch ideas while they are still molten—full of false starts, theological doubts, marginal calculations, and human warmth.
   - In **Gandhi's** letters to Sardar Vallabhbhai Patel or Mirabehn, **Tagore's** epistles, **Vivekananda's** urgent informal notes, and early **Indian mercantile and diplomatic correspondence**, we witness a civilization arguing with itself in real time across English, Gujarati, Bengali, Sanskrit, Persian, and Urdu concepts.
   - Just as importantly, alongside great names stand **lesser-known correspondents**—factors, soldiers, translators, mothers, clerks, exiled revolutionaries—whose letters are often the *only* surviving trace that they lived, loved, reasoned, and suffered at all.

Letters are simultaneously **witnesses to historic upheavals** (wars, revolutions, plagues, empires rising and cracking) and **witnesses to the timeless interior drama** of human beings wrestling with mortality, grief, ambition, faith, art, money, and love.

---

## 3. Why Digitization Neglected Correspondence

When mass library scanning swept through university stacks in the 2000s, optical character recognition (OCR) and standard e-book pipelines handled linear novels reasonably well—and **collapsed on printed correspondence**:

- **Typographic Complexity:** A single printed page in a 19th-century *Life and Letters* volume may contain biographer narrative in roman type, three short letters or telegrams in smaller type, outer-margin shoulder chronology (`1831. Æt. 39.`), right-aligned or bottom-placed datelines, four-line cascading 18th-century valedictions, redacted cipher dashes (`M——`, `Count ***`), enclosed inner letters, and two tiers of 6-point footnotes (`a, b, c` textual variants plus `* † ‡` editorial notes).
- **Polyglot & Archaic Orthography:** Correspondence switches mid-sentence from English into Latin, French, German, Greek, Russian transliteration, or Hunterian/IAST Sanskrit and Perso-Arabic terms, while preserving 18th-century medial long-s (`ſ`) and ligatures (`æ`, `œ`).
- **Structural Mismatch:** Traditional e-books lock letters inside massive chapter files where individual letters cannot be searched, cited, filtered by correspondent, or shared via URL; conversely, quote-collection websites strip letters out of their historical books, severing editorial context and footnotes.

Because correspondence was hard to structure automatically and expensive to keyboard by hand, thousands of extraordinary public-domain letter collections remained trapped in degraded scans.

---

## 4. The Four Commitments of Paper Lanterns

### Commitment I — 100% Diplomatic Archival Fidelity (Zero Modernization)
Our North Star is uncompromising: **preserve exactly what the historical edition printed**.
We never modernize `shew` to `show` or `publick` to `public`. We never flatten `Rájput` to `Rajput` or rewrite a printer's French `depôt` to `dépôt`. We never reorder a bottom-left dateline to the top of a letter to satisfy a modern template, and we never silently drop an editorial bridge paragraph or an unlinked footnote. Running page headers, bare page folios, and library stamps are cleanly separated from reading prose—everything else is sacred historical testimony.

### Commitment II — Dual Publication: Whole Books AND Individual Online Letters
Every volume compiled by Paper Lanterns produces **two first-class, mathematically consistent public artifacts** from a single `CollectionAST`:
1. **The Complete Book Edition** (Web HTML, EPUB 3.3, Scholarly Markdown, and TEI P5 XML with `<correspDesc>`): faithful to the physical book from title page and preface through interleaved biographical chapters, chapter-end bidirectional footnotes, appendices, and index.
2. **The Individual Online Letters Edition**: every letter stands on its own permanent URL (`letters/<slug>.html`), complete with its own linked footnote apparatus, preserved spatial alignment, optional collapsible preceding/following biographer framing context, and a fast correspondence catalog filterable by sender, recipient, date, language, and diacritic-insensitive search—paired with a one-click facsimile scan link so any scholar or curious reader can verify the ink on paper.

### Commitment III — Compiler Discipline Over Fragile Heuristics
No two correspondence editions share the same printer conventions. Paper Lanterns treats digitization as an **evidence-backed archival compiler**:
- Multi-model, multi-harness VLM extraction (`google-genai >= 3.2.0` Gemini 4 / 3.8 Flash + `gemini-decision-1` calibrated boundary judgments + Qwen and Claude cross-family fallbacks + native-vision scan checks) produces structured visual observations.
- Deterministic, language-agnostic compiler passes assemble sections, letters, and dual footnote graphs **without a single English content-word regex or book-specific hack**.
- Mathematical validators (`100% source_block_coverage`, bidirectional footnote parity, Unicode/script integrity, compiled XHTML/EPUB/TEI gates) verify every artifact before publication, while strict isolation separates `workspace/experiments/` from canonical `workspace/runs/`.

### Commitment IV — Honest Provenance & Distributed Community Proofreading (`P1 -> P2`)
Frontier vision models carry a difficult 400-page correspondence volume 99%+ of the way to perfection for a couple of dollars—making solo, out-of-pocket public-domain liberation practical today. Yet machine reading is never falsely passed off as human scholarship. Every edition carries an honest four-tier badge (`blocked < community < machine_checked < source_certified`) and ships with an embedded, zero-`innerHTML` **Distributed Proofreaders (`P1 -> P2`) Proofing Studio**, empowering volunteers around the world to check pages side-by-side against Internet Archive scans until two independent human readers certify every leaf—and guaranteeing that no future AI run will ever overwrite human-verified text.

Paper lanterns are small, warm vessels of light carried across dark water. Every letter rescued here is a voice carried intact across centuries, returned freely and permanently to the human commons.
