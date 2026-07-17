# OddsEngine — Landing Page

Landing page for [oddsengine.live](https://oddsengine.live)

## Deployment

Hosted via GitHub Pages.

**Custom domain:** `oddsengine.live`

## Setup

1. Push this repo to GitHub
2. Settings → Pages → Source: Deploy from branch → `main` / `/ (root)`
3. Custom domain: `oddsengine.live`
4. DNS records at your registrar:
   - `A` → `185.199.108.153`
   - `A` → `185.199.109.153`
   - `A` → `185.199.110.153`
   - `A` → `185.199.111.153`
5. Enable "Enforce HTTPS"

## Links

- Landing: `oddsengine.live`
- Public Dashboard: `public.oddsengine.live`
- Telegram: update the three `t.me/oddsengine` links in `index.html`

## Positioning

Prediction Market Intelligence — weather only. Crypto and sports are gone.

## The rule this page is built around

Spec §13: no invented performance figures, no unprovable hit rates, no
unsupported ROI claims.

The Track Record section therefore ships with an **empty reliability diagram**
and the status "In calibration". That is deliberate, not a placeholder to fill
with optimistic numbers. It gets filled in from `tools/calibration.py` in
`oddsengine-platform` once signals have actually resolved — whatever the number
turns out to be.

Pricing follows the same rule: free while the chart is empty.

## Assets (unchanged)

`logo.png` · `og-image.png` · `icon-180.png` · `icon-192.png` · `CNAME`
