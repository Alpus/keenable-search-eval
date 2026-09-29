#!/usr/bin/env bash
# Exercise actual command orchestration with Docker replaced before invocation.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp "$root/eval" "$root/.env.example" "$work/"
mkdir -p "$work/bin" "$work/configs" "$work/.gateway"
touch "$work/.env"
for name in devdex_docs martian martian-controls local-devdex local-martian local-controls; do
  touch "$work/configs/$name.yaml"
done
cat > "$work/bin/docker" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_LOG"
if [[ "$*" == "${FAIL_COMMAND:-}" ]]; then exit 23; fi
if [[ "$*" == *'scripts/prepare.py init '* && "${CUSTOM_URL:-}" == 1 ]]; then
  echo 'https://custom.example'
fi
if [[ "$*" == 'compose logs --no-color tunnel' ]]; then echo '| https://test.trycloudflare.com |'; fi
if [[ "$*" == *'scripts/prepare.py provision '* ]]; then
  printf 'validation/prepared/%s.yaml\n' devdex martian controls > .gateway/prepared-configs.txt
fi
STUB
chmod +x "$work/bin/docker"
export PATH="$work/bin:$PATH" DOCKER_LOG="$work/docker.log"

# Prepare once, preflight every suite before any live execution, then run in order.
: > "$DOCKER_LOG"
"$work/eval" run-all > "$work/output.log"
[[ "$(grep -c 'compose build runner' "$DOCKER_LOG")" == 1 ]]
[[ "$(grep -c 'scripts/prepare.py provision' "$DOCKER_LOG")" == 1 ]]
grep -q 'compose up -d tunnel' "$DOCKER_LOG"
tail -n 6 "$DOCKER_LOG" > "$work/actual"
cat > "$work/expected" <<'EOF'
compose run --rm -T runner doctor --config validation/prepared/devdex.yaml
compose run --rm -T runner doctor --config validation/prepared/martian.yaml
compose run --rm -T runner doctor --config validation/prepared/controls.yaml
compose run --rm -T runner run --config validation/prepared/devdex.yaml
compose run --rm -T runner run --config validation/prepared/martian.yaml
compose run --rm -T runner run --config validation/prepared/controls.yaml
EOF
diff -u "$work/expected" "$work/actual"

# Stable URL bypasses temporary tunnel; custom inputs are forwarded unchanged.
: > "$DOCKER_LOG"
export CUSTOM_URL=1
"$work/eval" run-all configs/local-devdex.yaml configs/local-martian.yaml configs/local-controls.yaml > "$work/output.log"
! grep -q 'compose up -d tunnel' "$DOCKER_LOG"
grep -q 'provision --public-url https://custom.example configs/local-devdex.yaml configs/local-martian.yaml configs/local-controls.yaml' "$DOCKER_LOG"
unset CUSTOM_URL

# Any failed preflight prevents all model calls; failed execution stops later stages.
for phase in doctor run; do
  : > "$DOCKER_LOG"
  export FAIL_COMMAND="compose run --rm -T runner $phase --config validation/prepared/martian.yaml"
  if "$work/eval" run-all > "$work/output.log" 2>&1; then exit 1; else status=$?; fi
  [[ "$status" == 23 ]]
  ! grep -q 'runner run --config validation/prepared/controls.yaml' "$DOCKER_LOG"
  if [[ "$phase" == doctor ]]; then ! grep -q 'runner run --config' "$DOCKER_LOG"; fi
done
unset FAIL_COMMAND

# Reject partial overrides and missing configs before starting Docker.
for scenario in partial missing; do
  : > "$DOCKER_LOG"
  if [[ "$scenario" == partial ]]; then
    set -- configs/local-devdex.yaml
  else
    set -- configs/local-devdex.yaml configs/local-martian.yaml configs/missing.yaml
  fi
  if "$work/eval" run-all "$@" > "$work/output.log" 2>&1; then exit 1; else status=$?; fi
  [[ "$status" == 2 && ! -s "$DOCKER_LOG" ]]
done
echo "run-all wrapper checks passed (stub Docker, no external calls)."
