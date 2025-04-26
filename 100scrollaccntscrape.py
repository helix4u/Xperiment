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
    
    # Track already seen tweets
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
        
        # Store for proper cleanup
        browser_contexts[account_name] = browser_context
        
        page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
        
        # Navigate to profile
        x_replies_url = f"https://x.com/{account_name}/with_replies"
        print(f"[INFO] Opening {x_replies_url}")
        page.goto(x_replies_url, timeout=20000)
        page.wait_for_selector("article", timeout=20000, state="attached")
        time.sleep(2)
        
        # Dismiss any popups/overlays
        try:
            page.keyboard.press("Escape")
            time.sleep(1)
        except:
            pass
            
        for scroll in range(total_scrolls):
            print(f"[PROGRESS] Scroll {scroll+1}/{total_scrolls}")
            
            # IMPROVED: First expand ALL "Show more" content and wait for it to load
            # Get current article count and content length before expansion
            pre_expansion_stats = page.evaluate("""
                () => {
                    const articles = document.querySelectorAll('article');
                    let totalTextLength = 0;
                    
                    articles.forEach(article => {
                        totalTextLength += article.textContent.length;
                    });
                    
                    return {
                        articleCount: articles.length,
                        textLength: totalTextLength
                    };
                }
            """)
            
            # Try to expand "Show more" content - wait until no more expansions happen
            expansions_performed = 0
            max_expansion_attempts = 3  # Limit how many times we try to expand content
            
            for _ in range(max_expansion_attempts):
                show_more_clicked = page.evaluate("""
                    () => {
                        // Get all show more buttons
                        const buttons = Array.from(document.querySelectorAll('[role="button"]'))
                            .filter(btn => btn.textContent && btn.textContent.includes('Show more'));
                        
                        if (buttons.length === 0) return false;
                        
                        // Click all of them
                        let clickedAny = false;
                        buttons.forEach(btn => {
                            try {
                                btn.click();
                                clickedAny = true;
                            } catch(e) {
                                // Ignore click errors
                            }
                        });
                        
                        return clickedAny;
                    }
                """)
                
                if show_more_clicked:
                    print(f"[INFO] Clicked 'Show more' buttons, waiting for content to load...")
                    expansions_performed += 1
                    
                    # Wait for DOM to update - we need a solid pause here
                    time.sleep(1.5)
                    
                    # Verify content has actually expanded
                    post_expansion_stats = page.evaluate("""
                        () => {
                            const articles = document.querySelectorAll('article');
                            let totalTextLength = 0;
                            
                            articles.forEach(article => {
                                totalTextLength += article.textContent.length;
                            });
                            
                            return {
                                articleCount: articles.length,
                                textLength: totalTextLength
                            };
                        }
                    """)
                    
                    # If text length increased significantly, expansion worked
                    if post_expansion_stats["textLength"] > pre_expansion_stats["textLength"] * 1.1:
                        print(f"[INFO] Content expanded successfully: {pre_expansion_stats['textLength']} → {post_expansion_stats['textLength']} chars")
                        pre_expansion_stats = post_expansion_stats
                    else:
                        print("[INFO] No significant content expansion detected, continuing...")
                        break
                else:
                    # No more show more buttons found
                    break
            
            if expansions_performed > 0:
                print(f"[INFO] Completed {expansions_performed} expansion(s), content is now ready for scraping")
                # Extra wait to make absolutely sure everything is loaded
                time.sleep(1)
            
            # NOW extract tweets after expansion is complete
            tweets_data = page.evaluate("""
                () => {
                    const extractText = (element) => {
                        return element ? element.innerText || element.textContent || '' : '';
                    };
                    
                    // Helper to extract username from URL
                    const extractUsernameFromUrl = (url) => {
                        if (!url) return null;
                        
                        // Parse URL to get the actual tweet author
                        // Twitter URLs are like: https://x.com/USERNAME/status/ID
                        const urlParts = url.split('/');
                        const statusIndex = urlParts.indexOf('status');
                        
                        // If we found 'status' in the URL, the username should be right before it
                        if (statusIndex > 0 && statusIndex - 1 >= 0) {
                            return urlParts[statusIndex - 1];
                        }
                        return null;
                    };
                    
                    const tweets = [];
                    const articles = document.querySelectorAll('article');
                    
                    articles.forEach(article => {
                        try {
                            // Initialize variables with defaults
                            let isRetweet = false;
                            let retweetedBy = '';
                            let username = '';
                            let mentionedUser = ''; // Additional var for mentioned users
                            let content = '';
                            let tweetUrl = '';
                            let tweetId = '';
                            let timestamp = '';
                            
                            // FIRST: Get tweet URL and extract the author from it
                            // This is the source of truth for who actually posted the tweet
                            const timeElement = article.querySelector('time');
                            if (timeElement) {
                                timestamp = timeElement.getAttribute('datetime') || '';
                                
                                // Get parent link (tweet URL)
                                const timeParent = timeElement.closest('a');
                                if (timeParent && timeParent.getAttribute('href')) {
                                    const href = timeParent.getAttribute('href');
                                    tweetUrl = href.startsWith('/') ? 'https://x.com' + href : href;
                                    
                                    // Extract username from the URL - this is the ACTUAL author
                                    username = extractUsernameFromUrl(tweetUrl);
                                    
                                    // Extract ID from URL
                                    const urlParts = href.split('/');
                                    const statusIndex = urlParts.indexOf('status');
                                    if (statusIndex >= 0 && statusIndex + 1 < urlParts.length) {
                                        tweetId = urlParts[statusIndex + 1];
                                    }
                                }
                            }
                            
                            // Backup URL finder if the time parent method failed
                            if (!tweetUrl || !username) {
                                const statusLinks = Array.from(article.querySelectorAll('a[href*="/status/"]'));
                                for (const link of statusLinks) {
                                    const href = link.getAttribute('href');
                                    if (href && href.includes('/status/')) {
                                        tweetUrl = href.startsWith('/') ? 'https://x.com' + href : href;
                                        username = extractUsernameFromUrl(tweetUrl);
                                        
                                        // Extract ID
                                        const match = href.match(/\\/status\\/(\\d+)/);
                                        if (match && match[1]) {
                                            tweetId = match[1];
                                        }
                                        break;
                                    }
                                }
                            }
                            
                            // Now detect if this is a retweet - using multiple methods
                            const rtPattern = /(.+?)\\s+reposted/i;
                            
                            // Method 1: Check spans for "reposted" text
                            const spans = Array.from(article.querySelectorAll('span'));
                            for (const span of spans) {
                                const text = extractText(span);
                                const match = text.match(rtPattern);
                                if (match && match[1]) {
                                    isRetweet = true;
                                    retweetedBy = match[1].trim();
                                    break;
                                }
                            }
                            
                            // Method 2: Check social context
                            if (!isRetweet) {
                                const socialContext = article.querySelector('[data-testid="socialContext"]');
                                if (socialContext) {
                                    const text = extractText(socialContext);
                                    if (text.includes('reposted') || text.includes('Reposted')) {
                                        isRetweet = true;
                                        const match = text.match(rtPattern);
                                        if (match && match[1]) {
                                            retweetedBy = match[1].trim();
                                        } else {
                                            retweetedBy = text.replace(/reposted/i, '').trim();
                                        }
                                    }
                                }
                            }
                            
                            // For retweets, if URL username doesn't match current profile, current profile is retweeter
                            if (username && username.toLowerCase() !== window.location.pathname.split('/')[1].toLowerCase() && !retweetedBy) {
                                isRetweet = true;
                                retweetedBy = window.location.pathname.split('/')[1];
                            }
                            
                            // Find all mentioned users - useful for tracking who's mentioned in the tweet
                            const userLinks = Array.from(article.querySelectorAll('a[role="link"]'))
                                .filter(a => a.getAttribute('href') && 
                                        a.getAttribute('href').startsWith('/') && 
                                        !a.getAttribute('href').includes('/status/'));
                            
                            if (userLinks.length > 0) {
                                // Just track the first mentioned user (might be useful)
                                const href = userLinks[0].getAttribute('href');
                                mentionedUser = href.replace('/', '');
                            }
                            
                            // Extract content using multiple methods
                            // Method 1: Language-tagged div (main content)
                            const langDiv = article.querySelector('div[lang]');
                            if (langDiv) {
                                content = extractText(langDiv);
                            }
                            
                            // Method 2: Tweet text element
                            if (!content) {
                                const tweetTextElement = article.querySelector('[data-testid="tweetText"]');
                                if (tweetTextElement) {
                                    content = extractText(tweetTextElement);
                                }
                            }
                            
                            // Method 3: Any substantial text in auto-direction divs
                            if (!content) {
                                const possibleContentElements = article.querySelectorAll('div[dir="auto"]');
                                for (const el of possibleContentElements) {
                                    const text = extractText(el).trim();
                                    // Look for reasonable tweet length text
                                    if (text.length > 5) {
                                        content = text;
                                        break;
                                    }
                                }
                            }
                            
                            // Method 4: Any text content at all
                            if (!content) {
                                content = extractText(article).trim();
                            }
                            
                            // Create a unique ID - use tweetId if available, otherwise create a composite
                            const id = tweetId || 
                                (timestamp && username ? 
                                `${username}_${timestamp}` : 
                                `unknown_${new Date().getTime()}_${Math.random().toString().substring(2)}`);
                            
                            // Always collect everything - don't filter out incomplete data
                            tweets.push({
                                id,
                                timestamp,
                                username: username || 'unknown',
                                mentioned_user: mentionedUser || null,
                                content: content || 'No content extracted',
                                is_retweet: isRetweet,
                                retweeted_by: isRetweet ? retweetedBy || 'unknown_retweeter' : null,
                                tweet_url: tweetUrl || null
                            });
                        } catch (e) {
                            console.error("Error processing tweet:", e);
                            // Fallback extraction on error
                            try {
                                const fallbackId = `fallback_${new Date().getTime()}_${Math.random().toString().substring(2)}`;
                                const rawText = article.textContent || '';
                                
                                tweets.push({
                                    id: fallbackId,
                                    timestamp: '',
                                    username: 'error_extracting',
                                    content: rawText.substring(0, 280) || 'Error extracting content',
                                    is_retweet: false,
                                    retweeted_by: null,
                                    tweet_url: null,
                                    error: true,
                                    error_message: e.toString()
                                });
                            } catch (fallbackError) {
                                // We tried
                            }
                        }
                    });
                    
                    return tweets;
                }
            """)
            
            # Process and save new tweets
            print(f"[INFO] Found {len(tweets_data)} potential tweets in current view")
            new_tweets = 0
            retweets_found = 0
            
            for tweet in tweets_data:
                tweet_id = tweet.get("id")
                if not tweet_id or tweet_id in seen_ids:
                    continue
                
                seen_ids.add(tweet_id)
                with tweet_file.open("a", encoding="utf-8") as tf:
                    tf.write(json.dumps(tweet, ensure_ascii=False) + "\n")
                total_collected += 1
                new_tweets += 1
                
                if tweet.get("is_retweet"):
                    retweets_found += 1
                    total_retweets += 1
            
            print(f"[INFO] Saved {new_tweets} new tweets (including {retweets_found} retweets) (total: {total_collected})")
            
            # Scroll down gently with JS
            page.evaluate("window.scrollBy({top: 2000, behavior: 'smooth'})")
            time.sleep(1.5)  # Give it time to load
    
    except Exception as e:
        print(f"[ERROR] Scraping failed: {e}")
    
    finally:
        # Clean up resources properly no matter what happened
        print(f"[INFO] Cleaning up browser resources for {account_name}")
        
        if account_name in browser_contexts:
            try:
                browser_contexts[account_name].close()
                del browser_contexts[account_name]
            except Exception as e:
                print(f"[ERROR] Failed to close browser context: {e}")
        
        if browser_context:
            try:
                if not browser_context.is_closed():
                    browser_context.close()
            except:
                pass
                
        if playwright:
            try:
                playwright.stop()
            except:
                pass
    
    print(f"[DONE] {total_collected} tweets collected (including {total_retweets} retweets) and saved to {tweet_file}")

