# Starship OS — Design Language

> Commonwealth Andromeda / Highguard inspired system interface.
> A local-first AI agent operating system, architected like a starship bridge.
> Every display is a mission-critical console. Every interaction is a command.

## 1. Visual Theme & Atmosphere

A **starship bridge command environment** — the bridge of a 3rd-generation
Commonwealth High Guard vessel. Deep space darkness punctuated by holographic
data readouts, warm brass-and-gold authority accents, and blue-cyan holographic
projections. Every element serves the mission; decorative exists only as
functional artifact.

| Token | Hex | Role |
|-------|-----|------|
| Background | `#0A0E1A` | Deep space canvas, primary bridge depth |
| Surface | `#0D1B2A` | Console panels, card surfaces |
| SurfaceAlt | `#122233` | Elevated panels, active consoles |
| Border | `#1B2D45` | Panel separation, frame structure |
| Primary | `#00D4FF` | Holographic readouts, active data, AI presence |
| PrimaryDim | `#0088AA` | Dimmed/dormant holographic elements |
| Accent | `#D4A843` | Highguard authority accents, rank indicators |
| AccentDim | `#8B6F30` | Muted gold for secondary authoritative elements |
| Text | `#E8E0D0` | Commonwealth warm white, primary body |
| TextDim | `#8899AA` | Secondary data, metadata |
| Success | `#00CC88` | Systems nominal, agent online |
| Warning | `#FF8C00` | Caution, degraded operations |
| Alert | `#FF3355` | Critical, fault, security breach |

*Every reading must be legible at a glance from the command chair.*

### Visual Archetypes

**Andromeda Ascendant (ship aesthetic)**
- Sleek, elongated forms with organic-curve accents
- Holographic blue-cyan projections as primary data surface
- Warm ambient lighting against cold space
- Systems feel alive — subtle data pulses, sensor sweeps
- Commonwealth elegance: refined, powerful, principled

**Highguard (authority/naval aesthetic)**
- Clean hierarchical information architecture
- Gold/brass signaling rank and authority
- Structured command chains visible in every interface
- Tactical data organized by priority, not alphabetically
- Everything has a designated station

### Prior Art

Andromeda Ascendant bridge displays (Commonwealth data systems),
Halo UNSC tactical interfaces, Star Trek LCARS (informational hierarchy),
modern fighter jet glass cockpits. All share: dark-adapted canvases,
high-information density, color-coded severity, and role-based console layouts.

## 2. Color Palette & Roles

### Surface Palette

| Token | Hex | Usage |
|-------|-----|-------|
| Background | `#0A0E1A` | Root canvas, bridge ambient |
| Surface | `#0D1B2A` | Panel surfaces, cards, widgets |
| SurfaceAlt | `#122233` | Active console, hover state, selected panel |
| Border | `#1B2D45` | Frame dividers, grid lines |
| BorderLight | `#2A4060` | Emphasis borders, active frame |

### Data Palette

| Token | Hex | Usage |
|-------|-----|-------|
| Primary | `#00D4FF` | Active data, agent messages, live values |
| PrimaryDim | `#0088AA` | Inactive data, historical readouts |
| Accent | `#D4A843` | Command indicators, rank markers, highlights |
| AccentDim | `#8B6F30` | Secondary authority indicators |
| Success | `#00CC88` | Online status, healthy metrics |
| Warning | `#FF8C00` | Degraded state, attention required |
| Alert | `#FF3355` | Critical fault, security event |

### Text Palette

| Token | Hex | Usage |
|-------|-----|-------|
| Text | `#E8E0D0` | Primary body, headings |
| TextDim | `#8899AA` | Labels, metadata, captions |
| TextBright | `#FFFFFF` | Emphasis, active command text |

All foreground colors on `#0A0E1A` pass WCAG AA (4.5:1 minimum).

### Dark Mode

Dark mode is the only mode. Starship bridges operate in low-light combat
conditions. No light mode exists by design.

