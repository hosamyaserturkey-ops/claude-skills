"""Scrape X (Twitter) for active traders and write a deduplicated list to Excel.

Uses an Apify Twitter scraper actor to search for tweets matching trading-related
keywords, then aggregates by author so each trader appears once with their bio,
follower count, and a sample tweet.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import pandas as pd
from apify_client import ApifyClient


DEFAULT_KEYWORDS = [
    "Propr",
    "Prop Firm",
    "FTMO",
    "funded trader",
    "funded account",
    "prop trading",
    "day trading",
    "TopstepTrader",
    "MyForexFunds",
    "The5ers",
]

ACTOR_ID = "apidojo/tweet-scraper"


def resolve_token() -> str:
    for key in ("APIFY_API_TOKEN", "APIFY_TOKEN", "apify_api_token", "apify_token"):
        value = os.environ.get(key)
        if value:
            return value
    for key, value in os.environ.items():
        if key.lower().startswith("apify") and value and value.startswith("apify_api_"):
            return value
        if value and value.startswith("apify_api_"):
            return value
    for key in os.environ:
        if key.startswith("apify_api_"):
            return key
    raise SystemExit(
        "No Apify token found. Set APIFY_API_TOKEN=apify_api_xxx before running."
    )


def get_first(d: dict, *keys: str, default: Any = "") -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def extract_author(item: dict) -> dict | None:
    author = item.get("author") or item.get("user") or {}
    if not isinstance(author, dict):
        return None
    username = get_first(author, "userName", "username", "screen_name", "handle")
    if not username:
        return None
    return {
        "username": username,
        "displayName": get_first(author, "name", "fullName", "displayName"),
        "profileUrl": f"https://x.com/{username}",
        "bio": get_first(author, "description", "bio"),
        "followers": int(get_first(author, "followers", "followersCount", "followers_count", default=0) or 0),
        "following": int(get_first(author, "following", "followingCount", "friends_count", default=0) or 0),
        "verified": bool(get_first(author, "isVerified", "isBlueVerified", "verified", default=False)),
        "location": get_first(author, "location"),
        "joined": get_first(author, "createdAt", "created_at"),
        "statuses": int(get_first(author, "statusesCount", "statuses_count", "tweetsCount", default=0) or 0),
    }


def run_scrape(token: str, keywords: list[str], max_tweets: int) -> list[dict]:
    client = ApifyClient(token)
    run_input = {
        "searchTerms": keywords,
        "maxItems": max_tweets,
        "sort": "Top",
        "tweetLanguage": "en",
        "includeSearchTerms": False,
        "onlyImage": False,
        "onlyVideo": False,
        "onlyQuote": False,
        "onlyVerifiedUsers": False,
        "onlyTwitterBlue": False,
    }
    print(f"Calling Apify actor '{ACTOR_ID}' with {len(keywords)} keywords, max_items={max_tweets}", flush=True)
    run = client.actor(ACTOR_ID).call(run_input=run_input)
    if not run or "defaultDatasetId" not in run:
        raise RuntimeError(f"Actor run failed: {run}")
    print(f"Run completed, fetching dataset {run['defaultDatasetId']}", flush=True)
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    print(f"Got {len(items)} tweet items", flush=True)
    return items


def aggregate_traders(items: list[dict]) -> pd.DataFrame:
    traders: dict[str, dict] = {}
    for item in items:
        author = extract_author(item)
        if not author:
            continue
        u = author["username"]
        text = (item.get("text") or item.get("fullText") or "")[:280]
        if u not in traders:
            author["tweetCount"] = 1
            author["sampleTweet"] = text
            author["totalLikes"] = int(item.get("likeCount", 0) or 0)
            author["totalRetweets"] = int(item.get("retweetCount", 0) or 0)
            traders[u] = author
        else:
            traders[u]["tweetCount"] += 1
            traders[u]["totalLikes"] += int(item.get("likeCount", 0) or 0)
            traders[u]["totalRetweets"] += int(item.get("retweetCount", 0) or 0)
    if not traders:
        return pd.DataFrame()
    df = pd.DataFrame(list(traders.values()))
    column_order = [
        "username",
        "displayName",
        "profileUrl",
        "bio",
        "location",
        "followers",
        "following",
        "verified",
        "statuses",
        "joined",
        "tweetCount",
        "totalLikes",
        "totalRetweets",
        "sampleTweet",
    ]
    df = df[[c for c in column_order if c in df.columns]]
    return df.sort_values(by=["followers", "tweetCount"], ascending=[False, False]).reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape X traders via Apify.")
    parser.add_argument("--max-tweets", type=int, default=100, help="Maximum tweets to fetch from Apify.")
    parser.add_argument("--output", default="traders.xlsx", help="Output Excel file path.")
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=DEFAULT_KEYWORDS,
        help="Twitter search keywords for trading-related tweets.",
    )
    args = parser.parse_args()

    token = resolve_token()
    print(f"Using Apify token: {token[:14]}...{token[-4:]}", flush=True)

    items = run_scrape(token, args.keywords, args.max_tweets)
    df = aggregate_traders(items)

    if df.empty:
        print("No traders extracted from results.", file=sys.stderr)
        return 1

    df.to_excel(args.output, index=False, sheet_name="Traders")
    print(f"Wrote {len(df)} unique traders to {args.output}", flush=True)
    print("\nTop 10 by followers:")
    print(df[["username", "displayName", "followers", "tweetCount"]].head(10).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
