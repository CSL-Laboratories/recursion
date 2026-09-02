#!/usr/bin/env bash
# Controller-owned Guerra Lenis-wire machine gate (Phase-5 strict-direct).
# Offline npm + proven font mock; asserts LenisProvider is wired in layout.
set -euo pipefail

export npm_config_offline=true
export NEXT_TELEMETRY_DISABLED=1
export NEXT_FONT_GOOGLE_MOCKED_RESPONSES=/opt/recursion/scripts/gates/guerra-next-font-mock-v1.cjs

npm ci --offline --cache /opt/recursion/cache/npm
npm run build
exec rg -n "LenisProvider" src/app/layout.tsx
