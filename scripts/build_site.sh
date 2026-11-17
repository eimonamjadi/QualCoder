#!/usr/bin/env bash
# Build the static GitHub Pages site into _site/ (or the folder given as $1):
#   /        landing page (site/index.html)
#   /demo/   the real UI from static/, backed by site/demo-api.js in the browser
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-_site}"
rm -rf "$OUT"
mkdir -p "$OUT/demo/samples"

cp site/index.html "$OUT/"
cp -r site/assets "$OUT/" 2>/dev/null || true
cp static/app.js static/style.css site/demo-api.js site/demo.css "$OUT/demo/"
cp samples/codebook.json samples/interview_transcript.txt "$OUT/demo/samples/"

# Same index.html as the app, with relative paths and the demo backend loaded first.
sed -e 's#/static/style.css">#./style.css">\n  <link rel="stylesheet" href="./demo.css">#' \
    -e 's#<script src="/static/app.js"></script>#<script src="./demo-api.js"></script>\n<script src="./app.js"></script>#' \
    -e 's#<title>QualCoder</title>#<title>QualCoder demo</title>#' \
    static/index.html > "$OUT/demo/index.html"
grep -q 'demo-api.js' "$OUT/demo/index.html" || { echo "failed to inject demo-api.js" >&2; exit 1; }

# The built-in instructions come from the Python source so the demo shows the same text.
QUALCODE_NO_AUTOAPP=1 python3 -c 'import json; from qualcode.coding import BASE_INSTRUCTIONS; print(json.dumps({"text": BASE_INSTRUCTIONS}))' \
  > "$OUT/demo/base_instructions.json"

touch "$OUT/.nojekyll"
echo "Built $OUT"