# Cleanup function called when the Flask app exits
def cleanup_browsers():
    print("[INFO] Flask app shutting down, cleaning up browser processes...")
    for account_name, context in list(browser_contexts.items()):
        try:
            context.close()
            print(f"[INFO] Closed browser for {account_name}")
        except:
            pass
    browser_contexts.clear()

# Register cleanup on Flask exit
import atexit
atexit.register(cleanup_browsers)

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        account = request.form.get("account_name", "").strip()
        if not account:
            return "<p>Invalid account name.</p>"
        if account not in scrape_threads or not scrape_threads[account].is_alive():
            t = threading.Thread(target=scrape_tweets, args=(account,))
            t.start()
            scrape_threads[account] = t
        return redirect(url_for("feed", account_name=account, live="1"))
    return """
    <html><body>
    <form method="POST">
        <label>Account:</label><input name="account_name" required>
        <button>Start</button>
    </form>
    </body></html>
    """

@app.route("/feed/<account_name>")
def feed(account_name):
    live = request.args.get("live", "0")
    subfolder = Path(account_name)
    tweet_file = subfolder / "tweets.jsonl"

    if not tweet_file.exists():
        return f"<p>No data found for {account_name}.</p>"

    tweets = []
    retweets_count = 0
    error_tweets = 0
    with tweet_file.open("r", encoding="utf-8") as tf:
        for line in tf:
            try:
                tweet = json.loads(line)
                tweets.append(tweet)
                if tweet.get("is_retweet"):
                    retweets_count += 1
                if tweet.get("error"):
                    error_tweets += 1
            except:
                continue

    tweets.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    if account_name in scrape_threads and not scrape_threads[account_name].is_alive():
        live = "0"

    return render_template_string("""
    <html><body><h1>{{account_name}}</h1>
    {% if live == "1" %}
        <p><i>Scraping in progress... {{tweets|length}} tweets collected so far ({{retweets_count}} retweets, {{error_tweets}} errors)</i></p>
        <p><a href="{{url_for('feed', account_name=account_name, live=1)}}">Refresh</a></p>
    {% else %}
        <p><i>{{tweets|length}} tweets collected ({{retweets_count}} retweets, {{error_tweets}} errors)</i></p>
    {% endif %}
    {% for tweet in tweets %}
    <div style="margin-bottom: 20px; padding: 10px; border: 1px solid #ccc; {% if tweet.error %}background-color: #ffeeee;{% endif %}">
        {% if tweet.is_retweet %}
            <div style="color: #777; margin-bottom: 5px;">🔄 Retweeted by {{tweet.retweeted_by}}</div>
        {% endif %}
        {% if tweet.mentioned_user and tweet.mentioned_user != tweet.username %}
            <div style="color: #777; margin-bottom: 5px;">👤 Mentions: @{{tweet.mentioned_user}}</div>
        {% endif %}
        <b>[{{tweet.timestamp or 'Unknown time'}}] @{{tweet.username}}</b>: {{tweet.content}}
        {% if tweet.tweet_url %}
        <p><a href="{{tweet.tweet_url}}" target="_blank">View on X</a></p>
        {% endif %}
    </div>
    {% endfor %}
    </body></html>
    """, account_name=account_name, tweets=tweets, retweets_count=retweets_count, error_tweets=error_tweets, live=live)

if __name__ == "__main__":
    try:
        app.run(debug=True, port=5000, use_reloader=False)
    finally:
        # One final cleanup when Flask terminates
        cleanup_browsers()
