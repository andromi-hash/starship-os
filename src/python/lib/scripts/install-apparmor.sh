#!/usr/bin/env bash
set -euo pipefail

PROFILES_DIR="$(cd "$(dirname "$0")/../security/apparmor" && pwd)"
APPARMOR_DIR="/etc/apparmor.d"

echo "=== Starship OS AppArmor Profile Installer ==="
echo "Profiles source: ${PROFILES_DIR}"
echo "Install target:  ${APPARMOR_DIR}"
echo ""

# Check root
if [[ "${EUID}" -ne 0 ]]; then
    echo "Error: must be run as root" >&2
    exit 1
fi

# Check apparmor is available
if ! command -v apparmor_parser &>/dev/null; then
    echo "Error: apparmor_parser not found. Install apparmor-utils:" >&2
    echo "  apt-get install -y apparmor-utils" >&2
    exit 1
fi

# Verify Ubuntu 24.04+
if command -v lsb_release &>/dev/null; then
    VERSION=$(lsb_release -rs)
    MAJOR=$(echo "${VERSION}" | cut -d. -f1)
    if [[ "${MAJOR}" -lt 24 ]]; then
        echo "Warning: detected Ubuntu ${VERSION}, profiles are written for 24.04+"
    fi
fi

# Check AppArmor is active
if ! aa-status &>/dev/null; then
    echo "Warning: AppArmor may not be active. Ensure 'apparmor=1' is in kernel cmdline."
fi

# Copy profiles
echo "Copying profiles..."
for profile in agnetic-agent ollama nats; do
    if [[ ! -f "${PROFILES_DIR}/${profile}" ]]; then
        echo "Error: profile ${profile} not found in ${PROFILES_DIR}" >&2
        exit 1
    fi
    cp "${PROFILES_DIR}/${profile}" "${APPARMOR_DIR}/${profile}"
    chmod 644 "${APPARMOR_DIR}/${profile}"
    echo "  Installed: ${APPARMOR_DIR}/${profile}"
done

# Load and enforce profiles
echo ""
echo "Loading profiles..."
for profile in agnetic-agent ollama nats; do
    echo "  Loading ${profile}..."
    apparmor_parser -r "${APPARMOR_DIR}/${profile}"
    echo "  Setting ${profile} to enforce..."
    aa-enforce "${APPARMOR_DIR}/${profile}" || true
    echo "  Done: ${profile}"
done

echo ""
echo "=== Installation complete ==="
echo ""
echo "Installed profiles:"
aa-status --profiled 2>/dev/null || true
echo ""
echo "To switch a profile to complain mode for testing:"
echo "  aa-complain /etc/apparmor.d/<profile>"
echo ""
echo "To reload a profile after edits:"
echo "  apparmor_parser -r /etc/apparmor.d/<profile>"
echo ""
echo "To remove a profile:"
echo "  aa-disable /etc/apparmor.d/<profile>"
echo "  rm /etc/apparmor.d/<profile>"
