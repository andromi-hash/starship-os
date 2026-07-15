# Open Design Integration

Open Design is built into Starship OS as a native tool for generating design artifacts — web prototypes, slide decks, mobile mockups, dashboards, and more.

## How It Works

When an agent needs to create a design, it calls the `opendesign` tool. This uses Open Design's 217+ skills and 150+ brand-grade design systems to produce real CSS/HTML artifacts.

```
Agent → opendesign tool → Open Design → coding agent (opencode/hermes) → HTML/PDF/PPTX/MP4
```

## Installation

```bash
# Clone Open Design
git clone https://github.com/nexu-io/open-design.git /opt/open-design

# Install dependencies
cd /opt/open-design && pnpm install

# Start daemon (optional, for API access)
cd /opt/open-design && pnpm tools-dev
```

## Configuration

```yaml
# /etc/agnetic/opendesign.yaml
opendesign:
  dir: /opt/open-design
  daemon_port: 7456
  default_agent: opencode
  default_design_system: linear
  output_dir: /tmp/agnetic-design
```

## Tool Definition

```json
{
  "name": "opendesign",
  "description": "Generate design artifacts using Open Design — web prototypes, slide decks, mobile mockups, dashboards.",
  "parameters": {
    "prompt": "Description of what to design",
    "skill": "web-prototype, slide-deck, dashboard, mobile-app",
    "design_system": "linear, stripe, vercel, notion, airbnb, apple, tesla",
    "output_dir": "/tmp/agnetic-design",
    "agent": "opencode"
  }
}
```

## Available Skills (217+)

| Skill | Description |
|---|---|
| web-prototype | Interactive web prototypes with real CSS |
| slide-deck | HTML presentations with PptxGenJS |
| dashboard | KPI layouts with data tables |
| mobile-app | Mobile UI mockups |
| social-carousel | Instagram/Twitter carousels |
| landing-page | Marketing landing pages |
| invoice | PDF invoice generation |
| threejs | Interactive 3D browser experiences |
| chart | Data visualization |

## Design Systems (150+)

Linear, Stripe, Vercel, Notion, Airbnb, Apple, Tesla, Spotify, GitHub, Supabase, Figma, Cursor, Anthropic, and 137+ more.

Each design system is a 9-section `DESIGN.md` covering:
1. Color palette (OKLCh tokens)
2. Typography (font families, sizes, weights)
3. Spacing and grid rules
4. Component styles (buttons, inputs, cards)
5. Motion and animation
6. Voice and tone
7. Brand identity
8. Anti-patterns
9. Layout patterns

## Usage Examples

### Landing page
```
Agent: opendesign("Create a landing page for Starship OS with hero section, features grid, and CTA", skill="landing-page", design_system="vercel")
```

### Dashboard
```
Agent: opendesign("Design a system monitoring dashboard with CPU, memory, disk, and network graphs", skill="dashboard", design_system="linear")
```

### Slide deck
```
Agent: opendesign("Create a 10-slide investor pitch deck for Starship OS", skill="slide-deck", design_system="stripe")
```

### Mobile app
```
Agent: opendesign("Design a mobile chat interface for the Starship OS agent", skill="mobile-app", design_system="notion")
```

## Output Formats

- **HTML** — Interactive web prototypes
- **PDF** — Exported documents
- **PPTX** — PowerPoint presentations
- **MP4** — Motion graphics via HyperFrames
- **ZIP** — Bundled assets

## Daemon API

When the Open Design daemon is running on port 7456:

```
POST http://localhost:7456/api/generate
{
  "prompt": "landing page",
  "skill": "web-prototype",
  "design_system": "linear",
  "agent": "opencode",
  "output_dir": "/tmp/agnetic-design"
}
```

## Toolsets

| Toolset | Includes Open Design |
|---|---|
| design | Yes |
| expansion | Yes |
| full | Yes |
| core | No |
| readonly | No |
