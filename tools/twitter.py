"""X (Twitter) API v2 tool — OAuth 1.0a User Context via tweepy."""
import logging
import os

logger = logging.getLogger(__name__)


def _get_client():
    """Build an authenticated tweepy Client from environment variables."""
    api_key = os.environ.get("X_API_KEY", "").strip()
    api_secret = os.environ.get("X_API_SECRET", "").strip()
    access_token = os.environ.get("X_ACCESS_TOKEN", "").strip()
    access_token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "").strip()

    missing = [
        name for name, val in [
            ("X_API_KEY", api_key),
            ("X_API_SECRET", api_secret),
            ("X_ACCESS_TOKEN", access_token),
            ("X_ACCESS_TOKEN_SECRET", access_token_secret),
        ] if not val
    ]
    if missing:
        raise ValueError(f"Ontbrekende omgevingsvariabelen: {', '.join(missing)}")

    import tweepy
    return tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
        wait_on_rate_limit=False,
    )


def _handle_tweepy_error(exc) -> str:
    """Convert a tweepy exception to a clear Dutch error message."""
    import tweepy
    if isinstance(exc, tweepy.errors.TooManyRequests):
        reset = getattr(exc.response, "headers", {}).get("x-rate-limit-reset", "onbekend")
        return f"Rate limit bereikt (429). Reset om: {reset}. Wacht even voor je opnieuw probeert."
    if isinstance(exc, tweepy.errors.Unauthorized):
        return "Authenticatiefout (401). Controleer X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN en X_ACCESS_TOKEN_SECRET."
    if isinstance(exc, tweepy.errors.Forbidden):
        return f"Toegang geweigerd (403): {exc}"
    if isinstance(exc, tweepy.errors.TwitterServerError):
        return f"Twitter serverfout (5xx): {exc}. Probeer later opnieuw."
    return f"Twitter API fout: {exc}"


def x_post_tweet(content: str, reply_to_tweet_id: str | None = None) -> str:
    """Post a new tweet."""
    try:
        client = _get_client()
    except ValueError as e:
        return f"Configuratiefout: {e}"

    try:
        import tweepy
        kwargs = {"text": content}
        if reply_to_tweet_id:
            kwargs["in_reply_to_tweet_id"] = reply_to_tweet_id
        response = client.create_tweet(**kwargs)
        tweet_id = response.data["id"]
        return f"Tweet geplaatst. ID: {tweet_id}\nURL: https://x.com/i/web/status/{tweet_id}"
    except Exception as exc:
        import tweepy
        if isinstance(exc, (tweepy.errors.TweepyException,)):
            return _handle_tweepy_error(exc)
        return f"Onverwachte fout: {exc}"


def x_get_tweets(tweet_id: str | None = None, query: str | None = None, max_results: int = 10) -> str:
    """Read a specific tweet by ID, or search recent tweets by query."""
    try:
        client = _get_client()
    except ValueError as e:
        return f"Configuratiefout: {e}"

    try:
        import tweepy
        tweet_fields = ["created_at", "author_id", "text", "public_metrics"]

        if tweet_id:
            response = client.get_tweet(tweet_id, tweet_fields=tweet_fields)
            if not response.data:
                return f"Tweet {tweet_id} niet gevonden."
            t = response.data
            metrics = t.public_metrics or {}
            return (
                f"Tweet ID: {t.id}\n"
                f"Tekst: {t.text}\n"
                f"Datum: {t.created_at}\n"
                f"Likes: {metrics.get('like_count', 0)} | Retweets: {metrics.get('retweet_count', 0)}"
            )

        if query:
            max_results = max(10, min(max_results, 100))
            response = client.search_recent_tweets(query=query, max_results=max_results, tweet_fields=tweet_fields)
            if not response.data:
                return f"Geen tweets gevonden voor query: {query}"
            lines = [f"Zoekresultaten voor '{query}' ({len(response.data)} tweets):"]
            for t in response.data:
                lines.append(f"- [{t.id}] {t.text[:120]}{'…' if len(t.text) > 120 else ''}")
            return "\n".join(lines)

        # Default: get authenticated user's own timeline (last N tweets)
        me = client.get_me()
        if not me.data:
            return "Kon gebruikersprofiel niet ophalen."
        user_id = me.data.id
        max_results = max(5, min(max_results, 100))
        response = client.get_users_tweets(user_id, max_results=max_results, tweet_fields=tweet_fields)
        if not response.data:
            return "Geen tweets gevonden op je tijdlijn."
        lines = [f"Tijdlijn ({len(response.data)} tweets):"]
        for t in response.data:
            lines.append(f"- [{t.id}] {t.text[:120]}{'…' if len(t.text) > 120 else ''}")
        return "\n".join(lines)

    except Exception as exc:
        import tweepy
        if isinstance(exc, tweepy.errors.TweepyException):
            return _handle_tweepy_error(exc)
        return f"Onverwachte fout: {exc}"


