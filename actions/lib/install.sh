# Source from a composite action after guard.sh has created its private temp directory.
lit_install() {
  local platform architecture asset version expected actual
  version=v0.1.4
  case "$(uname -s)" in
    Linux) platform=linux ;;
    Darwin) platform=darwin ;;
    MINGW*|MSYS*|CYGWIN*) platform=windows ;;
    *) echo "::error::lit has no release for $(uname -s)" >&2; return 1 ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) architecture=amd64 ;;
    aarch64|arm64) architecture=arm64 ;;
    *) echo "::error::lit has no release for $(uname -m)" >&2; return 1 ;;
  esac
  asset="lit-$platform-$architecture"
  if [[ $platform == windows ]]; then asset="$asset.exe"; fi
  expected="$(awk -v name="$asset" '$2 == name { print $1 }' "$GITHUB_ACTION_PATH/../lib/$version.sha256")" || return 1
  [[ $expected =~ ^[0-9a-f]{64}$ ]] || { echo "::error::missing checksum for $asset" >&2; return 1; }
  LIT_BIN="$lit_guard_dir/$asset"
  curl -q --fail --location --silent --show-error --retry 3 --proto '=https' --proto-redir '=https' \
    --output "$LIT_BIN" "https://github.com/CakeRepository/lit/releases/download/$version/$asset" || return 1
  if command -v sha256sum >/dev/null; then
    actual="$(sha256sum < "$LIT_BIN")" || return 1
  else
    actual="$(shasum -a 256 < "$LIT_BIN")" || return 1
  fi
  [[ ${actual%% *} == "$expected" ]] || { echo "::error::checksum mismatch for $asset" >&2; return 1; }
  chmod 700 "$LIT_BIN"
}
