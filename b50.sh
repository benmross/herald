export PATH=/opt/homebrew/bin:$PATH; cd ~/herald-browser; H="uv run -q --with playwright python hb.py"
curl -s -m 3 http://127.0.0.1:9222/json/version >/dev/null || { open -na "Google Chrome" --args --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="$HOME/Library/Application Support/Herald Chrome" --no-first-run --no-default-browser-check about:blank; sleep 6; echo relaunched; }
$H open "https://jobs.lever.co/shieldai/a7458df0-5632-4698-90c4-a0ad6b6851dd/apply" 5000 | cut -c1-80
$H js "a7458df0" nm.js
