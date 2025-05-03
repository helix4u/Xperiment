"""
425homescrape.py · v0.3.5 (Show‑More revival)
───────────────────────────────────────────────
This patch brings back the aggressive **“Show more” expander** that the
original prototype relied on to surface replies and threaded tweets.  It
runs _every scroll_ (up to 20 clicks per batch) so the scraper keeps a
steady view depth even on conversation‑heavy timelines.

Other tweaks
• Confirmed the persistent‑profile path is **exactly** the working‑dir
  `.chromium-profile/` unless you override `PROFILE_DIR` — so all your
  existing cookies/logins carry over untouched.
• Added `SHOWMORE_MAX` env var (default 20) to tune the expansion depth.
• Minor: extractor now grabs retweet context (`retweeted_by`).

Win 11 / Python 3.10 / Playwright 1.43 verified.
"""

from __future__ import annotations
import os, sys, json, time, atexit, logging
from datetime import datetime
from pathlib import Path
from multiprocessing import Process
from textwrap import dedent

from flask import Flask, request, redirect, url_for, render_template_string
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─────────────────── Config / Logging ───────────────────
LOG_LVL = os.getenv('LOGLEVEL', 'INFO').upper()
logging.basicConfig(format='[%(levelname)s] %(message)s', level=LOG_LVL)
log = logging.getLogger("x‑scrape")

HEADLESS_DEFAULT = bool(int(os.getenv('HEADLESS', '0')))  # 👈 default *visible*
SCROLLS_DEFAULT  = int(os.getenv('SCROLLS', 60))
WAIT_TIMEOUT_MS  = int(os.getenv('WAIT_MS', 30000))
SHOWMORE_MAX     = int(os.getenv('SHOWMORE_MAX', 20))

# Persistent Chrome/Edge profile directory (for cookies, logins, etc.)
PROFILE_DIR = Path(os.getenv('PROFILE_DIR', '.chromium-profile')).resolve()
PROFILE_DIR.mkdir(exist_ok=True)

SCRAPE_PROCS: dict[str, Process] = {}

# ─────────────────── Helpers ───────────────────

def js(src: str) -> str:
    """dedent & collapse multiline JS for page.evaluate()"""
    return dedent(src).strip().replace("\n", " ")


