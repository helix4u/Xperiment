import time
import json
import requests
import re
import threading
from queue import Queue, Empty
from pathlib import Path
from flask import Flask, request, redirect, url_for, render_template_string
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

app = Flask(__name__)
scrape_threads = {}
llm_threads = {}

def strip_think_tags(text):
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02}:{s:02}"

def generate_overall_report(account_name, summary_path, output_path):
    summaries = []
    try:
        with open(summary_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    summaries.append(json.loads(line))
                except Exception as e:
                    continue
    except FileNotFoundError:
        print("[REPORT] No summary file found.")
        return

    if not summaries:
        print("[REPORT] No summaries to process.")
        return

    prompt = (
        "You are a narrative analyst. Given the following set of summary reports, "
        "produce a comprehensive meta-narrative about this account's behavior. Highlight attention, themes, changes in tone, and any psychological or strategic patterns. "
        "Do not summarize each block; synthesize trends across all of them. English only.\n\n"
    )
    summaries_str = "\n\n".join(f"Summary:\n{entry['summary']}" for entry in summaries)
    messages = [{"role": "user", "content": prompt + summaries_str}]
    payload = {
        "model": "deepseek-r1-distill-llama-8b-abliterated",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 4096,
        "stream": False
    }
    try:
        res = requests.post("http://127.0.0.1:1234/v1/chat/completions", json=payload)
        res.raise_for_status()
        result = res.json()
        content = result["choices"][0]["message"]["content"].strip()
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        print("[REPORT] Meta-narrative report saved to", output_path)
    except Exception as e:
        print(f"[REPORT ERROR] Failed to generate meta summary: {e}")

def scrape_worker(account_name: str, total_scrolls: int, scroll_delay: float, tweet_queue: Queue, seen_timestamps: set):
    """
    Continuously scrolls and collects tweets from the account page.
    Clicks all “Show more” buttons within each tweet.
    New tweets are written to a file and enqueued for LLM processing.
    """
    subfolder = Path(account_name)
    subfolder.mkdir(parents=True, exist_ok=True)
    tweet_file = subfolder / "tweets.jsonl"
    
    x_replies_url = f"https://x.com/{account_name}/with_replies"
    profile_dir = Path(".chromium-profile").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    total_collected = 0

    def dismiss_overlay(page):
        # Try pressing Escape to dismiss any modal overlay
        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)
        except:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(x_replies_url, timeout=60000)
        time.sleep(3)
        for _ in range(total_scrolls):
            # Scroll the page
            try:
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            except Exception as e:
                print(f"[SCROLL ERROR] {e}")
            time.sleep(scroll_delay)
            dismiss_overlay(page)
            
            tweets = page.locator("article").all()
            for tweet in tweets:
                try:
                    # Expand any "Show more" in this tweet
                    show_more_buttons = tweet.locator("xpath=.//span[contains(text(), 'Show more')]")
                    for i in range(show_more_buttons.count()):
                        try:
                            show_more_buttons.nth(i).scroll_into_view_if_needed()
                            show_more_buttons.nth(i).click()
                            dismiss_overlay(page)
                            time.sleep(0.3)
                        except Exception as e:
                            print(f"[SHOW MORE ERROR] {e}")
                            continue
                    
                    # Get tweet timestamp
                    try:
                        timestamp = tweet.locator("time").nth(0).get_attribute("datetime")
                    except Exception:
                        continue
                    if not timestamp or timestamp in seen_timestamps:
                        continue

                    seen_timestamps.add(timestamp)
                    
                    # Get username
                    from_user = ""
                    try:
                        anchor = tweet.locator("a[role='link'][href^='/']").first
                        href = anchor.get_attribute("href")
                        if href and len(href.split("/")) == 2:
                            from_user = href.replace("/", "").strip()
                    except Exception as e:
                        pass
                    # Get tweet content
                    try:
                        content = tweet.locator("div[lang]").nth(0).inner_text(timeout=3000).strip()
                    except Exception:
                        continue
                    if not content:
                        continue

                    tweet_dict = {
                        "timestamp": timestamp,
                        "from_user": from_user,
                        "content": content
                    }
                    with tweet_file.open("a", encoding="utf-8") as tf:
                        tf.write(json.dumps(tweet_dict, ensure_ascii=False) + "\n")
                    total_collected += 1

                    # Enqueue the tweet for LLM processing
                    tweet_queue.put(tweet_dict)
                except Exception as e:
                    print(f"[SCRAPE ERROR] {e}")
                    continue
        browser.close()
    print(f"[SCRAPER] Collected {total_collected} tweets for @{account_name}.")

