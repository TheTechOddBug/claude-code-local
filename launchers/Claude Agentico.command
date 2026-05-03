#!/bin/bash
# Claude Agentico - Claude Code on Gemma 4 31B in AGENTIC mode (with tools)
# Double-click to launch
#
# Same as "Gemma 4 Code.command" but tuned for Mac base/Pro (16-32 GB):
#   - forces --effort low to disable Claude Code 2.1 extended thinking
#     (small models can't handle the two-call thinking → answer flow)
#   - applies the macOS keychain auth workaround so ANTHROPIC_API_KEY is
#     actually honored in interactive mode
#
# Tool-call reliability on a 16 GB Mac is lower than on Max/Ultra hardware:
# expect the model to swap, run at 5-8 tok/s, and occasionally produce
# garbled tool calls. The server already retries via recover_garbled_tool_json,
# but multi-step tool sequences can still fail. For mission-critical agentic
# work, fall back to the cloud Claude (claude without ANTHROPIC_BASE_URL).

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
echo "  Claude Code LOCAL - Modo AGENTICO (com ferramentas)"
echo "  Modelo: Gemma 4 31B Abliterated"
echo "  100% on-device - sem cloud, sem custo de API"
echo ""
echo "  Em 16 GB de RAM o modelo vai swappar. Tool-calls podem falhar."
echo "  Para conversa simples, use o launcher Chat."
echo ""

ANTHROPIC_BASE_URL=http://localhost:4000 \
ANTHROPIC_API_KEY=sk-local \
ANTHROPIC_AUTH_TOKEN=sk-local \
DISABLE_LOGIN_COMMAND=1 \
CLAUDE_SESSION_LABEL="Local Agentic" \
exec "$CLAUDE_BIN" --model claude-sonnet-4-6 \
  --effort low \
  --permission-mode auto \
  --settings "$SCRIPT_DIR/lib/local-settings.json"
