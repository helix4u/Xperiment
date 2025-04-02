import time
import json
import requests
import re
import threading
from pathlib import Path
from flask import Flask, request, redirect, url_for, render_template_string
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

app = Flask(__name__)
scrape_threads = {}

def extract_json_content(text: str):
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

def generate_overall_report(account_name, summary_path, output_path):
    summaries = []
    try:
        with open(summary_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    summaries.append(json.loads(line))
                except:
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
        "Do not summarize each block; synthesize trends across all of them.\n\n"
    )
    summaries_str = "\n\n".join(f"Summary:\n{entry['summary']}" for entry in summaries)
    messages = [
        {"role": "system", "content": ""},
        {"role": "user", "content": prompt + summaries_str}
    ]
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

def scrape_and_comment(
    account_name: str,
    model: str = "deepseek-r1-distill-llama-8b-abliterated",
    api_url: str = "http://127.0.0.1:1234/v1/chat/completions",
    rolling_context_length: int = 10,
    total_scrolls: int = 50,
    scroll_delay: float = 2.5,
):
    subfolder = Path(account_name)
    subfolder.mkdir(parents=True, exist_ok=True)

    tweet_file = subfolder / "tweets.jsonl"
    commentary_file = subfolder / "commentary.jsonl"
    summary_file = subfolder / "summary.jsonl"
    report_file = subfolder / "meta_summary.txt"

    tweet_file.touch(exist_ok=True)
    commentary_file.touch(exist_ok=True)
    summary_file.touch(exist_ok=True)

    x_replies_url = f"https://x.com/{account_name}/with_replies"
    context = [{"role": "system", "content": ""}]
    summary_prompt = (
        "Please produce a concise summary report of the following block "
        "of tweets and commentary. Highlight major themes, recurring patterns, and insights. "
        "Your summary should be clear, darkly witty, and provide context for individuals and the overall narrative."
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
        page.goto(x_replies_url, timeout=60000)
        time.sleep(5)

        scrolls_done = 0
        while scrolls_done < total_scrolls:
            page.mouse.wheel(0, 3000)
            time.sleep(scroll_delay)
            scrolls_done += 1
            tweets = page.locator("article").all()

            for tweet in tweets:
                try:
                    timestamp = tweet.locator("time").nth(0).get_attribute("datetime")
                    if not timestamp or timestamp in seen_timestamps:
                        continue

                    from_user = ""
                    try:
                        anchor = tweet.locator("a[role='link'][href^='/']").first
                        href = anchor.get_attribute("href")
                        if href and len(href.split("/")) == 2:
                            from_user = href.replace("/", "").strip()
                    except:
                        pass

                    content = tweet.locator("div[lang]").nth(0).inner_text(timeout=3000).strip()
                    if not content:
                        continue

                    seen_timestamps.add(timestamp)
                    tweet_dict = {
                        "timestamp": timestamp,
                        "from_user": from_user,
                        "content": content
                    }
                    with tweet_file.open("a", encoding="utf-8") as tf:
                        tf.write(json.dumps(tweet_dict, ensure_ascii=False) + "\n")
                    total_collected += 1

                    user_msg = f"@{from_user or 'someone'} tweeted: {content}. Summarize it in context. Use <think>thoughts</think> tags before responding."
                    context.append({"role": "user", "content": user_msg})
                    if len(context) > (rolling_context_length + 1):
                        context = [context[0]] + context[-rolling_context_length:]

                    payload = {
                        "model": model,
                        "messages": context,
                        "temperature": 0.7,
                        "max_tokens": 2048,
                        "stream": False
                    }

                    res = requests.post(api_url, json=payload)
                    res.raise_for_status()
                    llm_raw = res.json()["choices"][0]["message"]["content"]
                    commentary = llm_raw.strip()
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
                        summary_prompt_block = summary_prompt + "\n\n" + "\n\n".join(
                            f"Tweet from @{item['from_user']} at {item['timestamp']}:\n{item['content']}\nCommentary:\n{item['llm_commentary']}"
                            for item in block_tweets)
                        summary_payload = {
                            "model": model,
                            "messages": [
                                {"role": "system", "content": ""},
                                {"role": "user", "content": summary_prompt_block}
                            ],
                            "temperature": 0.7,
                            "max_tokens": 2048,
                            "stream": False
                        }
                        res_sum = requests.post(api_url, json=summary_payload)
                        res_sum.raise_for_status()
                        sum_text = res_sum.json()["choices"][0]["message"]["content"].strip()
                        with summary_file.open("a", encoding="utf-8") as sf:
                            sf.write(json.dumps({
                                "block_tweets": [item["timestamp"] for item in block_tweets],
                                "summary": sum_text,
                                "generated_at": format_time(time.time() - start_time)
                            }, ensure_ascii=False) + "\n")
                        block_tweets = []
                except Exception as e:
                    print(f"[ERROR] tweet error: {e}")
                    continue

        browser.close()

    generate_overall_report(account_name, summary_file, report_file)
    print(f"[DONE] {total_collected} tweets processed. Final report saved to: {report_file}")

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        account = request.form.get("account_name", "").strip()
        if not account:
            return "<p>Invalid account name.</p>"
        if account not in scrape_threads or not scrape_threads[account].is_alive():
            t = threading.Thread(target=scrape_and_comment, args=(account,))
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

    feed_data, summaries = [], []
    with commentary_file.open("r", encoding="utf-8") as cf:
        for line in cf:
            try: feed_data.append(json.loads(line))
            except: continue

    if summary_file.exists():
        with summary_file.open("r", encoding="utf-8") as sf:
            for line in sf:
                try: summaries.append(json.loads(line))
                except: continue

    feed_data.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    summaries.sort(key=lambda x: x.get("generated_at", ""))

    if account_name in scrape_threads and not scrape_threads[account_name].is_alive():
        live = "0"

    return render_template_string("""
    <html><body><h1>{{account_name}}</h1>
    {% for item in feed_data %}
    <div><b>@{{item.from_user}}</b>: {{item.content}}<br><i>{{item.llm_commentary}}</i></div>
    {% endfor %}
    <hr>
    {% for sum in summaries %}
    <div><b>Summary Block:</b><br>{{sum.summary}}</div>
    {% endfor %}
    </body></html>
    """, account_name=account_name, feed_data=feed_data, summaries=summaries, live=live)

if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
