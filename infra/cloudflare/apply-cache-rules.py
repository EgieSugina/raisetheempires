#!/usr/bin/env python3
"""Install the Raise the Empires cache rules on the Cloudflare zone.

    export CLOUDFLARE_API_TOKEN=...
    ./apply-cache-rules.py              # show what would change
    ./apply-cache-rules.py --apply      # write it
    ./apply-cache-rules.py --apply --tiered-cache
    ./apply-cache-rules.py --purge /js/empires.js /css/account.css
    ./apply-cache-rules.py --purge-all  # purge the whole hostname

The cache-rules phase entrypoint holds every cache rule in the zone, and
writing it replaces the lot. tsukimi.my may serve other sites, so this script
never blindly overwrites: it reads the current ruleset, drops only the rules
whose description starts with RULE_PREFIX (the ones it wrote last time), keeps
everything else untouched, and puts ours first so they win.

The API token needs, on the tsukimi.my zone:
  Zone > Zone Settings > Edit   (read the zone, tiered cache)
  Zone > Cache Rules   > Edit
  Zone > Cache Purge   > Purge  (only for --purge / --purge-all)
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
ZONE = "tsukimi.my"
HOSTNAME = "empireandallies.tsukimi.my"
RULE_PREFIX = "empireandallies:"
PHASE = "http_request_cache_settings"
RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache-rules.json")


def call(method, path, body=None, token=None, allow_404=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and allow_404:
            return None
        detail = error.read().decode(errors="replace")
        sys.exit("Cloudflare API %s %s failed: %s\n%s" % (method, path, error.code, detail))
    if not payload.get("success", False):
        sys.exit("Cloudflare API %s %s returned errors:\n%s"
                 % (method, path, json.dumps(payload.get("errors"), indent=2)))
    return payload["result"]


def zone_id(token):
    result = call("GET", "/zones?name=" + ZONE, token=token)
    if not result:
        sys.exit("No Cloudflare zone named %s is visible to this API token." % ZONE)
    return result[0]["id"]


def current_rules(zone, token):
    """Return the zone's existing cache rules, or [] when no ruleset exists yet."""
    # A zone with no cache rules yet has no entrypoint ruleset at all.
    entrypoint = call("GET", "/zones/%s/rulesets/phases/%s/entrypoint" % (zone, PHASE),
                      token=token, allow_404=True)
    if entrypoint is None:
        return []
    return entrypoint.get("rules", []) or []


def strip_managed_fields(rule):
    """Keep only the fields the API accepts back on a write."""
    return {key: rule[key] for key in ("description", "expression", "action", "action_parameters", "enabled")
            if key in rule}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the rules (default: dry run)")
    parser.add_argument("--tiered-cache", action="store_true",
                        help="also turn on Smart Tiered Cache (zone wide)")
    parser.add_argument("--purge", nargs="+", metavar="PATH",
                        help="purge these paths on " + HOSTNAME)
    parser.add_argument("--purge-all", action="store_true",
                        help="purge every cached file for " + HOSTNAME)
    args = parser.parse_args()

    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        sys.exit("Set CLOUDFLARE_API_TOKEN first.")

    zone = zone_id(token)
    print("zone %s -> %s" % (ZONE, zone))

    if args.purge:
        urls = ["https://%s%s" % (HOSTNAME, p if p.startswith("/") else "/" + p) for p in args.purge]
        call("POST", "/zones/%s/purge_cache" % zone, {"files": urls}, token)
        print("purged:\n  " + "\n  ".join(urls))
        return

    if args.purge_all:
        call("POST", "/zones/%s/purge_cache" % zone, {"hosts": [HOSTNAME]}, token)
        print("purged every cached file for " + HOSTNAME)
        return

    wanted = json.load(open(RULES_FILE, encoding="utf-8"))["rules"]
    existing = current_rules(zone, token)
    kept = [strip_managed_fields(r) for r in existing
            if not (r.get("description") or "").startswith(RULE_PREFIX)]
    replaced = [r for r in existing if (r.get("description") or "").startswith(RULE_PREFIX)]

    print("\ncurrent cache rules in the zone: %d" % len(existing))
    for rule in existing:
        print("  - %s" % (rule.get("description") or rule.get("expression", "")[:70]))
    print("\nof those, %d are ours and will be replaced; %d belong to other sites and are kept."
          % (len(replaced), len(kept)))
    print("\nrules to install (first, so they take priority):")
    for rule in wanted:
        print("  + %s" % rule["description"])

    if not args.apply:
        print("\nDry run. Re-run with --apply to write this.")
        return

    call("PUT", "/zones/%s/rulesets/phases/%s/entrypoint" % (zone, PHASE),
         {"rules": wanted + kept}, token)
    print("\ncache rules installed.")

    if args.tiered_cache:
        # Smart Tiered Cache gives the tunnel a single upper-tier datacenter to
        # pull from instead of every edge location fetching each asset itself.
        call("PATCH", "/zones/%s/argo/tiered_caching" % zone, {"value": "on"}, token)
        call("PATCH", "/zones/%s/cache/tiered_cache_smart_topology_enable" % zone,
             {"value": "on"}, token)
        print("Smart Tiered Cache enabled (zone wide).")


if __name__ == "__main__":
    main()