def llm_worker(account_name: str, model: str, api_url: str,
               rolling_context_length: int, summary_prompt: str,
               start_time: float, tweet_queue: Queue):
    """
    Processes tweets from the queue:
    Calls the LLM to generate commentary,
    Writes tweet+commentary to commentary.jsonl,
    And every 10 tweets performs a summarization.
    """
    subfolder = Path(account_name)
    commentary_file = subfolder / "commentary.jsonl"
    summary_file = subfolder / "summary.jsonl"
    context = [{"role": "user", "content": ""}]
    block_tweets = []
    
    while True:
        try:
            tweet = tweet_queue.get(timeout=5)
        except Empty:
            # If queue empty and scraping thread is done, exit.
            if account_name not in scrape_threads or not scrape_threads[account_name].is_alive():
                break
            else:
                continue
        
        timestamp = tweet["timestamp"]
        from_user = tweet["from_user"]
        content = tweet["content"]
        
        user_msg = f"[{timestamp}] Tweet from @{from_user or 'someone'}:\n{content}\nWrite a brief psychological or strategic interpretation."
        context.append({"role": "user", "content": user_msg})
        if len(context) > (rolling_context_length + 1):
            context = [context[0]] + context[-rolling_context_length:]
        
        payload = {
            "model": model,
            "messages": context,
            "temperature": 0.8,
            "max_tokens": 1024,
            "stream": False
        }
        try:
            res = requests.post(api_url, json=payload)
            res.raise_for_status()
            llm_raw = res.json()["choices"][0]["message"]["content"]
            commentary = llm_raw.strip()
        except Exception as e:
            commentary = f"[LLM ERROR: {e}]"
        context.append({"role": "assistant", "content": commentary})
        
        out_dict = {
            "timestamp": timestamp,
            "from_user": from_user,
            "content": content,
            "llm_commentary": commentary
        }
        with commentary_file.open("a", encoding="utf-8") as cf:
            cf.write(json.dumps(out_dict, ensure_ascii=False) + "\n")
        block_tweets.append(out_dict)
        
        if len(block_tweets) >= 10:
            block_tweets.sort(key=lambda x: x["timestamp"])
            block_str = "\n\n".join(
                f"[{item['timestamp']}] @{item['from_user']} tweeted:\n{item['content']}\nLLM Commentary:\n{strip_think_tags(item['llm_commentary'])}"
                for item in block_tweets
            )
            summary_prompt_block = summary_prompt + "\n\n" + block_str
            summary_payload = {
                "model": model,
                "messages": [{"role": "user", "content": summary_prompt_block}],
                "temperature": 0.7,
                "max_tokens": 2048,
                "stream": False
            }
            try:
                res_sum = requests.post(api_url, json=summary_payload)
                res_sum.raise_for_status()
                sum_text = res_sum.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                sum_text = f"[SUMMARY ERROR: {e}]"
            with summary_file.open("a", encoding="utf-8") as sf:
                sf.write(json.dumps({
                    "block_tweets": [item["timestamp"] for item in block_tweets],
                    "summary": sum_text,
                    "generated_at": format_time(time.time() - start_time)
                }, ensure_ascii=False) + "\n")
            block_tweets = []
    
    # Process any remaining block
    if block_tweets:
        block_tweets.sort(key=lambda x: x["timestamp"])
        block_str = "\n\n".join(
            f"[{item['timestamp']}] @{item['from_user']} tweeted:\n{item['content']}\nLLM Commentary:\n{strip_think_tags(item['llm_commentary'])}"
            for item in block_tweets
        )
        summary_prompt_block = summary_prompt + "\n\n" + block_str
        summary_payload = {
            "model": model,
            "messages": [{"role": "user", "content": summary_prompt_block}],
            "temperature": 0.7,
            "max_tokens": 2048,
            "stream": False
        }
        try:
            res_sum = requests.post(api_url, json=summary_payload)
            res_sum.raise_for_status()
            sum_text = res_sum.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            sum_text = f"[SUMMARY ERROR: {e}]"
        with summary_file.open("a", encoding="utf-8") as sf:
            sf.write(json.dumps({
                "block_tweets": [item["timestamp"] for item in block_tweets],
                "summary": sum_text,
                "generated_at": format_time(time.time() - start_time)
            }, ensure_ascii=False) + "\n")
    
    generate_overall_report(account_name, str(summary_file), str(subfolder / "meta_summary.txt"))
    print(f"[LLM WORKER] Finished processing tweets for @{account_name}.")

