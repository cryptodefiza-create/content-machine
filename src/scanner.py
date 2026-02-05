"""Multi-source content scanner with rate limiting and retry"""
import requests
import json
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
import time
import random

from .utils import get_env, load_config, generate_content_hash, logger


def deduplicate(items: List[Dict], key: str = "topic") -> List[Dict]:
    seen = set()
    result = []
    for item in items:
        value = item.get(key, "")
        if value and value not in seen:
            seen.add(value)
            result.append(item)
    return result


class Scanner:

    def __init__(self):
        self.news_api_key = get_env("NEWS_API_KEY", "")
        self.sources = self._load_sources()
        self.source_weights = self.sources.get("source_weights", {})
        self.trend_keywords = [k.lower() for k in self.sources.get("trend_keywords", [])]

        rate_config = self.sources.get("rate_limits", {})
        self.delays = {
            "coingecko": rate_config.get("coingecko_delay_seconds", 2.0),
            "newsapi": rate_config.get("newsapi_delay_seconds", 1.5),
            "rss": rate_config.get("rss_delay_seconds", 0.5),
        }
        self.max_retries = rate_config.get("max_retries", 3)
        self.retry_backoff = rate_config.get("backoff_multiplier", 2.0)

    def _request_with_retry(
        self,
        url: str,
        params: Optional[dict] = None,
        source_type: str = "default",
        timeout: int = 15
    ) -> requests.Response:
        delay = self.delays.get(source_type, 1.0)
        last_exception = None

        for attempt in range(self.max_retries):
            try:
                jitter = random.uniform(0, 0.5)
                time.sleep(delay + jitter)

                response = requests.get(url, params=params, timeout=timeout)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.warning(f"Rate limited by {source_type}. Waiting {retry_after}s")
                    last_exception = requests.HTTPError(
                        f"429 Too Many Requests from {source_type}", response=response
                    )
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                return response

            except requests.Timeout as e:
                last_exception = e
                logger.warning(f"Timeout for {url} (attempt {attempt + 1}/{self.max_retries})")
            except requests.RequestException as e:
                last_exception = e
                if attempt < self.max_retries - 1:
                    backoff = delay * (self.retry_backoff ** attempt)
                    logger.warning(f"Retry {attempt + 1}/{self.max_retries} in {backoff:.1f}s: {e}")
                    time.sleep(backoff)

        raise last_exception

    def _load_sources(self) -> dict:
        try:
            return load_config("sources.json")
        except FileNotFoundError:
            logger.error("sources.json not found; scanner will use minimal defaults")
            sources = self._default_sources()
            sources["_fallback"] = True
            return sources
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Invalid sources.json; scanner will use minimal defaults: {e}")
            sources = self._default_sources()
            sources["_fallback"] = True
            return sources

    @staticmethod
    def _default_sources() -> dict:
        return {
            "rss_feeds": [
                {
                    "name": "CoinDesk",
                    "url": "https://feeds.feedburner.com/CoinDesk",
                    "fallback_url": None,
                    "priority": 1
                }
            ],
            "news_queries": ["crypto privacy", "defi"],
            "rate_limits": {},
            "source_weights": {"CoinDesk": 1.1},
            "trend_keywords": ["privacy", "upgrade", "hack", "airdrop", "zk"]
        }

    def get_trending_coins(self, limit: int = 5) -> List[Dict]:
        logger.info("Fetching trending coins from CoinGecko")

        try:
            response = self._request_with_retry(
                "https://api.coingecko.com/api/v3/search/trending",
                source_type="coingecko"
            )

            coins = []
            for item in response.json().get("coins", [])[:limit]:
                coin = item.get("item", {})
                symbol = coin.get("symbol", "").upper()
                name = coin.get("name", "")

                if symbol and name:
                    coins.append({
                        "type": "trend",
                        "source": "CoinGecko",
                        "topic": f"${symbol} ({name}) is trending",
                        "details": {
                            "symbol": symbol,
                            "name": name,
                            "market_cap_rank": coin.get("market_cap_rank"),
                        },
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "url": f"https://www.coingecko.com/en/coins/{coin.get('id')}"
                    })

            logger.info(f"Found {len(coins)} trending coins")
            return coins

        except Exception as e:
            logger.error(f"CoinGecko fetch failed: {e}")
            return []

    def get_news_articles(self, limit: int = 5) -> List[Dict]:
        if not self.news_api_key:
            logger.warning("NEWS_API_KEY not set, skipping news fetch")
            return []

        logger.info("Fetching news from NewsAPI")
        articles = []

        for query in self.sources.get("news_queries", [])[:2]:
            try:
                response = self._request_with_retry(
                    "https://newsapi.org/v2/everything",
                    params={
                        "q": query,
                        "sortBy": "publishedAt",
                        "language": "en",
                        "pageSize": 3,
                        "apiKey": self.news_api_key
                    },
                    source_type="newsapi"
                )

                data = response.json()

                if data.get("status") == "error":
                    logger.warning(f"NewsAPI error for '{query}': {data.get('message', 'Unknown error')}")
                    continue

                for article in data.get("articles", []):
                    title = article.get("title", "")
                    if title and title != "[Removed]" and len(title) > 10:
                        published = article.get("publishedAt")
                        articles.append({
                            "type": "news",
                            "source": article.get("source", {}).get("name", "Unknown"),
                            "topic": title,
                            "details": {"description": article.get("description", "")},
                            "published_at": published,
                            "url": article.get("url")
                        })

            except Exception as e:
                logger.error(f"NewsAPI failed for '{query}': {e}")

        unique = deduplicate(articles, key="topic")
        logger.info(f"Found {len(unique)} unique articles")
        return unique[:limit]

    def _parse_rss_date(self, entry) -> Optional[datetime]:
        try:
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            if hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
        return None

    def _fetch_rss_feed(self, feed_config: dict) -> List[Dict]:
        try:
            import feedparser  # type: ignore
        except Exception:
            logger.warning("feedparser not installed, skipping RSS fetch")
            return []

        urls_to_try = [feed_config["url"]]
        if feed_config.get("fallback_url"):
            urls_to_try.append(feed_config["fallback_url"])

        for url in urls_to_try:
            try:
                time.sleep(self.delays["rss"])
                feed = feedparser.parse(url)

                if feed.bozo and not feed.entries:
                    logger.warning(f"RSS parse failed for {feed_config['name']}: {feed.bozo_exception}")
                    continue

                articles = []
                cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

                for entry in feed.entries[:5]:
                    published = self._parse_rss_date(entry)
                    if published and published < cutoff:
                        continue

                    title = entry.get("title", "").strip()
                    if title and len(title) > 10:
                        published = self._parse_rss_date(entry)
                        articles.append({
                            "type": "news",
                            "source": feed_config["name"],
                            "topic": title,
                            "details": {"description": entry.get("summary", "")[:500]},
                            "published_at": published.isoformat() if published else None,
                            "url": entry.get("link")
                        })

                if articles:
                    return articles

            except Exception as e:
                logger.warning(f"RSS fetch failed for {feed_config['name']} ({url}): {e}")
                continue

        return []

    def get_rss_feeds(self, limit: int = 5) -> List[Dict]:
        logger.info("Fetching RSS feeds")

        feeds = sorted(
            self.sources.get("rss_feeds", []),
            key=lambda x: x.get("priority", 99)
        )

        articles = []
        for feed_config in feeds:
            articles.extend(self._fetch_rss_feed(feed_config))

        unique = deduplicate(articles, key="topic")
        logger.info(f"Found {len(unique)} unique RSS articles")
        return unique[:limit]

    def scan_all(self, max_items: int = 10) -> List[Dict]:
        logger.info("Starting full content scan")

        all_items = []
        all_items.extend(self.get_trending_coins(limit=3))
        all_items.extend(self.get_news_articles(limit=4))
        all_items.extend(self.get_rss_feeds(limit=4))

        for item in all_items:
            item["content_hash"] = generate_content_hash(item["topic"])
            item["scanned_at"] = datetime.now(timezone.utc).isoformat()
            item["trend_score"] = self._score_item(item)

        unique_items = deduplicate(all_items, key="content_hash")
        unique_items.sort(key=lambda x: x.get("trend_score", 0), reverse=True)
        logger.info(f"Scan complete: {len(unique_items)} unique items from {len(all_items)} total")
        return unique_items[:max_items]

    def _score_item(self, item: Dict) -> float:
        now = datetime.now(timezone.utc)
        published_at = item.get("published_at")
        hours = 24.0
        try:
            if published_at:
                published_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                delta = now - published_dt
                hours = max(0.1, delta.total_seconds() / 3600.0)
        except Exception:
            hours = 24.0

        recency_score = max(0.0, 1.0 - (hours / 48.0))
        source = item.get("source", "")
        weight = float(self.source_weights.get(source, 1.0))
        topic = (item.get("topic") or "").lower()
        keyword_hits = sum(1 for k in self.trend_keywords if k in topic)
        keyword_boost = min(0.5, keyword_hits * 0.05)
        type_boost = 0.1 if item.get("type") == "trend" else 0.0
        return round((recency_score + keyword_boost + type_boost) * weight, 4)
