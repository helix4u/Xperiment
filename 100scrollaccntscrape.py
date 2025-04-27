import time
import json
import threading
import signal
import sys
from pathlib import Path
from datetime import datetime
from flask import Flask, request, redirect, url_for, render_template_string
from playwright.sync_api import sync_playwright

app = Flask(__name__)
scrape_threads = {}
browser_contexts = {}

# Graceful shutdown
def signal_handler(sig, frame):
    print("[INFO] Shutting down browsers...")
    for key, ctx in browser_contexts.items():
        try:
            ctx.close()
            print(f"[INFO] Closed browser for {key}")
        except:
            pass
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def scrape_tweets(account_name: str, feed_type: str, total_scrolls: int = 100):
    # Determine folder and filename
    folder_name = f"{account_name}_{feed_type}"
    subfolder = Path(folder_name)
    subfolder.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    tweet_file = subfolder / f"tweets_{ts_str}.jsonl"
    tweet_file.touch(exist_ok=True)

    seen_ids = set()
    total_collected = 0
    total_retweets = 0

    # Launch browser
    playwright = sync_playwright().start()
    browser_ctx = playwright.chromium.launch_persistent_context(
        user_data_dir=str(Path(".chromium-profile").resolve()),
        headless=False,
        args=["--disable-blink-features=AutomationControlled"]
    )
    browser_contexts[folder_name] = browser_ctx
    page = browser_ctx.pages[0] if browser_ctx.pages else browser_ctx.new_page()

    try:
        # Choose URL
        if feed_type == "home":
            url = "https://x.com/home"
        else:
            url = f"https://x.com/{account_name}/with_replies"

        print(f"[INFO] Opening {url}")
        page.goto(url, timeout=20000)
        page.wait_for_selector("article", timeout=20000)
        time.sleep(2)

        # Dismiss overlays
        try:
            page.keyboard.press("Escape")
            time.sleep(1)
        except:
            pass

        for scroll in range(total_scrolls):
            print(f"[PROGRESS] Scroll {scroll+1}/{total_scrolls}")

            # Expand Show More buttons until none remain
            expansions = 0
            while True:
                clicked = page.evaluate("""
                    () => {
                        const btns = Array.from(document.querySelectorAll('[role="button"]'))
                          .filter(b => {
                            const t = (b.textContent||'').toLowerCase();
                            if (!t.includes('show more')) return false;
                            if (b.closest('a[href*="/i/grok/share/"]')) return false;
                            return true;
                          });
                        if (btns.length === 0) return false;
                        try { btns[0].click(); return true; }
                        catch { return false; }
                    }
                """)
                if not clicked or expansions >= 20:
                    break
                expansions += 1
                print(f"[INFO] Clicked 'Show more' ({expansions})")
                time.sleep(2)

            if expansions:
                print(f"[INFO] Completed {expansions} expansions; waiting for content")
                time.sleep(2)

            # Extract tweets
            tweets_data = page.evaluate("""
                () => {
                    const extractText = el => el ? (el.innerText||el.textContent) : '';
                    const extractUser = url => {
                        const parts = url.split('/');
                        const i = parts.indexOf('status');
                        return i>0 ? parts[i-1] : null;
                    };
                    const out = [];
                    document.querySelectorAll('article').forEach(art => {
                        try {
                            let url='', user='', id='', ts='';
                            const tEl = art.querySelector('time');
                            if (tEl) {
                                ts = tEl.getAttribute('datetime')||'';
                                const link = tEl.closest('a');
                                if (link) {
                                    let h = link.getAttribute('href');
                                    if (h.startsWith('/')) h = 'https://x.com'+h;
                                    url=h; user=extractUser(h)||'unknown';
                                    const m=h.match(/status\\/(\\d+)/);
                                    id=m?m[1]:'';
                                }
                            }
                            if (!url) {
                                const alt = art.querySelector('a[href*="/status/"]');
                                if (alt) {
                                    let h2=alt.getAttribute('href');
                                    if (h2.startsWith('/')) h2='https://x.com'+h2;
                                    url=h2; user=extractUser(h2)||'unknown';
                                    const m2=h2.match(/status\\/(\\d+)/);
                                    id=m2?m2[1]:'';
                                }
                            }
                            let isRT=false, retBy=null;
                            const sc = art.querySelector('[data-testid="socialContext"]');
                            if (sc && /repost/i.test(sc.textContent||'')) {
                                isRT=true;
                                const m3=(sc.textContent||'').match(/(.+?)\\s+repost/i);
                                retBy=m3?m3[1].trim():null;
                            }
                            if (!isRT && user.toLowerCase() !== window.location.pathname.split('/')[1].toLowerCase()) {
                                isRT=true; retBy=window.location.pathname.split('/')[1];
                            }
                            const mention = Array.from(art.querySelectorAll('a[role="link"]'))
                                .find(a=>{ const h=a.getAttribute('href')||''; return h.startsWith('/')&&!h.includes('/status/'); });
                            const mentioned = mention?mention.getAttribute('href').replace('/',''):null;
                            let text = extractText(art.querySelector('div[lang]')) ||
                                       extractText(art.querySelector('[data-testid="tweetText"]'));
                            if (!text) {
                                for (const d of art.querySelectorAll('div[dir="auto"]')) {
                                    const t2=extractText(d).trim();
                                    if (t2.length>5){ text=t2; break; }
                                }
                            }
                            if (!text) text=extractText(art).trim();
                            const key = id||`${user}_${ts}`;
                            out.push({
                                id: key,
                                timestamp: ts,
                                username: user,
                                mentioned_user: mentioned,
                                content: text||'',
                                is_retweet: isRT,
                                retweeted_by: isRT?retBy:null,
                                tweet_url: url
                            });
                        } catch(e) {
                            console.error('Error:',e);
                        }
                    });
                    return out;
                }
            """)

            print(f"[INFO] Found {len(tweets_data)} articles")
            new_cnt=0; rt_cnt=0
            for tw in tweets_data:
                tid = tw['id']
                if not tid or tid in seen_ids:
                    continue
                seen_ids.add(tid)
                with tweet_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(tw, ensure_ascii=False)+"\n")
                new_cnt+=1; total_collected+=1
                if tw['is_retweet']:
                    rt_cnt+=1; total_retweets+=1
            print(f"[INFO] Saved {new_cnt} new ({rt_cnt} retweets)")

            page.evaluate("window.scrollBy({top:2000,behavior:'smooth'})")
            time.sleep(2)

    except Exception as e:
        print(f"[ERROR] Scraping failed: {e}")

    finally:
        print(f"[INFO] Cleaning up browser for {folder_name}")
        try: browser_ctx.close()
        except: pass
        playwright.stop()
        browser_contexts.pop(folder_name, None)

    print(f"[DONE] Collected {total_collected} tweets ({total_retweets} retweets)")

