#!/usr/bin/env bash

set -euo pipefail

alias=${1:?expected an image alias}
: "${IMAGE:?expected an image repository}"
: "${RUNNER_TEMP:?expected runner scratch}"

if [ "$alias" = latest ]; then
  git fetch --no-tags --depth=1 origin refs/heads/main:refs/remotes/origin/main
  revision=$(git rev-parse refs/remotes/origin/main^{commit})
elif [[ "$alias" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  git fetch --tags --depth=1 origin
  release=$(git tag --list "v$alias.*" --sort=-version:refname |
    awk '/^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/ && !selected { selected = $0 } END { print selected }')
  test -n "$release"
  revision=$(git rev-parse "$release^{commit}")
else
  printf 'invalid mutable image alias: %s\n' "$alias" >&2
  exit 1
fi

source="$IMAGE:commit-$revision"
manifest=$(mktemp "$RUNNER_TEMP/zoekt-manifest.XXXXXX")
errors=$(mktemp "$RUNNER_TEMP/zoekt-manifest-errors.XXXXXX")
trap 'rm -f "$manifest" "$errors"' EXIT

if docker buildx imagetools inspect "$source" --format '{{json .Manifest}}' >"$manifest" 2>"$errors"; then
  digest=$(jq -er '.digest | select(test("^sha256:[0-9a-f]{64}$"))' "$manifest")
else
  status=$?
  if grep -F "$source" "$errors" | grep -Eq ': (manifest unknown|not found)$'; then
    printf 'The current target %s has no published index yet; retaining %s.\n' "$revision" "$alias"
    exit 0
  fi
  cat "$errors" >&2
  exit "$status"
fi

docker buildx imagetools create --tag "$IMAGE:$alias" "$IMAGE@$digest"
docker buildx imagetools inspect "$IMAGE:$alias"
