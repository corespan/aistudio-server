#!/usr/bin/env bash
# End-to-end through agentgateway, per role: initialize, tools/list, then two tools/call.
#   gateway/check.sh [base-url]     default https://localhost:8050/mcp/aistudio, trusting $MCP_SECRETS_DIR/tls/ca.pem (CA=...)
# `make mcp-check` runs it with TOKEN = kc.py token in the aistudio-mcp image: a real Keycloak login per role.
URL=${1:-https://localhost:8050/mcp/aistudio}
CA=${CA:-${MCP_SECRETS_DIR:-$HOME/.aistudio-mcp}/tls/ca.pem}
TOKEN=${TOKEN:-python3 "$(dirname "$0")/kc.py" token}
H=(--cacert "$CA" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
rpc() { curl -s -m20 "${H[@]}" "${AUTH[@]}" ${SID:+-H "Mcp-Session-Id: $SID"} "$URL" -d "$1"; }
# one-line verdict of a tools/call response (SSE or JSON)
verdict() { python3 -c "
import sys, json, re
t = sys.stdin.read()
m = re.findall(r'^data: (.*)$', t, re.M) or [t]
try: d = json.loads(m[-1])
except Exception: print('no response:', t[:100].replace('\n', ' ')); sys.exit()
if 'error' in d: print('refused by gateway/server:', str(d['error'].get('message'))[:80])
else:
    r = d['result']; text = (r.get('content') or [{}])[0].get('text', '')
    print(('tool error: ' + text[:90]) if r.get('isError') else 'ok: ' + text.split(chr(10))[1][:80])
"; }
TMP=$(mktemp -d)
for role in none viewer operator admin; do
  AUTH=(); SID=
  # eval: TOKEN is a command line with quoted arguments (make mcp-check passes one).
  [ $role != none ] && AUTH=(-H "Authorization: Bearer $(eval "$TOKEN $role" 2>/dev/null | tr -d '\r')")
  code=$(curl -s -m20 -o "$TMP/init" -D "$TMP/hdr" -w '%{http_code}' "${H[@]}" "${AUTH[@]}" "$URL" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}')
  SID=$(tr -d '\r' <"$TMP/hdr" | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')
  if [ "$code" != 200 ]; then echo "$role: initialize -> HTTP $code $(head -c 100 "$TMP/init")"; continue; fi
  rpc '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
  tools=$(rpc '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | grep -o '"name":"[a-z_]*"' | cut -d'"' -f4 | sort | tr '\n' ' ')
  echo "$role: $(echo $tools | wc -w) tools"
  echo "   check_service_health : $(rpc '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"check_service_health","arguments":{}}}' | verdict)"
  echo "   list_containers      : $(rpc '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"list_containers","arguments":{}}}' | verdict)"
done
# Keycloak paths the gateway must not serve: expect 404 for each.
BASE=${URL%/mcp/aistudio}
for p in /realms/master/.well-known/openid-configuration /realms/aistudio/clients-registrations/default /admin/; do
  echo "closed $p: HTTP $(curl -s -m20 -o /dev/null -w '%{http_code}' --cacert "$CA" -X POST "$BASE$p")"
done
rm -rf "$TMP"
