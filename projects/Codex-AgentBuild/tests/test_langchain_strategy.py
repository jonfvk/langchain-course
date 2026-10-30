import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from src.agent.langchain_strategy import LangChainStrategy
from src.data.market_data.models import MUOptionContract, MUOptionSnapshot, MUUnderlyingSnapshot


class LangChainStrategyTests(unittest.TestCase):
    def setUp(self):
        expiry = "2030-01-18"
        self.contract = MUOptionContract(
            "MU",
            "MU    300118C00100000",
            ".MU300118C100",
            expiry,
            100,
            "C",
        )
        self.quote = MUOptionSnapshot(
            contract=self.contract,
            bid=1.0,
            ask=1.1,
            volume=25,
            open_interest=100,
            delta=0.5,
            iv=0.4,
        )
        self.underlying = MUUnderlyingSnapshot(bid=99.9, ask=100.1, last=100)
        self.chain = Mock()
        self.strategy = LangChainStrategy(chain=self.chain)

    def test_open_recommendation_becomes_one_contract_intent(self):
        self.chain.invoke.return_value = {
            "action": "OPEN",
            "symbol": self.contract.symbol,
            "rationale": "Candidate quote is within the supplied market.",
        }

        suggestion = self.strategy.recommend(
            self.underlying,
            [self.quote],
            now=datetime(2029, 1, 1, tzinfo=timezone.utc),
        )
        intent = suggestion.to_trade_intent(self.quote)

        self.assertEqual(intent.action, "OPEN")
        self.assertEqual(intent.quantity, 1)
        self.assertEqual(intent.limit_price, "1.10")
        self.assertEqual(intent.symbol, self.contract.symbol)
        self.assertIn("options", self.chain.invoke.call_args.args[0])

    def test_hold_cannot_become_trade_intent(self):
        self.chain.invoke.return_value = {
            "action": "HOLD",
            "symbol": None,
            "rationale": "Insufficient evidence.",
        }
        suggestion = self.strategy.recommend(self.underlying, [self.quote])

        with self.assertRaisesRegex(ValueError, "Only OPEN"):
            suggestion.to_trade_intent(self.quote)

    def test_unknown_model_symbol_is_rejected(self):
        self.chain.invoke.return_value = {
            "action": "OPEN",
            "symbol": "MU UNKNOWN",
            "rationale": "Unknown candidate.",
        }

        with self.assertRaisesRegex(ValueError, "invalid or unavailable"):
            self.strategy.recommend(self.underlying, [self.quote])

    def test_input_must_be_bounded_and_unambiguous(self):
        with self.assertRaisesRegex(ValueError, "At least one"):
            self.strategy.recommend(self.underlying, [])
        with self.assertRaisesRegex(ValueError, "unique"):
            self.strategy.recommend(self.underlying, [self.quote, self.quote])

    def test_trade_intent_requires_matching_quote(self):
        self.chain.invoke.return_value = {
            "action": "OPEN",
            "symbol": self.contract.symbol,
            "rationale": "Candidate quote is within the supplied market.",
        }
        suggestion = self.strategy.recommend(self.underlying, [self.quote])
        other_quote = MUOptionSnapshot(
            contract=MUOptionContract(
                "MU",
                "MU    300118P00100000",
                ".MU300118P100",
                "2030-01-18",
                100,
                "P",
            ),
            bid=1.0,
            ask=1.1,
        )

        with self.assertRaisesRegex(ValueError, "does not match"):
            suggestion.to_trade_intent(other_quote)


if __name__ == "__main__":
    unittest.main()
