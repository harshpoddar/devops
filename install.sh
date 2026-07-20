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

# The Vast.ai backend shells out to the official `vastai` CLI, which is a
# dependency in pyproject.toml — so `pip install -e .` above puts it INSIDE this
# venv. The skill always calls that copy (.venv/bin/vastai), never one on the
# user's PATH, keeping everything self-contained.
if [ -x .venv/bin/vastai ]; then
  echo "✓ vastai CLI bundled in the venv: $(.venv/bin/vastai --version 2>&1 | head -1)"
else
  echo "! vastai CLI did not install — re-run: .venv/bin/pip install -e ."
fi

# ------------------------------------------------------- Global CLI commands
# Symlink the venv's entry points into ~/.local/bin so `cloudops` and
# `cloudops-dashboard` work from any directory. The scripts' shebangs point at
# this venv's interpreter, so they stay isolated from other Python setups.
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
for tool in cloudops cloudops-dashboard; do
  ln -sf "$(pwd)/.venv/bin/$tool" "$BIN_DIR/$tool"
done
echo "✓ Linked cloudops + cloudops-dashboard into $BIN_DIR"

case ":$PATH:" in
  *":$BIN_DIR:"*)
    echo "✓ $BIN_DIR already on PATH"
    ;;
  *)
    case "${SHELL:-}" in
      */zsh)  PROFILE="$HOME/.zshrc" ;;
      */bash) PROFILE="$HOME/.bashrc" ;;
      *)      PROFILE="" ;;
    esac
    if [ -n "$PROFILE" ] && ! grep -qs 'HOME/.local/bin' "$PROFILE"; then
      printf '\n# added by cloud-devops skill installer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$PROFILE"
      echo "✓ Added $BIN_DIR to PATH in $PROFILE — open a new shell (or run: export PATH=\"\$HOME/.local/bin:\$PATH\")"
    else
      echo "! Add $BIN_DIR to your PATH to use the CLIs globally:"
      echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
    ;;
esac

# ---------------------------------------------------------------- Credentials
if aws sts get-caller-identity >/dev/null 2>&1; then
  echo "✓ AWS credentials working ($(aws sts get-caller-identity --query Arn --output text 2>/dev/null))"
else
  echo "✗ AWS credentials not set up — run: aws configure"
  echo "  (or, recommended: aws configure sso && aws sso login)"
fi

if [ -n "${VAST_API_KEY:-}" ] || [ -f "$HOME/.vast_api_key" ] || [ -f "$HOME/.config/vastai/vast_api_key" ]; then
  echo "✓ Vast.ai API key found"
else
  echo "✗ Vast.ai API key missing (optional) — create one at https://cloud.vast.ai/account/"
  echo "  then: export VAST_API_KEY=<key>   (or write it to ~/.vast_api_key)"
fi

# Vast injects an account-registered SSH public key into each instance at boot.
# spawn/clone register your local key automatically, but flag it if none exists.
if ls "$HOME"/.ssh/id_*.pub >/dev/null 2>&1; then
  echo "✓ Local SSH public key present (used for Vast instance access + the SSH self-check)"
else
  echo "! No SSH key at ~/.ssh/id_*.pub — generate one (ssh-keygen -t ed25519) so Vast"
  echo "  spawns can register it and verify SSH login before reporting success."
fi

cat <<EOF

Next steps (from any directory):
  cloudops                       # interactive menu (usage + instances)
  cloudops --help                # list all subcommands
  cloudops offers --provider vast --gpu-type "RTX 4090"
  cloudops spawn  --provider vast --offer-id <id> --quote
  cloudops-dashboard             # local web dashboard on :8787

Agents — keep it self-contained in one shell (each of your shells is fresh):
  source $(pwd)/.venv/bin/activate && cloudops <command> ...
  # or without activating:  $(pwd)/.venv/bin/cloudops <command> ...
EOF
