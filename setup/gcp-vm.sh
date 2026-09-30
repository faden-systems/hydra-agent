#!/usr/bin/env bash
# Run on the iMac only after cloud creation/spend is authorized. No bootstrap.
# Usage: bash setup/gcp-vm.sh PROJECT_ID

main() {
  if [[ $# -ne 1 || ! $1 =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
    printf 'Usage: bash setup/gcp-vm.sh PROJECT_ID (explicit GCP project ID required)\n' >&2
    return 1
  fi
  local project=$1 key_file="$HOME/.ssh/id_ed25519.pub" public_key
  if [[ ! -f $key_file || ! -r $key_file ]]; then
    printf 'Missing readable public key: %s\n' "$key_file" >&2
    return 1
  fi
  public_key=$(< "$key_file")
  # Metadata is comma-separated. Reject extra entries and multiline key material.
  if [[ ! $public_key =~ ^ssh-ed25519[[:blank:]][A-Za-z0-9+/=]+([[:blank:]].*)?$ ||
        $public_key == *$'\n'* || $public_key == *$'\r'* || $public_key == *,* ]]; then
    printf 'Expected one Ed25519 public key without commas or embedded newlines: %s\n' "$key_file" >&2
    return 1
  fi
  if ! command -v gcloud >/dev/null 2>&1; then
    printf 'gcloud is required on the iMac; install and authenticate the Google Cloud CLI first.\n' >&2
    return 1
  fi
  # No default project, startup script, SSH, retries, or destructive replacement.
  gcloud compute instances create hydra-manager \
    --project "$project" \
    --zone us-west1-b \
    --machine-type e2-standard-4 \
    --image-family ubuntu-2404-lts-amd64 \
    --image-project ubuntu-os-cloud \
    --boot-disk-size 100GB \
    --boot-disk-type pd-balanced \
    --metadata "ssh-keys=hermes:$public_key" \
    --tags hydra-manager
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  set -euo pipefail
  main "$@"
fi