```css
:root {
  --color-bg: #0A0E1A;
  --color-surface: #0D1B2A;
  --color-surface-alt: #122233;
  --color-border: #1B2D45;
  --color-border-light: #2A4060;
  --color-primary: #00D4FF;
  --color-primary-dim: #0088AA;
  --color-accent: #D4A843;
  --color-accent-dim: #8B6F30;
  --color-text: #E8E0D0;
  --color-text-dim: #8899AA;
  --color-text-bright: #FFFFFF;
  --color-success: #00CC88;
  --color-warning: #FF8C00;
  --color-alert: #FF3355;
}
```

## 3. Typography

| Role | Size | Weight | Line Ht | Font |
|------|------|--------|---------|------|
| Display | 32px | 700 | 1.1 | "Orbitron", "Exo 2", Inter, sans-serif |
| Heading | 16px | 600 | 1.2 | "Inter", "Exo 2", sans-serif |
| Subheading | 13px | 600 | 1.3 | "Inter", sans-serif, uppercase, 0.08em tracking |
| Body | 14px | 400 | 1.5 | "Inter", sans-serif |
| Data | 20px | 700 | 1.0 | "JetBrains Mono", "Fira Code", monospace |
| DataSmall | 13px | 500 | 1.2 | "JetBrains Mono", "Fira Code", monospace |
| Label | 11px | 600 | 1.0 | "Inter", sans-serif, uppercase, 0.1em tracking |
| Micro | 10px | 500 | 1.0 | "Inter", sans-serif, uppercase, 0.08em tracking |

**Font stack for system use:**
```
Display: "Orbitron", "Exo 2", "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif
Heading: "Inter", "Exo 2", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif
Body: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif
Mono: "JetBrains Mono", "Fira Code", "Cascadia Code", "SF Mono", Menlo, Consolas, monospace
```

### Typography Principles

- Display weights reserved for ship status, agent names, critical metrics
- All-caps with tracking for labels and command categories
- Monospace for all real-time data, telemetry, timeseries
- Never use display fonts for body text
- Line length capped at 72ch for readability in command panels

## 4. Component Language

### Console Panel

Base building block — every UI surface is a console panel.

```css
.console-panel {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  padding: 16px;
  position: relative;
}
.console-panel::before {
  content: '';
  position: absolute;
  inset: -1px;
  border-radius: 5px;
  border: 1px solid transparent;
  border-image: linear-gradient(135deg, var(--color-border-light) 0%, transparent 50%) 1;
  pointer-events: none;
}
.console-panel:focus-within {
  border-color: var(--color-primary-dim);
  box-shadow: 0 0 8px rgba(0, 212, 255, 0.08);
}
```

### Holo Data Readout

Live data value with label, mimicking a holographic projection.

```css
.holo-readout {
  font-family: "JetBrains Mono", monospace;
  font-size: 20px;
  font-weight: 700;
  color: var(--color-primary);
  letter-spacing: 0.02em;
  text-shadow: 0 0 6px rgba(0, 212, 255, 0.15);
}
.holo-readout.dim {
  color: var(--color-primary-dim);
  text-shadow: none;
}
.holo-readout.success {
  color: var(--color-success);
  text-shadow: 0 0 6px rgba(0, 204, 136, 0.15);
}
.holo-readout.warning {
  color: var(--color-warning);
  text-shadow: 0 0 6px rgba(255, 140, 0, 0.2);
}
.holo-readout.alert {
  color: var(--color-alert);
  text-shadow: 0 0 8px rgba(255, 51, 85, 0.25);
  animation: pulse-alert 1.5s ease-in-out infinite;
}
@keyframes pulse-alert {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}
```

### Status Indicator

Quad-state agent/service status indicator.

```css
.status-indicator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-text-dim);
}
.status-dot.online { background: var(--color-success); box-shadow: 0 0 6px rgba(0, 204, 136, 0.4); }
.status-dot.busy { background: var(--color-primary); box-shadow: 0 0 6px rgba(0, 212, 255, 0.4); }
.status-dot.warning { background: var(--color-warning); box-shadow: 0 0 6px rgba(255, 140, 0, 0.4); }
.status-dot.offline { background: var(--color-alert); box-shadow: 0 0 6px rgba(255, 51, 85, 0.4); }
```

### Command Button

Highguard-style action triggers — authoritative without aggression.

