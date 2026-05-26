#!/data/data/com.termux/files/usr/bin/bash
# Termux EDL Setup Script
# Installs dependencies needed to run EDL in Termux

set -e

echo "=========================================="
echo "  EDL Termux Setup"
echo "=========================================="
echo ""

# Update packages
echo "[*] Updating package lists..."
pkg update -y

# Install required system packages
echo "[*] Installing system dependencies..."
pkg install -y \
    libusb \
    python \
    termux-api \
    binutils \
    clang \
    make

# Install Python dependencies
echo "[*] Installing Python packages..."
pip install --upgrade pip
pip install \
    pyusb \
    pyserial \
    docopt \
    pycryptodome \
    colorama \
    capstone \
    keystone-engine \
    qrcode \
    requests \
    passlib \
    Exscript

echo ""
echo "=========================================="
echo "  Setup complete!"
echo ""
echo "  Usage:"
echo "    python edl.py <command>"
echo "    (auto-detects Termux environment)"
echo ""
echo "  First-time USB permission:"
echo "    1. Connect your device in EDL mode"
echo "    2. Run: termux-usb -r /dev/bus/usb/001/048"
echo "    3. Tap 'Grant' on the permission dialog"
echo ""
echo "  If you get permission errors, grant"
echo "  Termux storage access and USB access:"
echo "    termux-setup-storage"
echo "=========================================="
