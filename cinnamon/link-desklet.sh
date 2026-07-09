#!/bin/bash
# Symlink desklet source into Cinnamon desklets directory
# Run after editing files in starship-os/cinnamon/
SRC="/home/tech/starship-os/cinnamon"
DST="$HOME/.local/share/cinnamon/desklets/starship-os@starship-os"

echo "Linking desklet source..."
ln -sf "$SRC/metadata.json" "$DST/metadata.json"
ln -sf "$SRC/desklet.js" "$DST/desklet.js"
ln -sf "$SRC/stylesheet.css" "$DST/stylesheet.css"
ln -sf "$SRC/settings-schema.json" "$DST/settings-schema.json"
echo "Done. Restart Cinnamon (Alt+F2, r) to reload desklets."
