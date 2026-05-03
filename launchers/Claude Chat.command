#!/bin/bash
# Claude Chat - Claude Code on Gemma 4 31B in CHAT-ONLY mode (no tools)
# Double-click to launch
#
# Designed for Mac base/Pro with 16-32 GB unified memory, where agentic
# tool-use sessions are unreliable due to RAM pressure and the Claude Code
# 2.1 extended-thinking flow that splits each turn into two model calls.
#
# This launcher:
#   - disables all tools (--tools "")
#   - forces --effort low so Claude Code does NOT request extended thinking
#     (small/quantized models exhaust their budget thinking and emit empty
#      replies on the second call → "(No output)")
#   - applies the macOS keychain auth workaround (ANTHROPIC_AUTH_TOKEN +
#     hasCompletedOnboarding=true) so the local API key is actually used
#     instead of the model-selection login prompt
#
# Use this for: code Q&A, snippet generation, debugging help, conversations.
# For tool-driven sessions, see "Claude Agentico.command".

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/claude-local-common.sh"

CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo $HOME/.local/bin/claude)}"

MLX_MODEL_DEFAULT="$(resolve_mlx_model \
  "$HOME/.cache/huggingface/hub/gemma-4-31b-it-abliterated-4bit-mlx" \
  "divinetribe/gemma-4-31b-it-abliterated-4bit-mlx")"

ensure_mlx_server "${MLX_MODEL:-$MLX_MODEL_DEFAULT}" \
  "  Loading Gemma 4 31B Abliterated on MLX (~5-8 tok/s in 16 GB, may swap)..."

clear
echo ""
echo "  Claude Code LOCAL - Modo CHAT (sem ferramentas)"
echo "  Modelo: Gemma 4 31B Abliterated"
echo "  100% on-device - sem cloud, sem custo de API"
echo ""
echo "  Use para: perguntas de codigo, conceitos, debug, conversa."
echo "  Para criar/editar arquivos use o launcher Agentico."
echo ""

ANTHROPIC_BASE_URL=http://localhost:4000 \
ANTHROPIC_API_KEY=sk-local \
ANTHROPIC_AUTH_TOKEN=sk-local \
DISABLE_LOGIN_COMMAND=1 \
CLAUDE_SESSION_LABEL="Local Chat" \
exec "$CLAUDE_BIN" --model claude-sonnet-4-6 \
  --tools "" \
  --effort low \
  --settings "$SCRIPT_DIR/lib/local-settings.json"
