#!/bin/bash
# Coolify management script for studielan
# Usage: source infra/coolify/.env && bash infra/coolify/coolify.sh <command> [uuid]

set -euo pipefail

CMD="${1:-help}"
UUID="${2:-$COOLIFY_APP_UUID}"
API="${COOLIFY_API_URL}/api/v1"
AUTH="Authorization: Bearer ${COOLIFY_API_TOKEN}"

case "$CMD" in
  deploy)
    echo "Deploying ${UUID}..."
    curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
      "${API}/deploy?uuid=${UUID}&force=true"
    echo
    ;;
  restart)
    echo "Restarting ${UUID}..."
    curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
      "${API}/applications/${UUID}/restart"
    echo
    ;;
  status)
    curl -s -H "$AUTH" "${API}/applications/${UUID}" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print(f\"Name: {d['name']}\")
print(f\"Status: {d['status']}\")
print(f\"Branch: {d['git_branch']}\")
print(f\"Last online: {d.get('last_online_at','?')}\")
"
    ;;
  logs)
    curl -s -H "$AUTH" "${API}/applications/${UUID}/logs" | python3 -c "
import sys,json; d=json.load(sys.stdin)
for line in d.get('logs','').split('\n')[-50:]:
    print(line)
"
    ;;
  app-logs)
    # Alias for logs
    $0 logs "$UUID"
    ;;
  app-restart)
    # Alias for restart
    $0 restart "$UUID"
    ;;
  help|*)
    echo "Usage: coolify.sh <command> [uuid]"
    echo "  deploy   - Deploy (rebuild + restart)"
    echo "  restart  - Restart container"
    echo "  status   - Show app status"
    echo "  logs     - Show recent logs"
    ;;
esac
