#!/usr/bin/env bash
# Offline wrapper checks. Docker is replaced before any eval command runs.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp "$root/eval" "$work/eval"
mkdir -p "$work/bin" "$work/configs"
touch "$work/.env"
for name in devdex_docs martian martian-controls local-devdex local-martian local-controls; do
  touch "$work/configs/$name.yaml"
done
cat > "$work/bin/docker" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_LOG"
if [[ "$*" == "${FAIL_COMMAND:-}" ]]; then exit 23; fi
STUB
chmod +x "$work/bin/docker"
export PATH="$work/bin:$PATH" DOCKER_LOG="$work/docker.log"

expect_sequence() {
  : > "$work/expected.log"
  for config in "$@"; do
    cat >> "$work/expected.log" <<EOF
compose build runner
compose up -d --build --wait search-mcp
compose run --rm runner doctor --config $config
compose run --rm runner run --config $config
EOF
  done
  diff -u "$work/expected.log" "$DOCKER_LOG"
}

# Default order reuses the existing run command, which owns resume and reporting.
: > "$DOCKER_LOG"
"$work/eval" run-all > "$work/output.log"
expect_sequence configs/devdex_docs.yaml configs/martian.yaml configs/martian-controls.yaml

# Custom config paths preserve the supplied order.
: > "$DOCKER_LOG"
"$work/eval" run-all configs/local-devdex.yaml configs/local-martian.yaml configs/local-controls.yaml > "$work/output.log"
expect_sequence configs/local-devdex.yaml configs/local-martian.yaml configs/local-controls.yaml

# A failed stage preserves its exit code and prevents the third stage.
: > "$DOCKER_LOG"
export FAIL_COMMAND='compose run --rm runner run --config configs/martian.yaml'
if "$work/eval" run-all > "$work/output.log" 2>&1; then exit 1; else status=$?; fi
[[ "$status" == 23 ]]
expect_sequence configs/devdex_docs.yaml configs/martian.yaml
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
