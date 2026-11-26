#!/usr/bin/env bash

set -euo pipefail

source_tag() {
  local source_ref=$1 revision=$2 fingerprint
  fingerprint=$(printf '%s' "$source_ref" | sha256sum)
  printf 'source-%s-%s\n' "$revision" "${fingerprint%% *}"
}

alias=${1:?expected all, latest, a minor alias, or source-tag}
if [ "$alias" = source-tag ]; then
  test "$#" -eq 3
  source_tag "$2" "$3"
  exit 0
fi

: "${IMAGE:?expected an image repository}"
: "${RUNNER_TEMP:?expected runner scratch}"
if [ "$alias" != all ] && [ "$alias" != latest ] && ! [[ "$alias" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  printf 'invalid mutable image alias: %s\n' "$alias" >&2
  exit 1
fi

git fetch --no-tags --depth=1 --refmap= origin refs/heads/main
main_revision=$(git rev-parse 'FETCH_HEAD^{commit}')
git fetch --no-tags --depth=1 --refmap= origin 'refs/tags/v*:refs/tags/v*'
releases=$(
  git tag --list 'v*' |
    awk '/^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$/ { core=$0; sub(/\+.*/, "", core); print core "\t" $0 }' |
    LC_ALL=C sort -s -k1,1Vr |
    cut -f2-
)

aliases=("$alias")
if [ "$alias" = all ]; then
  aliases=(latest)
  minors=$(printf '%s\n' "$releases" | sed -E 's/^v([0-9]+\.[0-9]+)\..*/\1/' | uniq)
  if [ -n "$minors" ]; then
    mapfile -t minor_aliases <<<"$minors"
    aliases+=("${minor_aliases[@]}")
  fi
fi

temporary=$(mktemp -d "$RUNNER_TEMP/zoekt-promotion.XXXXXX")
trap 'rm -rf "$temporary"' EXIT
manifest="$temporary/manifest.json"
errors="$temporary/manifest-errors"

for alias in "${aliases[@]}"; do
  targets=()
  while IFS= read -r release; do
    [ -n "$release" ] || continue
    revision=$(git rev-parse "$release^{commit}")
    if { [ "$alias" = latest ] && [ "$revision" = "$main_revision" ]; } || [[ "$release" = v"$alias".* ]]; then
      targets+=("$(source_tag "refs/tags/$release" "$revision")")
    fi
  done <<<"$releases"
  if [ "$alias" = latest ]; then
    targets+=("$(source_tag refs/heads/main "$main_revision")")
  fi

  digest=
  for target in "${targets[@]}"; do
    source="$IMAGE:$target"
    if docker buildx imagetools inspect "$source" --format '{{json .Manifest}}' >"$manifest" 2>"$errors"; then
      digest=$(jq -er '.digest | select(test("^sha256:[0-9a-f]{64}$"))' "$manifest")
      break
    else
      status=$?
      if grep -F "$source" "$errors" | grep -Eq ': (manifest unknown|not found)$'; then
        printf 'The target %s has no published index yet.\n' "$target"
        continue
      fi
      cat "$errors" >&2
      exit "$status"
    fi
  done

  if [ -z "$digest" ]; then
    printf 'No current target has a published index; retaining %s.\n' "$alias"
    continue
  fi

  docker buildx imagetools create --tag "$IMAGE:$alias" "$IMAGE@$digest"
  docker buildx imagetools inspect "$IMAGE:$alias"
done