def orchestrator(account_name: str,
                 model: str = "deepseek-r1-distill-llama-8b-abliterated",
                 api_url: str = "http://127.0.0.1:1234/v1/chat/completions",
                 rolling_context_length: int = 10,
                 total_scrolls: int = 50,
                 scroll_delay: float = 2.5):
    subfolder = Path(account_name)
    subfolder.mkdir(parents=True, exist_ok=True)
    commentary_file = subfolder / "commentary.jsonl"
    summary_file = subfolder / "summary.jsonl"
    commentary_file.touch(exist_ok=True)
    summary_file.touch(exist_ok=True)
    
    summary_prompt = (
        "The following tweets are from a public account, arranged in chronological order. Write a single narrative paragraph that captures shifts in tone, rhetorical strategy, and attention. "
        "Highlight bias, contradictions, or strategic intent. Avoid listing or rephrasing each tweet. This should feel like you're tracing a person's thinking or narrative arc over time. Use english only. \n\n"
    )
    
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
    
    start_time = time.time()
    tweet_queue = Queue()
    
    # Start LLM worker thread (non-blocking)
    t_llm = threading.Thread(
        target=llm_worker,
        args=(account_name, model, api_url, rolling_context_length, summary_prompt, start_time, tweet_queue),
        daemon=True
    )
    t_llm.start()
    llm_threads[account_name] = t_llm
    
    # Start scraping in this thread (scraping continues regardless of LLM speed)
    scrape_worker(account_name, total_scrolls, scroll_delay, tweet_queue, seen_timestamps)
    
    print(f"[ORCHESTRATOR] Scraping complete for @{account_name}.")
    # Do not join t_llm here so that generation continues in the background.

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        account = request.form.get("account_name", "").strip()
        if not account:
            return "<p>Invalid account name.</p>"
        if account not in scrape_threads or not scrape_threads[account].is_alive():
            t = threading.Thread(target=orchestrator, args=(account,))
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
    commentary_file = subfolder / "commentary.jsonl"
    summary_file = subfolder / "summary.jsonl"
    
    if not commentary_file.exists():
        return f"<p>No data found for {account_name}.</p>"
    
    feed_data = []
    with commentary_file.open("r", encoding="utf-8") as cf:
        for line in cf:
            try:
                feed_data.append(json.loads(line))
            except:
                continue
    
    summaries = []
    if summary_file.exists():
        with summary_file.open("r", encoding="utf-8") as sf:
            for line in sf:
                try:
                    summaries.append(json.loads(line))
                except:
                    continue
    feed_data.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    summaries.sort(key=lambda x: x.get("generated_at", ""))
    
    if account_name in scrape_threads and not scrape_threads[account_name].is_alive():
        live = "0"
    
    return render_template_string("""
    <html><body><h1>{{account_name}}</h1>
    {% if live == "1" %}
        <p><i>Scraping in progress (tweets are being scraped; LLM generation runs in the background)...</i></p>
    {% endif %}
    {% for item in feed_data %}
      <div>
        <b>[{{item.timestamp}}] @{{item.from_user}}</b>: {{item.content}}<br>
        <i>{{ strip_think_tags(item.llm_commentary) }}</i>
      </div>
      <hr>
    {% endfor %}
    
    <h2>Summaries:</h2>
    {% for sum in summaries %}
      <div style="margin-bottom:1em;">
        <b>Summary Block ({{sum.block_tweets[0]}} → {{sum.block_tweets[-1]}}):</b>
        <br>{{sum.summary}}
      </div>
    {% endfor %}
    
    </body></html>
    """, account_name=account_name, feed_data=feed_data, summaries=summaries, live=live, strip_think_tags=strip_think_tags)

if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