```css
.btn {
  font-family: "Inter", sans-serif;
  font-size: 13px;
  font-weight: 600;
  padding: 8px 20px;
  border-radius: 3px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  cursor: pointer;
  transition: all 120ms ease-out;
  letter-spacing: 0.03em;
}
.btn:hover {
  border-color: var(--color-primary-dim);
  color: var(--color-primary);
  background: var(--color-surface-alt);
}
.btn:active {
  transform: scale(0.97);
}
.btn-primary {
  background: var(--color-primary-dim);
  border-color: var(--color-primary);
  color: var(--color-text-bright);
}
.btn-primary:hover {
  background: var(--color-primary);
  color: #000;
}
.btn-accent {
  border-color: var(--color-accent);
  color: var(--color-accent);
}
.btn-accent:hover {
  background: rgba(212, 168, 67, 0.1);
  border-color: var(--color-accent);
}
```

### Data Grid

Telemetry and metric displays — clean, scan-optimized.

```css
.data-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
}
.data-cell {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 3px;
  padding: 12px;
  text-align: center;
}
.data-cell .label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--color-text-dim);
  margin-bottom: 4px;
}
.data-cell .value {
  font-family: "JetBrains Mono", monospace;
  font-size: 18px;
  font-weight: 700;
  color: var(--color-primary);
}
```

### Agent Chat Message

In-character agent communications styled as bridge communiqués.

```css
.comm-message {
  padding: 12px 16px;
  margin-bottom: 8px;
  border-radius: 4px;
  border-left: 3px solid var(--color-border);
  background: var(--color-surface);
}
.comm-message.ergo {
  border-left-color: var(--color-accent);
}
.comm-message.romi {
  border-left-color: var(--color-primary);
}
.comm-message.proxy {
  border-left-color: var(--color-text-dim);
}
.comm-header {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--color-text-dim);
  margin-bottom: 4px;
}
.comm-header .rank {
  color: var(--color-accent);
  margin-right: 8px;
}
.comm-body {
  font-size: 14px;
  line-height: 1.5;
  color: var(--color-text);
}
```

## 5. Layout System

### Bridge Console Grid

Pages follow a **command bridge layout** — a central primary viewport flanked
by contextual data panels, with a status bar at top and action rail at bottom.

```
┌──────────────────────────────────────────────┐
│  Status Bar — Ship Name / Mission / Time     │
├──────────┬───────────────────────┬───────────┤
│ Console  │   PRIMARY VIEWPORT    │ Console   │
│ Panel    │   (Agent Chat /       │ Panel     │
│ (Agents) │    Dashboard /        │ (Telemetry│
│          │     Design View)      │  / Logs)  │
│          │                       │           │
├──────────┴───────────────────────┴───────────┤
│  Action Rail — Quick Commands / Navigation   │
└──────────────────────────────────────────────┘
```

### Spacing Scale

| Token | Value |
|-------|-------|
| Space-2xs | 4px |
| Space-xs | 8px |
| Space-sm | 12px |
| Space-md | 16px |
| Space-lg | 24px |
| Space-xl | 32px |
| Space-2xl | 48px |

### Grid

- 12-column responsive grid
- Max content width: 1280px (bridge main viewport)
- Gutter: 16px
- Panels snap to column multiples

## 6. Depth & Glow

Bridge interfaces use **simulated holographic projection** rather than
material drop shadows.

```css
.holo-glow {
  box-shadow:
    0 0 4px rgba(0, 212, 255, 0.06),
    0 0 12px rgba(0, 212, 255, 0.03);
}
.holo-glow-accent {
  box-shadow:
    0 0 4px rgba(212, 168, 67, 0.08),
    0 0 12px rgba(212, 168, 67, 0.03);
}
```

Elevation is expressed through:
- Brighter border on active/focused panels (`var(--color-border-light)`)
- Subtle glow on primary data elements
- Slightly lighter surface for active consoles (`var(--color-surface-alt)`)
- Never use `box-shadow` for depth; use borders and glow

## 7. Motion

| Transition | Duration | Easing |
|------------|----------|--------|
| Panel enter | 200ms | cubic-bezier(0.23, 1, 0.32, 1) |
| Panel exit | 140ms | cubic-bezier(0.23, 1, 0.32, 1) |
| Data value change | 100ms | linear |
| Status transition | 150ms | ease-out |
| Hover | 120ms | ease-out |

