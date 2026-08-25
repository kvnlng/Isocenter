#!/bin/bash
# Remote Benchmark Runner for Isocenter
# Usage: ./tests/benchmarks/run_remote.sh [SCENARIO]
# Scenarios: A (Baseline), B (Mixed), C (Scalability)

set -e

VM_NAME="isocenter-benchmark"
ZONE="us-central1-a"
REMOTE_DIR="Isocenter"
SCENARIO=${1:-A}

echo "--- Isocenter Remote Benchmark Runner ---"
echo "VM: $VM_NAME ($ZONE)"
echo "Scenario: $SCENARIO"
echo "-------------------------------------"

# 1. Sync Code (Local -> Remote)
echo "[1/3] Packaging and Syncing local code..."
# Create a temporary tarball, excluding heavy/ignored items
tar -czf /tmp/isocenter_payload.tar.gz \
    --exclude='.git' \
    --exclude='venv' \
    --exclude='data' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    .

# Upload to VM
gcloud compute scp --zone=$ZONE /tmp/isocenter_payload.tar.gz $VM_NAME:~/

# 2. Define Remote Support Function
# We embed this script to run on the remote machine to handle setup + execution
REMOTE_SCRIPT="
set -e

# --- Code Extraction ---
if [ ! -d \"$REMOTE_DIR\" ]; then
    mkdir -p $REMOTE_DIR
fi

echo '[Remote] Extracting updated code...'
tar -xzf ~/isocenter_payload.tar.gz -C $REMOTE_DIR --overwrite

cd $REMOTE_DIR

# --- Bootstrap ---
echo '[Remote] Checking environment...'
if ! dpkg -s python3.14-nogil >/dev/null 2>&1; then
    echo '[Remote] Installing Python 3.14 (Free-Threaded)...'
    sudo apt-get update -qq
    sudo apt-get install -y -qq software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -qq
    # Install standard 3.14 and experimental nogil/free-threaded build
    # Note: Package name varies, usually python3.14-nogil in deadsnakes
    sudo apt-get install -y -qq python3.14 python3.14-venv python3.14-dev python3.14-nogil
fi

# Detect generic python or free-threaded binary
PY_BIN=\"python3.14\"
if command -v python3.14t &> /dev/null; then
    PY_BIN=\"python3.14t\"
    echo \"[Remote] Found Free-Threaded Python: \$PY_BIN\"
elif command -v python3.14-nogil &> /dev/null; then
    PY_BIN=\"python3.14-nogil\"
    echo \"[Remote] Found Free-Threaded Python: \$PY_BIN\"
fi

if [ ! -d 'venv' ]; then
    echo \"[Remote] Creating virtual environment with \$PY_BIN...\"
    \$PY_BIN -m venv venv
fi

source venv/bin/activate

echo '[Remote] Installing Python dependencies...'
# Upgrade pip to ensure support for 3.13
pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q psutil
# Install Isocenter in editable mode
pip install -q -e .

# --- Execution ---
echo '[Remote] Starting Scalability Benchmark Suite...'
# Use the venv python (which is now linked to 3.13t if selected)
# Use the venv python (which is now linked to 3.13t if selected)
echo '[Remote] Verifying benchmark_suite.py version...'
grep "frames" tests/benchmarks/benchmark_suite.py | head -n 5

# Reliance on new default (CPU * 1)
# With 2GB/Core recommendation, this should be safe on balanced VMs
python -Xgil=0 tests/benchmarks/benchmark_suite.py --reuse
"

# 3. Execute Remote Command
echo "[3/3] Executing on VM..."
gcloud compute ssh --zone=$ZONE $VM_NAME --command "$REMOTE_SCRIPT"

echo "-------------------------------------"
echo "Benchmark Complete."