def write_jsonl(file: Path, obj: dict):
    with file.open("a", encoding="utf‑8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

# "Show more" expander JS
JS_EXPAND_SHOWMORE = js("""
    (maxClicks) => {
      let clicks = 0;
      const btns = () => Array.from(document.querySelectorAll('[role="button"]'))
        .filter(b => {
          const t = (b.textContent||'').toLowerCase();
          return t.includes('show more') && !b.closest('a[href*="/i/grok/share/"]');
        });
      while (clicks < maxClicks) {
        const b = btns()[0];
        if (!b) break;
        try { b.click(); clicks++; }
        catch(_) { break; }
      }
      return clicks;
    }
""")

# ─────────────────── Core scraper ───────────────────

def scrape_worker(account: str, feed: str, scrolls: int, headless: bool):
    """Runs in its own *OS process* so Playwright is fully isolated."""
    try:
        pw = sync_playwright().start()
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        url = "https://x.com/home" if feed == "home" else f"https://x.com/{account}/with_replies"
        log.info("[%s] ▶ %s", account, url)
        try:
            page.goto(url, timeout=WAIT_TIMEOUT_MS)
        except PWTimeout:
            log.warning("[%s] initial navigation timed out; retrying once", account)
            page.goto(url, timeout=WAIT_TIMEOUT_MS)

        # If /home requires auth, gracefully fall back
        if feed == "home" and "login" in page.url:
            log.warning("[%s] not logged in – falling back to public timeline", account)
            page.goto(f"https://x.com/{account}", timeout=WAIT_TIMEOUT_MS)

        def wait_article():
            try:
                page.wait_for_selector("article", timeout=WAIT_TIMEOUT_MS)
                return True
            except PWTimeout:
                Path("errors").mkdir(exist_ok=True)
                fname = Path("errors") / f"timeout_{account}_{int(time.time())}.png"
                page.screenshot(path=str(fname))
                log.error("[%s] selector timeout – screenshot dumped to %s", account, fname)
                return False

        if not wait_article():
            ctx.close(); pw.stop(); return  # abort

        page.keyboard.press("Escape"); time.sleep(1)  # dismiss any modal

        # JS extractor (now includes retweeted_by)
        extractor = js("""
            () => {
              const pick=(s,p=document)=>p.querySelector(s);
              const txt=n=>n? n.innerText||n.textContent||'' : '';
              const tweets=[];
              document.querySelectorAll('article').forEach(art=>{
                try{
                  const timeTag=pick('time',art);
                  if(!timeTag) return;
                  const a=timeTag.closest('a');
                  const href=a?a.href:'';
                  const id=(href.match(/status\/(\d+)/)||[])[1]||'';
                  const user=href.split('/').slice(-3,-2)[0]||'unknown';
                  const text=txt(pick('[data-testid="tweetText"], div[lang]',art)).trim();
                  const sc=pick('[data-testid="socialContext"]',art);
                  let retBy=null;
                  if(sc){
                    const m=(sc.textContent||'').match(/^(.*?) reposted/i);
                    if(m) retBy=m[1].trim();
                  }
                  tweets.push({id,username:user,content:text,timestamp:timeTag.dateTime,is_retweet:!!sc,retweeted_by:retBy,tweet_url:href});
                }catch(_){}
              });
              return tweets;
            }
        """)

        folder = Path(f"{account}_{feed}"); folder.mkdir(exist_ok=True)
        outfile = folder / f"tweets_{datetime.utcnow():%Y%m%d_%H%M%S}.jsonl"
        seen=set(); total=0

        for s in range(scrolls):
            # expand "Show more" buttons first
            clicks = page.evaluate(JS_EXPAND_SHOWMORE, SHOWMORE_MAX)
            if clicks:
                log.debug("[%s] expanded %d show‑more", account, clicks)
                time.sleep(1.5)  # let new content load

            tweets = page.evaluate(extractor)
            new=0
            for t in tweets:
                tid=t.get('id') or f"{t['username']}_{t['timestamp']}";
                if tid in seen: continue
                seen.add(tid); new+=1; total+=1
                write_jsonl(outfile,t)
            log.info("[%s] scroll %d/%d  +%d (total %d)", account, s+1, scrolls, new, total)
            page.evaluate("window.scrollBy(0,1800)"); time.sleep(1.6)

        ctx.close(); pw.stop()
        log.info("[%s] done – %d tweets", account, total)
    except Exception as e:
        log.exception("[%s] fatal: %s", account, e)

# ─────────────────── Tiny Flask UI ───────────────────
app = Flask(__name__)

INDEX = """
<!doctype html><title>X‑scrape</title>
<style>body{font:14px sans-serif;margin:40px}</style>
<h1>Start scrape</h1>
<form method=post>
  Account <input name=account required placeholder="elonmusk">
  Feed <select name=feed><option value=home>home</option><option value=with_replies>with_replies</option></select>
  Scrolls <input type=number name=scroll value="""+str(SCROLLS_DEFAULT)+""" min=1 max=500>
  <button>Go</button>
</form>
"""

@app.route('/', methods=['GET','POST'])
def index():
    if request.method=='POST':
        acct      = request.form['account'].strip()
        feed      = request.form['feed']
        scrolls   = int(request.form.get('scroll', SCROLLS_DEFAULT))
        headless  = HEADLESS_DEFAULT
        key       = f"{acct}_{feed}"
        if key not in SCRAPE_PROCS or not SCRAPE_PROCS[key].is_alive():
            p = Process(target=scrape_worker, args=(acct,feed,scrolls,headless), daemon=True)
            p.start(); SCRAPE_PROCS[key]=p
        return redirect(url_for('view', account=acct, feed=feed))
    return INDEX

@app.route('/feed/<account>/<feed>')
def view(account, feed):
    folder = Path(f"{account}_{feed}")
    files  = sorted(folder.glob('tweets_*.jsonl'))
    if not files:
        return f"<p>No data yet for {account} {feed}. <a href='/'>Back</a>"
    latest = files[-1]
    tweets = [json.loads(l) for l in latest.open(encoding='utf‑8')]
    tweets.sort(key=lambda t: t['timestamp'], reverse=True)
    rows = "".join(f"<tr><td>{t['timestamp']}</td><td>@{t['username']}</td><td>{t['content'][:140]}</td></tr>" for t in tweets)
    return f"""
    <meta http-equiv='refresh' content='6'>
    <h2>{account} · {feed} ({len(tweets)})</h2><a href='/'>Back</a>
    <table border=1 cellpadding=4>{rows}</table>"""

# ─────────────────── Entrypoint ───────────────────
if __name__ == '__main__':
    if len(sys.argv) >= 2:
        # CLI: python 425homescrape.py <acct> [feed] [scrolls]
        acct   = sys.argv[1]
        feed   = sys.argv[2] if len(sys.argv)>=3 else 'home'
        scroll = int(sys.argv[3]) if len(sys.argv)>=4 else SCROLLS_DEFAULT
        scrape_worker(acct, feed, scroll, HEADLESS_DEFAULT)
    else:
        port = int(os.getenv('PORT', 5000))
        log.info("Flask UI → http://127.0.0.1:%d", port)
        app.run(port=port, debug=False)
