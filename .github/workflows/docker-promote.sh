#!/usr/bin/env bash

set -euo pipefail

alias=${1:?expected an image alias}
: "${IMAGE:?expected an image repository}"
: "${RUNNER_TEMP:?expected runner scratch}"

if [ "$alias" = latest ]; then
  git fetch --no-tags --depth=1 --refmap= origin refs/heads/main
  targets=(FETCH_HEAD)
elif [[ "$alias" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  git fetch --no-tags --depth=1 --refmap= origin 'refs/tags/v*:refs/tags/v*'
  releases=$(git tag --list "v$alias.*" --sort=-version:refname | awk '/^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/ { print }')
  test -n "$releases"
  mapfile -t targets <<<"$releases"
else
  printf 'invalid mutable image alias: %s\n' "$alias" >&2
  exit 1
fi

temporary=$(mktemp -d "$RUNNER_TEMP/zoekt-promotion.XXXXXX")
trap 'rm -rf "$temporary"' EXIT
manifest="$temporary/manifest.json"
errors="$temporary/manifest-errors"
digest=

for target in "${targets[@]}"; do
  revision=$(git rev-parse "$target^{commit}")
  source="$IMAGE:commit-$revision"
  if docker buildx imagetools inspect "$source" --format '{{json .Manifest}}' >"$manifest" 2>"$errors"; then
    digest=$(jq -er '.digest | select(test("^sha256:[0-9a-f]{64}$"))' "$manifest")
    break
  else
    status=$?
    if grep -F "$source" "$errors" | grep -Eq ': (manifest unknown|not found)$'; then
      printf 'The target %s has no published index yet.\n' "$revision"
      continue
    fi
    cat "$errors" >&2
    exit "$status"
  fi
done

if [ -z "$digest" ]; then
  printf 'No current target has a published index; retaining %s.\n' "$alias"
  exit 0
fi

docker buildx imagetools create --tag "$IMAGE:$alias" "$IMAGE@$digest"
docker buildx imagetools inspect "$IMAGE:$alias"
