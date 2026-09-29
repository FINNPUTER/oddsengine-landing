# OddsEngine: Landing Page

oddsengine.live, hosted on GitHub Pages (custom domain in CNAME).

## Files
- index.html: the scroll page (hero, cities, signal, record, game, $ODDS)
- robot-hero.webp, robot-fist.webp, robot-point.webp, robot-sit.webp: The Engine, the mascot
- ring.png: logo ring, used as favicon and in the nav
- og-image.png: preview image for X, Telegram and others
- onboarding.html, logo.png, icon-180.png, icon-192.png: unchanged

## Live data
The page reads the public API (record and open signals). Open signals only show
city and date; the paid fields stay locked.

## After the token launch
The contract address goes into window.ODDS_CA at the top of index.html.
Until then every buy button says the token is launching soon.
