#!/usr/bin/env bash
# Installs the cloud-devops skill: AWS CLI v2 (if missing) + a local Python venv
# with the cloudops package, then checks which provider credentials are present.
set -euo pipefail

cd "$(dirname "$0")"

# ---------------------------------------------------------------- AWS CLI v2
if command -v aws >/dev/null 2>&1; then
  echo "✓ AWS CLI already installed: $(aws --version 2>&1)"
else
  echo "Installing AWS CLI v2..."
  OS="$(uname -s)"
  if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      brew install awscli
    else
      TMP="$(mktemp -d)"
      curl -fsSL "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "$TMP/AWSCLIV2.pkg"
      sudo installer -pkg "$TMP/AWSCLIV2.pkg" -target /
      rm -rf "$TMP"
    fi
  elif [ "$OS" = "Linux" ]; then
    ARCH="$(uname -m)"   # x86_64 or aarch64
    TMP="$(mktemp -d)"
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip" -o "$TMP/awscliv2.zip"
    (cd "$TMP" && unzip -q awscliv2.zip && sudo ./aws/install)
    rm -rf "$TMP"
  else
    echo "! Unsupported OS '$OS' — install the AWS CLI manually:"
    echo "  https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  fi
fi

# ---------------------------------------------------------------- Python env
PYTHON="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .
echo "✓ cloudops installed into $(pwd)/.venv"

# ---------------------------------------------------------------- Credentials
if aws sts get-caller-identity >/dev/null 2>&1; then
  echo "✓ AWS credentials working ($(aws sts get-caller-identity --query Arn --output text 2>/dev/null))"
else
  echo "✗ AWS credentials not set up — run: aws configure"
  echo "  (or, recommended: aws configure sso && aws sso login)"
fi

if [ -n "${VAST_API_KEY:-}" ] || [ -f "$HOME/.vast_api_key" ]; then
  echo "✓ Vast.ai API key found"
else
  echo "✗ Vast.ai API key missing (optional) — create one at https://cloud.vast.ai/account/"
  echo "  then: export VAST_API_KEY=<key>   (or write it to ~/.vast_api_key)"
fi

cat <<'EOF'

Next steps:
  source .venv/bin/activate
  cloudops                                            # interactive CLI (usage + instances)
  cloudops-dashboard                                  # local web dashboard on :8787
  python scripts/list_instances/list_instances.py     # or any script in scripts/
EOF
