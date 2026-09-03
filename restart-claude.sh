#!/bin/zsh
set -e

cd ~/Desktop/whatsapp-bot-render

echo "======================================"
echo " Reiniciando Claude Code"
echo " Projeto: whatsapp-bot-render"
echo "======================================"
echo

echo "Node:"
node --version

echo
echo "Claude:"
claude --version

echo
echo "Skills encontradas:"
find .claude/skills ~/.claude/skills \
  -maxdepth 2 \
  -name SKILL.md \
  -print 2>/dev/null || true

echo
echo "Playwright:"
command -v playwright-cli || true

echo
echo "A abrir Claude e continuar a última sessão..."
echo

exec claude --continue
