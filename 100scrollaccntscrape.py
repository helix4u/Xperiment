import time
import json
import threading
import signal
import sys
from pathlib import Path
from flask import Flask, request, redirect, url_for, render_template_string
from playwright.sync_api import sync_playwright

app = Flask(__name__)
scrape_threads = {}
browser_contexts = {}

# Handle termination signals
def signal_handler(sig, frame):
    print("[INFO] Shutting down and cleaning up browser processes...")
    for account_name, context in browser_contexts.items():
        try:
            context.close()
            print(f"[INFO] Closed browser for {account_name}")
        except:
            pass
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def scrape_tweets(account_name: str, total_scrolls: int = 100):
    subfolder = Path(account_name)
    subfolder.mkdir(parents=True, exist_ok=True)
    tweet_file = subfolder / "tweets.jsonl"
    tweet_file.touch(exist_ok=True)
    
    seen_ids = set()
    with tweet_file.open("r", encoding="utf-8") as tf:
        for line in tf:
            try:
                tweet = json.loads(line)
                if "id" in tweet:
                    seen_ids.add(tweet["id"])
            except:
                continue
    
    profile_dir = Path(".chromium-profile").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    total_collected = 0
    total_retweets = 0
    
    playwright = None
    browser_context = None

    try:
        playwright = sync_playwright().start()
        browser_context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        browser_contexts[account_name] = browser_context
        page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
        
        x_replies_url = f"https://x.com/{account_name}/with_replies"
        print(f"[INFO] Opening {x_replies_url}")
        page.goto(x_replies_url, timeout=20000)
        page.wait_for_selector("article", timeout=20000, state="attached")
        time.sleep(2)
        
        try:
            page.keyboard.press("Escape")
            time.sleep(1)
        except:
            pass
            
        for scroll in range(total_scrolls):
            print(f"[PROGRESS] Scroll {scroll+1}/{total_scrolls}")
            
            pre_stats = page.evaluate("""
                () => {
                    const arts = document.querySelectorAll('article');
                    let len = 0;
                    arts.forEach(a => { len += (a.textContent||'').length; });
                    return { count: arts.length, length: len };
                }
            """)
            
            expansions = 0
            for _ in range(3):
                clicked = page.evaluate("""
                    () => {
                        const buttons = Array.from(document.querySelectorAll('[role="button"]'))
                          .filter(btn => {
                            const txt = (btn.textContent||'').toLowerCase();
                            if (!txt.includes('show more')) return false;
                            if (btn.closest('a[href*="/i/grok/share/"]')) return false;
                            return true;
                          });
                        if (buttons.length === 0) return false;
                        try {
                            buttons[0].click();
                            return true;
                        } catch {
                            return false;
                        }
                    }
                """)
                if not clicked:
                    break
                expansions += 1
                print(f"[INFO] Clicked 'Show more' ({expansions})")
                time.sleep(2)
                
                post_stats = page.evaluate("""
                    () => {
                        const arts = document.querySelectorAll('article');
                        let len = 0;
                        arts.forEach(a => { len += (a.textContent||'').length; });
                        return { count: arts.length, length: len };
                    }
                """)
                if post_stats['length'] <= pre_stats['length'] * 1.05:
                    break
                pre_stats = post_stats
            
            if expansions:
                print(f"[INFO] Performed {expansions} expansions")
                time.sleep(1.5)
            
            tweets_data = page.evaluate("""
                () => {
                    const extractText = el => el ? el.innerText || el.textContent || '' : '';
                    const extractUser = url => {
                        const parts = url.split('/');
                        const idx = parts.indexOf('status');
                        return idx > 0 ? parts[idx - 1] : null;
                    };
                    const results = [];
                    document.querySelectorAll('article').forEach(art => {
                        try {
                            let url = '', username = '', tweetId = '', time = '';
                            const timeEl = art.querySelector('time');
                            if (timeEl) {
                                time = timeEl.getAttribute('datetime') || '';
                                const link = timeEl.closest('a');
                                if (link) {
                                    let href = link.getAttribute('href');
                                    if (href.startsWith('/')) href = 'https://x.com' + href;
                                    url = href;
                                    username = extractUser(href) || 'unknown';
                                    const m = href.match(/status\\/(\\d+)/);
                                    tweetId = m ? m[1] : '';
                                }
                            }
                            if (!url) {
                                const alt = Array.from(
                                    art.querySelectorAll('a[href*="/status/"]')
                                )[0];
                                if (alt) {
                                    let h = alt.getAttribute('href');
                                    if (h.startsWith('/')) h = 'https://x.com' + h;
                                    url = h;
                                    username = extractUser(h) || 'unknown';
                                    const m2 = h.match(/status\\/(\\d+)/);
                                    tweetId = m2 ? m2[1] : '';
                                }
                            }
                            let isRetweet = false, retBy = null;
                            const sc = art.querySelector('[data-testid="socialContext"]');
                            if (sc && /repost/i.test(sc.textContent || '')) {
                                isRetweet = true;
                                const m3 = (sc.textContent || '').match(/(.+?)\\s+repost/i);
                                retBy = m3 ? m3[1].trim() : null;
                            }
                            if (
                                !isRetweet &&
                                username.toLowerCase() !==
                                    window.location.pathname.split('/')[1].toLowerCase()
                            ) {
                                isRetweet = true;
                                retBy = window.location.pathname.split('/')[1];
                            }
                            const mentionLink = Array.from(
                                art.querySelectorAll('a[role="link"]')
                            ).find(a => {
                                const h2 = a.getAttribute('href') || '';
                                return h2.startsWith('/') && !h2.includes('/status/');
                            });
                            const mentioned = mentionLink
                                ? mentionLink.getAttribute('href').replace('/', '')
                                : null;
                            let content =
                                extractText(art.querySelector('div[lang]')) ||
                                extractText(
                                    art.querySelector('[data-testid="tweetText"]')
                                );
                            if (!content) {
                                for (const el of art.querySelectorAll('div[dir="auto"]')) {
                                    const t = extractText(el).trim();
                                    if (t.length > 5) {
                                        content = t;
                                        break;
                                    }
                                }
                            }
                            if (!content) content = extractText(art).trim();
                            const id = tweetId || `${username}_${time}`;
                            results.push({
                                id,
                                timestamp: time,
                                username,
                                mentioned_user: mentioned,
                                content: content || '',
                                is_retweet: isRetweet,
                                retweeted_by: isRetweet ? retBy : null,
                                tweet_url: url
                            });
                        } catch (e) {
                            console.error('Error processing tweet:', e);
                        }
                    });
                    return results;
                }
            """)
            
            print(f"[INFO] Found {len(tweets_data)} tweets in view")
            new_count = 0
            ret_count = 0
            for tw in tweets_data:
                tid = tw.get("id")
                if not tid or tid in seen_ids:
                    continue
                seen_ids.add(tid)
                with tweet_file.open("a", encoding="utf-8") as tf:
                    tf.write(json.dumps(tw, ensure_ascii=False) + "\n")
                new_count += 1
                total_collected += 1
                if tw.get("is_retweet"):
                    ret_count += 1
                    total_retweets += 1
            print(f"[INFO] Saved {new_count} new tweets ({ret_count} retweets)")
            
            page.evaluate("window.scrollBy({top:2000,behavior:'smooth'})")
            time.sleep(2)
    
    except Exception as e:
        print(f"[ERROR] Scraping failed: {e}")
    
    finally:
        print(f"[INFO] Cleaning up browser for {account_name}")
        if account_name in browser_contexts:
            try:
                browser_contexts[account_name].close()
                del browser_contexts[account_name]
            except:
                pass
        if browser_context:
            try:
                browser_context.close()
            except:
                pass
        if playwright:
            try:
                playwright.stop()
            except:
                pass
    
    print(f"[DONE] Collected {total_collected} tweets ({total_retweets} retweets)")