### Animation Principles

- Data values update instantly (linear, 100ms) — no latency on telemetry
- Panels fade in on mount, never slide
- Alert states pulse at 1.5s interval — never faster
- No decorative animation, no parallax, no confetti
- Motion communicates state change only

## 8. Do's and Don'ts

- Do use `var(--color-primary)` for all active holographic data
- Do use `var(--color-accent)` sparingly — Highguard gold signals authority
- Do keep text at WCAG AA minimum 4.5:1 on `#0A0E1A`
- Do use monospace for ALL real-time data values
- Do maintain the bridge metaphor: panels = consoles, data = telemetry
- Do use uppercase + tracking for labels and navigation
- Do pulse alert states — don't flash them
- Do not provide a light mode — bridges are dark-adapted
- Do not use rounded corners above 8px — bridge panels are precision-machined
- Do not use drop shadows — use glow for depth
- Do not animate data values (instant updates only)
- Do not use decorative illustrations or icons — data is the decoration
- Do not convey information by color alone — reinforce with icon or position

## 9. Responsive Behavior

- **Desktop (1024+):** Full bridge layout — three-column viewport
- **Tablet (768-1023):** Two-column — primary viewport + stacked consoles
- **Mobile (<768):** Single-column — primary viewport with drawer panels
- Status bar collapses to icon-only on mobile
- Action rail becomes bottom tab bar on mobile
- Data grids reflow to 2-column minimum
- Agent chat becomes full-width on all sizes

## 10. Agent Persona Integration

Each agent's visual identity derives from their role on the bridge:

### Ergo — The Captain / Strategic AI
- Visual signal: Highguard gold accent
- Status color: `var(--color-accent)`
- Border marker: gold left-border on messages
- Typography: Display-weight Orbitron for nameplate
- Console: Primary center viewport, command-level access

### Romi — The First Officer / UX AI
- Visual signal: Holographic cyan
- Status color: `var(--color-primary)`
- Border marker: cyan left-border on messages
- Typography: Clean Inter, approachable weight
- Console: User-facing interaction panel

### Proxy — The Operations / Engineering AI
- Visual signal: Neutral warm white
- Status color: `var(--color-text-dim)`
- Border marker: dim left-border on messages
- Typography: Monospace for diagnostic output
- Console: Systems and telemetry panel

### StarAgent — The Ship's Systems Monitor
- Visual signal: Success green
- Status color: `var(--color-success)`
- Icon: Crosshair or targeting reticle
- Console: Telemetry and metrics dashboard

## 11. Iconography

- Use a minimal subset of clear, geometric icons
- Prefer communication/military/ship symbology
- All icons outlined (no filled variants) at 16-20px
- Stroke width: 1.5px
- Color inherits from parent context
- Agent icons: simple geometric glyphs (circle, diamond, triangle, square)
- Status icons: dots and rings only

## 12. Design System Artifacts

The following outputs should be generated from this spec:

- `tokens.css` — CSS custom properties for all colors, fonts, spacing
- Prebuilt Cinnamon desklet with this theme
- Web dashboard styled with these tokens
- GTK4 CSS nodes matching this palette
- System tray icons in Monochrome + accent variants

## 13. Agent Prompt Guide

When generating Starship OS interfaces, prompt the model to:

- Set `--color-bg` to `#0A0E1A` (deep space) as the root background
- Use Orbitron or Inter for display text; JetBrains Mono or Fira Code for data
- Apply `#00D4FF` (cyan) as primary holographic color for all active data
- Use `#D4A843` (gold) only for Highguard authority elements (rank, commands)
- Use `5A9A5A` green (accessibility-adjusted) as secondary reference for grid lines
- Keep all data text in uppercase monospace with 0.02em letter-spacing
- Style agent messages with color-coded left borders per persona
- Use 3px border-radius maximum for interactive elements
- Apply subtle text-shadow on primary data (0 0 6px rgba(0, 212, 255, 0.15))
- Never add decorative animation or light mode variants
- Include a status indicator component with online/busy/warning/offline states
- Ensure all text passes 4.5:1 contrast on `#0A0E1A`
