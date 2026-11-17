#!/usr/bin/env bash
# Publish the mobile UI as a static site (GitHub Pages serves /docs).
# The site itself needs no server: it either talks to an API you point it at
# (?api=http://your-pc:8000) or runs in demo mode with sample data.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p docs
cp web/index.html docs/index.html
cp web/icon.svg docs/icon.svg
cat > docs/manifest.webmanifest <<'JSON'
{
  "name": "MT5 Mobile Bot",
  "short_name": "MT5 Bot",
  "start_url": "./",
  "scope": "./",
  "display": "standalone",
  "background_color": "#0b1220",
  "theme_color": "#0b1220",
  "icons": [{ "src": "icon.svg", "sizes": "any", "type": "image/svg+xml" }]
}
JSON
touch docs/.nojekyll
echo "docs/ is up to date ($(wc -c < docs/index.html) bytes)"