def cleanup_browsers():
    print("[INFO] Flask shutting down—cleaning browsers")
    for acct, ctx in list(browser_contexts.items()):
        try:
            ctx.close()
        except:
            pass
    browser_contexts.clear()

import atexit
atexit.register(cleanup_browsers)

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        acct = request.form.get("account_name", "").strip()
        if not acct:
            return "<p>Invalid account name.</p>"
        if acct not in scrape_threads or not scrape_threads[acct].is_alive():
            t = threading.Thread(target=scrape_tweets, args=(acct,))
            t.start()
            scrape_threads[acct] = t
        return redirect(url_for("feed", account_name=acct, live="1"))
    return """
    <html><body>
    <form method="POST">
      <label>Account</label>
      <input name="account_name" required>
      <button>Start</button>
    </form>
    </body></html>
    """

@app.route("/feed/<account_name>")
def feed(account_name):
    live = request.args.get("live", "0")
    sub = Path(account_name)
    tweet_file = sub / "tweets.jsonl"
    if not tweet_file.exists():
        return f"<p>No data for {account_name}.</p>"
    tweets = []
    retweets = 0
    errors = 0
    for line in tweet_file.open("r", encoding="utf-8"):
        try:
            tw = json.loads(line)
            tweets.append(tw)
            if tw.get("is_retweet"):
                retweets += 1
            if tw.get("error"):
                errors += 1
        except:
            continue
    tweets.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    if account_name in scrape_threads and not scrape_threads[account_name].is_alive():
        live = "0"
    return render_template_string("""
    <html><body><h1>{{account_name}}</h1>
    {% if live=='1' %}
      <p><i>Scraping in progress... {{tweets|length}} tweets so far ({{retweets}} retweets, {{errors}} errors)</i></p>
      <p><a href="{{url_for('feed', account_name=account_name, live=1)}}">Refresh</a></p>
    {% else %}
      <p><i>{{tweets|length}} tweets collected ({{retweets}} retweets, {{errors}} errors)</i></p>
    {% endif %}
    {% for tw in tweets %}
      <div style="margin-bottom:20px;padding:10px;border:1px solid #ccc;{% if tw.error %}background-color:#ffeeee;{% endif %}">
        {% if tw.is_retweet %}
          <div style="color:#555;margin-bottom:5px;">Retweeted by {{tw.retweeted_by}}</div>
        {% endif %}
        {% if tw.mentioned_user and tw.mentioned_user!=tw.username %}
          <div style="color:#555;margin-bottom:5px;">Mentions: @{{tw.mentioned_user}}</div>
        {% endif %}
        <b>[{{tw.timestamp or 'Unknown time'}}] @{{tw.username}}</b>: {{tw.content}}
        {% if tw.tweet_url %}
          <p><a href="{{tw.tweet_url}}" target="_blank">View on X</a></p>
        {% endif %}
      </div>
    {% endfor %}
    </body></html>
    """, account_name=account_name, tweets=tweets,
       retweets=retweets, errors=errors, live=live)

if __name__ == "__main__":
    try:
        app.run(debug=True, port=5000, use_reloader=False)
    finally:
        cleanup_browsers()
