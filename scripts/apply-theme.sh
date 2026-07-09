#!/bin/bash
# Apply Starship-OS Cinnamon/GTK theme
# Usage: ./apply-theme.sh [--cinnamon --gtk --all]

set -e
THEME_DIR="$HOME/.themes/Starship-OS"

apply_cinnamon() {
  gsettings set org.cinnamon.theme name "Starship-OS"
  echo "[OK] Cinnamon theme set to Starship-OS"
}

apply_gtk3() {
  gsettings set org.gnome.desktop.interface gtk-theme "Starship-OS" 2>/dev/null || true
  gsettings set org.gnome.desktop.wm.preferences theme "Starship-OS" 2>/dev/null || true
  echo "[OK] GTK3 theme set to Starship-OS"
}

apply_gtk4() {
  mkdir -p "$HOME/.config/gtk-4.0"
  ln -sf "$THEME_DIR/gtk-4.0/gtk.css" "$HOME/.config/gtk-4.0/gtk.css"
  echo "[OK] GTK4 theme linked"
}

case "${1:---all}" in
  --cinnamon) apply_cinnamon ;;
  --gtk)      apply_gtk3; apply_gtk4 ;;
  --all|*)    apply_cinnamon; apply_gtk3; apply_gtk4 ;;
esac

echo "Done — restart Cinnamon (Alt+F2, r) to see changes."
