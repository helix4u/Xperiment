import time
import json
import requests
import re
import threading  # for background thread
from pathlib import Path
from flask import Flask, request, redirect, url_for, render_template_string
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

app = Flask(__name__)

########################################
# Helper Functions
########################################

def extract_json_content(text: str):
    """Parses JSON from LLM response, removing code fences if present."""
    text = re.sub(r'^```(?:json)?|```$', '', text.strip(), flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print("[JSON ERROR] Failed to parse LLM response:")
        print(text)
        print(f"Error: {e}")
        return None

def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02}:{s:02}"

########################################
# Scraping + Commentary Logic
########################################

def scrape_and_comment(
    account_name: str,
    model: str = "deepseek-r1-distill-llama-8b-abliterated",
    api_url: str = "http://127.0.0.1:1234/v1/chat/completions",
    rolling_context_length: int = 10,
    total_scrolls: int = 50,
    scroll_delay: float = 2.5,
):
    """
    Scrapes tweets from https://x.com/<account_name>/with_replies using a local persistent profile.
    Now includes parsing the originating user handle in each tweet for richer context.
    Additionally, every 10 new tweets a summary report is generated based on the rolling context.
    """
    # Derive subfolder for data
    subfolder = Path(account_name)
    subfolder.mkdir(parents=True, exist_ok=True)

    # Prepare file paths
    tweet_file = subfolder / "tweets.jsonl"
    commentary_file = subfolder / "commentary.jsonl"
    summary_file = subfolder / "summary.jsonl"

    # Ensure files exist
    tweet_file.touch(exist_ok=True)
    commentary_file.touch(exist_ok=True)
    summary_file.touch(exist_ok=True)

    # Build the target URL
    x_replies_url = f"https://x.com/{account_name}/with_replies"

    # Prepare our LLM conversation context for individual tweet commentary
    system_prompt = ()
    context = [{"role": "system", "content": system_prompt}]

    # Additional system prompt for summary reports:
    summary_prompt = (
        "Please produce a concise summary report of the following block "
        "of tweets and commentary. Highlight major themes, recurring patterns, and insights. "
        "Your summary should be clear, darkly witty, and provide context for individuals and the overall narrative."
    )

    # Track seen timestamps from commentary file
    seen_timestamps = set()
    with commentary_file.open("r", encoding="utf-8") as cf:
        for line in cf:
            try:
                obj = json.loads(line)
                ts = obj.get("timestamp")
                if ts:
                    seen_timestamps.add(ts)
            except:
                continue

    # Prepare a block container to accumulate the last 10 tweets and commentary for summary
    block_tweets = []

    profile_dir = Path(".chromium-profile").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    start_time = time.time()
    total_collected = 0

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = browser.pages[0] if browser.pages else browser.new_page()

        print(f"[INIT] Navigating to {x_replies_url}")
        page.goto(x_replies_url, timeout=60000)

        print("[WAIT] Let page stabilize for 5 seconds.")
        time.sleep(5)

        scrolls_done = 0
        while scrolls_done < total_scrolls:
            page.mouse.wheel(0, 3000)
            time.sleep(scroll_delay)
            scrolls_done += 1

            tweets = page.locator("article").all()
            new_tweets_this_scroll = 0

            for idx, tweet in enumerate(tweets):
                try:
                    # Grab time element
                    time_locator = tweet.locator("time").nth(0)
                    timestamp = time_locator.get_attribute("datetime")
                    if not timestamp:
                        continue
                    if timestamp in seen_timestamps:
                        continue

                    # Attempt to parse the 'from user' handle.
                    from_user_handle = ""
                    try:
                        user_anchor = tweet.locator("a[role='link'][href^='/']").first
                        href_val = user_anchor.get_attribute("href")
                        if href_val and len(href_val.split("/")) == 2:
                            from_user_handle = href_val.replace("/", "").strip()
                    except Exception:
                        pass

                    # Grab tweet content
                    content_locator = tweet.locator("div[lang]").nth(0)
                    content = content_locator.inner_text(timeout=3000).strip()
                    if not content:
                        continue

                    seen_timestamps.add(timestamp)

                    tweet_dict = {
                        "timestamp": timestamp,
                        "from_user": from_user_handle,
                        "content": content,
                    }
                    # Save tweet record
                    with tweet_file.open("a", encoding="utf-8") as tf:
                        tf.write(json.dumps(tweet_dict, ensure_ascii=False) + "\n")
                    new_tweets_this_scroll += 1
                    total_collected += 1

                    # Prepare LLM user message for commentary
                    user_label = from_user_handle or "someone"
                    user_msg = f"@{user_label} tweeted: {content}. Summarize it in context. Use <think>thoughts</think> tags before responding. Craft a narrative of that flows."
                    context.append({"role": "user", "content": user_msg})
                    if len(context) > (rolling_context_length + 1):
                        context = [context[0]] + context[-rolling_context_length:]

                    payload = {
                        "model": model,
                        "messages": context,
                        "temperature": 0.7,
                        "max_tokens": 4096,
                        "stream": False,
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "commentary_response",
                                "strict": True,
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "commentary": {"type": "string"}
                                    },
                                    "required": ["commentary"]
                                }
                            }
                        }
                    }
                    try:
                        res = requests.post(api_url, json=payload)
                        res.raise_for_status()
                        response_json = res.json()
                        llm_raw = response_json["choices"][0]["message"]["content"]
                        commentary_obj = extract_json_content(llm_raw)
                        if not commentary_obj:
                            continue
                        commentary_text = commentary_obj.get("commentary", "").strip()
                        if not commentary_text:
                            continue
                        context.append({"role": "assistant", "content": commentary_text})
                        out_dict = {
                            "timestamp": timestamp,
                            "from_user": from_user_handle,
                            "content": content,
                            "llm_commentary": commentary_text,
                        }
                        with commentary_file.open("a", encoding="utf-8") as cf:
                            cf.write(json.dumps(out_dict, ensure_ascii=False) + "\n")
                        block_tweets.append(out_dict)
                        snippet = commentary_text[:80] + ("..." if len(commentary_text) > 80 else "")
                        print(f"[COMMENTARY] {timestamp} (@{from_user_handle}): {snippet}")
                    except Exception as e:
                        print(f"[ERROR] LLM commentary generation failed: {e}")
                        continue

                    # Every 10 new tweets, produce a summary report
                    if len(block_tweets) >= 10:
                        # Build the summary prompt
                        block_summary_prompt = (
                            summary_prompt + "\n\n" +
                            "\n\n".join(
                                f"Tweet from @{item['from_user'] or 'someone'} at {item['timestamp']}:\n{item['content']}\nCommentary: {item['llm_commentary']}"
                                for item in block_tweets
                            )
                        )
                        summary_context = [
                            {"role": "system", "content": ""},
                            {"role": "user", "content": block_summary_prompt}
                        ]
                        summary_payload = {
                            "model": model,
                            "messages": summary_context,
                            "temperature": 0.7,
                            "max_tokens": 4096,
                            "stream": False,
                            "response_format": {
                                "type": "json_schema",
                                "json_schema": {
                                    "name": "summary_response",
                                    "strict": True,
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "summary": {"type": "string"}
                                        },
                                        "required": ["summary"]
                                    }
                                }
                            }
                        }
                        try:
                            res_sum = requests.post(api_url, json=summary_payload)
                            res_sum.raise_for_status()
                            sum_response_json = res_sum.json()
                            sum_llm_raw = sum_response_json["choices"][0]["message"]["content"]
                            summary_obj = extract_json_content(sum_llm_raw)
                            if summary_obj and summary_obj.get("summary", "").strip():
                                summary_text = summary_obj["summary"].strip()
                                summary_record = {
                                    "block_tweets": [item["timestamp"] for item in block_tweets],
                                    "summary": summary_text,
                                    "generated_at": format_time(time.time() - start_time)
                                }
                                with summary_file.open("a", encoding="utf-8") as sf:
                                    sf.write(json.dumps(summary_record, ensure_ascii=False) + "\n")
                                print(f"[SUMMARY] Block summary generated for tweets: {summary_record['block_tweets']}")
                            else:
                                print("[WARNING] Summary response was empty.")
                        except Exception as e:
                            print(f"[ERROR] Summary generation failed: {e}")
                        # Reset block after generating summary
                        block_tweets = []

                except PlaywrightTimeoutError:
                    print(f"[SKIP] Timeout reading content for tweet #{idx}.")
                    continue
                except Exception as e:
                    print(f"[ERROR] Skipping tweet #{idx} due to: {e}")
                    continue

            elapsed = time.time() - start_time
            print(
                f"Scroll {scrolls_done}/{total_scrolls} | New: {new_tweets_this_scroll} | "
                f"Total: {total_collected} | Elapsed: {format_time(elapsed)}"
            )

        browser.close()

    print("\\nScraping and commentary complete.")
    print(f"Total tweets collected: {total_collected}")
    print(f"Total time elapsed: {format_time(time.time() - start_time)}")

