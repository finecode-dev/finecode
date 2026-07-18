#!/bin/sh
# Setup shell initialization to auto-activate dev_workspace venv

VENV_DIR=".venvs/dev_workspace"
BASHRC="${HOME}/.bashrc"

# Ensure .bashrc exists
if [ ! -f "$BASHRC" ]; then
    touch "$BASHRC"
fi

# Check if activation is already in .bashrc
if ! grep -q "dev_workspace.*activate" "$BASHRC"; then
    cat >> "$BASHRC" << 'EOF'

# Auto-activate dev_workspace venv if it exists
if [ -f ".venvs/dev_workspace/bin/activate" ]; then
    . ".venvs/dev_workspace/bin/activate"
fi
EOF
    echo "✓ Added venv auto-activation to .bashrc"
else
    echo "✓ Venv auto-activation already configured in .bashrc"
fi
