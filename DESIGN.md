# Design System Document: High-End Commercial Dashboard

## 1. Overview & Creative North Star
### Creative North Star: "The Architectural Curator"
This design system moves away from the cluttered, "row-and-column" density of legacy dashboards toward a high-end editorial experience. We treat data as content and the dashboard as a curated gallery. By leveraging **Architectural Layering**—using tonal shifts instead of structural lines—we create an interface that feels expansive, authoritative, and breathable.

The system breaks the "template" look through:
*   **Intentional Asymmetry:** Utilizing the 12-column grid to create varied focal points.
*   **Tonal Depth:** Replacing harsh borders with a sophisticated hierarchy of surface colors.
*   **Editorial Typography:** Pairing high-character display faces with ultra-legible body fonts to signal premium quality.

---

## 2. Colors & Surface Philosophy
The palette is a sophisticated blend of deep Navy (`primary`), muted Slate (`secondary`), and high-chroma Status accents.

### The "No-Line" Rule
**Explicit Instruction:** 1px solid borders are prohibited for sectioning. Boundaries must be defined solely through background color shifts. For example, a dashboard widget (`surface_container_lowest`) sits on a workspace background (`surface_container_low`), which sits on the global background (`surface`). This creates a "soft-edge" aesthetic that is easier on the eyes during long sessions.

### Surface Hierarchy & Nesting
Treat the UI as a series of physical layers. Use the following tiers to define importance:
*   **Base Layer (`surface` / `#f8f9fb`):** The global canvas.
*   **Section Layer (`surface_container_low` / `#f3f4f6`):** Groups of related widgets.
*   **Interactive Layer (`surface_container_lowest` / `#ffffff`):** Individual cards, inputs, and primary data modules.
*   **Elevated Layer (`surface_container_highest` / `#e1e2e4`):** Active states or nested elements requiring immediate focus.

### The "Glass & Gradient" Rule
To add "soul" to the commercial data, use **Glassmorphism** for floating elements (e.g., mobile navigation or quick-action menus). Apply `surface` with 80% opacity and a `backdrop-blur` of 12px.
*   **Signature Textures:** For high-impact CTAs, use a linear gradient from `primary` (#003d9b) to `primary_container` (#0052cc) at a 135-degree angle.

---

## 3. Typography
We employ a dual-font strategy to balance character with commercial clarity.

*   **Display & Headlines (Manrope):** A geometric sans-serif with a modern, high-end feel. 
    *   *Role:* Used for "Hero" metrics, page titles, and high-level section headers.
    *   *Identity:* Conveys "The Curator" voice—authoritative and clean.
*   **Body & Labels (Inter):** A workhorse typeface optimized for small-scale legibility.
    *   *Role:* Used for data tables, form fields, and long-form descriptions.
    *   *Identity:* Conveys "The Professional" voice—precise and dependable.

**Hierarchy Tip:** Use `display-md` for primary KPI figures to make them the undeniable focal point of the screen.

---

## 4. Elevation & Depth
Depth is achieved through **Tonal Layering** and physics-based lighting, never through high-contrast outlines.

*   **The Layering Principle:** Place a `surface_container_lowest` card on a `surface_container_low` background. This creates a natural "lift" that mimics fine paper stocks.
*   **Ambient Shadows:** For floating modals or dropdowns, use a 3-layer diffused shadow:
    *   `0px 4px 20px rgba(25, 28, 30, 0.04)`
    *   `0px 12px 40px rgba(25, 28, 30, 0.08)`
*   **The "Ghost Border" Fallback:** If accessibility requires a container edge, use a `Ghost Border`: `outline_variant` (#c3c6d6) at 20% opacity. 
*   **Glassmorphism:** Use semi-transparent `surface_tint` (#0c56d0 at 5% opacity) on white backgrounds to create a "frosted" look for secondary sidebars.

---

## 5. Components

### Buttons
*   **Primary:** Linear gradient (`primary` to `primary_container`), `md` (0.375rem) roundedness, `on_primary` text.
*   **Secondary:** `surface_container_high` background with `on_surface` text. No border.
*   **Tertiary:** Transparent background, `primary` text. Use for low-priority actions.

### Data Cards (The Core Module)
*   **Style:** `surface_container_lowest` background, `xl` (0.75rem) roundedness.
*   **Spacing:** Use `spacing.8` (1.75rem) internal padding to ensure the "Editorial" feel.
*   **Rule:** **No dividers.** Separate header from body using a `surface_container_low` background strip or a simple vertical jump in white space (`spacing.5`).

### Chips (Status Indicators)
*   **Success:** `primary_fixed` background with `on_primary_fixed_variant` text.
*   **Warning/Error:** `tertiary_fixed` background with `on_tertiary_fixed_variant` text.
*   **Shape:** `full` (9999px) roundedness for high contrast against rectangular cards.

### Input Fields
*   **Style:** `surface_container_low` background. 
*   **State:** On focus, transition background to `surface_container_lowest` and apply a 2px `surface_tint` Ghost Border.

---

## 6. Do's and Don'ts

### Do
*   **DO** use whitespace as a functional tool. If the data feels "cramped," increase the padding using the `spacing.10` or `12` tokens.
*   **DO** use `manrope` for numbers in KPIs. It feels more intentional and premium than standard sans-serifs.
*   **DO** stack elements vertically on mobile using a single-column layout with `spacing.4` gutters.

### Don't
*   **DON'T** use 100% black text. Always use `on_surface` (#191c1e) to maintain a sophisticated, softened contrast.
*   **DON'T** use 1px dividers between table rows. Instead, use alternating row tints (`surface` and `surface_container_low`) or simply ample vertical spacing.
*   **DON'T** use "Pure Red" for errors. Use the `error` (#ba1a1a) token, which is tuned for professional environments.
*   **DON'T** crowd the edges. Every screen should have a "Safe Zone" of at least `spacing.8` from the viewport edge.