########################################
# Minimal Flask UI
########################################

# We'll spin up a background thread for the scraping, so the page can refresh while it runs.
# This dictionary will hold references to scraping threads by account name
scrape_threads = {}

form_template = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Scrape X Account</title>
</head>
<body style="font-family: sans-serif; margin: 2rem;">
    <h1>Scrape X Account & Generate Commentary</h1>
    <form method="POST" action="/">
        <label for="account_name">Account Name (without @):</label>
        <input type="text" name="account_name" id="account_name" required>
        <button type="submit">Scrape & Comment (Live)</button>
    </form>
    <hr>
    <p><strong>Usage:</strong> Enter the account handle (e.g. <code>elonmusk</code>) then click "Scrape & Comment".
       The script will open a persistent browser session, scroll through the user's <code>with_replies</code> feed,
       generate commentary for each new tweet, and every 10 tweets produce a summary report. The results are stored
       in subfolders under the account name.</p>
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        account = request.form.get("account_name", "").strip()
        if not account:
            return "<p>Invalid account name.</p>"
        # Spawn a background thread so we don't block
        if account not in scrape_threads or not scrape_threads[account].is_alive():
            t = threading.Thread(target=scrape_and_comment, args=(account,))
            t.start()
            scrape_threads[account] = t
        # pass live=1 so the feed page auto-refreshes
        return redirect(url_for("feed", account_name=account, live="1"))
    else:
        return form_template

