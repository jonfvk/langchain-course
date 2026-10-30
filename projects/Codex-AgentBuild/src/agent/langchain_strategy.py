"""Optional LangChain market-suggestion layer; it cannot access broker tools."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import math
import os
from typing import Literal
from uuid import uuid4

from src.data.market_data.models import MUOptionSnapshot, MUUnderlyingSnapshot
from src.risk.paper import TradeIntent

MAX_CANDIDATES = 25
DEFAULT_MODEL = "gpt-4.1-mini"


@dataclass(frozen=True)
class StrategySuggestion:
    action: Literal["OPEN", "HOLD"]
    symbol: str | None
    rationale: str

    def to_trade_intent(self, quote: MUOptionSnapshot) -> TradeIntent:
        if self.action != "OPEN" or self.symbol is None:
            raise ValueError("Only OPEN suggestions can become trade intents")
        if quote.contract is None or quote.contract.symbol != self.symbol:
            raise ValueError("Suggestion does not match the supplied option quote")
        if quote.ask is None or not math.isfinite(quote.ask) or quote.ask <= 0:
            raise ValueError("A valid current ask is required to create an intent")
        price = Decimal(str(quote.ask)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if price <= 0:
            raise ValueError("The rounded limit price must be positive")
        return TradeIntent(
            intent_id=str(uuid4()),
            symbol=self.symbol,
            action="OPEN",
            quantity=1,
            limit_price=str(price),
            rationale=self.rationale,
        )


def _recommendation_schema():
    from pydantic import BaseModel, Field, model_validator

    class Recommendation(BaseModel):
        action: Literal["OPEN", "HOLD"]
        symbol: str | None
        rationale: str = Field(min_length=1, max_length=500)

        @model_validator(mode="after")
        def validate_symbol(self):
            if (self.action == "OPEN") != (self.symbol is not None):
                raise ValueError("OPEN requires a symbol; HOLD must not include one")
            return self

    return Recommendation


def _load_environment():
    from dotenv import load_dotenv

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    load_dotenv(os.path.join(project_root, ".env"), override=False)
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("Set OPENAI_API_KEY in the environment or project .env file")
    tracing = os.getenv("LANGSMITH_TRACING", "").strip().lower() in ("true", "1", "yes")
    if tracing and not os.getenv("LANGSMITH_API_KEY"):
        raise ValueError(
            "LANGSMITH_TRACING is enabled but LANGSMITH_API_KEY is not set; "
            "set the key or disable tracing"
        )


class LangChainStrategy:
    """Return bounded, structured suggestions without providing execution tools."""

    def __init__(self, *, model: str = DEFAULT_MODEL, chain=None):
        if chain is None:
            _load_environment()
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_openai import ChatOpenAI

            schema = _recommendation_schema()
            prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You are a cautious paper-trading research assistant. "
                        "Evaluate only the supplied MU option candidates and "
                        "current public quotes. Return OPEN only when the "
                        "provided data supports a clear rationale; otherwise "
                        "return HOLD. Never invent symbols or quote data. "
                        "Suggestions are not orders and may be rejected by "
                        "independent Python risk controls.",
                    ),
                    (
                        "human",
                        "As-of UTC: {as_of}\n"
                        "MU underlying snapshot: {underlying}\n"
                        "Available option candidates: {options}",
                    ),
                ]
            )
            chat_model = ChatOpenAI(
                model=model,
                temperature=0,
                timeout=20,
                max_retries=1,
            )
            chain = prompt | chat_model.with_structured_output(schema)
        self._chain = chain

    def recommend(
        self,
        underlying: MUUnderlyingSnapshot,
        options: list[MUOptionSnapshot],
        *,
        now: datetime | None = None,
    ) -> StrategySuggestion:
        if underlying.symbol != "MU":
            raise ValueError("Only MU underlying snapshots are supported")
        if not options:
            raise ValueError("At least one option candidate is required")
        if len(options) > MAX_CANDIDATES:
            raise ValueError(f"Limit strategy input to {MAX_CANDIDATES} option candidates")

        candidates = []
        by_symbol = {}
        for quote in options:
            contract = quote.contract
            if contract is None or quote.symbol != "MU" or contract.underlying != "MU":
                raise ValueError("Every candidate must be a normalized MU option quote")
            if contract.symbol in by_symbol:
                raise ValueError("Option candidate symbols must be unique")
            if (
                quote.bid is None
                or quote.ask is None
                or not math.isfinite(quote.bid)
                or not math.isfinite(quote.ask)
                or quote.bid <= 0
                or quote.ask < quote.bid
            ):
                raise ValueError(f"Invalid bid/ask for option candidate {contract.symbol}")
            by_symbol[contract.symbol] = quote
            candidates.append(
                {
                    "symbol": contract.symbol,
                    "expiration": contract.expiration,
                    "strike": contract.strike,
                    "option_type": contract.option_type,
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "volume": quote.volume,
                    "open_interest": quote.open_interest,
                    "delta": quote.delta,
                    "implied_volatility": quote.iv,
                }
            )

        now = now or datetime.now(timezone.utc)
        result = self._chain.invoke(
            {
                "as_of": now.isoformat(),
                "underlying": json.dumps(
                    {"bid": underlying.bid, "ask": underlying.ask, "last": underlying.last},
                    allow_nan=False,
                ),
                "options": json.dumps(candidates, allow_nan=False),
            }
        )
        if isinstance(result, dict):
            action, symbol, rationale = (
                result.get("action"),
                result.get("symbol"),
                result.get("rationale"),
            )
        else:
            action = getattr(result, "action", None)
            symbol = getattr(result, "symbol", None)
            rationale = getattr(result, "rationale", None)

        if action == "HOLD" and symbol is None and isinstance(rationale, str) and rationale.strip():
            return StrategySuggestion("HOLD", None, rationale.strip())
        if (
            action == "OPEN"
            and isinstance(symbol, str)
            and symbol in by_symbol
            and isinstance(rationale, str)
            and rationale.strip()
        ):
            return StrategySuggestion("OPEN", symbol, rationale.strip())
        raise ValueError("LangChain returned an invalid or unavailable strategy suggestion")