def x_send_dm(recipient_id: str, content: str) -> str:
    """Send a Direct Message to a user by their numeric user ID."""
    try:
        client = _get_client()
    except ValueError as e:
        return f"Configuratiefout: {e}"

    try:
        import tweepy
        response = client.create_direct_message(participant_id=recipient_id, text=content)
        dm_id = response.data.get("dm_conversation_id", "onbekend")
        return f"Direct Message verzonden. Conversatie ID: {dm_id}"
    except Exception as exc:
        import tweepy
        if isinstance(exc, tweepy.errors.TweepyException):
            return _handle_tweepy_error(exc)
        return f"Onverwachte fout: {exc}"


def x_get_dms(dm_conversation_id: str | None = None, max_results: int = 10) -> str:
    """Read Direct Messages. Optionally filter by conversation ID."""
    try:
        client = _get_client()
    except ValueError as e:
        return f"Configuratiefout: {e}"

    try:
        import tweepy
        max_results = max(1, min(max_results, 100))

        if dm_conversation_id:
            response = client.get_direct_message_events(
                dm_conversation_id=dm_conversation_id,
                max_results=max_results,
                dm_event_fields=["created_at", "sender_id", "text"],
            )
        else:
            response = client.get_direct_message_events(
                max_results=max_results,
                dm_event_fields=["created_at", "sender_id", "text"],
            )

        if not response.data:
            return "Geen Direct Messages gevonden."

        lines = [f"Direct Messages ({len(response.data)} berichten):"]
        for dm in response.data:
            sender = getattr(dm, "sender_id", "onbekend")
            text = getattr(dm, "text", "")
            created = getattr(dm, "created_at", "")
            lines.append(f"- Van {sender} [{created}]: {text[:200]}{'…' if len(text) > 200 else ''}")
        return "\n".join(lines)

    except Exception as exc:
        import tweepy
        if isinstance(exc, tweepy.errors.TweepyException):
            return _handle_tweepy_error(exc)
        return f"Onverwachte fout: {exc}"


# ─── Tool definitions (JSON schema) ──────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "x_post_tweet",
            "description": (
                "Plaats een nieuw bericht op X (Twitter). "
                "Optioneel als antwoord op een bestaand tweet via reply_to_tweet_id. "
                "Vereist: X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET in .env. "
                "Bij rate limit (429) of authenticatiefout ontvang je een heldere foutmelding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "De tekst van de tweet (max 280 tekens).",
                    },
                    "reply_to_tweet_id": {
                        "type": "string",
                        "description": "Optioneel: numeriek tweet-ID om op te antwoorden.",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "x_get_tweets",
            "description": (
                "Lees tweets van X (Twitter). Gebruik tweet_id om één specifieke tweet op te halen, "
                "query om recente tweets te doorzoeken, of laat beide leeg voor je eigen tijdlijn. "
                "Vereist OAuth 1.0a credentials in .env."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "Optioneel: numeriek ID van een specifieke tweet.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optioneel: zoekterm voor recente tweets (Twitter search syntax).",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Aantal resultaten (5–100, standaard 10).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "x_send_dm",
            "description": (
                "Stuur een Direct Message op X (Twitter) naar een gebruiker. "
                "Vereist het numerieke user ID van de ontvanger (niet de @username). "
                "Vereist OAuth 1.0a credentials met DM-rechten in .env."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient_id": {
                        "type": "string",
                        "description": "Numeriek X user ID van de ontvanger.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Inhoud van het Direct Message.",
                    },
                },
                "required": ["recipient_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "x_get_dms",
            "description": (
                "Lees Direct Messages op X (Twitter). "
                "Optioneel gefilterd op dm_conversation_id voor een specifieke conversatie. "
                "Vereist OAuth 1.0a credentials met DM-rechten in .env."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dm_conversation_id": {
                        "type": "string",
                        "description": "Optioneel: ID van een specifieke DM-conversatie.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Aantal berichten (1–100, standaard 10).",
                    },
                },
                "required": [],
            },
        },
    },
]

HANDLERS = {
    "x_post_tweet": lambda args: x_post_tweet(args["content"], args.get("reply_to_tweet_id")),
    "x_get_tweets": lambda args: x_get_tweets(args.get("tweet_id"), args.get("query"), args.get("max_results", 10)),
    "x_send_dm": lambda args: x_send_dm(args["recipient_id"], args["content"]),
    "x_get_dms": lambda args: x_get_dms(args.get("dm_conversation_id"), args.get("max_results", 10)),
}