feed_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Commentary Feed</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 2rem; background: #f5f5f5; }
        .entry { background: #fff; padding: 1rem; margin-bottom: 1rem; border-radius: 6px; box-shadow: 0 0 5px rgba(0,0,0,0.1); }
        .timestamp { font-size: 0.9rem; color: #666; }
        .content { font-weight: bold; margin-top: 0.5rem; }
        .commentary { margin-top: 0.5rem; }
        .summary { background: #e0e0e0; padding: 0.5rem; margin-top: 1rem; border-radius: 4px; }
        a { text-decoration: none; color: blue; }
    </style>
    {% if live == '1' %}
    <!-- If live mode is on, auto-refresh every 15s. -->
    <meta http-equiv="refresh" content="15" />
    {% endif %}
</head>
<body>
    <h1>Commentary Feed for {{ account_name }}</h1>
    <a href="/">Back to Scraper Form</a>
    {% if live == '1' %}
      <p>Live Mode: this page refreshes every 15 seconds until scraping is finished.</p>
    {% else %}
      <p>Static Mode: <a href="{{ url_for('feed', account_name=account_name, live='1') }}">Enable Live Refresh</a></p>
    {% endif %}
    <hr>
    <h2>Tweets & Commentary</h2>
    {% for item in feed_data %}
    <div class="entry">
        <div class="timestamp">{{ item.timestamp }}</div>
        <div class="content">@{{ item.from_user }} tweeted: {{ item.content }}</div>
        <div class="commentary"><em>Commentary:</em> {{ item.llm_commentary }}</div>
    </div>
    {% endfor %}
    <hr>
    <h2>Summary Reports</h2>
    {% for sum_item in summaries %}
    <div class="summary">
        <div class="timestamp">Block (tweets: {{ sum_item.block_tweets|join(', ') }}) | Generated at: {{ sum_item.generated_at }}</div>
        <div class="commentary"><em>Summary:</em> {{ sum_item.summary }}</div>
    </div>
    {% endfor %}

    <!-- SCROLL PRESERVATION SCRIPT -->
    <script>
      // On DOM ready, restore scroll from localStorage
      document.addEventListener("DOMContentLoaded", function() {
        const savedPos = localStorage.getItem("scrollPos");
        if (savedPos) {
          window.scrollTo(0, parseInt(savedPos, 10));
        }
      });

      // Before leaving/refreshing, save current scroll
      window.addEventListener("beforeunload", function() {
        localStorage.setItem("scrollPos", window.scrollY.toString());
      });
    </script>
</body>
</html>
"""

@app.route("/feed/<account_name>")
def feed(account_name):
    live = request.args.get("live", "0")  # '1' => auto-refresh
    subfolder = Path(account_name)
    commentary_file = subfolder / "commentary.jsonl"
    summary_file = subfolder / "summary.jsonl"

    if not commentary_file.exists():
        return f"<p>No data found for account '{account_name}'.</p>"

    feed_data = []
    with commentary_file.open("r", encoding="utf-8") as cf:
        for line in cf:
            try:
                obj = json.loads(line)
                feed_data.append(obj)
            except:
                continue

    summaries = []
    if summary_file.exists():
        with summary_file.open("r", encoding="utf-8") as sf:
            for line in sf:
                try:
                    obj = json.loads(line)
                    summaries.append(obj)
                except:
                    continue

    # Sort tweets newest-first
    feed_data.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    # Summaries are sorted by 'generated_at' in ascending order
    summaries.sort(key=lambda x: x.get("generated_at", ""))

    # If the background thread is no longer alive, turn off live mode automatically
    if account_name in scrape_threads:
        if not scrape_threads[account_name].is_alive():
            live = "0"

    return render_template_string(
        feed_template,
        account_name=account_name,
        feed_data=feed_data,
        summaries=summaries,
        live=live
    )

if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
