#!/usr/bin/env bash
# Check what Cloudflare is doing with each class of URL on the live site.
#
#   ./verify-cache.sh                                  # against production
#   ./verify-cache.sh http://127.0.0.1:5005            # origin only, no CDN
#
# Each asset URL is fetched twice: the first request fills the edge cache, the
# second should come back HIT. Dynamic URLs must stay BYPASS or DYNAMIC, and
# must never carry a public Cache-Control.

set -uo pipefail

BASE="${1:-https://empireandallies.tsukimi.my}"

CACHEABLE=(
  "/js/empires.js"
  "/css/account.css"
  "/img/avatars/blank.png"
  "/nullassets/game/GeneralAssetGroup_UI.swf"
  "/assets/ui/Hud_BombingRun_01_64.png"
  "/gameSettings.xml"
  "/files/empire-s.assets.zgncdn.com/assets/109338/ZGame.109338.swf"
)

DYNAMIC=(
  "/"
  "/ruffle.html"
  "/login.html"
  "/admin/status.json"
)

probe() {
    local url="$1"
    curl -sS -o /dev/null -D - --max-time 30 "$url" 2>/dev/null \
        | tr -d '\r' \
        | awk 'BEGIN{IGNORECASE=1}
               /^HTTP\//      {status=$2}
               /^cache-status:/{cfs=$0}
               /^cf-cache-status:/{cf=$2}
               /^cache-control:/ {sub(/^[^:]*: */,""); cc=$0}
               /^age:/         {age=$2}
               END{printf "%-4s cf-cache-status=%-10s age=%-6s cache-control=%s\n",
                           status, (cf==""?"-":cf), (age==""?"-":age), (cc==""?"(none)":cc)}'
}

printf '\n== should be cached ==\n'
for path in "${CACHEABLE[@]}"; do
    curl -sS -o /dev/null --max-time 30 "$BASE$path" 2>/dev/null   # warm the edge
    printf '%-62s ' "$path"
    probe "$BASE$path"
done

printf '\n== must NOT be cached ==\n'
for path in "${DYNAMIC[@]}"; do
    printf '%-62s ' "$path"
    probe "$BASE$path"
done

cat <<'EOF'

Reading the results
  cacheable URLs : cf-cache-status should settle on HIT (MISS then HIT on the
                   second run), with a public, max-age=... Cache-Control.
  dynamic URLs   : cf-cache-status BYPASS or DYNAMIC, Cache-Control no-store.
                   A HIT here is a bug - one player's session would be served
                   to another. Check the bypass cache rule first.
EOF