def cleanup_browsers():
    print("[INFO] Cleaning all browsers")
    for ctx in list(browser_contexts.values()):
        try: ctx.close()
        except: pass
    browser_contexts.clear()

import atexit
atexit.register(cleanup_browsers)

# Web interface

@app.route("/", methods=["GET","POST"])
def index():
    if request.method=="POST":
        acct = request.form.get("account_name","").strip()
        feed = request.form.get("feed_type","with_replies")
        key = f"{acct}_{feed}"
        if acct and (key not in scrape_threads or not scrape_threads[key].is_alive()):
            t = threading.Thread(target=scrape_tweets, args=(acct, feed))
            t.start()
            scrape_threads[key] = t
        return redirect(url_for("feed", account_name=acct, feed_type=feed, live="1"))
    return render_template_string("""
<html><body>
<form method="POST">
  <label>Account</label>
  <input name="account_name" required placeholder="e.g. elonmusk or home">
  <label>Feed type</label>
  <select name="feed_type">
    <option value="with_replies">With Replies</option>
    <option value="home">Home Timeline</option>
  </select>
  <button>Start</button>
</form>
</body></html>
""")

@app.route("/feed/<account_name>/<feed_type>")
def feed(account_name, feed_type):
    live = request.args.get("live","0")
    folder = Path(f"{account_name}_{feed_type}")
    if not folder.exists():
        return f"<p>No data folder for {account_name} {feed_type}.</p>"
    # pick latest file
    files = sorted(folder.glob("tweets_*.jsonl"))
    if not files:
        return f"<p>No data files found in {folder.name}.</p>"
    latest = files[-1]
    tweets=[]; rts=0; errs=0
    for ln in latest.open("r", encoding="utf-8"):
        try:
            d = json.loads(ln)
            tweets.append(d)
            if d.get("is_retweet"): rts+=1
            if d.get("error"): errs+=1
        except:
            continue
    tweets.sort(key=lambda x: x.get("timestamp",""), reverse=True)
    if f"{account_name}_{feed_type}" in scrape_threads and not scrape_threads[f"{account_name}_{feed_type}"].is_alive():
        live="0"
    return render_template_string("""
<html><body><h1>{{account_name}} {{feed_type}}</h1>
{% if live=='1' %}
<p><i>Scraping in progress... {{tweets|length}} tweets so far ({{rts}} retweets, {{errs}} errors)</i></p>
<p><a href="{{url_for('feed',account_name=account_name,feed_type=feed_type,live=1)}}">Refresh</a></p>
{% else %}
<p><i>{{tweets|length}} tweets collected ({{rts}} retweets, {{errs}} errors)</i></p>
{% endif %}
{% for t in tweets %}
<div style="margin:10px;padding:10px;border:1px solid #ccc;{% if t.error %}background-color:#ffeeee;{% endif %}">
{% if t.is_retweet %}<div>Retweeted by {{t.retweeted_by}}</div>{% endif %}
{% if t.mentioned_user and t.mentioned_user!=t.username %}<div>Mentions @{{t.mentioned_user}}</div>{% endif %}
<b>[{{t.timestamp or 'Unknown'}}] @{{t.username}}</b>: {{t.content}}
{% if t.tweet_url %}<p><a href="{{t.tweet_url}}" target="_blank">View on X</a></p>{% endif %}
</div>
{% endfor %}
</body></html>
""", account_name=account_name, feed_type=feed_type, tweets=tweets, rts=rts, errs=errs, live=live)

if __name__=="__main__":
    try:
        app.run(debug=True, port=5000, use_reloader=False)
    finally:
        cleanup_browsers()
